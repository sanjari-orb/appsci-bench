"""Booking, baggage-allowance and payment-limit tests.

The baggage table and the exact-total arithmetic are the two places where a silent off-by-one would
produce a tool rejection ("Payment amount does not add up") or an overcharge, so both are covered
exhaustively.
"""
from __future__ import annotations

import pytest

from policy.baggage import FREE_BAGS, baggage_cost, free_bag_allowance, nonfree_bags
from policy.booking import (
    check_cabin,
    check_insurance_decision,
    check_itinerary,
    check_passengers,
    compute_total,
    price_breakdown,
)
from policy.decision import Outcome
from policy.payment import (
    certificate_leftover_warning,
    check_booking_payments,
    check_update_payment,
)
from tests import fixtures as fx


# --- baggage allowance ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "membership, cabin, expected",
    [
        ("regular", "basic_economy", 0), ("regular", "economy", 1), ("regular", "business", 2),
        ("silver", "basic_economy", 1), ("silver", "economy", 2), ("silver", "business", 3),
        ("gold", "basic_economy", 2), ("gold", "economy", 3), ("gold", "business", 4),
    ],
)
def test_free_bag_table_matches_policy(membership, cabin, expected):
    assert free_bag_allowance(membership, cabin, passengers=1) == expected


def test_allowance_is_per_passenger():
    assert free_bag_allowance("gold", "business", passengers=3) == 12
    assert free_bag_allowance("regular", "basic_economy", passengers=4) == 0


def test_table_covers_every_combination():
    assert len(FREE_BAGS) == 9


@pytest.mark.parametrize(
    "membership, cabin, passengers, total, chargeable",
    [
        ("regular", "basic_economy", 1, 1, 1),   # no allowance at all
        ("silver", "economy", 1, 3, 1),          # 2 free, 3rd costs
        ("gold", "business", 2, 8, 0),           # exactly at the allowance
        ("gold", "business", 2, 9, 1),           # one over
        ("regular", "economy", 2, 1, 0),         # under the allowance
    ],
)
def test_chargeable_bags_and_cost(membership, cabin, passengers, total, chargeable):
    assert nonfree_bags(membership, cabin, passengers, total) == chargeable
    assert baggage_cost(membership, cabin, passengers, total) == chargeable * 50


# --- booking totals -------------------------------------------------------------------------------

def test_total_matches_the_tools_formula():
    """sum(segment price x pax) + 30 x pax (insurance) + 50 x nonfree bags."""
    assert compute_total([100, 200], passengers=2, insurance=False, nonfree_baggage_count=0) == 600
    assert compute_total([100, 200], passengers=2, insurance=True, nonfree_baggage_count=0) == 660
    assert compute_total([100, 200], passengers=2, insurance=True, nonfree_baggage_count=3) == 810
    assert compute_total([], passengers=1, insurance=False, nonfree_baggage_count=0) == 0


def test_price_breakdown_derives_chargeable_bags_from_membership():
    """A gold member in economy gets 3 free bags per passenger, so 2 bags cost nothing."""
    breakdown = price_breakdown(
        fx.user(fx.NORA), "economy", [110], passengers=1, insurance=True, total_bags=2
    )
    assert breakdown["nonfree_baggages"] == 0
    assert breakdown["baggage"] == 0
    assert breakdown["insurance"] == 30
    assert breakdown["total"] == 140


def test_price_breakdown_charges_a_regular_member_for_the_first_bag():
    """Priya is a regular member; in basic economy every bag is chargeable."""
    breakdown = price_breakdown(
        fx.user(fx.PRIYA), "basic_economy", [60], passengers=1, insurance=False, total_bags=2
    )
    assert breakdown["nonfree_baggages"] == 2
    assert breakdown["baggage"] == 100
    assert breakdown["total"] == 160


# --- booking validation ---------------------------------------------------------------------------

def test_at_most_five_passengers():
    passenger = {"first_name": "A", "last_name": "B", "dob": "1990-01-01"}
    assert check_passengers([passenger] * 5).outcome is Outcome.ALLOW
    assert check_passengers([passenger] * 6).rule == "book.too_many_passengers"
    assert check_passengers([]).rule == "book.no_passengers"


@pytest.mark.parametrize("missing", ["first_name", "last_name", "dob"])
def test_every_passenger_needs_name_and_dob(missing):
    passenger = {"first_name": "A", "last_name": "B", "dob": "1990-01-01"}
    del passenger[missing]
    decision = check_passengers([passenger])
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "book.passenger_details_missing"


@pytest.mark.parametrize("cabin", ["basic_economy", "economy", "business"])
def test_valid_cabins(cabin):
    assert check_cabin(cabin).outcome is Outcome.ALLOW


def test_invalid_cabin():
    assert check_cabin("first").rule == "book.unknown_cabin"


def test_only_available_flights_are_bookable():
    segments = [{"flight_number": "HAT906", "date": "2024-05-20"}]
    decision = check_itinerary(
        "one_way", "ORD", "DEN", segments, {("HAT906", "2024-05-20"): "cancelled"}
    )
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "book.segment_not_available"


