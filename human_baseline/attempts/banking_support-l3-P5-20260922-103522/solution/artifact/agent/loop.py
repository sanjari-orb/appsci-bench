"""The per-turn tool-use loop.

``run_turn`` takes the OpenAI-style conversation from ``/respond``, drives an Anthropic tool-use
loop (base tools only) against the model, dispatches each requested tool through the banking tool
API, and returns the assistant's next turn in the OpenAI ``choices`` shape.

The loop is bounded by MAX_ITERATIONS so a single turn can never spin forever. Discoverable
specialist actions are reached only through the base ``unlock``/``call`` plumbing -- the model is
never handed their schemas (see tools.py, P3).
"""
from __future__ import annotations

import json
from typing import Any

from .config import Config
from .model_client import ModelClient
from .prompt import SYSTEM_PROMPT
from .tool_client import BankingToolClient
from .tools import BASE_TOOL_NAMES, TOOL_SCHEMAS

# A turn may need several searches + lookups + unlock/call; bound it so it can't loop forever.
MAX_ITERATIONS = 25


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map OpenAI-style roles to Anthropic. Only user/assistant carry into the turn; a leading
    system message (if any) is ignored -- our policy prompt is authoritative.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role in ("user", "assistant"):
            out.append({"role": role, "content": m.get("content", "")})
    return out


def _text_from_content(content: Any) -> str:
    """Extract assistant text from an Anthropic content list (or a plain string)."""
    if isinstance(content, str):
        return content
    parts = [b.text for b in content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip()


def _stringify(result: dict[str, Any]) -> str:
    try:
        return json.dumps(result)
    except (TypeError, ValueError):
        return str(result)


# Specialist actions are reached only through these two base tools. The guard below requires the
# model to have written an explicit precondition checklist before either runs, so an action can't be
# lifted out of its procedure's eligibility gates (DEF-1 / R-16 / R-18 / R-19). It names NO specific
# procedure or action -- it only enforces that the reasoning step happened (P3-safe).
_GUARDED = frozenset({"unlock_discoverable_agent_tool", "call_discoverable_agent_tool"})

# The prompt tells the model to write "PRECONDITION CHECK ... DECISION: proceed" before acting.
# We look for that decision marker in the assistant text of the SAME turn.
_DECISION_MARKER = "decision: proceed"


def _stated_precondition_check(text: str) -> bool:
    """True if the assistant's text records a completed 'proceed' precondition decision."""
    return _DECISION_MARKER in (text or "").lower()


def _guard_message(name: str) -> str:
    return (
        f"Blocked before running {name!r}: you have not stated a precondition check for this "
        "action in this turn. Before unlocking or calling a specialist action you must write the "
        "PRECONDITION CHECK checklist (each eligibility gate + the record's value + PASS/FAIL, then "
        "'DECISION: proceed' or 'do not proceed'), using the record's values, not the customer's "
        "claims. Write that checklist now; only call the action if the decision is proceed."
    )


class Agent:
    def __init__(self, config: Config | None = None) -> None:
        self._config = config or Config.from_env()
        self._model = ModelClient(self._config)
        self._tools = BankingToolClient(self._config)

    def run_turn(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        convo = _to_anthropic_messages(messages)
        final_text = ""
        # Set once the model has written a "DECISION: proceed" precondition checklist this turn.
        # Guarded specialist calls are held back until it has (see _dispatch / _GUARDED).
        precondition_stated = False
        for _ in range(MAX_ITERATIONS):
            resp = self._model.create(
                system=SYSTEM_PROMPT, messages=convo, tools=TOOL_SCHEMAS
            )
            # Record the assistant turn (text + any tool_use blocks) verbatim for the next round.
            convo.append({"role": "assistant", "content": resp.content})
            if _stated_precondition_check(_text_from_content(resp.content)):
                precondition_stated = True
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                final_text = _text_from_content(resp.content)
                break
            tool_results = [self._dispatch(b, precondition_stated) for b in tool_uses]
            convo.append({"role": "user", "content": tool_results})
        else:
            final_text = final_text or "I need to hand this to a human agent to complete."
        return {"choices": [{"message": {"role": "assistant", "content": final_text}}]}

    def _dispatch(self, block: Any, precondition_stated: bool) -> dict[str, Any]:
        """Execute one tool_use block, returning an Anthropic tool_result block.

        Guarded specialist calls (unlock/call) are held back with a corrective result until the model
        has written a precondition checklist this turn -- this forces the eligibility reasoning to be
        explicit without hardcoding any procedure (P3). The model retries on the next iteration.
        """
        name = block.name
        args = block.input or {}
        if name in _GUARDED and not precondition_stated:
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": _stringify({"error": _guard_message(name)}),
            }
        if name not in BASE_TOOL_NAMES:
            # Should not happen -- only base schemas are offered -- but guard P3 anyway.
            result: dict[str, Any] = {
                "error": f"{name!r} is not a base tool; specialist actions go through "
                "unlock_discoverable_agent_tool then call_discoverable_agent_tool."
            }
        else:
            result = self._tools.call(name, args)
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": _stringify(result),
        }

    def close(self) -> None:
        self._tools.close()
