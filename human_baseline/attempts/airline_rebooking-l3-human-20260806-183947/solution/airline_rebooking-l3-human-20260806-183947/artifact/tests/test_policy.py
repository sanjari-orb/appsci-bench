"""Unit tests for the deterministic policy rules.

These are pure: no network, no LLM, no container, no API key. They are the always-green gate,
and they carry the bulk of the coverage because policy.py is where every rule the tools do not
enforce actually gets enforced.

The pricing expectations are not invented -- they were measured by executing
`update_reservation_flights` against an isolated copy of the real environment, so they encode the
tool's actual arithmetic rather than a reading of it.
"""
from __future__ import annotations

import pytest

import config
import policy
from policy import Context


# --- fixtures ----------------------------------------------------------------------------------
def make_user(membership="regular", payment_methods=None):
    """A minimal user profile; defaults to a regular member with one credit card."""
    return {
        "user_id": "test_user_1",
        "membership": membership,
        "payment_methods": payment_methods if payment_methods is not None else {
            "credit_card_1": {"id": "credit_card_1", "source": "credit_card"},
        },
    }


def make_reservation(cabin="economy", passengers=1, insurance="no", created_at="2024-05-01T10:00:00",
                     flights=None, total_baggages=0, nonfree_baggages=0, origin="SFO",
                     destination="SEA", status=None):
    """A minimal reservation record with one available SFO->SEA segment by default."""
    reservation = {
        "reservation_id": "TEST01",
        "user_id": "test_user_1",
        "cabin": cabin,
        "insurance": insurance,
        "created_at": created_at,
        "origin": origin,
        "destination": destination,
        "flight_type": "one_way",
        "passengers": [{"first_name": "A", "last_name": f"B{i}", "dob": "1990-01-01"}
                       for i in range(passengers)],
        "flights": flights if flights is not None else [
            {"flight_number": "HAT001", "date": "2024-05-20", "price": 100,
             "origin": origin, "destination": destination},
        ],
        "total_baggages": total_baggages,
        "nonfree_baggages": nonfree_baggages,
        "payment_history": [{"payment_id": "credit_card_1", "amount": 100}],
    }
    if status:
        reservation["status"] = status
    return reservation


def available(flight_number="HAT001", date="2024-05-20", origin="SFO", destination="SEA",
              prices=None, seats=None):
    """One bookable flight instance keyed for Context.available_flights."""
    return {(flight_number, date): {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "prices": prices or {"basic_economy": 50, "economy": 150, "business": 400},
        "available_seats": seats or {"basic_economy": 5, "economy": 5, "business": 5},
    }}


# --- the 3x3 baggage allowance table -----------------------------------------------------------
@pytest.mark.parametrize("membership,cabin,expected", [
    ("regular", "basic_economy", 0),
    ("regular", "economy", 1),
    ("regular", "business", 2),
    ("silver", "basic_economy", 1),
    ("silver", "economy", 2),
    ("silver", "business", 3),
    ("gold", "basic_economy", 2),
    ("gold", "economy", 3),
    ("gold", "business", 4),
])
def test_free_bag_allowance_all_nine_cells(membership, cabin, expected):
    """Every membership x cabin combination in policy.md, per passenger."""
    assert policy.free_bag_allowance(membership, cabin, 1) == expected


def test_free_bag_allowance_scales_with_passengers():
    assert policy.free_bag_allowance("gold", "economy", 3) == 9


def test_free_bag_allowance_rejects_unknown_inputs():
    with pytest.raises(ValueError):
        policy.free_bag_allowance("platinum", "economy", 1)


# --- pricing: the three cases measured against the real environment ----------------------------
def test_kept_segments_same_cabin_cost_nothing():
    """Measured: re-submitting 05XIX4's two segments unchanged in the same cabin charged $0.

    Both segments keep their originally-paid price, even though current fares differ -- had they
    repriced at today's economy fare the delta would have been $122.
    """
    reservation = make_reservation(cabin="economy", passengers=2, flights=[
        {"flight_number": "HAT036", "date": "2024-05-20", "price": 124,
         "origin": "SFO", "destination": "SEA"},
        {"flight_number": "HAT298", "date": "2024-05-28", "price": 192,
         "origin": "SEA", "destination": "SFO"},
    ])
    avail = {
        ("HAT036", "2024-05-20"): {"prices": {"economy": 186}, "available_seats": {"economy": 5}},
        ("HAT298", "2024-05-28"): {"prices": {"economy": 191}, "available_seats": {"economy": 5}},
    }
    requested = [{"flight_number": "HAT036", "date": "2024-05-20"},
                 {"flight_number": "HAT298", "date": "2024-05-28"}]
    assert policy.quote_flight_update(reservation, "economy", requested, avail) == 0


