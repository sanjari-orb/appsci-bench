"""The agent's internal tool-calling loop.

`/respond` is stateless and returns one assistant turn, so a single call to `Agent.run` performs a
complete reasoning loop: the model calls tools, each write is judged by the deterministic guard in
policy.py, and the loop exits only when the model produces user-facing text.

Two policy rules about the shape of a turn are enforced structurally here rather than trusted to
the prompt:
  * one tool call at a time -- extra tool_use blocks are rejected unexecuted;
  * never a tool call and a message together -- mixed turns cannot reach the caller, because only
    a text-only turn ends the loop.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

import config
import policy
import prompt as prompt_module
from env_client import EnvClient
from tools import llm_tools

logger = logging.getLogger("airline.agent")

# Returned when the loop cannot finish cleanly. A handoff is the safe outcome: it never asserts an
# unverified fact and never leaves a customer without a route forward.
FALLBACK_REPLY = (
    "I'm sorry, I haven't been able to complete this. Let me hand you to a human agent who can "
    "help further."
)

MAX_TURNS_REPLY = (
    "This conversation has gone on for a while, so let me hand you to a human agent who can "
    "help further."
)

MULTIPLE_TOOL_CALLS_FEEDBACK = (
    "Only one tool call per turn is allowed. This call was not executed -- issue it on its own."
)


class Agent:
    """Runs one conversational turn, including any tool calls needed to complete it."""

    def __init__(self, llm: Any | None = None, env: EnvClient | None = None):
        # Both clients are injectable so the loop can be tested without an API key or a container.
        # The SDK is imported lazily for the same reason: the loop's mechanics are testable with a
        # stub, and a missing key should fail at construction rather than at import.
        self._llm = llm if llm is not None else _anthropic_client()
        self.env = env if env is not None else EnvClient()
        self.tools = llm_tools()

    # --- public entry point ---------------------------------------------------------------------
    def run(self, messages: list[dict]) -> str:
        """Produce the next assistant message for `messages` (OpenAI-style role/content dicts)."""
        user_turns = sum(1 for m in messages if m.get("role") == "user")
        if user_turns > config.MAX_USER_TURNS:
            logger.warning("conversation exceeded %s user turns", config.MAX_USER_TURNS)
            return MAX_TURNS_REPLY

        convo = _to_anthropic_messages(messages)
        facts = _ConversationFacts(messages)
        violations: Counter[str] = Counter()

        for _ in range(config.MAX_TOOL_ITERS):
            response = self._llm.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=prompt_module.SYSTEM_PROMPT,
                tools=self.tools,
                messages=convo,
            )
            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]

            if not tool_uses:
                # A text-only turn is the only way out of the loop.
                return _text_of(response) or FALLBACK_REPLY

            results = []
            for index, block in enumerate(tool_uses):
                if index > 0:
                    # Policy: one tool call at a time. Reject the extras without executing them.
                    logger.warning("rejected extra tool call %s", block.name)
                    results.append(_tool_result(block.id, MULTIPLE_TOOL_CALLS_FEEDBACK, error=True))
                    continue
                outcome = self._handle_tool_call(block.name, block.input or {}, facts)
                if outcome.violation_code:
                    violations[outcome.violation_code] += 1
                results.append(_tool_result(block.id, outcome.text, error=outcome.is_error))

            convo.append({"role": "assistant", "content": _content_dicts(response.content)})
            convo.append({"role": "user", "content": results})

            if violations and violations.most_common(1)[0][1] >= config.MAX_REPEATED_VIOLATIONS:
                # The model is retrying a blocked action instead of accepting it. Stop and let it
                # word the refusal from the guard's own explanation.
                code, _ = violations.most_common(1)[0]
                logger.warning("stopping turn: violation %s repeated", code)
                return self._final_refusal(convo)

        logger.warning("tool iteration cap reached")
        return self._final_refusal(convo)

    # --- tool handling -------------------------------------------------------------------------
    def _handle_tool_call(self, name: str, arguments: dict, facts: "_ConversationFacts"
                          ) -> "_Outcome":
        """Guard a write, then proxy the call. A blocked write never reaches the environment."""
        if name in policy.WRITE_TOOLS:
            ctx = self._build_context(name, arguments, facts)
            violation = policy.check(name, arguments, ctx)
            if violation is not None:
                logger.info("blocked %s: %s", name, violation.code)
                return _Outcome(violation.as_feedback(), is_error=True,
                                violation_code=violation.code)

        result = self.env.call(name, arguments)
        if result.ok and name in {"cancel_reservation", "update_reservation_flights"}:
            # Evidence that the reservation was actually resolved this turn, which the delay
            # certificate depends on. Recorded from an observed success, never from a claim.
            facts.reservation_resolved = True
        return _Outcome(_stringify(result.payload), is_error=not result.ok)

    def _build_context(self, name: str, arguments: dict,
                       facts: "_ConversationFacts") -> policy.Context:
        """Fetch the records the guard needs, independently of what the model looked up."""
        reservation_id = arguments.get("reservation_id")
        reservation = self.env.get_reservation(reservation_id) if reservation_id else None

        user_id = arguments.get("user_id") or (reservation or {}).get("user_id")
        user = self.env.get_user(user_id) if user_id else None

        statuses: dict[tuple[str, str], str] = {}
        for flight in (reservation or {}).get("flights", []):
            key = (flight["flight_number"], flight["date"])
            status = self.env.get_flight_status(*key)
            if status is not None:
                statuses[key] = status

        # Requested flights need seats and prices, which only a route search returns. The route
        # comes from the reservation for a change, or from the booking arguments for a new booking.
        available: dict[tuple[str, str], dict] = {}
        origin = arguments.get("origin") or (reservation or {}).get("origin")
        destination = arguments.get("destination") or (reservation or {}).get("destination")
        for flight in arguments.get("flights") or []:
            key = (flight["flight_number"], flight["date"])
            found = self._find_flight(key, origin, destination)
            if found:
                available[key] = found

        return policy.Context(
            reservation=reservation,
            user=user,
            flight_statuses=statuses,
            available_flights=available,
            cancellation_reason=facts.cancellation_reason,
            complaint=facts.complaint,
            reservation_resolved=facts.reservation_resolved,
        )

    def _find_flight(self, key: tuple[str, str], origin: str | None,
                     destination: str | None) -> dict | None:
        """Locate a bookable instance, trying the known route and then its reverse.

        The reverse covers the return leg of a round trip, whose origin and destination are the
        mirror of the reservation's. `search_direct_flight` does not echo `origin`/`destination`
        back on the instances it returns, so they are stamped on here from the route that was
        actually searched -- the guard's route checks depend on those fields being present and
        correct.
        """
        flight_number, date = key
        for o, d in [(origin, destination), (destination, origin)]:
            if o and d:
                found = self.env.find_available_flight(o, d, date, flight_number)
                if found:
                    return {**found, "origin": o, "destination": d}
        return None

    def _final_refusal(self, convo: list[dict]) -> str:
        """Ask the model for a closing message with the tools withdrawn, so it must reply in text."""
        try:
            response = self._llm.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=prompt_module.SYSTEM_PROMPT,
                messages=convo + [{
                    "role": "user",
                    "content": "Reply to the customer now, in plain text, with no tool calls. "
                               "Explain the outcome accurately based on what the policy checks "
                               "told you, or transfer them to a human agent if that is required.",
                }],
            )
            return _text_of(response) or FALLBACK_REPLY
        except Exception:  # noqa: BLE001 - a failure here must still yield a usable reply
            logger.exception("failed to compose closing message")
            return FALLBACK_REPLY


def _anthropic_client() -> Any:
    """Construct the real Anthropic client, importing the SDK on demand."""
    import anthropic

    return anthropic.Anthropic()


class _Outcome:
    """The text handed back to the model for one tool call, and whether it was an error."""

    def __init__(self, text: str, is_error: bool = False, violation_code: str | None = None):
        self.text = text
        self.is_error = is_error
        self.violation_code = violation_code


class _ConversationFacts:
    """Conversational facts the guard needs but cannot read from the database.

    Cancellation reason and complaint type are stated by the customer, so they are read from the
    transcript. `reservation_resolved` is different in kind: it is set only when a write is
    observed to succeed during this turn, never inferred from what anyone says.
    """

    CANCEL_REASON_HINTS = {
        "health": ("ill", "sick", "health", "medical", "hospital", "doctor"),
        "weather": ("weather", "storm", "hurricane", "snow", "fog"),
        "change_of_plan": ("change of plan", "changed my plan", "changed my mind", "plans changed"),
        "airline_cancelled": ("airline cancelled", "airline canceled", "flight was cancelled",
                              "flight was canceled"),
    }

    def __init__(self, messages: list[dict]):
        text = " ".join(str(m.get("content", "")) for m in messages
                        if m.get("role") == "user").lower()
        self.cancellation_reason = self._reason(text)
        self.complaint = self._complaint(text)
        self.reservation_resolved = False

    def _reason(self, text: str) -> str | None:
        for reason, hints in self.CANCEL_REASON_HINTS.items():
            if any(h in text for h in hints):
                return reason
        return None

    def _complaint(self, text: str) -> str | None:
        """Which compensable complaint, if any, the customer has raised."""
        asks_for_compensation = any(word in text for word in
                                    ("compensation", "compensate", "certificate", "voucher",
                                     "credit", "goodwill", "owed"))
        if not asks_for_compensation:
            return None
        if any(w in text for w in ("cancelled", "canceled", "cancellation")):
            return "cancelled_flight"
        if any(w in text for w in ("delay", "delayed")):
            return "delayed_flight"
        return "other"


# --- message translation --------------------------------------------------------------------------
def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Convert the OpenAI-style transcript to Anthropic format, dropping system turns.

    Only user and assistant turns are carried: policy lives in the system prompt, and any tool
    traces from previous turns are gone by design (the caller never saw them).
    """
    out = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"} or not content:
            continue
        out.append({"role": role, "content": str(content)})
    if not out or out[0]["role"] != "user":
        # Anthropic requires the first message to be from the user.
        out.insert(0, {"role": "user", "content": "Hello"})
    return out


def _content_dicts(blocks: list[Any]) -> list[dict]:
    """Serialise response content blocks back into request-shaped dicts."""
    out = []
    for block in blocks:
        kind = getattr(block, "type", None)
        if kind == "text":
            out.append({"type": "text", "text": block.text})
        elif kind == "tool_use":
            out.append({"type": "tool_use", "id": block.id, "name": block.name,
                        "input": block.input})
    return out


def _tool_result(tool_use_id: str, content: str, error: bool = False) -> dict:
    """One tool_result block for the model's next turn."""
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content,
            "is_error": error}


def _text_of(response: Any) -> str:
    """Concatenate the text blocks of a response."""
    return "".join(b.text for b in response.content
                   if getattr(b, "type", None) == "text").strip()


def _stringify(payload: Any) -> str:
    """Render a tool payload for the model."""
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, default=str)
