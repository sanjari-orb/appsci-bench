"""Author the airline BUILD-phase seed store: a small synthetic airline the agent explores.

WHY THIS EXISTS. The agent explores the task environment during the build phase and can read every
record in it -- through the airline tools or by writing its own client. The 50 eval cases are drawn from
the real store, so an agent that reads it can shape its artifact around the very rows it will be scored
on. The build/eval environment split (docs/task_env_split.md) fixes that by giving each phase its own
data. This script writes the build phase's half.

WHY SYNTHETIC RATHER THAN A CARVE. docs/task_env_carve_spec.md specifies carving a referentially closed
subset out of the real store. Generating fresh records instead trades one property for another, and both
directions are deliberate:

  + Disjointness becomes structural. A carve has to prove it excluded all 34 customers and 118 flights
    the eval cases touch; fresh identifiers cannot collide at all.
  + Category coverage is authored, not hoped for. The spec notes a strict carve leaves ~1% of bookings --
    "one of everything and many of nothing". Here every enumerated value is placed on purpose.
  - Scale is lost: 10 reservations against 2000, 5 airports against 20. An artifact that caches
    `list_all_airports`, or assumes a store this small, will be wrong at eval. That is arguably the
    generalisation pressure the split exists to create, but it IS a behaviour change a carve would not
    have introduced -- so it is the first thing to suspect if eval scores move.

NO RANDOMNESS. The spec asks for determinism from a seed so a reviewer can re-run and get the same files.
At this size the records are more reviewable than any generator would be, so they are literals here and
only the mechanical parts are derived: the 30 per-date flight instances, the reservation totals, the
`user["reservations"]` back-references, and the split-out per-collection files. Determinism is then a
property of the data rather than an argument about a seed.

Run:  python scripts/generate_airline_build_seed.py [--check]
      --check validates and reports without writing (use in review to confirm an empty diff).
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TASK = REPO_ROOT / "tasks" / "airline_rebooking"
OUT_DIR = TASK / "environment" / "build" / "data"
EVAL_DB = TASK / "environment" / "db" / "db.json"

# ---------------------------------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------------------------------

# Real IATA codes drawn from the eval store's 20, so code shape and length match. `list_all_airports`
# derives the list from the FLIGHT records (tools.py), not from a table, so each of these must appear as
# some flight's origin or destination -- asserted below.
AIRPORTS = ["DEN", "JFK", "LAX", "ORD", "SFO"]

# The eval store's own window. Kept identical because the policy and the user-sim reason about "already
# flown" and the 24-hour cancellation rule against the fixed clock; no TOOL compares a date to it.
DATES = [f"2024-05-{d:02d}" for d in range(1, 31)]
NOW_DATE = "2024-05-15"          # matches tools.NOW = "2024-05-15T15:00:00"

# Flight numbers start at HAT901: the eval store runs HAT001..HAT300, so this range cannot collide.
# ORD is the hub, which is what makes a two-leg SFO->ORD->JFK itinerary constructible. Return legs exist
# for SFO<->ORD, ORD<->JFK, ORD<->DEN and LAX<->ORD so round trips can be booked; DEN->LAX is
# deliberately one-way-only, so the store has an unpaired route too.
#
# `day15` is that flight's status on the clock date. Chosen from the schedule so the store is internally
# honest: a flight that lands before 15:00 reads `landed`, one still airborne reads `flying`, one not yet
# departed reads `on time` or `delayed`. HAT905 is a long transcon specifically so something IS airborne
# at 15:00 -- without it the store could never show `flying`.
FLIGHTS: list[dict] = [
    {"n": "HAT901", "o": "SFO", "d": "ORD", "dep": "08:00:00", "arr": "14:15:00", "day15": "landed"},
    {"n": "HAT902", "o": "ORD", "d": "SFO", "dep": "16:00:00", "arr": "18:30:00", "day15": "on time"},
    {"n": "HAT903", "o": "ORD", "d": "JFK", "dep": "07:30:00", "arr": "10:45:00", "day15": "landed"},
    {"n": "HAT904", "o": "JFK", "d": "ORD", "dep": "12:00:00", "arr": "13:50:00", "day15": "landed"},
    {"n": "HAT905", "o": "SFO", "d": "JFK", "dep": "11:00:00", "arr": "19:20:00", "day15": "flying"},
    {"n": "HAT906", "o": "ORD", "d": "DEN", "dep": "09:15:00", "arr": "10:40:00", "day15": "landed"},
    {"n": "HAT907", "o": "DEN", "d": "ORD", "dep": "11:30:00", "arr": "14:45:00", "day15": "landed"},
    {"n": "HAT908", "o": "LAX", "d": "ORD", "dep": "07:00:00", "arr": "13:10:00", "day15": "landed"},
    {"n": "HAT909", "o": "ORD", "d": "LAX", "dep": "15:30:00", "arr": "17:55:00", "day15": "delayed"},
    {"n": "HAT910", "o": "DEN", "d": "LAX", "dep": "08:45:00", "arr": "10:05:00", "day15": "landed"},
]

# Airline-cancelled instances, named explicitly rather than derived, because two of them carry meaning:
# HAT906 on 05-20 is the flight TRN009 is booked on (the compensation scenario), and the past ones give
# the store a `cancelled` status in its history. A cancelled instance holds ONLY {"status"} -- matching
# the eval store, where cancelled dates carry no times, prices or seats.
CANCELLED = {("HAT906", "2024-05-20"), ("HAT902", "2024-05-04"), ("HAT908", "2024-05-11")}

# Per-cabin fares and seat counts for a bookable instance. Ints on purpose: book_reservation compares the
# payment total with exact equality, so a float fare makes the correct payment a float and moves the
# DB-state hash even when the arithmetic is right.
def _prices(i: int) -> dict[str, int]:
    return {"basic_economy": 60 + 5 * i, "economy": 110 + 8 * i, "business": 400 + 20 * i}


def _seats(i: int) -> dict[str, int]:
    # Every bookable instance carries ALL THREE cabins in both prices and available_seats. tools.py
    # subscripts inst["prices"][cabin] bare, so a missing cabin is a KeyError -> HTTP 500 rather than a
    # clean tool refusal. Minimum economy seats stay >= 4 so a 4-passenger booking is always possible.
    return {"basic_economy": 8 + (i % 5), "economy": 6 + (i % 7), "business": 3 + (i % 4)}


# ---------------------------------------------------------------------------------------------------
# Users. Payment-method ids use an 8-DIGIT suffix; every one of the eval store's 1486 payment ids has
# exactly 7, so this whole space is disjoint by construction. It also avoids certificate_3221322/3/4,
# the ids send_certificate mints (tools.py) -- a seed holding one of those would silently change which id
# a live certificate gets and break golden-action replay.
# ---------------------------------------------------------------------------------------------------
USERS: list[dict] = [
    {
        "user_id": "nora_castillo_9412", "membership": "gold",
        "name": {"first_name": "Nora", "last_name": "Castillo"},
        "dob": "1986-03-14", "email": "nora.castillo9412@example.com",
        "address": {"address1": "884 Cedar Street", "address2": "Suite 210", "city": "San Francisco",
                    "country": "USA", "state": "CA", "zip": "94107"},
        "payment_methods": {
            "credit_card_50000001": {"brand": "visa", "id": "credit_card_50000001",
                                     "last_four": "4417", "source": "credit_card"},
            "gift_card_50000002": {"amount": 500, "id": "gift_card_50000002", "source": "gift_card"},
            "certificate_50000003": {"amount": 250, "id": "certificate_50000003",
                                     "source": "certificate"},
        },
        "saved_passengers": [{"dob": "1984-11-02", "first_name": "Diego", "last_name": "Castillo"}],
    },
    {
        "user_id": "owen_delgado_9137", "membership": "silver",
        "name": {"first_name": "Owen", "last_name": "Delgado"},
        "dob": "1979-07-30", "email": "owen.delgado9137@example.com",
        "address": {"address1": "17 Lakeshore Drive", "address2": "Apt 4B", "city": "Chicago",
                    "country": "USA", "state": "IL", "zip": "60611"},
        "payment_methods": {
            "credit_card_50000004": {"brand": "mastercard", "id": "credit_card_50000004",
                                     "last_four": "8823", "source": "credit_card"},
            "gift_card_50000005": {"amount": 300, "id": "gift_card_50000005", "source": "gift_card"},
        },
        "saved_passengers": [{"dob": "1981-02-17", "first_name": "Marta", "last_name": "Delgado"}],
    },
    {
        # Deliberately holds NO certificate and NO gift card: the only user for whom a paid change must
        # go on a credit card. Keeps the "certificate cannot be used to update reservation" branch and
        # the card path both reachable from real records.
        "user_id": "priya_venkat_9268", "membership": "regular",
        "name": {"first_name": "Priya", "last_name": "Venkat"},
        "dob": "1992-12-05", "email": "priya.venkat9268@example.com",
        "address": {"address1": "3320 Alameda Avenue", "address2": "", "city": "Denver",
                    "country": "USA", "state": "CO", "zip": "80204"},
        "payment_methods": {
            "credit_card_50000006": {"brand": "visa", "id": "credit_card_50000006",
                                     "last_four": "1902", "source": "credit_card"},
        },
        "saved_passengers": [],
    },
    {
        # The mirror image: gift card + certificate but NO credit card, so a refund has somewhere to go
        # and an update must use the gift card.
        "user_id": "quinn_okafor_9503", "membership": "silver",
        "name": {"first_name": "Quinn", "last_name": "Okafor"},
        "dob": "1995-09-21", "email": "quinn.okafor9503@example.com",
        "address": {"address1": "51 Harbor Way", "address2": "Unit 7", "city": "Los Angeles",
                    "country": "USA", "state": "CA", "zip": "90014"},
        "payment_methods": {
            "gift_card_50000007": {"amount": 150, "id": "gift_card_50000007", "source": "gift_card"},
            "certificate_50000008": {"amount": 100, "id": "certificate_50000008",
                                     "source": "certificate"},
        },
        "saved_passengers": [{"dob": "1990-06-08", "first_name": "Ada", "last_name": "Okafor"},
                             {"dob": "2015-01-19", "first_name": "Tomas", "last_name": "Okafor"}],
    },
    {
        "user_id": "rafael_moreau_9821", "membership": "gold",
        "name": {"first_name": "Rafael", "last_name": "Moreau"},
        "dob": "1974-04-11", "email": "rafael.moreau9821@example.com",
        "address": {"address1": "1290 Beacon Road", "address2": "Floor 3", "city": "New York",
                    "country": "USA", "state": "NY", "zip": "10018"},
        "payment_methods": {
            "credit_card_50000009": {"brand": "mastercard", "id": "credit_card_50000009",
                                     "last_four": "6650", "source": "credit_card"},
            "gift_card_50000010": {"amount": 800, "id": "gift_card_50000010", "source": "gift_card"},
            "certificate_50000011": {"amount": 500, "id": "certificate_50000011",
                                     "source": "certificate"},
        },
        "saved_passengers": [{"dob": "1976-08-25", "first_name": "Celine", "last_name": "Moreau"}],
    },
]

# ---------------------------------------------------------------------------------------------------
# Reservations. Ids are TRN001..TRN010 -- 6 uppercase alphanumerics, the eval store's shape, and outside
# both the 2000 existing ids and the HATHAT/HATHAU/HATHAV reservoir book_reservation mints.
#
# Each entry names its scenario, because that is why it exists: between them they cover all three cabins,
# both trip types, both insurance values, 1-4 passengers, single and multi-segment, and the situations the
# POLICY turns on -- already flown, basic economy (unmodifiable), business (cancellable), booked inside
# the 24-hour window, and an airline-cancelled flight.
#
# Segment prices are authored rather than read from the flight instance: a past or cancelled instance has
# no `prices` at all, and in the real store a segment's price is what was paid at booking, not today's
# fare. Payment totals ARE derived from these, so the arithmetic cannot drift.
# `nonfree_baggages` is 0 throughout, matching the eval store where it is 0 in all 2000 records.
# ---------------------------------------------------------------------------------------------------
RESERVATIONS: list[dict] = [
    {   # business + round trip + insurance: fully flexible, cancellable on cabin alone.
        "reservation_id": "TRN001", "user_id": "nora_castillo_9412",
        "origin": "SFO", "destination": "ORD", "flight_type": "round_trip", "cabin": "business",
        "created_at": "2024-05-02T10:24:00", "insurance": "yes", "total_baggages": 2,
        "segments": [("HAT901", "2024-05-20", 420), ("HAT902", "2024-05-24", 440)],
        "passengers": [{"dob": "1986-03-14", "first_name": "Nora", "last_name": "Castillo"}],
        "pay": "credit_card_50000001",
    },
    {   # basic economy: flights CANNOT be changed, only the cabin can.
        "reservation_id": "TRN002", "user_id": "nora_castillo_9412",
        "origin": "ORD", "destination": "JFK", "flight_type": "one_way", "cabin": "basic_economy",
        "created_at": "2024-05-06T18:03:00", "insurance": "no", "total_baggages": 0,
        "segments": [("HAT903", "2024-05-22", 72)],
        "passengers": [{"dob": "1986-03-14", "first_name": "Nora", "last_name": "Castillo"}],
        "pay": "gift_card_50000002",
    },
    {   # two passengers, economy round trip with insurance -- the ordinary case.
        "reservation_id": "TRN003", "user_id": "owen_delgado_9137",
        "origin": "LAX", "destination": "ORD", "flight_type": "round_trip", "cabin": "economy",
        "created_at": "2024-05-03T08:47:00", "insurance": "yes", "total_baggages": 3,
        "segments": [("HAT908", "2024-05-21", 168), ("HAT909", "2024-05-25", 174)],
        "passengers": [{"dob": "1979-07-30", "first_name": "Owen", "last_name": "Delgado"},
                       {"dob": "1981-02-17", "first_name": "Marta", "last_name": "Delgado"}],
        "pay": "credit_card_50000004",
    },
    {   # ALREADY FLOWN (05-10, and HAT905 landed that day): no change, no cancel -- transfer to a human.
        "reservation_id": "TRN004", "user_id": "owen_delgado_9137",
        "origin": "SFO", "destination": "JFK", "flight_type": "one_way", "cabin": "economy",
        "created_at": "2024-05-01T14:12:00", "insurance": "no", "total_baggages": 1,
        "segments": [("HAT905", "2024-05-10", 142)],
        "passengers": [{"dob": "1979-07-30", "first_name": "Owen", "last_name": "Delgado"}],
        "pay": "gift_card_50000005",
    },
    {   # MULTI-SEGMENT one-way, SFO->ORD->JFK, three passengers. Both legs on the same day.
        "reservation_id": "TRN005", "user_id": "priya_venkat_9268",
        "origin": "SFO", "destination": "JFK", "flight_type": "one_way", "cabin": "business",
        "created_at": "2024-05-05T21:35:00", "insurance": "no", "total_baggages": 4,
        "segments": [("HAT901", "2024-05-18", 415), ("HAT903", "2024-05-18", 445)],
        "passengers": [{"dob": "1992-12-05", "first_name": "Priya", "last_name": "Venkat"},
                       {"dob": "1988-10-30", "first_name": "Arun", "last_name": "Venkat"},
                       {"dob": "2012-05-02", "first_name": "Isha", "last_name": "Venkat"}],
        "pay": "credit_card_50000006",
    },
    {   # basic economy ROUND TRIP with insurance -- insured but still unmodifiable as to flights.
        "reservation_id": "TRN006", "user_id": "priya_venkat_9268",
        "origin": "ORD", "destination": "DEN", "flight_type": "round_trip", "cabin": "basic_economy",
        "created_at": "2024-05-07T11:58:00", "insurance": "yes", "total_baggages": 1,
        "segments": [("HAT906", "2024-05-19", 85), ("HAT907", "2024-05-23", 90)],
        "passengers": [{"dob": "1992-12-05", "first_name": "Priya", "last_name": "Venkat"}],
        "pay": "credit_card_50000006",
    },
    {   # FOUR passengers -- the largest party, and the reason economy seat counts never drop below 4.
        "reservation_id": "TRN007", "user_id": "quinn_okafor_9503",
        "origin": "DEN", "destination": "LAX", "flight_type": "one_way", "cabin": "economy",
        "created_at": "2024-05-08T16:41:00", "insurance": "no", "total_baggages": 4,
        "segments": [("HAT910", "2024-05-26", 182)],
        "passengers": [{"dob": "1995-09-21", "first_name": "Quinn", "last_name": "Okafor"},
                       {"dob": "1990-06-08", "first_name": "Ada", "last_name": "Okafor"},
                       {"dob": "2015-01-19", "first_name": "Tomas", "last_name": "Okafor"},
                       {"dob": "1963-03-03", "first_name": "Grace", "last_name": "Okafor"}],
        "pay": "gift_card_50000007",
    },
    {   # BOOKED INSIDE THE 24-HOUR WINDOW (clock is 2024-05-15T15:00:00): cancellable on that ground
        # alone, whatever the cabin.
        "reservation_id": "TRN008", "user_id": "quinn_okafor_9503",
        "origin": "ORD", "destination": "JFK", "flight_type": "round_trip", "cabin": "business",
        "created_at": "2024-05-15T09:12:00", "insurance": "no", "total_baggages": 2,
        "segments": [("HAT903", "2024-05-27", 455), ("HAT904", "2024-05-29", 470)],
        "passengers": [{"dob": "1995-09-21", "first_name": "Quinn", "last_name": "Okafor"},
                       {"dob": "1990-06-08", "first_name": "Ada", "last_name": "Okafor"}],
        "pay": "certificate_50000008",
    },
    {   # AIRLINE-CANCELLED FLIGHT (HAT906 on 05-20 is cancelled): the compensation scenario.
        "reservation_id": "TRN009", "user_id": "rafael_moreau_9821",
        "origin": "ORD", "destination": "DEN", "flight_type": "one_way", "cabin": "economy",
        "created_at": "2024-05-04T13:26:00", "insurance": "yes", "total_baggages": 1,
        "segments": [("HAT906", "2024-05-20", 155)],
        "passengers": [{"dob": "1974-04-11", "first_name": "Rafael", "last_name": "Moreau"}],
        "pay": "credit_card_50000009",
    },
    {   # basic economy, no bags, no insurance -- the floor case, and a gold member holding it.
        "reservation_id": "TRN010", "user_id": "rafael_moreau_9821",
        "origin": "ORD", "destination": "LAX", "flight_type": "one_way", "cabin": "basic_economy",
        "created_at": "2024-05-09T07:19:00", "insurance": "no", "total_baggages": 0,
        "segments": [("HAT909", "2024-05-28", 95)],
        "passengers": [{"dob": "1974-04-11", "first_name": "Rafael", "last_name": "Moreau"}],
        "pay": "gift_card_50000010",
    },
]


# ---------------------------------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------------------------------

def _minutes(hhmmss: str) -> int:
    h, m, _ = hhmmss.split(":")
    return int(h) * 60 + int(m)


def _stamp(date: str, hhmmss: str, offset_min: int = 0) -> str:
    """A `YYYY-MM-DDTHH:MM:SS` timestamp, rolling into the next day when the offset passes midnight."""
    total = _minutes(hhmmss) + offset_min
    day_shift, total = divmod(total, 24 * 60)
    hh, mm = divmod(total, 60)
    if day_shift:
        y, mo, d = (int(x) for x in date.split("-"))
        date = f"{y:04d}-{mo:02d}-{d + day_shift:02d}"
    return f"{date}T{hh:02d}:{mm:02d}:00"


def build_flights() -> dict[str, dict]:
    """One flight record per route, each carrying all 30 dates.

    Status by date, mirroring the eval store's own convention: before the clock date the flight has
    `landed` (or `cancelled`); on the clock date it takes the status its schedule implies; after, it is
    `available` and therefore bookable. Only `available` instances carry prices and seats, and only
    those appear in search results (`search_direct_flight` admits `status == "available"` alone).
    """
    out: dict[str, dict] = {}
    for i, f in enumerate(FLIGHTS):
        dates: dict[str, dict] = {}
        for day_index, date in enumerate(DATES):
            if (f["n"], date) in CANCELLED:
                dates[date] = {"status": "cancelled"}       # cancelled holds ONLY status
                continue
            if date < NOW_DATE:
                # Deterministic small deviation from schedule, so the history is not suspiciously exact.
                dep_off = (i * 7 + day_index * 3) % 21 - 6
                arr_off = (i * 5 + day_index * 11) % 25 - 8
                dates[date] = {
                    "actual_arrival_time_est": _stamp(date, f["arr"], arr_off),
                    "actual_departure_time_est": _stamp(date, f["dep"], dep_off),
                    "status": "landed",
                }
            elif date == NOW_DATE:
                dates[date] = _clock_day_instance(f)
            else:
                dates[date] = {"available_seats": _seats(i + day_index),
                               "prices": _prices(i), "status": "available"}
        out[f["n"]] = {
            "dates": dates,
            "destination": f["d"],
            "flight_number": f["n"],
            "origin": f["o"],
            "scheduled_arrival_time_est": f["arr"],
            "scheduled_departure_time_est": f["dep"],
        }
    return out


def _clock_day_instance(f: dict) -> dict:
    """The clock-date instance, shaped by status exactly as the eval store shapes it."""
    status = f["day15"]
    if status == "landed":
        return {"actual_arrival_time_est": _stamp(NOW_DATE, f["arr"], -4),
                "actual_departure_time_est": _stamp(NOW_DATE, f["dep"], 2),
                "status": "landed"}
    if status == "flying":                       # departed, still airborne at 15:00
        return {"actual_departure_time_est": _stamp(NOW_DATE, f["dep"], 6),
                "estimated_arrival_time_est": _stamp(NOW_DATE, f["arr"], 12),
                "status": "flying"}
    # `on time` and `delayed` have not departed, so both carry ESTIMATES and no actuals.
    lateness = 95 if status == "delayed" else 0
    return {"estimated_arrival_time_est": _stamp(NOW_DATE, f["arr"], lateness),
            "estimated_departure_time_est": _stamp(NOW_DATE, f["dep"], lateness),
            "status": status}


def build_reservations_and_users() -> tuple[dict[str, dict], dict[str, dict]]:
    """Expand the reservation literals, derive their payment totals, and back-reference them onto users.

    The 13 keys and their nesting match what `book_reservation` writes, so a record authored here is
    indistinguishable from one the tool would have created. No `status` key: `cancel_reservation` adds
    that as the cancellation marker, and none of the eval store's 2000 reservations carries one.
    """
    flights = build_flights()
    users = {u["user_id"]: {**copy.deepcopy(u), "reservations": []} for u in USERS}
    reservations: dict[str, dict] = {}

    for r in RESERVATIONS:
        segments = []
        for flight_number, date, price in r["segments"]:
            f = flights[flight_number]
            segments.append({"origin": f["origin"], "destination": f["destination"],
                             "flight_number": flight_number, "date": date, "price": price})
        n_pax = len(r["passengers"])
        # Same arithmetic book_reservation applies: fare per passenger per segment, $30 insurance per
        # passenger, $50 per non-free bag. Derived rather than typed so it cannot drift from the fares.
        total = sum(s["price"] for s in segments) * n_pax
        if r["insurance"] == "yes":
            total += 30 * n_pax
        reservations[r["reservation_id"]] = {
            "cabin": r["cabin"],
            "created_at": r["created_at"],
            "destination": r["destination"],
            "flight_type": r["flight_type"],
            "flights": segments,
            "insurance": r["insurance"],
            "nonfree_baggages": 0,
            "origin": r["origin"],
            "passengers": copy.deepcopy(r["passengers"]),
            "payment_history": [{"amount": total, "payment_id": r["pay"]}],
            "reservation_id": r["reservation_id"],
            "total_baggages": r["total_baggages"],
            "user_id": r["user_id"],
        }
        # A reservation naming a user who does not exist is left un-backreferenced ON PURPOSE rather than
        # raising here: `validate` then reports it as "user_id is not in users", naming the record. Before
        # this guard the same defect surfaced as a bare `KeyError: 'ghost_user_0001'` from the append
        # below -- a crash that says nothing about which reservation is wrong or why it matters.
        if r["user_id"] in users:
            users[r["user_id"]]["reservations"].append(r["reservation_id"])

    return flights, reservations, users


# ---------------------------------------------------------------------------------------------------
# Validation. Every check here guards a failure that would otherwise show up as an HTTP 500 or a
# silently wrong DB-state hash rather than as an error a reader can act on.
# ---------------------------------------------------------------------------------------------------

CABINS = ("basic_economy", "economy", "business")
EXPECTED = {
    "membership": {"regular", "silver", "gold"},
    "cabin": set(CABINS),
    "flight_type": {"one_way", "round_trip"},
    "insurance": {"yes", "no"},
    "source": {"certificate", "credit_card", "gift_card"},
    "brand": {"visa", "mastercard"},
    "status": {"available", "landed", "cancelled", "delayed", "flying", "on time"},
}
# Ids the tools mint at runtime. A seed containing one of these silently changes which id a live booking
# or certificate receives, which breaks golden-action replay -- see tools.py's two fixed reservoirs.
MINTED_RESERVATIONS = {"HATHAT", "HATHAU", "HATHAV"}
MINTED_CERTIFICATES = {"certificate_3221322", "certificate_3221323", "certificate_3221324"}


def validate(db: dict, eval_db: dict) -> list[str]:
    """Return a list of problems; empty means the store is safe to write."""
    problems: list[str] = []
    flights, users, reservations = db["flights"], db["users"], db["reservations"]

    def bad(msg: str) -> None:
        problems.append(msg)

    # -- key/id agreement. search_direct_flight returns the record's own flight_number, not the key.
    for coll, id_field in (("flights", "flight_number"), ("users", "user_id"),
                           ("reservations", "reservation_id")):
        for key, rec in db[coll].items():
            if rec.get(id_field) != key:
                bad(f"{coll}[{key}]: {id_field} is {rec.get(id_field)!r}, must equal its key")

    # -- schema parity with the eval store: same field names, so the agent learns the real shape.
    for coll in ("flights", "users", "reservations"):
        want = set(next(iter(eval_db[coll].values())))
        for key, rec in db[coll].items():
            if set(rec) != want:
                bad(f"{coll}[{key}]: keys {sorted(set(rec) ^ want)} differ from the eval store's schema")

    # -- every airport must be reachable via a FLIGHT record, or list_all_airports will not report it.
    listed = {f["origin"] for f in flights.values()} | {f["destination"] for f in flights.values()}
    if listed != set(AIRPORTS):
        bad(f"list_all_airports would return {sorted(listed)}, not {AIRPORTS}")

    # -- bookable instances must be complete in both cabin maps (tools.py subscripts prices bare).
    for fn, f in flights.items():
        for date, inst in f["dates"].items():
            if inst["status"] != "available":
                if "prices" in inst or "available_seats" in inst:
                    bad(f"flights[{fn}].dates[{date}]: non-available instance carries prices/seats")
                continue
            for field in ("prices", "available_seats"):
                missing = set(CABINS) - set(inst.get(field, {}))
                if missing:
                    bad(f"flights[{fn}].dates[{date}]: {field} missing {sorted(missing)}")
                if any(not isinstance(v, int) for v in inst.get(field, {}).values()):
                    bad(f"flights[{fn}].dates[{date}]: {field} has a non-int value")

    # -- referential closure, and the two-way user<->reservation view the tools assume.
    for rid, r in reservations.items():
        if "status" in r:
            bad(f"reservations[{rid}]: carries a `status` key -- that is cancel_reservation's marker")
        if r["user_id"] not in users:
            bad(f"reservations[{rid}]: user_id {r['user_id']!r} is not in users")
        elif rid not in users[r["user_id"]]["reservations"]:
            bad(f"reservations[{rid}]: not listed on user {r['user_id']}")
        for seg in r["flights"]:
            f = flights.get(seg["flight_number"])
            if f is None:
                bad(f"reservations[{rid}]: flight {seg['flight_number']} is not in flights")
            elif seg["date"] not in f["dates"]:
                bad(f"reservations[{rid}]: {seg['flight_number']} has no date {seg['date']}")
            if not isinstance(seg["price"], int):
                bad(f"reservations[{rid}]: segment price {seg['price']!r} is not an int")
        for p in r["payment_history"]:
            if not isinstance(p["amount"], int):
                bad(f"reservations[{rid}]: payment amount {p['amount']!r} is not an int")
            if p["payment_id"] not in users.get(r["user_id"], {}).get("payment_methods", {}):
                bad(f"reservations[{rid}]: pays with {p['payment_id']!r}, which its user does not hold")

    for uid, u in users.items():
        if not isinstance(u.get("payment_methods"), dict):
            bad(f"users[{uid}]: payment_methods must be a dict (send_certificate subscripts it bare)")
        for pid, pm in u.get("payment_methods", {}).items():
            if pm.get("id") != pid:
                bad(f"users[{uid}].payment_methods[{pid}]: id field disagrees with its key")
            if pm.get("source") in {"gift_card", "certificate"} and not isinstance(pm.get("amount"), int):
                bad(f"users[{uid}].payment_methods[{pid}]: {pm.get('source')} needs an int amount")
        for rid in u.get("reservations", []):
            if rid not in reservations:
                bad(f"users[{uid}]: lists reservation {rid!r} which does not exist")

    # -- category coverage, the property a small store is most likely to lose.
    seen = {
        "membership": {u["membership"] for u in users.values()},
        "cabin": {r["cabin"] for r in reservations.values()},
        "flight_type": {r["flight_type"] for r in reservations.values()},
        "insurance": {r["insurance"] for r in reservations.values()},
        "source": {pm["source"] for u in users.values() for pm in u["payment_methods"].values()},
        "brand": {pm["brand"] for u in users.values() for pm in u["payment_methods"].values()
                  if "brand" in pm},
        "status": {inst["status"] for f in flights.values() for inst in f["dates"].values()},
    }
    for field, want in EXPECTED.items():
        if missing := want - seen[field]:
            bad(f"category coverage: {field} is missing {sorted(missing)}")

    # -- disjointness from the eval store, plus the runtime reservoirs.
    eval_pay = {pid for u in eval_db["users"].values() for pid in u["payment_methods"]}
    our_pay = {pid for u in users.values() for pid in u["payment_methods"]}
    for label, ours, theirs in (
        ("flight numbers", set(flights), set(eval_db["flights"])),
        ("user ids", set(users), set(eval_db["users"])),
        ("reservation ids", set(reservations), set(eval_db["reservations"])),
        ("payment ids", our_pay, eval_pay),
    ):
        if clash := ours & theirs:
            bad(f"{label} collide with the eval store: {sorted(clash)[:5]}")
    if clash := set(reservations) & MINTED_RESERVATIONS:
        bad(f"reservation ids collide with book_reservation's reservoir: {sorted(clash)}")
    if clash := our_pay & MINTED_CERTIFICATES:
        bad(f"payment ids collide with send_certificate's reservoir: {sorted(clash)}")

    return problems


def exercise_tools(db: dict) -> list[str]:
    """Run every tool against the store and require each one to actually SUCCEED.

    Two failure kinds, and the bar is set at the stricter one on purpose:

      * a `KeyError`/`TypeError` is malformed data -- the service turns it into an HTTP 500, and it is
        the kind a shape check alone would miss (a bookable instance missing one cabin, say);
      * a `ToolError` is a business refusal the service turns into a clean 400. Legitimate in general --
        but every call below is authored against records chosen to satisfy it, so a refusal here means
        the data no longer supports the operation.

    Accepting refusals would make this check pass vacuously on a store where nothing works, which is the
    trap worth avoiding: all 14 calls do succeed today, so demanding that is both true and sharp.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from tasks.airline_rebooking.environment import AirlineEnvironment       # noqa: PLC0415
    from tasks.airline_rebooking.environment.db import AirlineDB             # noqa: PLC0415
    from tasks.airline_rebooking.environment.tools import ToolError          # noqa: PLC0415

    problems: list[str] = []
    base = AirlineEnvironment(AirlineDB(db["flights"], db["users"], db["reservations"]))

    uid = "nora_castillo_9412"
    rid = "TRN001"
    calls = [
        ("list_all_airports", {}),
        ("get_user_details", {"user_id": uid}),
        ("get_reservation_details", {"reservation_id": rid}),
        ("get_flight_status", {"flight_number": "HAT901", "date": "2024-05-20"}),
        ("search_direct_flight", {"origin": "SFO", "destination": "ORD", "date": "2024-05-20"}),
        ("search_onestop_flight", {"origin": "SFO", "destination": "JFK", "date": "2024-05-20"}),
        ("calculate", {"expression": "2 * (110 + 30)"}),
        ("transfer_to_human_agents", {"summary": "cannot help"}),
        ("update_reservation_baggages", {"reservation_id": rid, "total_baggages": 3,
                                         "nonfree_baggages": 1, "payment_id": "credit_card_50000001"}),
        ("update_reservation_passengers", {"reservation_id": rid, "passengers": [
            {"dob": "1986-03-14", "first_name": "Nora", "last_name": "Castillo"}]}),
        ("update_reservation_flights", {"reservation_id": rid, "cabin": "business", "flights": [
            {"flight_number": "HAT901", "date": "2024-05-20"},
            {"flight_number": "HAT902", "date": "2024-05-26"}],
            "payment_id": "credit_card_50000001"}),
        ("cancel_reservation", {"reservation_id": rid}),
        ("send_certificate", {"user_id": uid, "amount": 100}),
        ("book_reservation", {
            "user_id": uid, "origin": "ORD", "destination": "DEN", "flight_type": "one_way",
            "cabin": "economy", "flights": [{"flight_number": "HAT906", "date": "2024-05-19"}],
            "passengers": [{"dob": "1986-03-14", "first_name": "Nora", "last_name": "Castillo"}],
            "payment_methods": [{"payment_id": "gift_card_50000002", "amount": 150}],
            "total_baggages": 1, "nonfree_baggages": 0, "insurance": "no"}),
    ]
    for name, args in calls:
        env = base.fresh()          # a fresh copy per call, so one write cannot disturb the next
        try:
            env.tools.execute(name, args)
        except ToolError as exc:
            problems.append(f"{name}: refused -- {exc} (the data no longer supports this call)")
        except Exception as exc:    # noqa: BLE001 -- malformed data; this is what we are hunting for
            problems.append(f"{name}: {type(exc).__name__}: {exc}")
    if len(calls) != 14:
        problems.append(f"only {len(calls)} of the 14 tools are exercised")
    return problems