def test_cabin_change_reprices_every_segment_at_current_fare():
    """Measured on 23LMN8: 2 passengers, paid 151/segment, business today 340 -> charged 378.

    The naive "fare difference on what was paid" reading gives the same number for a single
    segment, so the multi-segment case below is what actually distinguishes the two formulas.
    """
    reservation = make_reservation(cabin="economy", passengers=2, flights=[
        {"flight_number": "HAT001", "date": "2024-05-20", "price": 151,
         "origin": "SFO", "destination": "SEA"},
    ])
    avail = available(prices={"economy": 160, "business": 340}, seats={"business": 5})
    requested = [{"flight_number": "HAT001", "date": "2024-05-20"}]
    assert policy.quote_flight_update(reservation, "business", requested, avail) == 378


def test_cabin_change_does_not_keep_old_prices_on_multi_segment():
    """A cabin change re-prices *both* segments at today's fare, not just the changed one."""
    reservation = make_reservation(cabin="economy", passengers=1, flights=[
        {"flight_number": "HAT036", "date": "2024-05-20", "price": 100,
         "origin": "SFO", "destination": "SEA"},
        {"flight_number": "HAT298", "date": "2024-05-28", "price": 100,
         "origin": "SEA", "destination": "SFO"},
    ])
    avail = {
        ("HAT036", "2024-05-20"): {"prices": {"business": 500}, "available_seats": {"business": 5}},
        ("HAT298", "2024-05-28"): {"prices": {"business": 500}, "available_seats": {"business": 5}},
    }
    requested = [{"flight_number": "HAT036", "date": "2024-05-20"},
                 {"flight_number": "HAT298", "date": "2024-05-28"}]
    # 500 + 500 - (100 + 100) = 800, not the 400 a per-segment-difference reading would give.
    assert policy.quote_flight_update(reservation, "business", requested, avail) == 800


def test_quote_rejects_unbookable_segment():
    """No honest quote exists for a flight that cannot be booked in the requested cabin."""
    reservation = make_reservation(cabin="economy")
    with pytest.raises(ValueError):
        policy.quote_flight_update(
            reservation, "business",
            [{"flight_number": "HAT001", "date": "2024-05-20"}],
            available(prices={"economy": 150}),  # no business fare
        )


def test_booking_total_includes_insurance_and_extra_bags():
    assert policy.booking_total([100, 200], passengers=2, insurance="yes",
                                nonfree_baggages=3) == 600 + 60 + 150


def test_booking_total_without_extras():
    assert policy.booking_total([100], passengers=1, insurance="no", nonfree_baggages=0) == 100


# --- cancellation ------------------------------------------------------------------------------
def test_cancel_allowed_within_24h():
    reservation = make_reservation(cabin="basic_economy", created_at="2024-05-14T22:33:39")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"})
    assert policy.check("cancel_reservation", {"reservation_id": "TEST01"}, ctx) is None


def test_cancel_allowed_for_business():
    reservation = make_reservation(cabin="business", created_at="2024-05-01T10:00:00")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"})
    assert policy.check("cancel_reservation", {"reservation_id": "TEST01"}, ctx) is None


def test_cancel_allowed_when_airline_cancelled_a_flight():
    reservation = make_reservation(cabin="economy")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "cancelled"})
    assert policy.check("cancel_reservation", {"reservation_id": "TEST01"}, ctx) is None


@pytest.mark.parametrize("status", ["landed", "flying"])
def test_cancel_transfers_when_a_segment_has_flown(status):
    """A flown or in-flight segment means the agent cannot help and must hand off."""
    reservation = make_reservation(cabin="business", created_at="2024-05-14T22:00:00")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): status})
    violation = policy.check("cancel_reservation", {"reservation_id": "TEST01"}, ctx)
    assert violation is not None
    assert violation.code == "FLOWN_SEGMENT"
    assert violation.transfer is True
    assert "HUMAN AGENT" in violation.as_feedback()