@pytest.mark.parametrize("status", ["cancelled", "delayed", "on time", "landed", "flying"])
def test_every_non_available_status_blocks_booking(status):
    segments = [{"flight_number": "HAT903", "date": "2024-05-18"}]
    decision = check_itinerary(
        "one_way", "ORD", "JFK", segments, {("HAT903", "2024-05-18"): status}
    )
    assert decision.outcome is Outcome.DENY


def test_itinerary_needs_statuses_read_first():
    segments = [{"flight_number": "HAT903", "date": "2024-05-18"}]
    decision = check_itinerary("one_way", "ORD", "JFK", segments, {})
    assert decision.rule == "book.statuses_unverified"


def test_valid_itinerary_passes():
    segments = [{"flight_number": "HAT903", "date": "2024-05-18"}]
    decision = check_itinerary(
        "one_way", "ORD", "JFK", segments, {("HAT903", "2024-05-18"): "available"}
    )
    assert decision.outcome is Outcome.ALLOW


def test_insurance_must_be_asked():
    assert check_insurance_decision(None).rule == "book.insurance_not_asked"
    assert check_insurance_decision(True).outcome is Outcome.ALLOW
    assert check_insurance_decision(False).outcome is Outcome.ALLOW


# --- payment limits -------------------------------------------------------------------------------

def test_payment_must_be_in_the_profile():
    decision = check_booking_payments(
        fx.user(fx.PRIYA), [{"payment_id": "credit_card_99999", "amount": 100}]
    )
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "payment.not_in_profile"


def test_gift_card_limit_is_three():
    """Synthetic profile: the sample users don't hold four gift cards."""
    raw = {
        "user_id": "u",
        "membership": "regular",
        "payment_methods": {
            f"gift_card_{i}": {"id": f"gift_card_{i}", "source": "gift_card", "amount": 500}
            for i in range(4)
        },
    }
    user = fx.User(raw)
    three = [{"payment_id": f"gift_card_{i}", "amount": 10} for i in range(3)]
    assert check_booking_payments(user, three).outcome is Outcome.ALLOW

    four = [{"payment_id": f"gift_card_{i}", "amount": 10} for i in range(4)]
    decision = check_booking_payments(user, four)
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "payment.too_many_gift_card"


def test_one_credit_card_and_one_certificate_max():
    raw = {
        "user_id": "u",
        "membership": "regular",
        "payment_methods": {
            "credit_card_1": {"id": "credit_card_1", "source": "credit_card"},
            "credit_card_2": {"id": "credit_card_2", "source": "credit_card"},
            "certificate_1": {"id": "certificate_1", "source": "certificate", "amount": 100},
            "certificate_2": {"id": "certificate_2", "source": "certificate", "amount": 100},
        },
    }
    user = fx.User(raw)
    two_cards = [{"payment_id": "credit_card_1", "amount": 10},
                 {"payment_id": "credit_card_2", "amount": 10}]
    assert check_booking_payments(user, two_cards).rule == "payment.too_many_credit_card"

    two_certs = [{"payment_id": "certificate_1", "amount": 10},
                 {"payment_id": "certificate_2", "amount": 10}]
    assert check_booking_payments(user, two_certs).rule == "payment.too_many_certificate"


def test_gift_card_balance_must_cover_its_share():
    """Quinn's gift card holds $150."""
    decision = check_booking_payments(
        fx.user(fx.QUINN), [{"payment_id": "gift_card_50000007", "amount": 500}]
    )
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "payment.insufficient_balance"


def test_duplicate_payment_method_is_rejected():
    decision = check_booking_payments(
        fx.user(fx.NORA),
        [{"payment_id": "gift_card_50000002", "amount": 10},
         {"payment_id": "gift_card_50000002", "amount": 10}],
    )
    assert decision.rule == "payment.duplicate_method"


def test_certificate_cannot_fund_an_update():
    """The environment tool rejects this outright, so policy must catch it first."""
    decision = check_update_payment(fx.user(fx.NORA), "certificate_50000003", amount_due=50)
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "payment.certificate_on_update"


def test_update_payment_must_be_on_profile_and_solvent():
    user = fx.user(fx.QUINN)  # gift card $150
    assert check_update_payment(user, "gift_card_50000007", amount_due=100).outcome is Outcome.ALLOW
    assert check_update_payment(user, "gift_card_50000007", amount_due=500).rule == \
        "payment.insufficient_balance"
    assert check_update_payment(user, "nope", amount_due=10).rule == "payment.not_in_profile"
    assert check_update_payment(user, None).rule == "payment.update_missing"


def test_refund_direction_ignores_balance():
    """A refund (negative amount due) never fails a balance check."""
    decision = check_update_payment(fx.user(fx.QUINN), "gift_card_50000007", amount_due=-300)
    assert decision.outcome is Outcome.ALLOW


def test_certificate_leftover_is_warned_about():
    """Nora's certificate is $250; spending $100 forfeits $150."""
    warning = certificate_leftover_warning(
        fx.user(fx.NORA), [{"payment_id": "certificate_50000003", "amount": 100}]
    )
    assert warning is not None
    assert "150" in warning


def test_no_leftover_warning_when_fully_used():
    warning = certificate_leftover_warning(
        fx.user(fx.NORA), [{"payment_id": "certificate_50000003", "amount": 250}]
    )
    assert warning is None
