"""Generic tool-use agent loop for the Rho-Bank agent.

This depends ONLY on interfaces confirmed to exist:
  * the Anthropic Messages protocol (via ``llm_client.AnthropicMessagesClient``), and
  * the banking tool API transport (via ``tools.ToolClient``: POST /<name> {"arguments":..} -> {"result":..}).

The specific tools advertised to the model are passed in as ``ToolSpec`` objects; this module
hard-codes NONE of them, so it stays correct regardless of which BASE / specialist / verification
schemas are eventually confirmed. The confirmed retrieval ToolSpecs live in ``tool_specs.py``; the
BASE / unlock / verification ToolSpecs are added there only once their schemas are confirmed from the
KB documentation -- never guessed.

One ``/respond`` call == one visible assistant turn. Internally the loop may call tools many times
(search -> read procedure -> verify -> unlock -> call -> confirm) up to ``max_iterations`` before it
produces the final grounded answer. Tool failures are fed back to the model as ``is_error`` tool
results so it can react honestly per the system prompt, rather than being masked as success.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import config
from llm_client import LLMResponse, ToolCall

# Upper bound on internal tool-use rounds for a single visible turn. Generous enough for the full
# search -> verify -> unlock -> call -> confirm cycle, bounded so a loop cannot run forever.
DEFAULT_MAX_ITERATIONS = 12

# Cap the size of a single tool result fed back to the model, so a large KB dump cannot blow up the
# context. Truncation is marked so the model knows the text was clipped.
_MAX_TOOL_RESULT_CHARS = 8000

# What the agent says when it runs out of internal steps without reaching a final answer. Honest and
# non-fabricated: it does NOT claim the request was completed.
_EXHAUSTED_MESSAGE = (
    "I'm sorry, but I wasn't able to finish working through this request within my available steps. "
    "I don't want to give you an unconfirmed result, so I haven't completed it. Please try again or "
    "ask to be connected with a human agent."
)


@dataclass
class ToolSpec:
    """A tool advertised to the model, plus the name used to invoke it over the tool API.

    ``name`` is both the name shown to the model and the path segment used by ``ToolClient.call``.
    """

    name: str
    description: str
    input_schema: Dict[str, Any]

    def to_anthropic(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _coerce_text(content: Any) -> str:
    """Flatten an incoming message's content to plain text.

    Incoming ``/respond`` messages are OpenAI-style: content is usually a string, but may be a list of
    content parts. We keep only text so the conversation we send upstream is well-formed.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def _role_content(message: Any) -> Tuple[str, Any]:
    if isinstance(message, dict):
        return str(message.get("role", "user")), message.get("content")
    # Unknown shape: treat the whole thing as user text.
    return "user", message


def _split_system(messages: List[Any], base_system: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Convert OpenAI-style messages into (system_prompt, anthropic_messages).

    Anthropic takes ``system`` as a top-level string, so any incoming system messages are appended to
    our base system prompt. user/assistant turns become ``{"role","content"}`` string messages.
    """
    convo: List[Dict[str, Any]] = []
    extra_system: List[str] = []
    for message in messages:
        role, content = _role_content(message)
        text = _coerce_text(content)
        if role == "system":
            if text:
                extra_system.append(text)
        elif role == "assistant":
            convo.append({"role": "assistant", "content": text})
        else:  # default anything unexpected to a user turn
            convo.append({"role": "user", "content": text})
    system = base_system
    if extra_system:
        system = base_system + "\n\n" + "\n\n".join(extra_system)
    return system, convo


def _truncate(text: str) -> str:
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    return text[:_MAX_TOOL_RESULT_CHARS] + "\n...[truncated]"


def _stringify_result(result: Any) -> str:
    if isinstance(result, str):
        return config.redact(_truncate(result))
    try:
        rendered = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(result)
    return config.redact(_truncate(rendered))


def _looks_like_error_result(result: Any) -> bool:
    """Detect a failure reported as an ``Error:``-prefixed string inside an HTTP 200 response.

    The banking tools sometimes signal a failure this way (per the confirmed BASE-interface spec)
    rather than with a non-2xx status. We must surface those to the model as errors so it does not
    treat a failed lookup or action as success.
    """
    return isinstance(result, str) and result.lstrip().startswith("Error:")


def _tool_result_block(tool_use_id: str, content: str, is_error: bool = False) -> Dict[str, Any]:
    block: Dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return block


class AgentLoop:
    """Drives a multi-step tool-use conversation to a single grounded assistant answer."""

    def __init__(
        self,
        llm: Any,
        tool_client: Any,
        tools: List[ToolSpec],
        system_prompt: str,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_tokens: Optional[int] = None,
    ) -> None:
        self._llm = llm
        self._tool_client = tool_client
        self._tools = list(tools)
        self._tool_names = {t.name for t in self._tools}
        self._system = system_prompt
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens

    def run(self, messages: List[Any]) -> str:
        system, convo = _split_system(messages, self._system)
        if not convo:
            # Degenerate input (no user turn). Don't call the model with an empty conversation.
            return "How can I help you with your Rho-Bank account today?"

        anthropic_tools = [t.to_anthropic() for t in self._tools] or None

        for _ in range(self._max_iterations):
            response: LLMResponse = self._llm.create_message(
                system=system,
                messages=convo,
                tools=anthropic_tools,
                max_tokens=self._max_tokens,
            )
            if not response.tool_calls:
                return response.text

            # Replay the assistant's tool_use turn verbatim, then answer each tool call.
            convo.append({"role": "assistant", "content": response.raw_content})
            results = [self._execute(call) for call in response.tool_calls]
            convo.append({"role": "user", "content": results})

        return _EXHAUSTED_MESSAGE

    def _execute(self, call: ToolCall) -> Dict[str, Any]:
        if call.name not in self._tool_names:
            # The model asked for a tool we did not advertise. Report it faithfully rather than
            # inventing a result; the model can recover or be honest about the limitation.
            return _tool_result_block(
                call.id,
                f"Error: tool {call.name!r} is not available.",
                is_error=True,
            )
        arguments = call.input if isinstance(call.input, dict) else {}
        result = self._tool_client.call(call.name, arguments)
        if result.ok:
            # HTTP succeeded, but the tool may still report a failure as an "Error:"-prefixed string
            # in the result body. Flag those as errors rather than passing them off as success.
            is_error = _looks_like_error_result(result.result)
            return _tool_result_block(call.id, _stringify_result(result.result), is_error=is_error)

        # Faithful, non-secret failure surfaced to the model.
        detail = result.kind
        if result.status_code is not None:
            detail += f" (HTTP {result.status_code})"
        if result.detail:
            detail += f": {config.redact(str(result.detail))}"
        return _tool_result_block(call.id, f"Tool error: {detail}", is_error=True)
