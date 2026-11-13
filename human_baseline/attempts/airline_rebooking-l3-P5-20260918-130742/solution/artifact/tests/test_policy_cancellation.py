"""Cancellation policy tests, driven by the real fixture records.

The cases below are the environment's deliberate policy edges:

* TRN004 -- segment landed  -> TRANSFER (flown wins)
* TRN009 -- airline cancelled -> ALLOW
* TRN008 -- created 09:12 on the clock date, business cabin -> ALLOW twice over
* TRN007 -- economy, no insurance, future -> DENY
* TRN006 -- basic economy WITH insurance -> ALLOW only for health/weather
"""
from __future__ import annotations

import pytest

from policy.cancellation import (
    REASON_AIRLINE_CANCELLED,
    REASON_CHANGE_OF_PLAN,
    REASON_OTHER,
    check_cancellation,
    reason_is_insurance_covered,
    refund_summary,
)
from policy.decision import Outcome
from tests import fixtures as fx


@pytest.mark.parametrize(
    "reservation_id, reason, detail, expected_outcome, expected_rule",
    [
        # Flown segment beats every other condition -> transfer, never a cancel.
        ("TRN004", REASON_CHANGE_OF_PLAN, None, Outcome.TRANSFER, "cancel.flown_transfer"),
        ("TRN004", REASON_AIRLINE_CANCELLED, None, Outcome.TRANSFER, "cancel.flown_transfer"),
        # Airline cancelled a segment -> allowed regardless of the customer's stated reason.
        ("TRN009", REASON_CHANGE_OF_PLAN, None, Outcome.ALLOW, "cancel.airline_cancelled"),
        ("TRN009", REASON_AIRLINE_CANCELLED, None, Outcome.ALLOW, "cancel.airline_cancelled"),
        # Booked at 09:12 against a 15:00 clock, same day -> inside the 24-hour window.
        ("TRN008", REASON_CHANGE_OF_PLAN, None, Outcome.ALLOW, "cancel.within_24h"),
        # Business cabin, booked outside 24h, no insurance -> business path.
        ("TRN005", REASON_CHANGE_OF_PLAN, None, Outcome.ALLOW, "cancel.business_cabin"),
        ("TRN001", REASON_CHANGE_OF_PLAN, None, Outcome.ALLOW, "cancel.business_cabin"),
        # Economy, no insurance, outside 24h, nothing cancelled -> flat denial.
        ("TRN007", REASON_CHANGE_OF_PLAN, None, Outcome.DENY, "cancel.no_qualifying_condition"),
        ("TRN010", REASON_CHANGE_OF_PLAN, None, Outcome.DENY, "cancel.no_qualifying_condition"),
        # Insurance path: covered reason allows, uncovered reason denies.
        ("TRN006", REASON_OTHER, "I'm ill and can't travel", Outcome.ALLOW, "cancel.insurance_covered"),
        ("TRN006", REASON_OTHER, "a storm is coming", Outcome.ALLOW, "cancel.insurance_covered"),
        ("TRN006", REASON_CHANGE_OF_PLAN, "changed my mind", Outcome.DENY,
         "cancel.insurance_reason_not_covered"),
        ("TRN003", REASON_CHANGE_OF_PLAN, "changed my mind", Outcome.DENY,
         "cancel.insurance_reason_not_covered"),
    ],
)
def test_cancellation_matrix(reservation_id, reason, detail, expected_outcome, expected_rule):
    decision = check_cancellation(fx.reservation(reservation_id), reason, detail)
    assert decision.outcome is expected_outcome, f"{reservation_id}: {decision.reason}"
    assert decision.rule == expected_rule


def test_refuses_to_decide_before_statuses_are_read():
    """The verification guard: no verdict until every segment status has been read."""
    decision = check_cancellation(
        fx.reservation("TRN009", with_statuses=False), REASON_AIRLINE_CANCELLED
    )
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "cancel.statuses_unverified"


def test_already_cancelled_is_not_cancelled_again():
    """cancel_reservation is not idempotent, so a second cancel must never be attempted."""
    decision = check_cancellation(
        fx.mutated_reservation("TRN009", status="cancelled"), REASON_AIRLINE_CANCELLED
    )
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "cancel.already_cancelled"


def test_unknown_reason_is_rejected():
    decision = check_cancellation(fx.reservation("TRN008"), "because I feel like it")
    assert decision.outcome is Outcome.DENY
    assert decision.rule == "cancel.reason_missing"


@pytest.mark.parametrize(
    "detail, covered",
    [
        ("I am sick", True),
        ("medical emergency", True),
        ("hospital appointment", True),
        ("there is a hurricane", True),
        ("snow storm closed the road", True),
        ("I changed my mind", False),
        ("work meeting moved", False),
        ("", False),
        (None, False),
    ],
)
def test_insurance_covered_reasons(detail, covered):
    assert reason_is_insurance_covered(detail) is covered


def test_certificate_leftover_is_flagged_in_refund_summary():
    """TRN008 is the certificate-paid reservation: the leftover must never be promised back."""
    summary = refund_summary(fx.reservation("TRN008"))
    assert "5 to 7 business days" in summary
    assert "isn't refundable" in summary


def test_plain_refund_summary_has_no_certificate_caveat():
    summary = refund_summary(fx.reservation("TRN009"))
    assert "5 to 7 business days" in summary
    assert "certificate" not in summary.lower()
