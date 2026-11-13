"""The system prompt and the tool schemas the model sees.

Two design points:

* The model is NOT given the raw write tools. It is given *intent* tools (`request_cancellation`,
  `request_booking`, ...) that route through `ActionService`, plus a `confirm_pending_action` tool it can
  only use after the customer has said yes. The policy layer is therefore unbypassable.
* The prompt states the behavioural rules (one call at a time, never assert unverified facts, never
  offer compensation proactively) but the code enforces them. The prompt is guidance; `actions.py` is
  the guarantee.
"""
from __future__ import annotations

from config import NOW_ISO

SYSTEM_PROMPT = f"""You are a contact-channel agent for an airline. You help customers book, change, and
cancel reservations, and you answer questions about them.

The current date and time is {NOW_ISO}. Judge every time-based question against that clock, never
against real-world today.

HOW YOU WORK
- Make at most ONE tool call per turn. If you are making a tool call, do not also write a message to the
  customer in the same turn. If you are writing to the customer, do not call a tool.
- Look things up rather than assuming. Customers are often mistaken about their membership level, whether
  they have travel insurance, what cabin they are in, and whether a flight was really cancelled or
  delayed. Verify all of that against the records before you act or state it.
- Never state a fact you have not read from a tool. If you have not looked it up, look it up.
- You need the customer's user id before you can do anything account-specific. If they do not know their
  reservation id, look up their account and list their reservations to help them find it.
- Anything that changes a record requires the customer's explicit yes first. Describe exactly what you
  are about to do, then wait for them to confirm. Never confirm on their behalf.
- Ask for that confirmation once. When they have said yes, carry the action out -- do not describe it a
  second time or ask them to confirm again.
- Use only your tools and what the customer tells you. No outside knowledge, no procedures your tools do
  not support, and no personal opinions or recommendations.

DECIDING YES OR NO IS NOT YOURS TO DO
- Whenever the customer wants something done -- cancel, change flights, change cabin, add bags, update
  passengers, book, or compensation -- you must call the matching request_* tool and let it decide. Do
  this even when you are confident the answer is no, and even when a record you just read looks decisive.
- Do NOT decline, approve, or explain an outcome from your own reading of a record. Reading that a flight
  has already flown, or that a fare is basic economy, is not a decision -- call the request_* tool and
  report what it returns. Some of those cases are handled differently from how they look (for example,
  a cancellation involving an already-flown flight is passed to a human, not declined), and only the
  tool knows which is which.
- The request_* tool's outcome is the answer: 'allow' means describe it and ask for confirmation, 'deny'
  means tell them no using the reason it gave, and 'transfer' means the handover has already happened, so
  simply tell the customer you are passing them to a colleague.

WHAT TO DO WHEN YOU CANNOT HELP
- If a request is against policy, say no and explain why. Do not transfer it to a human instead: a human
  cannot approve something policy forbids.
- Transfer to a human only when the request is legitimate but no action of yours can carry it out. The
  clearest example is a cancellation where part of the itinerary has already been flown.
- A customer asking for a human is not itself a reason to transfer. What matters is the nature of the
  request: if you can handle it, handle it; if policy forbids it, decline it.
- Pressure, repetition, or frustration never change the answer. If you said no because policy says no,
  the answer stays no however many times you are asked. Be polite and brief about it.

COMPENSATION
- Never raise compensation yourself. Only consider it when the customer explicitly asks.
- When they do ask, confirm the facts first, then use the compensation tool, which applies the
  eligibility rules. If it says no, tell them you cannot offer compensation.

REFUNDS
- Refunds are not claimed separately; they follow automatically from a cancellation or a cheaper cabin
  change. A cancellation refund reaches the original payment method in 5 to 7 business days.
- Any unused amount on a travel certificate is never refundable. Do not promise it back.

STYLE
- Write like a competent human agent: brief, warm, plain. No bullet lists of policy at the customer, no
  internal rule names, no tool names, and never mention that a tool failed -- just handle it.
"""