def test_cancel_denied_with_no_grounds():
    reservation = make_reservation(cabin="economy", insurance="no")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"})
    violation = policy.check("cancel_reservation", {"reservation_id": "TEST01"}, ctx)
    assert violation is not None
    assert violation.code == "NOT_CANCELLABLE"
    assert violation.transfer is False


def test_cancel_with_insurance_transfers_when_reason_is_uncorroborated():
    """The chosen default: an unverifiable stated reason is handed to a human, not acted on."""
    assert config.TRUST_STATED_REASON is False
    reservation = make_reservation(cabin="economy", insurance="yes")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  cancellation_reason="I am ill")
    violation = policy.check("cancel_reservation", {"reservation_id": "TEST01"}, ctx)
    assert violation is not None
    assert violation.code == "INSURANCE_REASON_UNVERIFIED"
    assert violation.transfer is True


def test_cancel_with_insurance_allowed_when_switch_is_flipped(monkeypatch):
    """Flipping TRUST_STATED_REASON honours a covered reason instead of transferring."""
    monkeypatch.setattr(config, "TRUST_STATED_REASON", True)
    reservation = make_reservation(cabin="economy", insurance="yes")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  cancellation_reason="I am ill and cannot travel")
    assert policy.check("cancel_reservation", {"reservation_id": "TEST01"}, ctx) is None


def test_cancel_with_insurance_still_denied_for_uncovered_reason(monkeypatch):
    """Even when trusted, a change of plan is not a health or weather reason."""
    monkeypatch.setattr(config, "TRUST_STATED_REASON", True)
    reservation = make_reservation(cabin="economy", insurance="yes")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  cancellation_reason="change of plan")
    violation = policy.check("cancel_reservation", {"reservation_id": "TEST01"}, ctx)
    assert violation is not None
    assert violation.code == "INSURANCE_REASON_UNVERIFIED"


def test_cancel_already_cancelled_is_blocked():
    """Cancelling twice would append a second set of negative refund rows."""
    reservation = make_reservation(cabin="business", status="cancelled")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"})
    violation = policy.check("cancel_reservation", {"reservation_id": "TEST01"}, ctx)
    assert violation is not None
    assert violation.code == "ALREADY_CANCELLED"


def test_cancel_unknown_reservation_is_blocked():
    ctx = Context(reservation=None, user=make_user())
    violation = policy.check("cancel_reservation", {"reservation_id": "NOPE01"}, ctx)
    assert violation is not None
    assert violation.code == "UNKNOWN_RESERVATION"


