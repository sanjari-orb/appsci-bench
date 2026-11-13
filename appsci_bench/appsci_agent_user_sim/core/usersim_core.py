"""LLM-backed appsci agent user sim -- the simulator core.

``AppsciAgentLLMUserSim`` role-plays a task's stakeholder (L1/L2/L3) and ANSWERS the agent's questions. It is a
pure, state-free, single-LLM-call object: the caller owns the conversation history and passes it in
each turn (the HTTP service layer, later, holds the per-session transcript and replays it here).

Design rules it honors (design doc 1-3):
  - the sim ANSWERS, never leads or ends the conversation (no should_stop; output is {message} only);
  - it answers only from the persona's known facts (no fabrication) -- enforced by the prompt;
  - the role flip: from the model's view the sim is the *assistant* and the agent is the *user*.

Part of the standalone appsci_agent_user_sim package -- imports nothing from appsci_bench.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from .persona import TaskContextLike, render_persona_prompt
from appsci_bench.common.model_config import ModelConfig, complete, config_for

logger = logging.getLogger(__name__)

# Public history turns use these roles (the agent's point of view); see _to_llm_messages for the flip.
ROLE_AGENT = "agent"
ROLE_SIM = "user_sim"


class AppsciAgentUserSimError(RuntimeError):
    """Raised when the model's output cannot be parsed into a {message} turn (fail loud, no default)."""


class _SimulatorTurn(BaseModel):
    """The sim's structured output. EXACTLY one field -- no should_stop (the sim cannot end the convo)."""
    model_config = {"extra": "forbid"}
    message: str


def _parse_message(raw: str) -> str:
    """Parse the model's raw text into the turn's message, defense-in-depth (never default-fill).

    (1) strict JSON validate; (2) strip a ```json ... ``` fence and retry; (3) extract the first
    balanced {...} block and validate; (4) else raise AppsciAgentUserSimError.
    """
    candidates: list[str] = [raw]

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))

    block = _first_json_object(raw)
    if block is not None:
        candidates.append(block)

    for candidate in candidates:
        try:
            return _SimulatorTurn.model_validate_json(candidate).message
        except ValidationError:
            continue

    raise AppsciAgentUserSimError(f"could not parse a {{message}} turn from model output: {raw!r}")


def _first_json_object(text: str) -> str | None:
    """Return the first balanced {...} substring of text, or None. Brace-matching (string-aware)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _to_llm_messages(system_prompt: str, history: list[dict], question: str) -> list[dict]:
    """Build the LLM message list, applying the role flip.

    From the model's perspective it is the assistant: persona -> system; agent turns -> user; sim
    turns -> assistant. The new agent question is appended as the final user turn.

    The sim's own prior turns are re-serialized into the ``{"message": ...}`` JSON wrapper the persona
    prompt asks for, NOT replayed as the bare prose the parser extracted. Otherwise the model sees its
    last turn was plain prose and mimics that, dropping the JSON format -- which the parser then can't
    read. First-party providers that enforce response_format=json_object hide this; gateways that don't
    (e.g. a gateway) expose it as a turn-2 parse failure. Keeping the in-context examples in JSON
    fixes the drift regardless of whether the provider honors the format hint.
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for turn in history:
        role = turn.get("role")
        content = turn.get("content", "")
        if role == ROLE_AGENT:
            messages.append({"role": "user", "content": content})
        elif role == ROLE_SIM:
            messages.append(
                {"role": "assistant", "content": json.dumps({"message": content})}
            )
        else:
            raise AppsciAgentUserSimError(
                f"history turn has unknown role {role!r} (expected {ROLE_AGENT!r} or {ROLE_SIM!r})"
            )
    messages.append({"role": "user", "content": question})
    return messages