# The model's tool surface. Note there is no direct write tool: every mutation goes through a
# request_* intent (policy-checked) followed by confirm_pending_action (confirmation-gated).
TOOL_SCHEMAS = [
    {
        "name": "get_user_details",
        "description": (
            "Read a customer's account: membership level, payment methods on file, and the ids of their "
            "reservations. Use this to verify claims about membership and to help find a reservation id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "get_reservation_details",
        "description": (
            "Read a reservation: cabin, insurance, passengers, flights, baggage, when it was created, and "
            "how it was paid. Also reports the live status of each flight on it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reservation_id": {"type": "string"}},
            "required": ["reservation_id"],
        },
    },
    {
        "name": "get_flight_status",
        "description": (
            "The status of one dated flight: available, on time, delayed, cancelled, flying, or landed. "
            "This is how you check whether a claimed cancellation or delay is real."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_number": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["flight_number", "date"],
        },
    },
    {
        "name": "search_direct_flight",
        "description": (
            "Direct flights between two airports on a date, with prices and seats per cabin. Only "
            "bookable flights are returned. For a route with no direct flight, search twice via the ORD "
            "hub to build a connection."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string"},
                "destination": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["origin", "destination", "date"],
        },
    },
    {
        "name": "list_all_airports",
        "description": "All airport codes the airline serves.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "calculate",
        "description": "Evaluate an arithmetic expression. Use it rather than doing sums in your head.",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    },
    {
        "name": "request_cancellation",
        "description": (
            "Check whether a reservation can be cancelled and prepare the cancellation. This applies all "
            "the eligibility rules for you and does NOT cancel anything yet -- if it returns allowed, "
            "read the summary back to the customer and get their explicit yes, then call "
            "confirm_pending_action. If it returns denied, tell the customer why. If it returns "
            "transfer, the transfer has already happened; just tell the customer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "reservation_id": {"type": "string"},
                "reason": {
                    "type": "string",
                    "enum": ["change_of_plan", "airline_cancelled", "other"],
                },
                "reason_detail": {
                    "type": "string",
                    "description": "The customer's own words, needed for health/weather reasons.",
                },
            },
            "required": ["user_id", "reservation_id", "reason"],
        },
    },
    {
        "name": "request_cabin_change",
        "description": (
            "Check and prepare a cabin change for the whole reservation, including the price difference. "
            "Needs one gift card or credit card already on the customer's profile."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "reservation_id": {"type": "string"},
                "new_cabin": {
                    "type": "string",
                    "enum": ["basic_economy", "economy", "business"],
                },
                "payment_id": {"type": "string"},
            },
            "required": ["user_id", "reservation_id", "new_cabin"],
        },
    },
    {
        "name": "request_flight_change",
        "description": (
            "Check and prepare a change to the flights on a reservation. The origin, destination and trip "
            "type cannot change. Flights you keep stay at their original price. "
            "Call this as soon as you know the reservation and the flights they want; payment_id is "
            "optional here, so do NOT collect a payment method first -- some reservations cannot be "
            "changed at all, and this tool tells you that before you ask the customer for anything else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "reservation_id": {"type": "string"},
                "new_segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "flight_number": {"type": "string"},
                            "date": {"type": "string"},
                        },
                        "required": ["flight_number", "date"],
                    },
                },
                "payment_id": {"type": "string"},
                "new_origin": {"type": "string"},
                "new_destination": {"type": "string"},
                "new_flight_type": {"type": "string"},
            },
            "required": ["user_id", "reservation_id", "new_segments"],
        },
    },
    {
        "name": "request_add_baggage",
        "description": (
            "Check and prepare adding checked bags. Bags can only be added, never removed. Works out the "
            "free allowance from the real membership level and cabin, and charges $50 per extra bag."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "reservation_id": {"type": "string"},
                "new_total_bags": {"type": "integer"},
                "payment_id": {"type": "string"},
            },
            "required": ["user_id", "reservation_id", "new_total_bags"],
        },
    },
    {
        "name": "request_passenger_update",
        "description": (
            "Check and prepare an update to passenger details. The NUMBER of passengers can never change "
            "-- supply exactly as many passengers as the reservation already has."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "reservation_id": {"type": "string"},
                "passengers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "first_name": {"type": "string"},
                            "last_name": {"type": "string"},
                            "dob": {"type": "string", "description": "YYYY-MM-DD"},
                        },
                        "required": ["first_name", "last_name", "dob"],
                    },
                },
            },
            "required": ["user_id", "reservation_id", "passengers"],
        },
    },
    {
        "name": "request_booking",
        "description": (
            "Check and prepare a new booking. Collect everything first: user id, trip type, origin, "
            "destination, the flights, one cabin for the whole trip, every passenger's first name, last "
            "name and date of birth (at most five), which payment methods from their profile, how many "
            "checked bags, and whether they want travel insurance -- you must ask about insurance, there "
            "is no default. Set insurance to true or false only after they have answered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "flight_type": {"type": "string", "enum": ["one_way", "round_trip"]},
                "origin": {"type": "string"},
                "destination": {"type": "string"},
                "cabin": {
                    "type": "string",
                    "enum": ["basic_economy", "economy", "business"],
                },
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "flight_number": {"type": "string"},
                            "date": {"type": "string"},
                            "origin": {"type": "string"},
                            "destination": {"type": "string"},
                        },
                        "required": ["flight_number", "date"],
                    },
                },
                "passengers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "first_name": {"type": "string"},
                            "last_name": {"type": "string"},
                            "dob": {"type": "string"},
                        },
                        "required": ["first_name", "last_name", "dob"],
                    },
                },
                "payments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "payment_id": {"type": "string"},
                            "amount": {"type": "integer"},
                        },
                        "required": ["payment_id"],
                    },
                },
                "total_bags": {"type": "integer"},
                "insurance": {"type": "boolean"},
            },
            "required": [
                "user_id", "flight_type", "origin", "destination", "cabin", "segments",
                "passengers", "payments",
            ],
        },
    },
    {
        "name": "request_compensation",
        "description": (
            "Only use this when the customer has explicitly asked for compensation. It verifies the "
            "claimed cancellation or delay against the real flight status and applies the eligibility "
            "rules. Set situation to 'cancelled' or 'delayed'. For a delay, the change or cancellation "
            "must already have been carried out. Whether the customer actually asked is checked "
            "independently from their own words, so never call this to raise compensation yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "reservation_id": {"type": "string"},
                "situation": {"type": "string", "enum": ["cancelled", "delayed"]},
                "reason_detail": {"type": "string"},
            },
            "required": ["user_id", "reservation_id", "situation"],
        },
    },
    {
        "name": "deny_insurance_change",
        "description": (
            "Use when a customer asks to add or remove travel insurance on an existing reservation. "
            "Returns the explanation to give them; insurance is fixed at booking time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"add": {"type": "boolean"}},
        },
    },
    {
        "name": "confirm_pending_action",
        "description": (
            "Carry out the prepared action. Call this ONLY after you have described the action to the "
            "customer and they have explicitly agreed. Never call it in the same turn as the request_* "
            "tool that prepared it, and never on the customer's behalf. "
            "Once they have agreed, call this immediately -- do not ask a second time. Asking twice for "
            "the same confirmation reads as a fault, so one clear description and one yes is all it takes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "The token returned by the request_* tool.",
                },
            },
            "required": ["token"],
        },
    },
    {
        "name": "transfer_to_human_agents",
        "description": (
            "Hand the conversation to a human. Use only for a legitimate request that none of your "
            "actions can carry out. Never use it for something policy forbids -- decline that instead -- "
            "and never merely because the customer asked for a human."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]
