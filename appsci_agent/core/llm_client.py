"""Unified LLM client using litellm for multi-provider support.

litellm provides a unified interface for calling various LLM providers
(Anthropic, OpenAI, etc.) with consistent tool calling support.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import litellm

from .config import AgentConfig


@dataclass
class ToolCall:
    """Represents a tool call from the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Response from the LLM."""
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    raw_response: Any = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class LLMClient:
    """Unified LLM client using litellm."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.model = config.get_litellm_model()

        # Set API key if provided
        if config.api_key:
            if config.llm_provider == "anthropic":
                litellm.anthropic_key = config.api_key
            elif config.llm_provider == "openai":
                litellm.openai_key = config.api_key

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """Send a chat completion request to the LLM.

        Args:
            messages: List of message dicts with 'role' and 'content'
            tools: Optional list of tool definitions in OpenAI format
            **kwargs: Additional arguments passed to litellm.completion

        Returns:
            LLMResponse with content and/or tool calls
        """
        # Build request parameters
        params = {
            "model": self.model,
            "messages": messages,
            "max_tokens": kwargs.pop("max_tokens", self.config.max_tokens),
        }

        # Only add temperature if explicitly set in kwargs (some models reject it)
        if "temperature" in kwargs:
            params["temperature"] = kwargs.pop("temperature")

        # Add auth from config
        if self.config.api_key:
            params["api_key"] = self.config.api_key
        if self.config.base_url:
            params["base_url"] = self.config.base_url
        # Gateway identity/attribution headers (e.g. X-Gateway-User), when configured. Only set the
        # parameter when there are headers, so a first-party/no-gateway request is unchanged.
        if self.config.custom_headers:
            params["extra_headers"] = dict(self.config.custom_headers)

        if tools:
            params["tools"] = tools
            # Allow the model to choose whether to call tools
            params["tool_choice"] = kwargs.pop("tool_choice", "auto")

        # Resilience: let litellm retry transient errors (gateway Timeout, RateLimit, connection
        # blips) with exponential backoff instead of crashing a long multi-turn build on a single
        # hiccup. litellm only auto-selects backoff for RateLimitError; a generic Timeout/APIError
        # would otherwise retry at a flat interval, so set the strategy explicitly. Callers can
        # override via kwargs (applied by params.update below).
        params.setdefault("num_retries", 3)
        params.setdefault("retry_strategy", "exponential_backoff_retry")
        # Explicit per-request timeout so a hung/throttled gateway call fails fast (and gets retried)
        # instead of parking on litellm's 6000s default. Tunable via env.
        params.setdefault("timeout", float(os.getenv("APPSCI_LLM_TIMEOUT_S", "120")))
        # Let litellm drop a temperature the target model does not accept, rather than sending it and
        # taking a 400. Some current reasoning models reject any value but their own default, so a
        # temperature-pinned replicate run would otherwise fail at its first turn on those models --
        # while the mini-swe sub-agent, whose config has carried `drop_params: true` all along, ran the
        # same request fine. Two build-side agents differing on one setting is the surprise worth removing.
        #
        # SCOPED TO REQUESTS THAT ACTUALLY PIN A TEMPERATURE, because `drop_params` is not a
        # temperature switch: it lets litellm drop ANY parameter it considers unsupported for the target
        # model, `tools` included. Set unconditionally, an agent pointed at a model litellm treats as
        # non-tool-capable would silently run tool-less and burn its whole turn budget emitting prose,
        # where today it fails fast with a 400 -- trading a legible error for an expensive, plausible
        # looking non-answer. Every request that does not pin a temperature therefore stays exactly as it
        # was, and the widened behaviour is confined to the opt-in runs that need it.
        if "temperature" in params:
            params.setdefault("drop_params", True)

        params.update(kwargs)

        # Make the API call
        response = litellm.completion(**params)

        return self._parse_response(response)

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse litellm response into our LLMResponse format."""
        choice = response.choices[0]
        message = choice.message

        # Extract tool calls if present
        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                # Parse arguments from JSON string
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {"raw": tc.function.arguments}

                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        # Extract usage stats
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
            # Cache counters, when the provider sent them. Copied through rather than hand-listed
            # above because they are the difference between a priceable row and a wrong one: cache
            # reads bill around a tenth of an input token and cache writes more, so pricing this
            # agent's usage by multiplying prompt tokens by the input rate is materially wrong for a
            # long multi-turn build once caching is on. They were dropped HERE, at the writer -- the
            # logs carry them for every other caller (mini-swe and the shared transport both pass
            # the provider's dict straight through), so this loop's row was the one that could never
            # show a cache figure no matter what the reader did. Absent on providers that do not
            # report them, which is why each is conditional rather than defaulted to 0: a real zero
            # and "this provider never says" are different facts.
            for field in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                value = getattr(response.usage, field, None)
                if value is not None:
                    usage[field] = value

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
            raw_response=response,
        )

    def format_tool_result(
        self,
        tool_call_id: str,
        result: Any,
    ) -> dict[str, Any]:
        """Format a tool result for inclusion in messages.

        Args:
            tool_call_id: The ID of the tool call being responded to
            result: The result from executing the tool

        Returns:
            Message dict in the format expected by the LLM
        """
        # Convert result to string if necessary
        if isinstance(result, dict):
            content = json.dumps(result)
        elif isinstance(result, str):
            content = result
        else:
            content = str(result)

        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        }

    def format_assistant_message_with_tool_calls(
        self,
        content: str | None,
        tool_calls: list[ToolCall],
    ) -> dict[str, Any]:
        """Format an assistant message that includes tool calls.

        This is needed when appending the assistant's response that contained
        tool calls back to the conversation history.
        """
        message = {"role": "assistant"}

        if content:
            message["content"] = content

        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in tool_calls
            ]

        return message


def make_llm_client(
    config: AgentConfig | None = None,
    *,
    optional: bool = False,
) -> "LLMClient | None":
    """Factory for the agent's LLM client (Claude via AgentConfig).

    Centralizes LLMClient construction so callers don't hard-wire it (easier to swap/stub).
    With ``optional=True``, returns ``None`` instead of raising when a client can't be built
    (no config / no creds) -- callers then fall back to deterministic behavior. With the default
    ``optional=False`` it constructs (or raises) like ``LLMClient(config)``.
    """
    try:
        return LLMClient(config or AgentConfig.from_env())
    except Exception:  # noqa: BLE001 - surface as None when the caller opted into a fallback
        if optional:
            return None
        raise
