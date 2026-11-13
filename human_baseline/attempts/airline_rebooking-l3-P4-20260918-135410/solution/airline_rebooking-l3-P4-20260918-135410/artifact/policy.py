"""Policy predicates: pure functions over records already fetched.

No I/O, no model, no tool calls. Each rule answers one question about data handed to it, so each is
readable against the elicited policy line it implements and testable on its own. Fetching the records
and acting on a verdict is gate.py's job.

Every rule here was elicited from the stakeholder (see REQUIREMENTS.md). Where the stakeholder could
not confirm a rule from written policy, it is NOT enforced here -- see `PREFER_AVAILABLE_ON_CHANGE`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import clock

# Free checked bags per passenger: membership tier x that passenger's cabin.
FREE_BAGS = {
    "regular": {"basic_economy": 0, "economy": 1, "business": 2},
    "silver": {"basic_economy": 1, "economy": 2, "business": 3},
    "gold": {"basic_economy": 2, "economy": 3, "business": 4},
}

EXTRA_BAG_FEE = 50
INSURANCE_PER_PASSENGER = 30
MAX_PASSENGERS = 5

# Payment-method limits, per reservation, at booking time.
MAX_CERTIFICATES = 1
MAX_CREDIT_CARDS = 1
MAX_GIFT_CARDS = 3

# Compensation amounts, per passenger. Computed here, never taken from the model.
COMPENSATION_CANCELLED = 100
COMPENSATION_DELAYED = 50

# Only these may pay for, or be refunded on, a post-booking change. A certificate may not.
CHANGE_PAYMENT_SOURCES = frozenset({"gift_card", "credit_card"})

# The one rule the stakeholder could not confirm from written policy: whether a flight a customer is
# moved ONTO must itself be 'available'. They gave it as their judgment and said to flag it as
# unconfirmed. Decision for this build: prefer available, do not enforce. The two firm rules either
# side of it -- only 'available' is bookable, a departed segment blocks changes -- are enforced
# separately, so allowing this cannot breach a confirmed rule.
PREFER_AVAILABLE_ON_CHANGE = True


@dataclass
class Decision:
    """A policy verdict. `verdict` is one of allow / deny / transfer."""

    verdict: str
    rule: str = ""
    message: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


ALLOW = Decision("allow")


def _deny(rule: str, message: str, **evidence) -> Decision:
    return Decision("deny", rule, message, evidence)


def _transfer(rule: str, message: str, **evidence) -> Decision:
    return Decision("transfer", rule, message, evidence)


# --- helpers ---------------------------------------------------------------------------------


def free_baggage_allowance(membership: str, cabin: str, passenger_count: int) -> int:
    """Total free checked bags for a reservation: per-passenger allowance x passengers."""
    tier = FREE_BAGS.get(str(membership).strip().lower(), FREE_BAGS["regular"])
    per_passenger = tier.get(str(cabin).strip().lower(), 0)
    return per_passenger * max(0, passenger_count)


def payment_source(user: dict, payment_id: str) -> str | None:
    """The source ('credit_card' / 'gift_card' / 'certificate') of a payment id on the profile."""
    method = (user.get("payment_methods") or {}).get(payment_id)
    return method.get("source") if isinstance(method, dict) else None


def payment_balance(user: dict, payment_id: str) -> float | None:
    """Remaining balance for a gift card or certificate; None for a credit card or unknown id."""
    method = (user.get("payment_methods") or {}).get(payment_id)
    if isinstance(method, dict) and method.get("source") in {"gift_card", "certificate"}:
        return method.get("amount")
    return None


def is_compensation_eligible(user: dict, reservation: dict) -> bool:
    """Silver/gold, OR has travel insurance, OR flying business. Otherwise no compensation at all."""
    membership = str(user.get("membership", "")).strip().lower()
    if membership in {"silver", "gold"}:
        return True
    if str(reservation.get("insurance", "")).strip().lower() == "yes":
        return True
    return str(reservation.get("cabin", "")).strip().lower() == "business"


# --- booking ---------------------------------------------------------------------------------


def check_booking(args: dict, user: dict, flight_statuses: dict[tuple[str, str], str]) -> Decision:
    passengers = args.get("passengers") or []
    if len(passengers) > MAX_PASSENGERS:
        return _deny(
            "booking.max_passengers",
            f"A reservation can hold at most {MAX_PASSENGERS} passengers; this one has "
            f"{len(passengers)}. Tell the customer and split the booking if they want more.",
            passenger_count=len(passengers),
        )
    if not passengers:
        return _deny("booking.no_passengers", "A booking needs at least one passenger.")

    for flight in args.get("flights") or []:
        key = (flight.get("flight_number"), flight.get("date"))
        status = flight_statuses.get(key)
        if status is None:
            return _deny(
                "booking.status_unknown",
                f"Could not read the status of {key[0]} on {key[1]}, so it cannot be booked.",
                flight=key[0], date=key[1],
            )
        if not clock.is_bookable(status):
            return _deny(
                "booking.not_available",
                f"{key[0]} on {key[1]} has status '{status}'. Only flights with status 'available' "
                f"can be booked. Tell the customer and offer an available alternative.",
                flight=key[0], date=key[1], status=status,
            )

    payments = args.get("payment_methods") or []
    if not payments:
        return _deny("booking.no_payment", "A booking needs at least one payment method.")

    counts = {"certificate": 0, "credit_card": 0, "gift_card": 0}
    for payment in payments:
        payment_id = payment.get("payment_id")
        source = payment_source(user, payment_id)
        if source is None:
            return _deny(
                "booking.payment_not_on_profile",
                f"Payment method '{payment_id}' is not on this customer's profile. Only methods "
                f"already saved to the profile may be used; a new one cannot be added here.",
                payment_id=payment_id,
            )
        counts[source] = counts.get(source, 0) + 1
        balance = payment_balance(user, payment_id)
        amount = payment.get("amount") or 0
        if balance is not None and amount > balance:
            return _deny(
                "booking.insufficient_balance",
                f"'{payment_id}' holds {balance}, which does not cover the {amount} charged to it.",
                payment_id=payment_id, balance=balance, amount=amount,
            )

    for source, limit, label in (
        ("certificate", MAX_CERTIFICATES, "travel certificate"),
        ("credit_card", MAX_CREDIT_CARDS, "credit card"),
        ("gift_card", MAX_GIFT_CARDS, "gift card"),
    ):
        if counts.get(source, 0) > limit:
            plural = "" if limit == 1 else "s"
            return _deny(
                f"booking.too_many_{source}",
                f"A reservation may use at most {limit} {label}{plural}; this one uses "
                f"{counts[source]}.",
                source=source, used=counts[source], limit=limit,
            )

    expected_free = free_baggage_allowance(
        user.get("membership", "regular"), args.get("cabin", ""), len(passengers)
    )
    total_bags = int(args.get("total_baggages") or 0)
    stated_nonfree = int(args.get("nonfree_baggages") or 0)
    correct_nonfree = max(0, total_bags - expected_free)
    if stated_nonfree != correct_nonfree:
        return _deny(
            "booking.baggage_math",
            f"This customer is {user.get('membership')} in {args.get('cabin')}, so "
            f"{expected_free} checked bag(s) are free across {len(passengers)} passenger(s). With "
            f"{total_bags} bag(s) total, nonfree_baggages must be {correct_nonfree}, not "
            f"{stated_nonfree}. Re-state the baggage and the fee to the customer with the correct "
            f"figures and re-confirm.",
            free_allowance=expected_free, total=total_bags,
            stated_nonfree=stated_nonfree, correct_nonfree=correct_nonfree,
        )

    return ALLOW


# --- flights / cabin -------------------------------------------------------------------------


def _same_flights(requested: list, existing: list) -> bool:
    def key(flights):
        return [(f.get("flight_number"), f.get("date")) for f in flights or []]

    return key(requested) == key(existing)


def check_flight_or_cabin_change(
    args: dict,
    reservation: dict,
    user: dict,
    segment_statuses: list[str],
    new_flight_statuses: dict[tuple[str, str], str],
) -> Decision:
    """`update_reservation_flights` covers two distinct policy paths. Pick one and check it."""
    existing_cabin = str(reservation.get("cabin", "")).strip().lower()
    requested_cabin = str(args.get("cabin", "")).strip().lower()
    cabin_changing = requested_cabin != existing_cabin
    flights_changing = not _same_flights(args.get("flights"), reservation.get("flights"))

    if cabin_changing and flights_changing:
        return _deny(
            "change.cabin_and_flights_together",
            "A cabin change does not change the flights, and a flight change keeps the cabin. Do "
            "one or the other in a single call, and confirm each with the customer separately.",
        )

    payment_id = args.get("payment_id")
    source = payment_source(user, payment_id)
    if source is None:
        return _deny(
            "change.payment_not_on_profile",
            f"Payment method '{payment_id}' is not on this customer's profile.",
            payment_id=payment_id,
        )
    if source not in CHANGE_PAYMENT_SOURCES:
        return _deny(
            "change.payment_source_not_allowed",
            f"A change is paid or refunded with a single gift card or credit card. '{payment_id}' "
            f"is a {source.replace('_', ' ')}, which cannot be used for a change.",
            payment_id=payment_id, source=source,
        )

    if cabin_changing:
        if any(clock.has_departed(status) for status in segment_statuses):
            return _deny(
                "change.cabin_after_departure",
                "The cabin cannot be changed once any flight in the reservation has been flown. "
                "Tell the customer this is not possible.",
                statuses=segment_statuses,
            )
        return ALLOW

    if not flights_changing:
        return _deny(
            "change.no_change_requested",
            "This call changes neither the flights nor the cabin. Establish what the customer "
            "actually wants to change before writing.",
        )

    if existing_cabin == "basic_economy":
        return _deny(
            "change.basic_economy_flights",
            "Basic economy flights cannot be modified at all. This is not something a human agent "
            "can do either, so deny it rather than transferring -- though the customer CAN change "
            "cabin if they want to move out of basic economy.",
            cabin=existing_cabin,
        )

    if str(args.get("origin") or reservation.get("origin")) != str(reservation.get("origin")):
        return _deny("change.origin_immutable", "The origin of a reservation cannot be changed.")
    if str(args.get("destination") or reservation.get("destination")) != str(
        reservation.get("destination")
    ):
        return _deny(
            "change.destination_immutable", "The destination of a reservation cannot be changed."
        )

    requested = args.get("flights") or []
    existing = reservation.get("flights") or []
    if len(requested) != len(existing):
        return _deny(
            "change.trip_type_immutable",
            f"The trip type cannot change: this reservation has {len(existing)} segment(s) and the "
            f"change asks for {len(requested)}. A one-way cannot become a round trip or vice versa.",
            existing_segments=len(existing), requested_segments=len(requested),
        )

    for flight in requested:
        key = (flight.get("flight_number"), flight.get("date"))
        if any(
            key == (f.get("flight_number"), f.get("date")) for f in existing
        ):
            continue  # a kept segment; keeps its original price and is not re-checked
        status = new_flight_statuses.get(key)
        if status is not None and clock.has_departed(status):
            return _deny(
                "change.target_flight_departed",
                f"{key[0]} on {key[1]} has already departed (status '{status}'), so the customer "
                f"cannot be moved onto it.",
                flight=key[0], date=key[1], status=status,
            )

    return ALLOW


# --- baggage ---------------------------------------------------------------------------------


def check_baggage_change(args: dict, reservation: dict, user: dict) -> Decision:
    existing_total = int(reservation.get("total_baggages") or 0)
    requested_total = int(args.get("total_baggages") or 0)
    if requested_total < existing_total:
        return _deny(
            "baggage.removal",
            f"Checked bags can be added but never removed. This reservation has "
            f"{existing_total}; the change asks for {requested_total}. Tell the customer bags "
            f"cannot be taken off a booking.",
            existing=existing_total, requested=requested_total,
        )

    payment_id = args.get("payment_id")
    source = payment_source(user, payment_id)
    if requested_total > existing_total:
        if source is None:
            return _deny(
                "baggage.payment_not_on_profile",
                f"Payment method '{payment_id}' is not on this customer's profile.",
                payment_id=payment_id,
            )
        if source not in CHANGE_PAYMENT_SOURCES:
            return _deny(
                "baggage.payment_source_not_allowed",
                f"Extra bags are paid with a single gift card or credit card; '{payment_id}' is a "
                f"{source.replace('_', ' ')}.",
                payment_id=payment_id, source=source,
            )

    passengers = reservation.get("passengers") or []
    expected_free = free_baggage_allowance(
        user.get("membership", "regular"), reservation.get("cabin", ""), len(passengers)
    )
    stated_nonfree = int(args.get("nonfree_baggages") or 0)
    correct_nonfree = max(0, requested_total - expected_free)
    if stated_nonfree != correct_nonfree:
        return _deny(
            "baggage.math",
            f"{expected_free} bag(s) are free for this reservation "
            f"({user.get('membership')}, {reservation.get('cabin')}, {len(passengers)} "
            f"passenger(s)). With {requested_total} total, nonfree_baggages must be "
            f"{correct_nonfree}, not {stated_nonfree}. The fee is "
            f"{correct_nonfree * EXTRA_BAG_FEE}. Re-state this to the customer and re-confirm.",
            free_allowance=expected_free, total=requested_total,
            stated_nonfree=stated_nonfree, correct_nonfree=correct_nonfree,
        )

    return ALLOW


# --- passengers ------------------------------------------------------------------------------


def check_passenger_change(args: dict, reservation: dict) -> Decision:
    existing = len(reservation.get("passengers") or [])
    requested = len(args.get("passengers") or [])
    if requested != existing:
        return _deny(
            "passengers.count_immutable",
            f"The number of passengers on a reservation can never change -- not by this agent and "
            f"not by a human agent. This reservation has {existing} passenger(s) and the change "
            f"asks for {requested}. Deny this; do not offer a transfer, because a human cannot do "
            f"it either. Passenger DETAILS can be corrected, as long as the count stays the same.",
            existing=existing, requested=requested,
        )
    return ALLOW


# --- cancellation ----------------------------------------------------------------------------

INSURANCE_COVERED_REASONS = frozenset({"other"})  # health / weather, per the stakeholder


def check_cancellation(
    args: dict, reservation: dict, segment_statuses: list[str]
) -> Decision:
    if any(clock.has_departed(status) for status in segment_statuses):
        return _transfer(
            "cancel.already_flown",
            "Part of this itinerary has already been flown, so this cancellation is outside what "
            "this agent can do. This is a legitimate need that needs a human -- transfer.",
            statuses=segment_statuses,
        )

    reason = str(args.get("cancellation_reason", "")).strip().lower()
    conditions = {
        "booked_within_24h": clock.booked_within_24h(reservation.get("created_at", "")),
        "airline_cancelled": any(clock.is_cancelled(s) for s in segment_statuses),
        "business_cabin": str(reservation.get("cabin", "")).strip().lower() == "business",
        "insurance_covers_reason": (
            str(reservation.get("insurance", "")).strip().lower() == "yes"
            and reason in INSURANCE_COVERED_REASONS
        ),
    }
    if any(conditions.values()):
        return ALLOW

    return _deny(
        "cancel.no_condition_met",
        "This reservation does not qualify for cancellation: it was not booked within the last 24 "
        "hours, no flight on it was cancelled by the airline, it is not a business booking, and "
        "travel insurance does not cover the stated reason. Tell the customer plainly that it "
        "cannot be cancelled, and why. Do not transfer -- a human agent cannot override this.",
        created_at=reservation.get("created_at"),
        cabin=reservation.get("cabin"),
        insurance=reservation.get("insurance"),
        reason=reason,
        conditions=conditions,
    )


# --- compensation ----------------------------------------------------------------------------


def check_compensation(
    args: dict, user: dict, reservation: dict, segment_statuses: list[str], prior_writes: list[dict]
) -> Decision:
    if not is_compensation_eligible(user, reservation):
        return _deny(
            "compensation.not_eligible",
            f"This customer is not eligible for compensation at all: {user.get('membership')} "
            f"membership, travel insurance '{reservation.get('insurance')}', cabin "
            f"'{reservation.get('cabin')}'. Compensation requires silver or gold membership, or "
            f"travel insurance, or a business cabin. Apologise, explain there is no compensation "
            f"available, and do not offer anything else instead.",
            membership=user.get("membership"),
            insurance=reservation.get("insurance"),
            cabin=reservation.get("cabin"),
        )

    ground = str(args.get("justification_ground", "")).strip().lower()
    passenger_count = len(reservation.get("passengers") or [])

    if ground == "airline_cancelled_flight":
        if not any(clock.is_cancelled(s) for s in segment_statuses):
            return _deny(
                "compensation.ground_not_supported",
                "No flight on this reservation is cancelled, so compensation on that ground is not "
                "available. Confirm the facts with the tools before offering anything.",
                statuses=segment_statuses,
            )
        expected = COMPENSATION_CANCELLED * passenger_count
    elif ground == "delayed_flight_change_or_cancel":
        if not any(clock.is_delayed(s) for s in segment_statuses):
            return _deny(
                "compensation.ground_not_supported",
                "No flight on this reservation is delayed, so compensation on that ground is not "
                "available.",
                statuses=segment_statuses,
            )
        reservation_id = reservation.get("reservation_id")
        changed = any(
            w.get("reservation_id") == reservation_id
            and w.get("tool") in {"update_reservation_flights", "cancel_reservation"}
            for w in prior_writes
        )
        if not changed:
            return _deny(
                "compensation.change_not_yet_made",
                "Compensation for a delayed flight comes AFTER the change or cancellation the "
                "customer wants has actually been made. Make that change first, then issue the "
                "certificate.",
                reservation_id=reservation_id,
            )
        expected = COMPENSATION_DELAYED * passenger_count
    else:
        return _deny(
            "compensation.ground_not_permitted",
            "Compensation is permitted on exactly two grounds: a flight cancelled by the airline, "
            "or a delayed flight where the customer then changes or cancels. There is no "
            "compensation for any other reason.",
            ground=ground,
        )

    amount = args.get("amount")
    if amount != expected:
        return _deny(
            "compensation.wrong_amount",
            f"The amount is fixed by policy: {expected} for {passenger_count} passenger(s) on this "
            f"ground, not {amount}. Re-issue with {expected}.",
            stated=amount, expected=expected, passengers=passenger_count,
        )

    return ALLOW
