"""The airline tool API: transport, and the 14 tool definitions handed to the model.

The tool names and argument schemas are not published anywhere -- they were elicited from the
stakeholder and transcribed here (see REQUIREMENTS.md in the workspace root). Two definitions carry
an extra field the real API does not have; see JUSTIFICATION_FIELDS below for why.

The tool API enforces NO policy: every write it accepts will be performed, whether or not the
airline allows it. Nothing in this module refuses anything -- that is gate.py's job. This module is
pure transport.
"""
from __future__ import annotations

import os

import httpx

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ToolCallError(RuntimeError):
    """The tool API refused or failed. Distinct from a policy denial, which never gets this far."""


def call_tool(name: str, arguments: dict) -> object:
    """POST {AIRLINE_TOOLS_URL}/<name> with {"arguments": ...} -> the "result" value.

    Both env vars are read per call, never cached at import: the deployer injects a different tool
    URL and a different token per environment copy, and a value captured at import time would send
    every request to the copy that happened to start first.
    """
    base = (os.environ.get("AIRLINE_TOOLS_URL") or "").rstrip("/")
    token = os.environ.get("AIRLINE_TOOLS_TOKEN") or ""
    if not base:
        raise ToolCallError("AIRLINE_TOOLS_URL is not set")

    try:
        response = httpx.post(
            f"{base}/{name}",
            json={"arguments": arguments},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ToolCallError(f"could not reach the airline tool API: {exc}") from exc

    if response.status_code >= 400:
        raise ToolCallError(f"{name} failed ({response.status_code}): {response.text[:400]}")
    try:
        return response.json().get("result")
    except ValueError as exc:
        raise ToolCallError(f"{name} returned a non-JSON body: {response.text[:200]}") from exc


# --- tool inventory -------------------------------------------------------------------------

WRITE_TOOLS = frozenset({
    "book_reservation",
    "update_reservation_flights",
    "update_reservation_baggages",
    "update_reservation_passengers",
    "cancel_reservation",
    "send_certificate",
})

TRANSFER_TOOL = "transfer_to_human_agents"

# Fields added to two write schemas that the real tool API does not accept, and which are stripped
# before the call. They exist so a policy decision the arguments alone cannot support becomes
# checkable against live data:
#   * a cancellation's eligibility depends on WHY the customer is cancelling (the insurance
#     condition), and `cancel_reservation` takes only a reservation id;
#   * compensation is permitted on exactly two grounds and `send_certificate` takes only a user id
#     and an amount, so without naming the reservation there is nothing to verify the ground against.
# The model must state its justification; the gate then checks that justification against the store
# rather than trusting it.
JUSTIFICATION_FIELDS = {
    "cancel_reservation": ("cancellation_reason",),
    "send_certificate": ("justification_reservation_id", "justification_ground"),
}


def strip_justification(name: str, arguments: dict) -> dict:
    """Drop our added fields so the payload matches the real tool schema."""
    extra = JUSTIFICATION_FIELDS.get(name, ())
    return {k: v for k, v in arguments.items() if k not in extra}


_PASSENGER = {
    "type": "object",
    "properties": {
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "dob": {"type": "string", "description": "YYYY-MM-DD"},
    },
    "required": ["first_name", "last_name", "dob"],
}

_FLIGHT = {
    "type": "object",
    "properties": {
        "flight_number": {"type": "string"},
        "date": {"type": "string", "description": "YYYY-MM-DD"},
    },
    "required": ["flight_number", "date"],
}

_PAYMENT = {
    "type": "object",
    "properties": {
        "payment_id": {"type": "string", "description": "must already exist on the user's profile"},
        "amount": {"type": "number"},
    },
    "required": ["payment_id", "amount"],
}

TOOL_SCHEMAS = [
    {
        "name": "get_user_details",
        "description": "Look up a customer by user id: name, dob, membership tier, payment methods, "
                       "saved passengers, and the ids of their reservations.",
        "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}},
                         "required": ["user_id"]},
    },
    {
        "name": "get_reservation_details",
        "description": "Look up one reservation by id: cabin, flights, passengers, baggage, "
                       "insurance, payment history, and when it was created.",
        "input_schema": {"type": "object", "properties": {"reservation_id": {"type": "string"}},
                         "required": ["reservation_id"]},
    },
    {
        "name": "get_flight_status",
        "description": "Status of one flight on one date: available, on time, delayed, flying, "
                       "landed, or cancelled.",
        "input_schema": {"type": "object",
                         "properties": {"flight_number": {"type": "string"},
                                        "date": {"type": "string", "description": "YYYY-MM-DD"}},
                         "required": ["flight_number", "date"]},
    },
    {
        "name": "search_direct_flight",
        "description": "Direct flights for a route and date, with per-cabin prices, available seats "
                       "and status. Prices come from here -- never invent one.",
        "input_schema": {"type": "object",
                         "properties": {"origin": {"type": "string"},
                                        "destination": {"type": "string"},
                                        "date": {"type": "string", "description": "YYYY-MM-DD"}},
                         "required": ["origin", "destination", "date"]},
    },
    {
        "name": "search_onestop_flight",
        "description": "Connecting itineraries for a route and date. Each result is one ITINERARY and "
                       "may consist of more than one flight segment; if it does, every segment "
                       "belongs in the reservation's flight list, in order. Do not assume the shape "
                       "matches the direct search -- read what comes back before acting on it.",
        "input_schema": {"type": "object",
                         "properties": {"origin": {"type": "string"},
                                        "destination": {"type": "string"},
                                        "date": {"type": "string", "description": "YYYY-MM-DD"}},
                         "required": ["origin", "destination", "date"]},
    },
    {
        "name": "list_all_airports",
        "description": "Every airport code the airline serves.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "calculate",
        "description": "Evaluate an arithmetic expression. Use this for every total, difference and "
                       "baggage fee rather than doing arithmetic in your head.",
        "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}},
                         "required": ["expression"]},
    },
    {
        "name": "book_reservation",
        "description": "Create a reservation. Only flights with status 'available' may be booked. "
                       "Requires the customer's explicit confirmation of the full details first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "origin": {"type": "string"},
                "destination": {"type": "string"},
                "flight_type": {"type": "string", "enum": ["one_way", "round_trip"]},
                "cabin": {"type": "string",
                          "enum": ["basic_economy", "economy", "business"],
                          "description": "same for every flight in the reservation"},
                "flights": {"type": "array", "items": _FLIGHT},
                "passengers": {"type": "array", "items": _PASSENGER,
                               "description": "at most five"},
                "payment_methods": {"type": "array", "items": _PAYMENT,
                                    "description": "at most 1 certificate, 1 credit card, 3 gift cards"},
                "total_baggages": {"type": "integer"},
                "nonfree_baggages": {"type": "integer",
                                     "description": "bags beyond the free allowance, $50 each"},
                "insurance": {"type": "string", "enum": ["yes", "no"]},
            },
            "required": ["user_id", "origin", "destination", "flight_type", "cabin", "flights",
                         "passengers", "payment_methods", "total_baggages", "nonfree_baggages",
                         "insurance"],
        },
    },
    {
        "name": "update_reservation_flights",
        "description": "Change the flights OR the cabin of a reservation -- one or the other in a "
                       "single call, never both. Basic economy flights cannot be changed at all, "
                       "though basic economy cabin can. Pass a single gift card or credit card as "
                       "payment_id for the difference; never a certificate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {"type": "string"},
                "cabin": {"type": "string", "enum": ["basic_economy", "economy", "business"]},
                "flights": {"type": "array", "items": _FLIGHT,
                            "description": "the complete resulting flight list, kept segments included"},
                "payment_id": {"type": "string"},
            },
            "required": ["reservation_id", "cabin", "flights", "payment_id"],
        },
    },
    {
        "name": "update_reservation_baggages",
        "description": "Change baggage counts. Bags may be added, never removed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {"type": "string"},
                "total_baggages": {"type": "integer"},
                "nonfree_baggages": {"type": "integer"},
                "payment_id": {"type": "string",
                               "description": "a single gift card or credit card on the profile"},
            },
            "required": ["reservation_id", "total_baggages", "nonfree_baggages", "payment_id"],
        },
    },
    {
        "name": "update_reservation_passengers",
        "description": "Change passenger details. The NUMBER of passengers can never change -- not "
                       "by you and not by a human agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {"type": "string"},
                "passengers": {"type": "array", "items": _PASSENGER},
            },
            "required": ["reservation_id", "passengers"],
        },
    },
    {
        "name": "cancel_reservation",
        "description": "Cancel a whole reservation. State the customer's reason: it decides "
                       "eligibility when the only qualifying condition is travel insurance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {"type": "string"},
                "cancellation_reason": {
                    "type": "string",
                    "enum": ["change_of_plan", "airline_cancelled", "other"],
                    "description": "the customer's stated reason. Use 'other' for health or weather, "
                                   "which travel insurance covers.",
                },
            },
            "required": ["reservation_id", "cancellation_reason"],
        },
    },
    {
        "name": "send_certificate",
        "description": "Issue a compensation certificate. Only when the customer has explicitly "
                       "asked, only on the two permitted grounds, and only after confirming the "
                       "facts with the tools. Name the reservation and the ground so both can be "
                       "verified; the amount is fixed by policy, not by you.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "amount": {"type": "number"},
                "justification_reservation_id": {
                    "type": "string",
                    "description": "the reservation the compensation is for",
                },
                "justification_ground": {
                    "type": "string",
                    "enum": ["airline_cancelled_flight", "delayed_flight_change_or_cancel"],
                    "description": "the only two permitted grounds",
                },
            },
            "required": ["user_id", "amount", "justification_reservation_id",
                         "justification_ground"],
        },
    },
    {
        "name": TRANSFER_TOOL,
        "description": "Hand the conversation to a human agent. Only for a legitimate need you "
                       "cannot service yourself -- never as a way out of telling a customer no.",
        "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}},
                         "required": ["summary"]},
    },
]

assert len(TOOL_SCHEMAS) == 14, f"expected 14 tools, have {len(TOOL_SCHEMAS)}"