# --- flight and cabin changes -------------------------------------------------------------------
def test_basic_economy_flight_change_denied():
    reservation = make_reservation(cabin="basic_economy")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  available_flights=available("HAT002", "2024-05-22"))
    violation = policy.check("update_reservation_flights", {
        "reservation_id": "TEST01", "cabin": "basic_economy",
        "flights": [{"flight_number": "HAT002", "date": "2024-05-22"}],
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "BASIC_ECONOMY_NOT_MODIFIABLE"


def test_basic_economy_cabin_upgrade_on_same_flights_is_allowed():
    """Policy denies changing basic-economy *flights*, but a cabin change is still permitted."""
    reservation = make_reservation(cabin="basic_economy")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  available_flights=available())
    assert policy.check("update_reservation_flights", {
        "reservation_id": "TEST01", "cabin": "economy",
        "flights": [{"flight_number": "HAT001", "date": "2024-05-20"}],
        "payment_id": "credit_card_1",
    }, ctx) is None


def test_cabin_change_denied_after_a_flight_has_flown():
    reservation = make_reservation(cabin="economy")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "landed"},
                  available_flights=available())
    violation = policy.check("update_reservation_flights", {
        "reservation_id": "TEST01", "cabin": "business",
        "flights": [{"flight_number": "HAT001", "date": "2024-05-20"}],
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "CABIN_CHANGE_AFTER_FLOWN"


def test_origin_change_denied():
    reservation = make_reservation(cabin="economy", origin="SFO", destination="SEA")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  available_flights=available("HAT009", "2024-05-21", origin="LAX",
                                              destination="SEA"))
    violation = policy.check("update_reservation_flights", {
        "reservation_id": "TEST01", "cabin": "economy",
        "flights": [{"flight_number": "HAT009", "date": "2024-05-21"}],
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "ORIGIN_CHANGED"


def test_destination_change_denied():
    reservation = make_reservation(cabin="economy", origin="SFO", destination="SEA")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  available_flights=available("HAT009", "2024-05-21", origin="SFO",
                                              destination="JFK"))
    violation = policy.check("update_reservation_flights", {
        "reservation_id": "TEST01", "cabin": "economy",
        "flights": [{"flight_number": "HAT009", "date": "2024-05-21"}],
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "DESTINATION_CHANGED"


def test_trip_type_change_denied():
    """Turning a one-way into a two-segment trip changes the trip type."""
    reservation = make_reservation(cabin="economy")
    avail = {}
    avail.update(available("HAT001", "2024-05-20"))
    avail.update(available("HAT002", "2024-05-27", origin="SEA", destination="SEA"))
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  available_flights=avail)
    violation = policy.check("update_reservation_flights", {
        "reservation_id": "TEST01", "cabin": "economy",
        "flights": [{"flight_number": "HAT001", "date": "2024-05-20"},
                    {"flight_number": "HAT002", "date": "2024-05-27"}],
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "TRIP_TYPE_CHANGED"


def test_change_denied_when_new_flight_not_bookable():
    reservation = make_reservation(cabin="economy")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  available_flights={})
    violation = policy.check("update_reservation_flights", {
        "reservation_id": "TEST01", "cabin": "economy",
        "flights": [{"flight_number": "HAT999", "date": "2024-05-22"}],
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "NOT_BOOKABLE"


def test_change_denied_with_certificate_payment():
    user = make_user(payment_methods={
        "certificate_1": {"id": "certificate_1", "source": "certificate", "amount": 500},
    })
    reservation = make_reservation(cabin="economy")
    ctx = Context(reservation=reservation, user=user,
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  available_flights=available())
    violation = policy.check("update_reservation_flights", {
        "reservation_id": "TEST01", "cabin": "business",
        "flights": [{"flight_number": "HAT001", "date": "2024-05-20"}],
        "payment_id": "certificate_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "CERTIFICATE_NOT_ALLOWED"


def test_change_denied_with_payment_not_in_profile():
    reservation = make_reservation(cabin="economy")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  available_flights=available())
    violation = policy.check("update_reservation_flights", {
        "reservation_id": "TEST01", "cabin": "business",
        "flights": [{"flight_number": "HAT001", "date": "2024-05-20"}],
        "payment_id": "credit_card_elsewhere",
    }, ctx)
    assert violation is not None
    assert violation.code == "PAYMENT_NOT_IN_PROFILE"


def test_same_flights_same_cabin_change_is_allowed():
    reservation = make_reservation(cabin="economy")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  available_flights=available())
    assert policy.check("update_reservation_flights", {
        "reservation_id": "TEST01", "cabin": "economy",
        "flights": [{"flight_number": "HAT001", "date": "2024-05-20"}],
        "payment_id": "credit_card_1",
    }, ctx) is None


# --- baggage ------------------------------------------------------------------------------------
def test_baggage_decrease_denied():
    reservation = make_reservation(cabin="economy", total_baggages=2, nonfree_baggages=1)
    ctx = Context(reservation=reservation, user=make_user())
    violation = policy.check("update_reservation_baggages", {
        "reservation_id": "TEST01", "total_baggages": 1, "nonfree_baggages": 0,
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "BAGGAGE_DECREASE"


def test_baggage_within_free_allowance_charges_nothing():
    """A gold member in economy gets 3 free bags per passenger, so 3 bags cost nothing."""
    reservation = make_reservation(cabin="economy", total_baggages=0, nonfree_baggages=0)
    ctx = Context(reservation=reservation, user=make_user("gold"))
    assert policy.check("update_reservation_baggages", {
        "reservation_id": "TEST01", "total_baggages": 3, "nonfree_baggages": 0,
        "payment_id": "credit_card_1",
    }, ctx) is None


def test_baggage_overcharge_denied():
    """Charging for a bag that falls inside the free allowance is a communication error."""
    reservation = make_reservation(cabin="economy", total_baggages=0, nonfree_baggages=0)
    ctx = Context(reservation=reservation, user=make_user("gold"))
    violation = policy.check("update_reservation_baggages", {
        "reservation_id": "TEST01", "total_baggages": 3, "nonfree_baggages": 1,
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "BAGGAGE_ALLOWANCE"
    assert "3 checked bag(s) are free" in violation.message


def test_baggage_undercharge_denied():
    """A regular member in economy gets 1 free bag; a 3rd bag must be charged."""
    reservation = make_reservation(cabin="economy", total_baggages=0, nonfree_baggages=0)
    ctx = Context(reservation=reservation, user=make_user("regular"))
    violation = policy.check("update_reservation_baggages", {
        "reservation_id": "TEST01", "total_baggages": 3, "nonfree_baggages": 0,
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "BAGGAGE_ALLOWANCE"


def test_baggage_charge_requires_valid_payment():
    reservation = make_reservation(cabin="economy", total_baggages=0, nonfree_baggages=0)
    ctx = Context(reservation=reservation, user=make_user("regular"))
    violation = policy.check("update_reservation_baggages", {
        "reservation_id": "TEST01", "total_baggages": 2, "nonfree_baggages": 1,
        "payment_id": "not_mine",
    }, ctx)
    assert violation is not None
    assert violation.code == "PAYMENT_NOT_IN_PROFILE"


# --- passengers ---------------------------------------------------------------------------------
def test_passenger_count_change_is_refused_not_transferred():
    """Policy says even a human agent cannot do this, so it must not become a handoff."""
    reservation = make_reservation(passengers=1)
    ctx = Context(reservation=reservation, user=make_user())
    violation = policy.check("update_reservation_passengers", {
        "reservation_id": "TEST01",
        "passengers": [{"first_name": "A", "last_name": "B", "dob": "1990-01-01"},
                       {"first_name": "C", "last_name": "D", "dob": "1991-01-01"}],
    }, ctx)
    assert violation is not None
    assert violation.code == "PASSENGER_COUNT_IMMUTABLE"
    assert violation.transfer is False
    assert "HUMAN AGENT" not in violation.as_feedback()


def test_passenger_detail_change_is_allowed():
    reservation = make_reservation(passengers=1)
    ctx = Context(reservation=reservation, user=make_user())
    assert policy.check("update_reservation_passengers", {
        "reservation_id": "TEST01",
        "passengers": [{"first_name": "New", "last_name": "Name", "dob": "1990-01-01"}],
    }, ctx) is None


# --- already-cancelled reservations --------------------------------------------------------------
@pytest.mark.parametrize("tool,args", [
    ("update_reservation_flights", {"cabin": "economy",
     "flights": [{"flight_number": "HAT001", "date": "2024-05-20"}], "payment_id": "credit_card_1"}),
    ("update_reservation_baggages", {"total_baggages": 1, "nonfree_baggages": 0,
     "payment_id": "credit_card_1"}),
    ("update_reservation_passengers", {"passengers": [
        {"first_name": "New", "last_name": "Name", "dob": "1990-01-01"}]}),
])
def test_cancelled_reservation_blocks_every_modification(tool, args):
    reservation = make_reservation(status="cancelled")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  available_flights=available())
    violation = policy.check(tool, {"reservation_id": "TEST01", **args}, ctx)
    assert violation is not None
    assert violation.code == "RESERVATION_CANCELLED"


# --- duplicate segments and baggage sanity on modify-flights --------------------------------------
def test_modify_flights_duplicate_segment_denied():
    reservation = make_reservation(cabin="economy")
    ctx = Context(reservation=reservation, user=make_user(),
                  flight_statuses={("HAT001", "2024-05-20"): "available"},
                  available_flights=available())
    violation = policy.check("update_reservation_flights", {
        "reservation_id": "TEST01", "cabin": "economy",
        "flights": [{"flight_number": "HAT001", "date": "2024-05-20"},
                    {"flight_number": "HAT001", "date": "2024-05-20"}],
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "DUPLICATE_SEGMENT"


@pytest.mark.parametrize("total,nonfree", [(-1, 0), (2, -1)])
def test_baggage_negative_counts_denied(total, nonfree):
    reservation = make_reservation(cabin="economy", total_baggages=0, nonfree_baggages=0)
    ctx = Context(reservation=reservation, user=make_user())
    violation = policy.check("update_reservation_baggages", {
        "reservation_id": "TEST01", "total_baggages": total, "nonfree_baggages": nonfree,
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "BAGGAGE_COUNT_INVALID"


def test_baggage_nonfree_exceeding_total_denied():
    reservation = make_reservation(cabin="economy", total_baggages=0, nonfree_baggages=0)
    ctx = Context(reservation=reservation, user=make_user())
    violation = policy.check("update_reservation_baggages", {
        "reservation_id": "TEST01", "total_baggages": 1, "nonfree_baggages": 2,
        "payment_id": "credit_card_1",
    }, ctx)
    assert violation is not None
    assert violation.code == "BAGGAGE_COUNT_INVALID"


# --- booking ------------------------------------------------------------------------------------
def _booking_args(passengers=1, cabin="economy", insurance="no", total_baggages=0,
                  nonfree_baggages=0, payments=None, amount=None):
    """Booking arguments that are self-consistent unless a test deliberately breaks them."""
    price = 150
    total = amount if amount is not None else policy.booking_total(
        [price], passengers, insurance, nonfree_baggages)
    return {
        "user_id": "test_user_1", "origin": "SFO", "destination": "SEA",
        "flight_type": "one_way", "cabin": cabin,
        "flights": [{"flight_number": "HAT001", "date": "2024-05-20"}],
        "passengers": [{"first_name": "A", "last_name": f"B{i}", "dob": "1990-01-01"}
                       for i in range(passengers)],
        "payment_methods": payments if payments is not None else [
            {"payment_id": "credit_card_1", "amount": total}],
        "total_baggages": total_baggages, "nonfree_baggages": nonfree_baggages,
        "insurance": insurance,
    }


def test_booking_six_passengers_denied():
    ctx = Context(user=make_user(), available_flights=available())
    violation = policy.check("book_reservation", _booking_args(passengers=6), ctx)
    assert violation is not None
    assert violation.code == "TOO_MANY_PASSENGERS"


def test_booking_five_passengers_allowed():
    ctx = Context(user=make_user(), available_flights=available(seats={"economy": 9}))
    assert policy.check("book_reservation", _booking_args(passengers=5), ctx) is None


def test_booking_denied_when_not_enough_seats():
    ctx = Context(user=make_user(), available_flights=available(seats={"economy": 2}))
    violation = policy.check("book_reservation", _booking_args(passengers=3), ctx)
    assert violation is not None
    assert violation.code == "NOT_ENOUGH_SEATS"


def test_booking_denied_when_flight_not_available():
    ctx = Context(user=make_user(), available_flights={})
    violation = policy.check("book_reservation", _booking_args(), ctx)
    assert violation is not None
    assert violation.code == "NOT_BOOKABLE"


def test_booking_denied_with_too_many_gift_cards():
    methods = {f"gift_card_{i}": {"id": f"gift_card_{i}", "source": "gift_card", "amount": 1000}
               for i in range(4)}
    ctx = Context(user=make_user(payment_methods=methods), available_flights=available())
    payments = [{"payment_id": f"gift_card_{i}", "amount": 37.5} for i in range(4)]
    violation = policy.check("book_reservation", _booking_args(payments=payments), ctx)
    assert violation is not None
    assert violation.code == "PAYMENT_COMPOSITION"


def test_booking_denied_with_two_credit_cards():
    methods = {f"credit_card_{i}": {"id": f"credit_card_{i}", "source": "credit_card"}
               for i in range(2)}
    ctx = Context(user=make_user(payment_methods=methods), available_flights=available())
    payments = [{"payment_id": "credit_card_0", "amount": 75},
                {"payment_id": "credit_card_1", "amount": 75}]
    violation = policy.check("book_reservation", _booking_args(payments=payments), ctx)
    assert violation is not None
    assert violation.code == "PAYMENT_COMPOSITION"


def test_booking_denied_with_unknown_payment_method():
    ctx = Context(user=make_user(), available_flights=available())
    payments = [{"payment_id": "credit_card_stolen", "amount": 150}]
    violation = policy.check("book_reservation", _booking_args(payments=payments), ctx)
    assert violation is not None
    assert violation.code == "PAYMENT_NOT_IN_PROFILE"


def test_booking_denied_when_total_does_not_match():
    ctx = Context(user=make_user(), available_flights=available())
    violation = policy.check("book_reservation", _booking_args(amount=10), ctx)
    assert violation is not None
    assert violation.code == "TOTAL_MISMATCH"


def test_booking_total_includes_insurance_and_bags_end_to_end():
    """A silver member in economy gets 2 free bags, so a 3rd is charged at $50."""
    ctx = Context(user=make_user("silver"), available_flights=available())
    args = _booking_args(insurance="yes", total_baggages=3, nonfree_baggages=1)
    assert policy.check("book_reservation", args, ctx) is None
    assert args["payment_methods"][0]["amount"] == 150 + 30 + 50


def test_booking_denied_when_baggage_allowance_miscomputed():
    ctx = Context(user=make_user("silver"), available_flights=available())
    args = _booking_args(total_baggages=3, nonfree_baggages=3)
    violation = policy.check("book_reservation", args, ctx)
    assert violation is not None
    assert violation.code == "BAGGAGE_ALLOWANCE"


def test_booking_denied_with_no_flights():
    ctx = Context(user=make_user(), available_flights=available())
    args = _booking_args()
    args["flights"] = []
    violation = policy.check("book_reservation", args, ctx)
    assert violation is not None
    assert violation.code == "NO_FLIGHTS"


def test_booking_denied_with_duplicate_segment():
    ctx = Context(user=make_user(), available_flights=available())
    args = _booking_args()
    args["flights"] = [{"flight_number": "HAT001", "date": "2024-05-20"},
                       {"flight_number": "HAT001", "date": "2024-05-20"}]
    violation = policy.check("book_reservation", args, ctx)
    assert violation is not None
    assert violation.code == "DUPLICATE_SEGMENT"


def test_booking_denied_when_one_way_has_two_segments():
    avail = available()
    avail[("HAT002", "2024-05-20")] = {**avail[("HAT001", "2024-05-20")], "flight_number": "HAT002"}
    ctx = Context(user=make_user(), available_flights=avail)
    args = _booking_args()
    args["flights"] = [{"flight_number": "HAT001", "date": "2024-05-20"},
                       {"flight_number": "HAT002", "date": "2024-05-20"}]
    violation = policy.check("book_reservation", args, ctx)
    assert violation is not None
    assert violation.code == "SEGMENT_COUNT_MISMATCH"


def test_booking_denied_when_round_trip_has_one_segment():
    ctx = Context(user=make_user(), available_flights=available())
    args = _booking_args()
    args["flight_type"] = "round_trip"
    violation = policy.check("book_reservation", args, ctx)
    assert violation is not None
    assert violation.code == "SEGMENT_COUNT_MISMATCH"


def test_booking_denied_when_flight_does_not_match_stated_route():
    """The flight found under a reversed search is genuinely SEA->SFO, not SFO->SEA as booked."""
    avail = available(origin="SEA", destination="SFO")
    ctx = Context(user=make_user(), available_flights=avail)
    args = _booking_args()
    violation = policy.check("book_reservation", args, ctx)
    assert violation is not None
    assert violation.code == "ROUTE_MISMATCH"


def test_booking_round_trip_route_allowed():
    avail = available(origin="SFO", destination="SEA")
    avail[("HAT002", "2024-05-25")] = {**avail[("HAT001", "2024-05-20")],
                                       "flight_number": "HAT002",
                                       "origin": "SEA", "destination": "SFO"}
    ctx = Context(user=make_user(), available_flights=avail)
    args = _booking_args()
    args["flight_type"] = "round_trip"
    args["flights"] = [{"flight_number": "HAT001", "date": "2024-05-20"},
                       {"flight_number": "HAT002", "date": "2024-05-25"}]
    args["payment_methods"][0]["amount"] = policy.booking_total([150, 150], 1, "no", 0)
    assert policy.check("book_reservation", args, ctx) is None


def test_booking_denied_with_negative_baggage_count():
    ctx = Context(user=make_user(), available_flights=available())
    args = _booking_args()
    args["total_baggages"] = -1
    violation = policy.check("book_reservation", args, ctx)
    assert violation is not None
    assert violation.code == "BAGGAGE_COUNT_INVALID"


# --- compensation -------------------------------------------------------------------------------
def test_compensation_denied_for_regular_economy_without_insurance():
    reservation = make_reservation(cabin="economy", insurance="no")
    ctx = Context(reservation=reservation, user=make_user("regular"),
                  complaint="cancelled_flight")
    violation = policy.check("send_certificate", {"user_id": "test_user_1", "amount": 100}, ctx)
    assert violation is not None
    assert violation.code == "COMPENSATION_NOT_ELIGIBLE"


@pytest.mark.parametrize("membership,cabin,insurance", [
    ("silver", "economy", "no"),
    ("gold", "economy", "no"),
    ("regular", "business", "no"),
    ("regular", "economy", "yes"),
])
def test_compensation_eligibility_grounds(membership, cabin, insurance):
    """Silver/gold, business cabin, or travel insurance each make a customer eligible."""
    reservation = make_reservation(cabin=cabin, insurance=insurance)
    assert policy.is_compensation_eligible(make_user(membership), reservation) is True


def test_compensation_cancelled_flight_amount_per_passenger():
    reservation = make_reservation(cabin="economy", insurance="yes", passengers=3)
    ctx = Context(reservation=reservation, user=make_user("gold"), complaint="cancelled_flight")
    assert policy.check("send_certificate", {"user_id": "test_user_1", "amount": 300}, ctx) is None


def test_compensation_wrong_amount_denied():
    reservation = make_reservation(cabin="economy", insurance="yes", passengers=3)
    ctx = Context(reservation=reservation, user=make_user("gold"), complaint="cancelled_flight")
    violation = policy.check("send_certificate", {"user_id": "test_user_1", "amount": 500}, ctx)
    assert violation is not None
    assert violation.code == "COMPENSATION_AMOUNT"


def test_compensation_delay_requires_the_change_to_have_happened():
    reservation = make_reservation(cabin="business", passengers=2)
    ctx = Context(reservation=reservation, user=make_user("gold"), complaint="delayed_flight",
                  reservation_resolved=False)
    violation = policy.check("send_certificate", {"user_id": "test_user_1", "amount": 100}, ctx)
    assert violation is not None
    assert violation.code == "COMPENSATION_PREMATURE"


def test_compensation_delay_allowed_after_the_change():
    reservation = make_reservation(cabin="business", passengers=2)
    ctx = Context(reservation=reservation, user=make_user("gold"), complaint="delayed_flight",
                  reservation_resolved=True)
    assert policy.check("send_certificate", {"user_id": "test_user_1", "amount": 100}, ctx) is None


def test_compensation_denied_for_other_reasons():
    reservation = make_reservation(cabin="business")
    ctx = Context(reservation=reservation, user=make_user("gold"), complaint="rude_crew")
    violation = policy.check("send_certificate", {"user_id": "test_user_1", "amount": 100}, ctx)
    assert violation is not None
    assert violation.code == "COMPENSATION_REASON"


def test_compensation_requires_the_facts_to_be_confirmed():
    """No reservation read means the facts were never confirmed, so no certificate."""
    ctx = Context(reservation=None, user=make_user("gold"), complaint="cancelled_flight")
    violation = policy.check("send_certificate", {"user_id": "test_user_1", "amount": 100}, ctx)
    assert violation is not None
    assert violation.code == "FACTS_NOT_CONFIRMED"


def test_compensation_amount_helper():
    assert policy.compensation_amount("cancelled_flight", 2) == 200
    assert policy.compensation_amount("delayed_flight", 2) == 100
    assert policy.compensation_amount(None, 2) is None


# --- dispatch -----------------------------------------------------------------------------------
def test_read_tools_are_never_blocked():
    """Reads cannot damage the records and the agent needs them freely to establish facts."""
    for name in ["get_user_details", "get_reservation_details", "search_direct_flight",
                 "list_all_airports", "get_flight_status", "calculate",
                 "transfer_to_human_agents"]:
        assert policy.check(name, {}, Context()) is None


def test_every_write_tool_has_a_check():
    """A new write tool must not slip through the guard unnoticed."""
    assert set(policy.WRITE_TOOLS) == set(policy._CHECKS)
