"""Compensation policy tests.

The two failure modes these guard against are the ones the stakeholder called out:

* the agent inventing compensation nobody asked for (never proactive);
* the agent believing a customer's claim instead of checking flight status.

Fixture combinations used:
  TRN009 -- gold + insurance + a genuinely cancelled segment -> $100 x 1
  TRN003 -- silver, 2 pax, no cancelled segment              -> claim must fail on the facts
  TRN006 -- regular member, basic economy, WITH insurance    -> insurance route needs covered reason
  TRN007 -- regular?  no: silver. Used for the pax-count math.
  TRN005 -- regular + no insurance + business                -> business route still qualifies
"""
from __future__ import annotations

import pytest

from policy.compensation import (
    SITUATION_CANCELLED,
    SITUATION_DELAYED,
    check_compensation,
    check_eligibility,
)
from policy.decision import Outcome
from tests import fixtures as fx


def test_never_offered_unless_the_customer_asks():
    """The first gate: no request, no compensation, whatever the facts say."""
    decision = check_compensation(
        fx.user(fx.RAFAEL), fx.reservation("TRN009"), SITUATION_CANCELLED, user_asked=False
    )
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "comp.not_requested"


def test_cancelled_flight_pays_100_per_passenger():
    """TRN009: gold member, HAT906 on 2024-05-20 really is cancelled, 1 passenger."""
    decision = check_compensation(
        fx.user(fx.RAFAEL), fx.reservation("TRN009"), SITUATION_CANCELLED, user_asked=True
    )
    assert decision.outcome is Outcome.ALLOW
    assert decision.facts["amount"] == 100
    assert decision.facts["passengers"] == 1


def test_amount_scales_with_passenger_count():
    """TRN008: 2 passengers, business cabin. Forced cancelled to isolate the arithmetic."""
    decision = check_compensation(
        fx.user(fx.QUINN),
        fx.reservation_with_status("TRN008", "cancelled"),
        SITUATION_CANCELLED,
        user_asked=True,
    )
    assert decision.outcome is Outcome.ALLOW
    assert decision.facts["amount"] == 200


def test_claimed_cancellation_that_is_not_real_is_refused():
    """The lying-customer case: TRN003's flights are not cancelled, so the claim fails on the facts."""
    decision = check_compensation(
        fx.user(fx.OWEN), fx.reservation("TRN003"), SITUATION_CANCELLED, user_asked=True
    )
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "comp.cancellation_not_real"


def test_claimed_delay_that_is_not_real_is_refused():
    decision = check_compensation(
        fx.user(fx.OWEN),
        fx.reservation("TRN003"),
        SITUATION_DELAYED,
        user_asked=True,
        write_completed=True,
    )
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "comp.delay_not_real"


def test_delay_pays_50_per_passenger_only_after_the_write():
    """Ordering rule: the change/cancel must have actually happened first."""
    reservation = fx.reservation_with_status("TRN003", "delayed")  # silver owner, 2 passengers

    before = check_compensation(
        fx.user(fx.OWEN), reservation, SITUATION_DELAYED, user_asked=True, write_completed=False
    )
    assert before.outcome is Outcome.DENY
    assert before.rule == "comp.delay_write_not_done"

    after = check_compensation(
        fx.user(fx.OWEN), reservation, SITUATION_DELAYED, user_asked=True, write_completed=True
    )
    assert after.outcome is Outcome.ALLOW
    assert after.facts["amount"] == 100  # $50 x 2 passengers


def test_statuses_must_be_read_before_any_compensation_talk():
    decision = check_compensation(
        fx.user(fx.RAFAEL),
        fx.reservation("TRN009", with_statuses=False),
        SITUATION_CANCELLED,
        user_asked=True,
    )
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "comp.statuses_unverified"


@pytest.mark.parametrize("situation", ["lost_baggage", "rude_crew", "bad_food", "", "refund"])
def test_only_two_situations_ever_qualify(situation):
    decision = check_compensation(
        fx.user(fx.RAFAEL), fx.reservation("TRN009"), situation, user_asked=True
    )
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "comp.situation_not_qualifying"


# --- eligibility gate -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "user_id, reservation_id, detail, expected, expected_rule",
    [
        # Silver / gold membership qualifies on its own.
        (fx.RAFAEL, "TRN009", None, Outcome.ALLOW, "comp.eligible_membership"),
        (fx.OWEN, "TRN003", None, Outcome.ALLOW, "comp.eligible_membership"),
        (fx.QUINN, "TRN007", None, Outcome.ALLOW, "comp.eligible_membership"),
        # Regular member in business cabin qualifies on the cabin.
        (fx.PRIYA, "TRN005", None, Outcome.ALLOW, "comp.eligible_business"),
        # Regular member, basic economy, but WITH insurance: needs a covered reason.
        (fx.PRIYA, "TRN006", "I'm ill", Outcome.ALLOW, "comp.eligible_insurance"),
        (fx.PRIYA, "TRN006", "storm", Outcome.ALLOW, "comp.eligible_insurance"),
        (fx.PRIYA, "TRN006", "changed my mind", Outcome.DENY,
         "comp.insurance_reason_not_covered"),
        (fx.PRIYA, "TRN006", None, Outcome.DENY, "comp.insurance_reason_not_covered"),
    ],
)
def test_eligibility_matrix(user_id, reservation_id, detail, expected, expected_rule):
    decision = check_eligibility(fx.user(user_id), fx.reservation(reservation_id), detail)
    assert decision.outcome is expected, decision.reason
    assert decision.rule == expected_rule


def test_regular_no_insurance_economy_gets_nothing():
    """The explicitly excluded combination from the policy.

    Built by forcing TRN006 (regular owner, basic economy) to have no insurance -- the exact
    regular + no-insurance + basic-economy case the policy says gets nothing.
    """
    reservation = fx.mutated_reservation("TRN006", insurance="no")
    decision = check_eligibility(fx.user(fx.PRIYA), reservation, "I'm ill")
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "comp.not_eligible"


def test_excluded_combination_blocks_even_a_real_cancellation():
    """Eligibility is independent of the situation: a real cancellation still pays nothing here."""
    raw = dict(fx.RESERVATIONS["TRN006"])
    raw["insurance"] = "no"
    reservation = fx.Reservation(
        raw, {(f["flight_number"], f["date"]): "cancelled" for f in raw["flights"]}
    )
    decision = check_compensation(
        fx.user(fx.PRIYA), reservation, SITUATION_CANCELLED, user_asked=True
    )
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "comp.not_eligible"
