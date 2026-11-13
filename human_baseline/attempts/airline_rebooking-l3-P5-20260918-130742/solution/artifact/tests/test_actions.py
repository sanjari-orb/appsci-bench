"""Action-layer tests: the write-gating guarantees.

These are the tests that matter most, because they assert what the scoring oracle can see: that no
write tool is ever called unless policy allowed it AND the customer confirmed.

Every test asserts on `fake.writes`, which records only mutating calls.
"""
from __future__ import annotations

import pytest

from actions import ActionService
from policy.cancellation import REASON_AIRLINE_CANCELLED, REASON_CHANGE_OF_PLAN, REASON_OTHER
from policy.compensation import SITUATION_CANCELLED, SITUATION_DELAYED
from policy.decision import Outcome
from tests.fake_env import FakeEnvClient
from tests import fixtures as fx


@pytest.fixture()
def env():
    return FakeEnvClient()


@pytest.fixture()
def service(env):
    return ActionService(env)


# --- the core guarantee ---------------------------------------------------------------------------

def test_allowed_action_does_not_write_until_confirmed(service, env):
    """An ALLOW verdict alone must not mutate anything."""
    result = service.request_cancellation(fx.RAFAEL, "TRN009", REASON_AIRLINE_CANCELLED)
    assert result.outcome is Outcome.ALLOW
    assert result.pending is not None
    assert env.writes == [], "no write may happen before confirmation"

    # Only after an explicit confirmation does the write fire.
    executed = service.execute_confirmed(result.pending)
    assert executed.outcome is Outcome.ALLOW
    assert env.write_tools_called == ["cancel_reservation"]


def test_denied_cancellation_never_writes(service, env):
    """TRN007: economy, no insurance, future flight, change of plan -> denial."""
    result = service.request_cancellation(fx.QUINN, "TRN007", REASON_CHANGE_OF_PLAN)
    assert result.outcome is Outcome.DENY
    assert result.pending is None
    assert env.writes == []


def test_flown_reservation_transfers_and_never_cancels(service, env):
    """TRN004 has a landed segment: transfer, and the cancel tool must not be reached."""
    result = service.request_cancellation(fx.OWEN, "TRN004", REASON_CHANGE_OF_PLAN)
    assert result.outcome is Outcome.TRANSFER
    assert result.transferred is True
    assert env.writes == []
    assert len(env.transfer_summaries) == 1


def test_statuses_are_actually_read_before_deciding(service, env):
    """The agent must verify flight status itself rather than trust the customer."""
    service.request_cancellation(fx.RAFAEL, "TRN009", REASON_AIRLINE_CANCELLED)
    status_calls = [c for c in env.call_log if c["tool"] == "get_flight_status"]
    assert status_calls, "flight status must be read before a cancellation decision"


# --- ownership ------------------------------------------------------------------------------------

def test_cannot_act_on_someone_elses_reservation(service, env):
    """TRN009 belongs to Rafael; Nora must not be able to cancel it."""
    result = service.request_cancellation(fx.NORA, "TRN009", REASON_AIRLINE_CANCELLED)
    assert result.outcome is Outcome.DENY
    assert env.writes == []


def test_unknown_user_is_rejected(service, env):
    result = service.request_cancellation("not_a_user_0000", "TRN009", REASON_AIRLINE_CANCELLED)
    assert result.outcome is Outcome.DENY
    assert env.writes == []


def test_unknown_reservation_is_rejected(service, env):
    result = service.request_cancellation(fx.NORA, "TRN999", REASON_CHANGE_OF_PLAN)
    assert result.outcome is Outcome.DENY
    assert env.writes == []


# --- compensation ---------------------------------------------------------------------------------

def test_compensation_requires_the_customer_to_ask(service, env):
    result = service.request_compensation(
        fx.RAFAEL, "TRN009", SITUATION_CANCELLED, user_asked=False
    )
    assert result.outcome is Outcome.DENY
    assert env.writes == []


def test_real_cancellation_compensation_is_offered_then_sent(service, env):
    """TRN009: gold + genuinely cancelled -> $100, but only sent after confirmation."""
    result = service.request_compensation(
        fx.RAFAEL, "TRN009", SITUATION_CANCELLED, user_asked=True
    )
    assert result.outcome is Outcome.ALLOW
    assert result.facts["amount"] == 100
    assert env.writes == []

    service.execute_confirmed(result.pending)
    assert env.write_tools_called == ["send_certificate"]
    assert env.writes[0]["arguments"]["amount"] == 100


def test_fabricated_cancellation_claim_sends_nothing(service, env):
    """The lying-customer case: TRN003's flights are not cancelled."""
    result = service.request_compensation(
        fx.OWEN, "TRN003", SITUATION_CANCELLED, user_asked=True
    )
    assert result.outcome is Outcome.DENY
    assert "none of them is showing as cancelled" in result.message
    assert env.writes == []


