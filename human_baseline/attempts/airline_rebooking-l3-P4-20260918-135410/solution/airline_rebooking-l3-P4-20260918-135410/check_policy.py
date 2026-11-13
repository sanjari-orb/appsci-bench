#!/usr/bin/env python
"""Local sanity check of the policy predicates. Not a test suite -- a fast confidence pass.

Runs policy.py against records copied from the live build store (plus a couple of synthetic ones for
cases the small build store does not contain) and asserts the verdict each elicited rule requires.
No network, no model, no containers: this is here to catch a predicate that is simply wrong before
spending a deploy cycle on it.

Run:  python3 check_policy.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "artifact")

import policy  # noqa: E402

GOLD = {"membership": "gold", "payment_methods": {
    "credit_card_50000001": {"id": "credit_card_50000001", "source": "credit_card"},
    "gift_card_50000002": {"id": "gift_card_50000002", "source": "gift_card", "amount": 500},
    "certificate_50000003": {"id": "certificate_50000003", "source": "certificate", "amount": 250},
}}
SILVER = {"membership": "silver", "payment_methods": {
    "credit_card_1": {"id": "credit_card_1", "source": "credit_card"},
}}
REGULAR = {"membership": "regular", "payment_methods": {
    "credit_card_2": {"id": "credit_card_2", "source": "credit_card"},
}}

# Copied from the live build store.
TRN002 = {"reservation_id": "TRN002", "user_id": "nora_castillo_9412", "cabin": "basic_economy",
          "flight_type": "one_way", "created_at": "2024-05-06T18:03:00", "insurance": "no",
          "origin": "SFO", "destination": "ORD", "total_baggages": 0, "nonfree_baggages": 0,
          "passengers": [{"first_name": "Nora", "last_name": "Castillo", "dob": "1986-03-14"}],
          "flights": [{"flight_number": "HAT903", "date": "2024-05-22"}]}
TRN003 = {"reservation_id": "TRN003", "cabin": "economy", "created_at": "2024-05-03T08:47:00",
          "insurance": "yes", "total_baggages": 3, "nonfree_baggages": 0,
          "passengers": [{}, {}],
          "flights": [{"flight_number": "HAT908", "date": "2024-05-21"},
                      {"flight_number": "HAT909", "date": "2024-05-25"}]}
TRN005 = {"reservation_id": "TRN005", "cabin": "business", "created_at": "2024-05-05T21:35:00",
          "insurance": "no", "total_baggages": 4, "nonfree_baggages": 0,
          "passengers": [{}, {}, {}], "flights": [{"flight_number": "HAT901", "date": "2024-05-18"}]}
TRN007 = {"reservation_id": "TRN007", "cabin": "economy", "created_at": "2024-05-08T16:41:00",
          "insurance": "no", "total_baggages": 4, "nonfree_baggages": 0,
          "passengers": [{}, {}, {}, {}],
          "flights": [{"flight_number": "HAT910", "date": "2024-05-26"}]}
TRN008 = {"reservation_id": "TRN008", "cabin": "business", "created_at": "2024-05-15T09:12:00",
          "insurance": "no", "total_baggages": 2, "nonfree_baggages": 0, "passengers": [{}, {}],
          "flights": [{"flight_number": "HAT903", "date": "2024-05-27"}]}
TRN009 = {"reservation_id": "TRN009", "cabin": "economy", "created_at": "2024-05-04T13:26:00",
          "insurance": "yes", "total_baggages": 1, "nonfree_baggages": 0, "passengers": [{}],
          "flights": [{"flight_number": "HAT906", "date": "2024-05-20"}]}
# Synthetic: the build store has no regular member with no insurance in economy, which is the one
# combination that must get no compensation at all.
ECONOMY_NO_INSURANCE = {"reservation_id": "SYN1", "cabin": "economy", "insurance": "no",
                        "created_at": "2024-05-01T00:00:00", "passengers": [{}],
                        "total_baggages": 0, "nonfree_baggages": 0,
                        "flights": [{"flight_number": "HAT906", "date": "2024-05-20"}]}

failures = []


def expect(label, decision, verdict, rule_contains=""):
    ok = decision.verdict == verdict and rule_contains in decision.rule
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {decision.verdict} [{decision.rule}]")
    if not ok:
        failures.append(f"{label}: expected {verdict}/{rule_contains}, "
                        f"got {decision.verdict}/{decision.rule}")


print("cancellation")
expect("flown segment -> transfer",
       policy.check_cancellation({"cancellation_reason": "change_of_plan"}, TRN003, ["landed"]),
       "transfer", "already_flown")
expect("no condition met -> deny",
       policy.check_cancellation({"cancellation_reason": "change_of_plan"}, TRN007, ["available"]),
       "deny", "no_condition_met")
expect("booked within 24h -> allow",
       policy.check_cancellation({"cancellation_reason": "change_of_plan"}, TRN008, ["available"]),
       "allow")
expect("business cabin -> allow",
       policy.check_cancellation({"cancellation_reason": "change_of_plan"}, TRN005, ["available"]),
       "allow")
expect("airline cancelled -> allow",
       policy.check_cancellation({"cancellation_reason": "airline_cancelled"}, TRN009, ["cancelled"]),
       "allow")
expect("insurance + covered reason -> allow",
       policy.check_cancellation({"cancellation_reason": "other"}, TRN003, ["available", "available"]),
       "allow")
expect("insurance + change of plan -> deny",
       policy.check_cancellation({"cancellation_reason": "change_of_plan"}, TRN003,
                                 ["available", "available"]),
       "deny", "no_condition_met")

print("flights / cabin")
expect("basic economy flight change -> deny",
       policy.check_flight_or_cabin_change(
           {"reservation_id": "TRN002", "cabin": "basic_economy", "payment_id": "credit_card_50000001",
            "flights": [{"flight_number": "HAT903", "date": "2024-05-23"}]},
           TRN002, GOLD, ["available"], {("HAT903", "2024-05-23"): "available"}),
       "deny", "basic_economy_flights")
expect("basic economy CABIN change -> allow",
       policy.check_flight_or_cabin_change(
           {"reservation_id": "TRN002", "cabin": "economy", "payment_id": "credit_card_50000001",
            "flights": [{"flight_number": "HAT903", "date": "2024-05-22"}]},
           TRN002, GOLD, ["available"], {}),
       "allow")
expect("cabin change after departure -> deny",
       policy.check_flight_or_cabin_change(
           {"reservation_id": "TRN002", "cabin": "economy", "payment_id": "credit_card_50000001",
            "flights": [{"flight_number": "HAT903", "date": "2024-05-22"}]},
           TRN002, GOLD, ["landed"], {}),
       "deny", "cabin_after_departure")
expect("certificate as change payment -> deny",
       policy.check_flight_or_cabin_change(
           {"reservation_id": "TRN003", "cabin": "economy", "payment_id": "certificate_50000003",
            "flights": [{"flight_number": "HAT908", "date": "2024-05-22"},
                        {"flight_number": "HAT909", "date": "2024-05-25"}]},
           TRN003, GOLD, ["available", "available"], {}),
       "deny", "payment_source_not_allowed")
expect("trip type change (segment count) -> deny",
       policy.check_flight_or_cabin_change(
           {"reservation_id": "TRN003", "cabin": "economy", "payment_id": "credit_card_50000001",
            "flights": [{"flight_number": "HAT908", "date": "2024-05-22"}]},
           TRN003, GOLD, ["available", "available"], {}),
       "deny", "trip_type_immutable")
expect("legitimate flight change -> allow",
       policy.check_flight_or_cabin_change(
           {"reservation_id": "TRN003", "cabin": "economy", "payment_id": "credit_card_50000001",
            "flights": [{"flight_number": "HAT908", "date": "2024-05-22"},
                        {"flight_number": "HAT909", "date": "2024-05-25"}]},
           TRN003, GOLD, ["available", "available"],
           {("HAT908", "2024-05-22"): "available"}),
       "allow")
expect("unconfirmed rule NOT enforced: move onto 'on time' -> allow",
       policy.check_flight_or_cabin_change(
           {"reservation_id": "TRN003", "cabin": "economy", "payment_id": "credit_card_50000001",
            "flights": [{"flight_number": "HAT908", "date": "2024-05-22"},
                        {"flight_number": "HAT909", "date": "2024-05-25"}]},
           TRN003, GOLD, ["available", "available"],
           {("HAT908", "2024-05-22"): "on time"}),
       "allow")
expect("move onto a departed flight -> deny",
       policy.check_flight_or_cabin_change(
           {"reservation_id": "TRN003", "cabin": "economy", "payment_id": "credit_card_50000001",
            "flights": [{"flight_number": "HAT908", "date": "2024-05-10"},
                        {"flight_number": "HAT909", "date": "2024-05-25"}]},
           TRN003, GOLD, ["available", "available"],
           {("HAT908", "2024-05-10"): "landed"}),
       "deny", "target_flight_departed")

print("passengers / baggage")
expect("adding a passenger -> deny",
       policy.check_passenger_change({"passengers": [{}, {}, {}]}, TRN003),
       "deny", "count_immutable")
expect("correcting passenger details -> allow",
       policy.check_passenger_change({"passengers": [{}, {}]}, TRN003), "allow")
expect("removing bags -> deny",
       policy.check_baggage_change(
           {"reservation_id": "TRN003", "total_baggages": 1, "nonfree_baggages": 0,
            "payment_id": "credit_card_1"}, TRN003, SILVER),
       "deny", "baggage.removal")
expect("silver economy 2 pax: 4 free, 5th chargeable, correct math -> allow",
       policy.check_baggage_change(
           {"reservation_id": "TRN003", "total_baggages": 5, "nonfree_baggages": 1,
            "payment_id": "credit_card_1"}, TRN003, SILVER),
       "allow")
expect("same case, wrong nonfree count -> deny",
       policy.check_baggage_change(
           {"reservation_id": "TRN003", "total_baggages": 5, "nonfree_baggages": 0,
            "payment_id": "credit_card_1"}, TRN003, SILVER),
       "deny", "baggage.math")

print("compensation")
expect("regular + no insurance + economy -> deny outright",
       policy.check_compensation(
           {"user_id": "u", "amount": 100, "justification_ground": "airline_cancelled_flight",
            "justification_reservation_id": "SYN1"},
           REGULAR, ECONOMY_NO_INSURANCE, ["cancelled"], []),
       "deny", "not_eligible")
expect("gold + cancelled flight, $100 x 1 -> allow",
       policy.check_compensation(
           {"user_id": "u", "amount": 100, "justification_ground": "airline_cancelled_flight",
            "justification_reservation_id": "TRN009"},
           GOLD, TRN009, ["cancelled"], []),
       "allow")
expect("gold + cancelled flight, wrong amount -> deny",
       policy.check_compensation(
           {"user_id": "u", "amount": 500, "justification_ground": "airline_cancelled_flight",
            "justification_reservation_id": "TRN009"},
           GOLD, TRN009, ["cancelled"], []),
       "deny", "wrong_amount")
expect("cancelled ground claimed but no flight cancelled -> deny",
       policy.check_compensation(
           {"user_id": "u", "amount": 100, "justification_ground": "airline_cancelled_flight",
            "justification_reservation_id": "TRN005"},
           REGULAR, TRN005, ["available"], []),
       "deny", "ground_not_supported")
expect("delayed ground before the change is made -> deny",
       policy.check_compensation(
           {"user_id": "u", "amount": 100, "justification_ground": "delayed_flight_change_or_cancel",
            "justification_reservation_id": "TRN005"},
           REGULAR, TRN005, ["delayed"], []),
       "deny", "change_not_yet_made")
expect("delayed ground after the change, $50 x 3 -> allow",
       policy.check_compensation(
           {"user_id": "u", "amount": 150, "justification_ground": "delayed_flight_change_or_cancel",
            "justification_reservation_id": "TRN005"},
           REGULAR, TRN005, ["delayed"],
           [{"tool": "update_reservation_flights", "reservation_id": "TRN005"}]),
       "allow")
expect("a third ground -> deny",
       policy.check_compensation(
           {"user_id": "u", "amount": 100, "justification_ground": "rude_staff",
            "justification_reservation_id": "TRN009"},
           GOLD, TRN009, ["cancelled"], []),
       "deny", "ground_not_permitted")

print("booking")
expect("six passengers -> deny",
       policy.check_booking({"passengers": [{}] * 6, "flights": [], "cabin": "economy",
                             "payment_methods": [], "total_baggages": 0, "nonfree_baggages": 0},
                            GOLD, {}),
       "deny", "max_passengers")
expect("booking an 'on time' flight -> deny",
       policy.check_booking(
           {"passengers": [{}], "cabin": "economy", "total_baggages": 0, "nonfree_baggages": 0,
            "flights": [{"flight_number": "HAT901", "date": "2024-05-20"}],
            "payment_methods": [{"payment_id": "credit_card_50000001", "amount": 100}]},
           GOLD, {("HAT901", "2024-05-20"): "on time"}),
       "deny", "not_available")
expect("payment method not on profile -> deny",
       policy.check_booking(
           {"passengers": [{}], "cabin": "economy", "total_baggages": 0, "nonfree_baggages": 0,
            "flights": [{"flight_number": "HAT901", "date": "2024-05-20"}],
            "payment_methods": [{"payment_id": "credit_card_9999", "amount": 100}]},
           GOLD, {("HAT901", "2024-05-20"): "available"}),
       "deny", "payment_not_on_profile")
expect("gift card over balance -> deny",
       policy.check_booking(
           {"passengers": [{}], "cabin": "economy", "total_baggages": 0, "nonfree_baggages": 0,
            "flights": [{"flight_number": "HAT901", "date": "2024-05-20"}],
            "payment_methods": [{"payment_id": "gift_card_50000002", "amount": 900}]},
           GOLD, {("HAT901", "2024-05-20"): "available"}),
       "deny", "insufficient_balance")
expect("gold economy 1 pax, 3 free bags, 4th chargeable -> allow",
       policy.check_booking(
           {"passengers": [{}], "cabin": "economy", "total_baggages": 4, "nonfree_baggages": 1,
            "flights": [{"flight_number": "HAT901", "date": "2024-05-20"}],
            "payment_methods": [{"payment_id": "credit_card_50000001", "amount": 160}]},
           GOLD, {("HAT901", "2024-05-20"): "available"}),
       "allow")

print("baggage allowance table")
for (tier, cabin, pax), want in {
    ("regular", "basic_economy", 1): 0,
    ("regular", "economy", 2): 2,
    ("regular", "business", 1): 2,
    ("silver", "basic_economy", 1): 1,
    ("silver", "economy", 2): 4,
    ("silver", "business", 1): 3,
    ("gold", "basic_economy", 1): 2,
    ("gold", "economy", 3): 9,
    ("gold", "business", 1): 4,
}.items():
    got = policy.free_baggage_allowance(tier, cabin, pax)
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {tier}/{cabin} x{pax} = {got} (want {want})")
    if not ok:
        failures.append(f"allowance {tier}/{cabin}x{pax}: got {got}, want {want}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for failure in failures:
        print("  -", failure)
    sys.exit(1)
print("all policy checks pass")
