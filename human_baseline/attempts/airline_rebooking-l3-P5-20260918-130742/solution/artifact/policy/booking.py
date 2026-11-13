"""Booking validation and exact total pricing.

The environment's `book_reservation` computes the price itself and rejects the call unless the supplied
payment amounts sum to it **exactly**, so the total is computed here with the same formula:

    sum(segment price x passengers) + 30 x passengers (if insurance) + 50 x nonfree bags

The free-bag allowance is *not* computed by the tool -- the agent derives `nonfree_baggages` from
membership and cabin (see `policy.baggage`).
"""
from __future__ import annotations

from typing import Any

from domain import BOOKABLE_STATUS, User
from policy.baggage import EXTRA_BAG_FEE, INSURANCE_FEE_PER_PASSENGER, nonfree_bags
from policy.decision import Decision, allow, deny

MAX_PASSENGERS = 5
VALID_CABINS = ("basic_economy", "economy", "business")
VALID_FLIGHT_TYPES = ("one_way", "round_trip")


def check_passengers(passengers: list[dict[str, Any]]) -> Decision:
    """At most five passengers, each with first name, last name and date of birth."""
    if not passengers:
        return deny("I need the name and date of birth of each passenger to make a booking.",
                    rule="book.no_passengers")
    if len(passengers) > MAX_PASSENGERS:
        return deny(
            f"A single reservation can hold at most {MAX_PASSENGERS} passengers, and this has "
            f"{len(passengers)}.",
            rule="book.too_many_passengers",
            count=len(passengers),
        )
    for index, passenger in enumerate(passengers, start=1):
        missing = [
            field for field in ("first_name", "last_name", "dob") if not passenger.get(field)
        ]
        if missing:
            return deny(
                f"I'm missing some details for passenger {index}: "
                f"{', '.join(m.replace('dob', 'date of birth') for m in missing)}.",
                rule="book.passenger_details_missing",
                passenger_index=index,
                missing=missing,
            )
    return allow(f"{len(passengers)} passenger(s) with complete details.", rule="book.passengers_ok")


def check_cabin(cabin: str) -> Decision:
    """One cabin for the whole reservation; it must be a real cabin class."""
    if cabin not in VALID_CABINS:
        return deny(
            "The cabin has to be basic economy, economy, or business.",
            rule="book.unknown_cabin",
            requested=cabin,
        )
    return allow(f"Cabin {cabin} is valid.", rule="book.cabin_ok")


def check_itinerary(
    flight_type: str,
    origin: str,
    destination: str,
    segments: list[dict[str, Any]],
    statuses: dict[tuple[str, str], str],
) -> Decision:
    """Validate the itinerary shape and that every segment is bookable.

    Args:
        flight_type: ``one_way`` or ``round_trip``.
        origin / destination: the trip endpoints the customer gave.
        segments: ``[{"flight_number": ..., "date": ...}, ...]`` in travel order.
        statuses: status of each segment's dated instance, read from the environment.
    """
    if flight_type not in VALID_FLIGHT_TYPES:
        return deny("I need to know whether this is a one way or a round trip.",
                    rule="book.unknown_flight_type", requested=flight_type)
    if not segments:
        return deny("I need to know which flights you'd like to book.", rule="book.no_segments")
    if not origin or not destination:
        return deny("I need both the origin and the destination for the trip.",
                    rule="book.endpoints_missing")
    if origin == destination:
        return deny("The origin and destination need to be different airports.",
                    rule="book.same_endpoints", origin=origin)

    unverified = [s for s in segments if (s.get("flight_number"), s.get("date")) not in statuses]
    if unverified:
        return deny(
            "I still need to confirm each of those flights is available before booking.",
            rule="book.statuses_unverified",
            unverified=[f"{s.get('flight_number')}|{s.get('date')}" for s in unverified],
        )

    unavailable = {
        (s["flight_number"], s["date"]): statuses[(s["flight_number"], s["date"])]
        for s in segments
        if statuses[(s["flight_number"], s["date"])] != BOOKABLE_STATUS
    }
    if unavailable:
        listed = ", ".join(f"{fn} on {d} ({st})" for (fn, d), st in sorted(unavailable.items()))
        return deny(
            f"Only flights showing as available can be booked, and these aren't: {listed}.",
            rule="book.segment_not_available",
            unavailable={f"{fn}|{d}": st for (fn, d), st in unavailable.items()},
        )

    return allow(f"Itinerary of {len(segments)} segment(s) is bookable.", rule="book.itinerary_ok")


def compute_total(
    segment_prices: list[int],
    passengers: int,
    insurance: bool,
    nonfree_baggage_count: int,
) -> int:
    """The exact total the booking tool will compute. Must match, or the call is rejected."""
    total = sum(segment_prices) * passengers
    if insurance:
        total += INSURANCE_FEE_PER_PASSENGER * passengers
    total += EXTRA_BAG_FEE * nonfree_baggage_count
    return total


def price_breakdown(
    user: User,
    cabin: str,
    segment_prices: list[int],
    passengers: int,
    insurance: bool,
    total_bags: int,
) -> dict[str, Any]:
    """Itemised price for the confirmation message the customer must approve.

    Returns a dict carrying the per-line amounts plus `total` and the derived `nonfree_baggages`, which
    is what the booking tool needs.
    """
    chargeable_bags = nonfree_bags(user.membership, cabin, passengers, total_bags)
    fare = sum(segment_prices) * passengers
    insurance_cost = INSURANCE_FEE_PER_PASSENGER * passengers if insurance else 0
    baggage_cost = EXTRA_BAG_FEE * chargeable_bags
    return {
        "fare": fare,
        "insurance": insurance_cost,
        "baggage": baggage_cost,
        "nonfree_baggages": chargeable_bags,
        "total_baggages": total_bags,
        "total": fare + insurance_cost + baggage_cost,
    }


def check_insurance_decision(insurance: bool | None) -> Decision:
    """The customer must be *asked* about insurance; there is no default."""
    if insurance is None:
        return deny(
            "I need to ask whether you'd like travel insurance before booking -- it's $30 per "
            "passenger.",
            rule="book.insurance_not_asked",
        )
    return allow("Insurance choice recorded.", rule="book.insurance_ok", insurance=insurance)
