"""Modification policy tests -- flights, cabin, baggage, passengers, insurance.

The stakeholder stressed that these are *denials*, not transfers. Each test asserts the outcome is
DENY (never TRANSFER) so an over-transferring regression is caught.

Fixtures: TRN002 / TRN006 / TRN010 are basic economy; TRN004 has a landed segment.
"""
from __future__ import annotations

import pytest

from policy.baggage import check_add_baggage
from policy.decision import Outcome
from policy.modification import (
    check_cabin_change,
    check_flight_change,
    check_insurance_change,
    check_new_segments_bookable,
    check_passenger_change,
)
from tests import fixtures as fx


# --- flight changes -------------------------------------------------------------------------------

@pytest.mark.parametrize("reservation_id", ["TRN002", "TRN006", "TRN010"])
def test_basic_economy_flights_cannot_be_changed(reservation_id):
    decision = check_flight_change(fx.reservation(reservation_id))
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "modify.basic_economy_flights"


@pytest.mark.parametrize("reservation_id", ["TRN001", "TRN003", "TRN005", "TRN009"])
def test_non_basic_economy_flights_can_be_changed(reservation_id):
    assert check_flight_change(fx.reservation(reservation_id)).outcome is Outcome.ALLOW


def test_origin_destination_and_trip_type_can_never_change():
    reservation = fx.reservation("TRN009")  # ORD -> DEN, one_way, economy
    assert check_flight_change(reservation, new_origin="LAX").rule == "modify.origin_change"
    assert check_flight_change(reservation, new_destination="LAX").rule == "modify.destination_change"
    assert check_flight_change(reservation, new_flight_type="round_trip").rule == \
        "modify.trip_type_change"
    for kwargs in ({"new_origin": "LAX"}, {"new_destination": "LAX"},
                   {"new_flight_type": "round_trip"}):
        assert check_flight_change(reservation, **kwargs).outcome is Outcome.DENY


def test_supplying_the_same_endpoints_is_not_a_change():
    reservation = fx.reservation("TRN009")
    decision = check_flight_change(
        reservation,
        new_origin=reservation.origin,
        new_destination=reservation.destination,
        new_flight_type=reservation.flight_type,
    )
    assert decision.outcome is Outcome.ALLOW


def test_new_segments_must_be_available():
    bad = check_new_segments_bookable({("HAT906", "2024-05-20"): "cancelled"})
    assert bad.outcome is Outcome.DENY
    assert bad.rule == "modify.segment_not_available"

    good = check_new_segments_bookable({("HAT903", "2024-05-27"): "available"})
    assert good.outcome is Outcome.ALLOW


# --- cabin ----------------------------------------------------------------------------------------

def test_basic_economy_may_still_change_cabin():
    """The policy's deliberate oddity: basic economy can't change flights but can change cabin."""
    decision = check_cabin_change(fx.reservation("TRN010"), "economy")
    assert decision.outcome is Outcome.ALLOW
    assert decision.facts["from_cabin"] == "basic_economy"


def test_cabin_cannot_change_once_a_segment_is_flown():
    """TRN004's HAT905 on 2024-05-10 is landed. This is a DENY, not a transfer."""
    decision = check_cabin_change(fx.reservation("TRN004"), "business")
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "modify.cabin_after_flown"


def test_cabin_change_requires_statuses_first():
    decision = check_cabin_change(fx.reservation("TRN009", with_statuses=False), "business")
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "modify.statuses_unverified"


def test_same_cabin_is_not_a_change():
    assert check_cabin_change(fx.reservation("TRN009"), "economy").rule == "modify.cabin_unchanged"


def test_unknown_cabin_is_rejected():
    assert check_cabin_change(fx.reservation("TRN009"), "first_class").rule == "modify.unknown_cabin"


# --- passengers, insurance, baggage ---------------------------------------------------------------

def test_passenger_count_can_never_change():
    """TRN005 has 3 passengers. Adding or removing is a denial, explicitly not a transfer."""
    reservation = fx.reservation("TRN005")
    for requested in (2, 4):
        decision = check_passenger_change(reservation, requested)
        assert decision.outcome is Outcome.DENY
        assert decision.rule == "modify.passenger_count"


def test_passenger_details_can_be_updated():
    decision = check_passenger_change(fx.reservation("TRN005"), 3)
    assert decision.outcome is Outcome.ALLOW


@pytest.mark.parametrize("add", [True, False])
def test_insurance_can_never_be_added_or_removed(add):
    decision = check_insurance_change(add=add)
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "modify.insurance_immutable"


@pytest.mark.parametrize(
    "current, requested, expected_rule",
    [
        (1, 3, "baggage.add_allowed"),
        (0, 1, "baggage.add_allowed"),
        (3, 1, "baggage.remove_forbidden"),
        (2, 0, "baggage.remove_forbidden"),
        (2, 2, "baggage.no_change"),
    ],
)
def test_baggage_is_add_only(current, requested, expected_rule):
    decision = check_add_baggage(current, requested)
    assert decision.rule == expected_rule
    assert decision.allowed is (expected_rule == "baggage.add_allowed")
