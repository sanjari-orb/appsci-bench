"""Modification rules: flights, cabin, baggage, passengers.

The stakeholder was explicit that modification is "more about denials than transfers" -- an attempt to
modify a basic-economy flight, to change origin/destination/trip type, to change passenger count, or to
add insurance is a DENY, never a transfer.

The one transfer-shaped case here is a *cabin* change on a partly-flown itinerary: the policy forbids
it outright, so it is a denial too. Only a cancellation of a flown itinerary transfers.
"""
from __future__ import annotations

from domain import BOOKABLE_STATUS, Reservation
from policy.decision import Decision, allow, deny


def check_flight_change(
    reservation: Reservation,
    new_origin: str | None = None,
    new_destination: str | None = None,
    new_flight_type: str | None = None,
) -> Decision:
    """Decide whether the flights on a reservation may be changed.

    Args:
        reservation: the reservation being modified.
        new_origin / new_destination / new_flight_type: only supplied when the customer asked to change
            them; any of these is an immediate denial.
    """
    if reservation.cancelled:
        return deny("That reservation is cancelled, so its flights can't be changed.",
                    rule="modify.cancelled_reservation")

    if reservation.is_basic_economy:
        return deny(
            "Basic economy flights can't be changed -- that restriction comes with the fare. I can "
            "still look at a cabin change for you if that helps.",
            rule="modify.basic_economy_flights",
            cabin=reservation.cabin,
        )

    if new_origin and new_origin != reservation.origin:
        return deny(
            "The origin of a reservation can never be changed. A new origin means a new booking.",
            rule="modify.origin_change",
            current=reservation.origin,
            requested=new_origin,
        )
    if new_destination and new_destination != reservation.destination:
        return deny(
            "The destination of a reservation can never be changed. A new destination means a new "
            "booking.",
            rule="modify.destination_change",
            current=reservation.destination,
            requested=new_destination,
        )
    if new_flight_type and new_flight_type != reservation.flight_type:
        return deny(
            "The trip type (one way or round trip) can't be changed on an existing reservation.",
            rule="modify.trip_type_change",
            current=reservation.flight_type,
            requested=new_flight_type,
        )

    return allow(
        "The flights on this reservation can be changed.",
        rule="modify.flights_allowed",
        note="Segments kept from the original booking keep their original price.",
    )


def check_new_segments_bookable(statuses: dict[tuple[str, str], str]) -> Decision:
    """Every newly added segment must be an `available` dated instance."""
    unusable = {k: v for k, v in statuses.items() if v != BOOKABLE_STATUS}
    if unusable:
        listed = ", ".join(f"{fn} on {d} ({st})" for (fn, d), st in sorted(unusable.items()))
        return deny(
            f"I can only put you on flights that are available to book, and these aren't: {listed}.",
            rule="modify.segment_not_available",
            unusable={f"{fn}|{d}": st for (fn, d), st in unusable.items()},
        )
    return allow("All new segments are available.", rule="modify.segments_available")


def check_cabin_change(reservation: Reservation, new_cabin: str) -> Decision:
    """Decide whether the cabin may be changed.

    Any cabin -- including basic economy -- may change cabin, but not once a flight has been flown, and
    the new cabin applies to every segment (no per-segment cabin).
    """
    if reservation.cancelled:
        return deny("That reservation is cancelled, so its cabin can't be changed.",
                    rule="modify.cancelled_reservation")

    if new_cabin not in {"basic_economy", "economy", "business"}:
        return deny(
            "The cabin has to be basic economy, economy, or business.",
            rule="modify.unknown_cabin",
            requested=new_cabin,
        )

    if new_cabin == reservation.cabin:
        return deny(
            f"This reservation is already in {new_cabin.replace('_', ' ')}, so there's no cabin change "
            "to make.",
            rule="modify.cabin_unchanged",
            cabin=reservation.cabin,
        )

    if not reservation.statuses_known:
        return deny(
            "I still need to check the status of each flight on this reservation before changing the "
            "cabin.",
            rule="modify.statuses_unverified",
        )

    # Forbidden outright by policy -- a denial, not a transfer.
    if reservation.has_flown_segment:
        return deny(
            "The cabin can't be changed once part of the itinerary has been flown.",
            rule="modify.cabin_after_flown",
            flown_segments=[s.key for s in reservation.segments if s.flown],
        )

    return allow(
        "The cabin can be changed, and it applies to every flight on the reservation.",
        rule="modify.cabin_allowed",
        from_cabin=reservation.cabin,
        to_cabin=new_cabin,
    )


def check_passenger_change(reservation: Reservation, new_passenger_count: int) -> Decision:
    """Passenger *details* may be edited; the passenger *count* can never change.

    Per the stakeholder this is a denial rather than a transfer -- nobody can do it, not even a human.
    """
    if reservation.cancelled:
        return deny("That reservation is cancelled, so its passengers can't be changed.",
                    rule="modify.cancelled_reservation")

    if new_passenger_count != reservation.passenger_count:
        return deny(
            "The number of passengers on a reservation can't be changed -- not by me and not by a "
            "human colleague. I can update the details of the existing passengers, or you could book "
            "a separate reservation.",
            rule="modify.passenger_count",
            current=reservation.passenger_count,
            requested=new_passenger_count,
        )
    return allow("Passenger details can be updated.", rule="modify.passenger_details_allowed")


def check_insurance_change(add: bool = True) -> Decision:
    """Insurance is a book-time decision: it can never be added or removed afterwards."""
    action = "added to" if add else "removed from"
    return deny(
        f"Travel insurance can't be {action} a reservation after booking -- it's decided at the time of "
        "booking and stays as it is.",
        rule="modify.insurance_immutable",
        requested_add=add,
    )