# ---------------------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="validate and report without writing (confirms an empty diff in review)")
    args = ap.parse_args()

    flights, reservations, users = build_reservations_and_users()
    db = {"flights": flights, "reservations": reservations, "users": users}
    eval_db = json.loads(EVAL_DB.read_text())

    problems = validate(db, eval_db) + exercise_tools(db)
    if problems:
        print(f"REFUSING TO WRITE -- {len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"build seed: {len(flights)} flights, {len(users)} users, {len(reservations)} reservations")
    print(f"  airports:   {sorted({f['origin'] for f in flights.values()} | {f['destination'] for f in flights.values()})}")
    print(f"  dates:      {DATES[0]} .. {DATES[-1]} ({len(DATES)} per flight)")
    for field, want in EXPECTED.items():
        print(f"  {field:12} covered: {sorted(want)}")
    print(f"  disjoint from the eval store ({len(eval_db['flights'])} flights, "
          f"{len(eval_db['users'])} users, {len(eval_db['reservations'])} reservations)")
    print("  all 14 tools exercised, every one succeeded (no refusal, no crash)")

    if args.check:
        print("\n--check: nothing written")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Same convention as scripts/convert_tau2_airline.py, so re-running produces an empty diff.
    def write(name: str, payload: object) -> None:
        (OUT_DIR / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"  wrote {(OUT_DIR / name).relative_to(REPO_ROOT)}")

    print()
    write("db.json", db)             # the ONLY file AirlineDB.load reads
    write("flights.json", flights)   # the rest mirror the eval fixture's layout, for inspection
    write("users.json", users)
    write("reservations.json", reservations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