class AppsciAgentLLMUserSim:
    """A single-LLM-call appsci agent user sim bound to one rendered persona (implements AppsciAgentUserSimulator)."""

    def __init__(self, system_prompt: str, config: ModelConfig) -> None:
        self._system_prompt = system_prompt
        self._config = config

    @property
    def system_prompt(self) -> str:
        """The rendered persona system prompt (read-only; useful for inspection/debugging)."""
        return self._system_prompt

    @classmethod
    def from_persona(
        cls,
        task: TaskContextLike,
        persona_path: str | pathlib.Path,
        config: ModelConfig | None = None,
        docs_root: str | pathlib.Path | None = None,
    ) -> "AppsciAgentLLMUserSim":
        """Build a sim from a task + persona file, reusing persona.render_persona_prompt for the prompt.

        ``docs_root`` is the task's user-sim docs directory (``/user_sim_docs`` in the container), from
        which the persona's ``known_docs`` are loaded. Optional: a persona declaring none does not need
        it. A persona that declares documents and cannot get them raises UserSimDocsError here, which
        fails service startup rather than yielding a sim that silently knows nothing.
        """
        system_prompt = render_persona_prompt(task, persona_path, docs_root)
        return cls(system_prompt, config or config_for("APPSCI_USERSIM_MODEL"))

    def answer(self, question: str, history: list[dict], parent_span_id: str | None = None) -> str:
        """Answer one agent question given the prior history (the AppsciAgentUserSimulator primitive).

        The caller owns ``history`` (turns of {"role": "agent"|"user_sim", "content": str}, oldest
        first). The reply is parsed into the {message} and returned as plain text.

        RETRIES ONCE when the model answers in prose instead of the ``{"message": ...}`` JSON the
        persona prompt asks for. `json_object=True` is a hint, and the gateway this runs behind accepts
        it and then ignores it (see `_to_llm_messages`), so compliance is probabilistic: measured on
        airline builds, one exchange in roughly a dozen runs came back as markdown prose. Without a
        retry that single turn ends the whole exchange -- the service turns the parse failure into a
        500, the agent's `talk_to_user` reports it as a bare "Server error", and a build that has
        already paid for the call loses it. The retry re-sends the same conversation with one explicit
        corrective appended, which is cheap next to abandoning the turn.

        The offending output is logged either way. It used to be visible only in the 500's `detail`,
        which the caller discards, so the reason a build stalled existed nowhere on disk.
        """
        messages = _to_llm_messages(self._system_prompt, history, question)
        raw = complete(messages, self._config, json_object=True, caller="user_sim",
                       parent_span_id=parent_span_id)
        try:
            return _parse_message(raw)
        except AppsciAgentUserSimError:
            logger.warning(
                "user-sim answered in prose, not the {message} JSON the prompt asks for; retrying "
                "once with an explicit corrective. Offending output: %r", raw[:2000],
            )

        retry_messages = [
            *messages,
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    "That reply was not valid JSON. Send the SAME answer again as a single JSON "
                    'object and nothing else: {"message": "<your reply>"}. No prose outside the '
                    "object, no markdown fence."
                ),
            },
        ]
        retried = complete(retry_messages, self._config, json_object=True, caller="user_sim",
                           parent_span_id=parent_span_id)
        try:
            return _parse_message(retried)
        except AppsciAgentUserSimError as exc:
            # Still unparseable after a corrective. Fail loud rather than default-fill: an invented
            # persona turn would put words in the stakeholder's mouth, and the elicitation metric is
            # scored on what the persona actually said.
            logger.error(
                "user-sim still unparseable after the corrective retry. First output: %r Retry: %r",
                raw[:1000], retried[:1000],
            )
            raise AppsciAgentUserSimError(
                f"{exc} (retried once with an explicit JSON corrective; both replies unparseable)"
            ) from exc

    def run_conversation(
        self,
        agent_fn: Callable[[str | None], str | None],
        max_turns: int,
        *,
        agent_retries: int = 2,
    ) -> list[dict]:
        """Self-driving loop for in-process testing/eval (the agent drives; the sim never stops).

        Alternates: ask the agent for its next question (``agent_fn`` gets the sim's last reply, or
        None on the first turn), then answer it. Stops ONLY when ``agent_fn`` returns None/"" (the
        agent decides it is done) or ``max_turns`` exchanges have happened (the turn-ceiling backstop).
        The sim has no way to end the conversation.

        ``agent_fn`` is the least reliable dependency, so its calls are retried (``agent_retries``).

        Returns the transcript as a list of {"role": "agent"|"user_sim", "content": str} turns.
        """
        history: list[dict] = []
        last_sim_msg: str | None = None

        for _ in range(max_turns):
            question = _call_with_retries(agent_fn, last_sim_msg, retries=agent_retries)
            if not question:  # agent signals it has what it needs
                break
            # answer() gets the PRIOR history; _to_llm_messages appends this question as the final
            # user turn. Appending it to history first would duplicate it in the prompt.
            reply = self.answer(question, history)
            history.append({"role": ROLE_AGENT, "content": question})
            history.append({"role": ROLE_SIM, "content": reply})
            last_sim_msg = reply

        return history


def _call_with_retries(fn: Callable[[Any], Any], arg: Any, *, retries: int) -> Any:
    """Call fn(arg), retrying on exception up to ``retries`` times before re-raising the last error."""
    last_exc: Exception | None = None
    for _ in range(retries + 1):
        try:
            return fn(arg)
        except Exception as exc:  # noqa: BLE001 -- agent is arbitrary external code
            last_exc = exc
    raise last_exc  # type: ignore[misc]