def test_delay_compensation_blocked_until_the_write_happens(service, env):
    """$50/pax requires the change or cancel to have actually been performed first."""
    env.statuses[("HAT903", "2024-05-27")] = "delayed"
    env.statuses[("HAT904", "2024-05-29")] = "delayed"

    blocked = service.request_compensation(
        fx.QUINN, "TRN008", SITUATION_DELAYED, user_asked=True
    )
    assert blocked.outcome is Outcome.DENY
    assert env.writes == []

    # Perform the cancellation first (TRN008 is business + inside 24h, so it is allowed).
    cancel = service.request_cancellation(fx.QUINN, "TRN008", REASON_CHANGE_OF_PLAN)
    assert cancel.outcome is Outcome.ALLOW
    service.execute_confirmed(cancel.pending)

    allowed = service.request_compensation(
        fx.QUINN, "TRN008", SITUATION_DELAYED, user_asked=True
    )
    assert allowed.outcome is Outcome.ALLOW
    assert allowed.facts["amount"] == 100  # $50 x 2 passengers


def test_ineligible_customer_gets_no_compensation_even_for_a_real_cancellation(service, env):
    """Regular member + no insurance + basic economy = the excluded combination."""
    env.reservations["TRN006"]["insurance"] = "no"
    for key in [("HAT906", "2024-05-19"), ("HAT907", "2024-05-23")]:
        env.statuses[key] = "cancelled"

    result = service.request_compensation(
        fx.PRIYA, "TRN006", SITUATION_CANCELLED, user_asked=True
    )
    assert result.outcome is Outcome.DENY
    assert env.writes == []


# --- modification ---------------------------------------------------------------------------------

@pytest.mark.parametrize("reservation_id, user_id", [("TRN002", fx.NORA), ("TRN010", fx.RAFAEL)])
def test_basic_economy_flight_change_is_denied_not_transferred(service, env, reservation_id, user_id):
    result = service.request_flight_change(
        user_id, reservation_id, [{"flight_number": "HAT903", "date": "2024-05-27"}],
        payment_id="gift_card_50000002",
    )
    assert result.outcome is Outcome.DENY
    assert result.transferred is False
    assert env.writes == []


def test_basic_economy_can_still_change_cabin(service, env):
    """TRN010 is basic economy: flights locked, cabin changeable."""
    result = service.request_cabin_change(
        fx.RAFAEL, "TRN010", "economy", payment_id="gift_card_50000010"
    )
    assert result.outcome is Outcome.ALLOW
    assert env.writes == []
    service.execute_confirmed(result.pending)
    assert env.write_tools_called == ["update_reservation_flights"]
    assert env.reservations["TRN010"]["cabin"] == "economy"


def test_cabin_change_denied_after_a_flown_segment(service, env):
    result = service.request_cabin_change(
        fx.OWEN, "TRN004", "business", payment_id="gift_card_50000005"
    )
    assert result.outcome is Outcome.DENY
    assert result.transferred is False
    assert env.writes == []


def test_certificate_cannot_pay_for_a_cabin_change(service, env):
    """Nora holds a certificate; the tool would reject it, so policy must catch it first."""
    result = service.request_cabin_change(
        fx.NORA, "TRN002", "economy", payment_id="certificate_50000003"
    )
    assert result.outcome is Outcome.DENY
    assert env.writes == []


def test_passenger_count_change_is_denied(service, env):
    """TRN005 has 3 passengers; offering 2 must be refused."""
    result = service.request_passenger_update(
        fx.PRIYA,
        "TRN005",
        [
            {"first_name": "A", "last_name": "B", "dob": "1990-01-01"},
            {"first_name": "C", "last_name": "D", "dob": "1991-01-01"},
        ],
    )
    assert result.outcome is Outcome.DENY
    assert env.writes == []


def test_passenger_details_update_is_allowed(service, env):
    passengers = [
        {"first_name": "A", "last_name": "B", "dob": "1990-01-01"},
        {"first_name": "C", "last_name": "D", "dob": "1991-01-01"},
        {"first_name": "E", "last_name": "F", "dob": "1992-01-01"},
    ]
    result = service.request_passenger_update(fx.PRIYA, "TRN005", passengers)
    assert result.outcome is Outcome.ALLOW
    service.execute_confirmed(result.pending)
    assert env.write_tools_called == ["update_reservation_passengers"]


def test_insurance_cannot_be_added_after_booking(service, env):
    result = service.deny_insurance_change(add=True)
    assert result.outcome is Outcome.DENY
    assert env.writes == []


# --- baggage --------------------------------------------------------------------------------------

def test_baggage_removal_is_denied(service, env):
    result = service.request_add_baggage(fx.PRIYA, "TRN005", new_total_bags=1)
    assert result.outcome is Outcome.DENY
    assert env.writes == []


def test_adding_bags_within_free_allowance_costs_nothing(service, env):
    """TRN005: 3 passengers, business, regular member -> 2 free per passenger = 6 free."""
    result = service.request_add_baggage(fx.PRIYA, "TRN005", new_total_bags=6)
    assert result.outcome is Outcome.ALLOW
    assert result.facts["charge"] == 0
    service.execute_confirmed(result.pending)
    assert env.reservations["TRN005"]["total_baggages"] == 6
    assert env.reservations["TRN005"]["nonfree_baggages"] == 0


