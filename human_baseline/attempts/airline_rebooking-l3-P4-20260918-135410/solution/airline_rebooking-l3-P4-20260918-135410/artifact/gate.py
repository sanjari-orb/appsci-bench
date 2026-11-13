"""The chokepoint every tool call passes through.

The airline tool API performs whatever it is told, so this module is the only thing standing between
the model and a policy violation. It fetches the records a rule needs, asks policy.py for a verdict,
and only then -- if the verdict allows it -- calls the real tool.

A denial here is final. The model is told so in its system prompt, and the denial text says what to
say to the customer instead. Every blocked write is written to stdout as an audit line, so after
scoring we can tell a wrong elicited rule from a misbehaving model.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field

import policy
import tools

TRANSFER_MESSAGE = "YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON."

# Writes that change the booking database and therefore need the customer's explicit yes first.
# send_certificate is deliberately absent: a certificate is something the customer has already asked
# for and the agent offers, not a change to their booking they must approve.
NEEDS_CONFIRMATION = frozenset({
    "book_reservation",
    "update_reservation_flights",
    "update_reservation_baggages",
    "update_reservation_passengers",
    "cancel_reservation",
})

_AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|confirm(ed|ing)?|correct|right|agreed|absolutely|"
    r"affirmative|proceed|go ahead|do it|please do|sounds good|that works|looks good|"
    r"let'?s do it|book it|cancel it|change it|i agree|i confirm|fine|perfect|great)\b",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"\b(no|nope|don'?t|do not|stop|wait|hold on|not yet|cancel that|never ?mind|actually)\b",
    re.IGNORECASE,
)


@dataclass
class ConversationState:
    """What the gate needs to know about the conversation so far."""

    last_user_text: str = ""
    last_assistant_text: str = ""
    prior_writes: list[dict] = field(default_factory=list)


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    transferred: bool = False
    write_performed: dict | None = None


def _audit(event: str, **fields) -> None:
    """One JSON line per blocked or performed write, to stdout so `docker logs` keeps it."""
    try:
        print("AUDIT " + json.dumps({"event": event, **fields}, default=str), flush=True)
    except Exception:  # noqa: BLE001 -- auditing must never break a customer conversation
        pass


def _confirmation_present(state: ConversationState) -> bool:
    """Did the agent recap the action and did the customer then say yes?"""
    if not state.last_assistant_text.strip():
        return False
    user_text = state.last_user_text or ""
    if not _AFFIRMATIVE.search(user_text):
        return False
    # "no, don't do that" contains no affirmative; "yes but wait" does -- treat a negation in the
    # same breath as withheld consent rather than granted.
    affirmative_at = _AFFIRMATIVE.search(user_text).start()
    negative = _NEGATIVE.search(user_text)
    return not (negative and negative.start() < affirmative_at)


# --- record fetching -------------------------------------------------------------------------


def _segment_statuses(reservation: dict) -> list[str]:
    statuses = []
    for flight in reservation.get("flights") or []:
        try:
            statuses.append(
                str(tools.call_tool("get_flight_status", {
                    "flight_number": flight.get("flight_number"),
                    "date": flight.get("date"),
                }))
            )
        except tools.ToolCallError:
            statuses.append("unknown")
    return statuses


def _statuses_for(flights: list) -> dict[tuple[str, str], str]:
    out = {}
    for flight in flights or []:
        key = (flight.get("flight_number"), flight.get("date"))
        try:
            out[key] = str(tools.call_tool("get_flight_status", {
                "flight_number": key[0], "date": key[1],
            }))
        except tools.ToolCallError:
            pass
    return out


# --- the gate --------------------------------------------------------------------------------


def _evaluate_write(name: str, args: dict, state: ConversationState) -> policy.Decision:
    """Fetch what the rule needs and return the verdict. Never calls a write tool."""
    if name == "book_reservation":
        user = tools.call_tool("get_user_details", {"user_id": args.get("user_id")})
        statuses = _statuses_for(args.get("flights"))
        return policy.check_booking(args, user or {}, statuses)

    if name in {
        "update_reservation_flights",
        "update_reservation_baggages",
        "update_reservation_passengers",
        "cancel_reservation",
    }:
        reservation = tools.call_tool(
            "get_reservation_details", {"reservation_id": args.get("reservation_id")}
        ) or {}

        if name == "update_reservation_passengers":
            return policy.check_passenger_change(args, reservation)

        user = tools.call_tool(
            "get_user_details", {"user_id": reservation.get("user_id")}
        ) or {}

        if name == "update_reservation_baggages":
            return policy.check_baggage_change(args, reservation, user)

        statuses = _segment_statuses(reservation)

        if name == "cancel_reservation":
            return policy.check_cancellation(args, reservation, statuses)

        existing_keys = {
            (f.get("flight_number"), f.get("date")) for f in reservation.get("flights") or []
        }
        new_flights = [
            f for f in args.get("flights") or []
            if (f.get("flight_number"), f.get("date")) not in existing_keys
        ]
        return policy.check_flight_or_cabin_change(
            args, reservation, user, statuses, _statuses_for(new_flights)
        )

    if name == "send_certificate":
        reservation = tools.call_tool(
            "get_reservation_details",
            {"reservation_id": args.get("justification_reservation_id")},
        ) or {}
        user = tools.call_tool("get_user_details", {"user_id": args.get("user_id")}) or {}
        statuses = _segment_statuses(reservation)
        return policy.check_compensation(args, user, reservation, statuses, state.prior_writes)

    return policy.ALLOW


def run_tool(name: str, args: dict, state: ConversationState) -> ToolOutcome:
    """Execute one tool call, subject to policy. The only path to the tool API."""
    if name == tools.TRANSFER_TOOL:
        try:
            tools.call_tool(name, args)
        except tools.ToolCallError as exc:
            # The handoff is what matters to the customer; a failed bookkeeping call must not
            # strand them mid-sentence.
            _audit("transfer_tool_failed", error=str(exc))
        _audit("transferred", summary=args.get("summary"))
        return ToolOutcome(content="Transferred to a human agent.", transferred=True)

    if name not in tools.WRITE_TOOLS:
        try:
            result = tools.call_tool(name, args)
        except tools.ToolCallError as exc:
            return ToolOutcome(content=str(exc), is_error=True)
        return ToolOutcome(content=json.dumps(result, default=str))

    # --- from here on: a write ---
    if name in NEEDS_CONFIRMATION and not _confirmation_present(state):
        _audit("blocked", tool=name, rule="confirmation.missing", args=args)
        return ToolOutcome(
            content=(
                "BLOCKED: the customer has not explicitly confirmed this action. Before any change "
                "to a booking you must state the details of what you are about to do -- flights, "
                "cabin, passengers, baggage and the fee, insurance, the total, and which payment "
                "method is charged or refunded -- and get a clear yes. Do that now, in a message to "
                "the customer. Do not call this tool again until they have agreed."
            ),
            is_error=True,
        )

    try:
        decision = _evaluate_write(name, args, state)
    except tools.ToolCallError as exc:
        # Could not read the records the rule depends on. Withhold the write: an unverifiable
        # eligibility check is not a satisfied one.
        _audit("blocked", tool=name, rule="policy.unverifiable", error=str(exc), args=args)
        return ToolOutcome(
            content=(
                f"BLOCKED: could not verify eligibility for this action ({exc}). Do not perform it. "
                f"Tell the customer you are unable to complete this right now."
            ),
            is_error=True,
        )

    if decision.verdict == "transfer":
        _audit("blocked_to_transfer", tool=name, rule=decision.rule,
               evidence=decision.evidence, args=args)
        try:
            tools.call_tool(tools.TRANSFER_TOOL, {"summary": decision.message})
        except tools.ToolCallError as exc:
            _audit("transfer_tool_failed", error=str(exc))
        return ToolOutcome(content=decision.message, transferred=True)

    if not decision.allowed:
        _audit("blocked", tool=name, rule=decision.rule,
               evidence=decision.evidence, args=args)
        return ToolOutcome(
            content=(
                f"BLOCKED by airline policy [{decision.rule}]. This decision is final -- do not "
                f"retry this tool, do not look for another route to the same outcome, and do not "
                f"transfer to a human unless the reason below says to. {decision.message}"
            ),
            is_error=True,
        )

    payload = tools.strip_justification(name, args)
    try:
        result = tools.call_tool(name, payload)
    except tools.ToolCallError as exc:
        return ToolOutcome(content=f"The tool API rejected this call: {exc}", is_error=True)

    _audit("write_performed", tool=name, args=payload)
    return ToolOutcome(
        content=json.dumps(result, default=str),
        write_performed={
            "tool": name,
            "reservation_id": payload.get("reservation_id")
            or args.get("justification_reservation_id"),
        },
    )