def test_adding_bags_beyond_allowance_requires_valid_payment(service, env):
    """A 7th bag on TRN005 costs $50 and needs a valid method on the profile."""
    missing = service.request_add_baggage(fx.PRIYA, "TRN005", new_total_bags=7)
    assert missing.outcome is Outcome.DENY  # no payment method supplied

    ok = service.request_add_baggage(
        fx.PRIYA, "TRN005", new_total_bags=7, payment_id="credit_card_50000006"
    )
    assert ok.outcome is Outcome.ALLOW
    assert ok.facts["charge"] == 50


# --- double-write protection ----------------------------------------------------------------------

def test_a_reservation_is_never_cancelled_twice(service, env):
    """cancel_reservation is not idempotent, so the second attempt must fail loudly."""
    first = service.request_cancellation(fx.QUINN, "TRN008", REASON_CHANGE_OF_PLAN)
    service.execute_confirmed(first.pending)
    assert env.write_tools_called == ["cancel_reservation"]

    again = service.request_cancellation(fx.QUINN, "TRN008", REASON_CHANGE_OF_PLAN)
    assert again.outcome is Outcome.DENY  # now reads status=cancelled
    assert env.write_tools_called == ["cancel_reservation"], "must not cancel twice"


# --- booking --------------------------------------------------------------------------------------

def test_booking_requires_an_insurance_answer(service, env):
    result = service.request_booking(
        user_id=fx.NORA,
        flight_type="one_way",
        origin="SFO",
        destination="ORD",
        cabin="economy",
        segments=[{"flight_number": "HAT901", "date": "2024-05-18",
                   "origin": "SFO", "destination": "ORD"}],
        passengers=[{"first_name": "Nora", "last_name": "Castillo", "dob": "1986-03-14"}],
        payments=[{"payment_id": "credit_card_50000001"}],
        insurance=None,
    )
    assert result.outcome is Outcome.DENY
    assert env.writes == []


def test_booking_an_unavailable_flight_is_denied(service, env):
    """HAT906 on 2024-05-20 is cancelled, so it can never be booked."""
    result = service.request_booking(
        user_id=fx.NORA,
        flight_type="one_way",
        origin="ORD",
        destination="DEN",
        cabin="economy",
        segments=[{"flight_number": "HAT906", "date": "2024-05-20",
                   "origin": "ORD", "destination": "DEN"}],
        passengers=[{"first_name": "Nora", "last_name": "Castillo", "dob": "1986-03-14"}],
        payments=[{"payment_id": "credit_card_50000001"}],
        insurance=False,
    )
    assert result.outcome is Outcome.DENY
    assert env.writes == []


def test_booking_more_than_five_passengers_is_denied(service, env):
    passenger = {"first_name": "A", "last_name": "B", "dob": "1990-01-01"}
    result = service.request_booking(
        user_id=fx.NORA,
        flight_type="one_way",
        origin="SFO",
        destination="ORD",
        cabin="economy",
        segments=[{"flight_number": "HAT901", "date": "2024-05-18",
                   "origin": "SFO", "destination": "ORD"}],
        passengers=[passenger] * 6,
        payments=[{"payment_id": "credit_card_50000001"}],
        insurance=False,
    )
    assert result.outcome is Outcome.DENY
    assert env.writes == []


def test_successful_booking_computes_the_exact_total(service, env):
    """Gold member, economy, 1 pax, 2 bags (3 free) + insurance: 110 + 30 = 140."""
    result = service.request_booking(
        user_id=fx.NORA,
        flight_type="one_way",
        origin="SFO",
        destination="ORD",
        cabin="economy",
        segments=[{"flight_number": "HAT901", "date": "2024-05-18",
                   "origin": "SFO", "destination": "ORD"}],
        passengers=[{"first_name": "Nora", "last_name": "Castillo", "dob": "1986-03-14"}],
        payments=[{"payment_id": "credit_card_50000001"}],
        total_bags=2,
        insurance=True,
    )
    assert result.outcome is Outcome.ALLOW, result.message
    assert result.facts["total"] == 140
    assert result.facts["nonfree_baggages"] == 0
    assert env.writes == []

    service.execute_confirmed(result.pending)
    assert env.write_tools_called == ["book_reservation"]
    booked = env.writes[0]["arguments"]
    assert booked["payment_methods"][0]["amount"] == 140
    assert booked["insurance"] == "yes"


def test_booking_with_a_payment_method_not_on_file_is_denied(service, env):
    result = service.request_booking(
        user_id=fx.PRIYA,
        flight_type="one_way",
        origin="SFO",
        destination="ORD",
        cabin="economy",
        segments=[{"flight_number": "HAT901", "date": "2024-05-18",
                   "origin": "SFO", "destination": "ORD"}],
        passengers=[{"first_name": "Priya", "last_name": "Venkat", "dob": "1990-01-01"}],
        payments=[{"payment_id": "gift_card_99999999"}],
        insurance=False,
    )
    assert result.outcome is Outcome.DENY
    assert env.writes == []
