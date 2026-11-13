#!/usr/bin/env python
"""Mine the real fixture for test cases covering each policy branch.

There are no transcripts of past customer requests, so test cases are derived from the actual
reservation database instead of invented: for every policy branch we find real reservations whose
records genuinely exhibit it (already-flown segments, airline-cancelled flights, bookings inside
the 24h window, and so on). Grounding the expectations in real records means they stay honest --
re-running the miner after a fixture change re-grounds them rather than silently going stale.

A few negative cases cannot be *found*, only *constructed* (nobody has a 6-passenger reservation
because the tool would have to allow it first); those are emitted as request-shaped cases.

Run inside the environment container, where the fixture lives:

    docker exec airline-workspace-1 python /artifact/tests/mine_cases.py --out /artifact/tests/cases.json

The output is committed so the test suite needs neither the container nor this script to run.
"""
from __future__ import annotations

import argparse
import json
import pathlib

DB_PATH = "/app/tasks/airline_rebooking/environment/db/db.json"

# The environment's fixed reference clock, and the cancellation window measured from it.
NOW = "2024-05-15T15:00:00"
WINDOW_START = "2024-05-14T15:00:00"

FLOWN_STATUSES = {"landed", "flying"}

# Free checked bags per passenger, by membership and cabin (policy.md "Checked bag allowance").
FREE_BAGS = {
    "regular": {"basic_economy": 0, "economy": 1, "business": 2},
    "silver": {"basic_economy": 1, "economy": 2, "business": 3},
    "gold": {"basic_economy": 2, "economy": 3, "business": 4},
}


def segment_statuses(db: dict, reservation: dict) -> list[str]:
    """Status of each segment of a reservation on its own date ("MISSING" if absent)."""
    out = []
    for f in reservation["flights"]:
        inst = db["flights"].get(f["flight_number"], {}).get("dates", {}).get(f["date"])
        out.append(inst["status"] if inst else "MISSING")
    return out


def _case(case_id, branch, db, reservation, outcome, user_turns, why,
          write_actions=None, forbidden_tools=None) -> dict:
    """Assemble one case, denormalising the facts a test needs to assert on."""
    user = db["users"][reservation["user_id"]]
    return {
        "id": case_id,
        "branch": branch,
        "why": why,
        "user_id": reservation["user_id"],
        "reservation_id": reservation["reservation_id"],
        "facts": {
            "membership": user["membership"],
            "cabin": reservation["cabin"],
            "insurance": reservation["insurance"],
            "passengers": len(reservation["passengers"]),
            "created_at": reservation["created_at"],
            "segment_statuses": segment_statuses(db, reservation),
            "total_baggages": reservation["total_baggages"],
            "nonfree_baggages": reservation["nonfree_baggages"],
            "free_bag_allowance": FREE_BAGS[user["membership"]][reservation["cabin"]]
                                  * len(reservation["passengers"]),
        },
        "user_turns": user_turns,
        "expect": {
            "outcome": outcome,
            "write_actions": write_actions or [],
            "forbidden_tools": forbidden_tools or [],
        },
    }


def _has_payment(db: dict, reservation: dict, source: str) -> bool:
    """Whether the reservation's owner holds a payment method of the given source."""
    methods = db["users"][reservation["user_id"]]["payment_methods"].values()
    return any(m["source"] == source for m in methods)


def _pick(db, predicate, limit):
    """First `limit` reservations satisfying `predicate`, in stable id order."""
    out = []
    for rid in sorted(db["reservations"]):
        r = db["reservations"][rid]
        if predicate(r):
            out.append(r)
            if len(out) == limit:
                break
    return out


def mine(db: dict, per_branch: int = 2) -> list[dict]:
    """Build the case list. Each branch contributes up to `per_branch` real reservations."""
    cases: list[dict] = []

    def add(branch, predicate, outcome, turns_fn, why, writes_fn=None, forbidden=None):
        for i, r in enumerate(_pick(db, predicate, per_branch)):
            cases.append(_case(
                f"{branch}_{i}", branch, db, r, outcome, turns_fn(r), why,
                write_actions=writes_fn(r) if writes_fn else None,
                forbidden_tools=forbidden,
            ))

    not_flown = lambda r: not (set(segment_statuses(db, r)) & FLOWN_STATUSES)

    # --- cancellation: eligible ---------------------------------------------------------------
    add(
        "cancel_within_24h",
        lambda r: r["created_at"] >= WINDOW_START and not_flown(r),
        "allowed",
        lambda r: [f"I need to cancel reservation {r['reservation_id']}, my user id is "
                   f"{r['user_id']}. My plans changed.", "yes"],
        "Booked within the last 24h of the reference clock, so cancellable whatever the cabin.",
        lambda r: [{"name": "cancel_reservation",
                    "arguments": {"reservation_id": r["reservation_id"]}}],
    )
    add(
        "cancel_business",
        lambda r: r["cabin"] == "business" and r["created_at"] < WINDOW_START and not_flown(r),
        "allowed",
        lambda r: [f"Please cancel {r['reservation_id']} for user {r['user_id']} - change of plan.",
                   "yes"],
        "Business cabin is cancellable regardless of when it was booked.",
        lambda r: [{"name": "cancel_reservation",
                    "arguments": {"reservation_id": r["reservation_id"]}}],
    )
    add(
        "cancel_airline_cancelled_flight",
        lambda r: "cancelled" in segment_statuses(db, r) and not_flown(r)
                  and r["created_at"] < WINDOW_START,
        "allowed",
        lambda r: [f"The airline cancelled my flight. Cancel {r['reservation_id']}, "
                   f"user {r['user_id']}.", "yes"],
        "A segment was cancelled by the airline, which makes the reservation cancellable.",
        lambda r: [{"name": "cancel_reservation",
                    "arguments": {"reservation_id": r["reservation_id"]}}],
    )

    # --- cancellation: refused or transferred -------------------------------------------------
    add(
        "cancel_flown_segment",
        lambda r: bool(set(segment_statuses(db, r)) & FLOWN_STATUSES),
        "transferred",
        lambda r: [f"Cancel {r['reservation_id']} please, user {r['user_id']}. Change of plan.",
                   "yes"],
        "Part of the trip has already flown, so policy requires a human handoff.",
        forbidden=["cancel_reservation"],
    )
    add(
        "cancel_insurance_reason_uncorroborated",
        lambda r: r["insurance"] == "yes" and r["cabin"] != "business"
                  and r["created_at"] < WINDOW_START and not_flown(r)
                  and "cancelled" not in segment_statuses(db, r),
        "transferred",
        lambda r: [f"I have insurance and I'm ill, so cancel {r['reservation_id']}. "
                   f"User {r['user_id']}.", "yes"],
        "Eligibility rests only on an unverifiable stated reason; TRUST_STATED_REASON is off, "
        "so the agent hands off instead of acting on it.",
        forbidden=["cancel_reservation"],
    )
    add(
        "cancel_no_grounds",
        lambda r: r["insurance"] == "no" and r["cabin"] != "business"
                  and r["created_at"] < WINDOW_START and not_flown(r)
                  and "cancelled" not in segment_statuses(db, r),
        "denied",
        lambda r: [f"I want to cancel {r['reservation_id']}, user {r['user_id']}. "
                   f"I just changed my mind.", "yes"],
        "No eligibility ground applies: outside 24h, not business, no insurance, nothing "
        "cancelled by the airline.",
        forbidden=["cancel_reservation"],
    )

    # --- modification -------------------------------------------------------------------------
    add(
        "modify_basic_economy_denied",
        lambda r: r["cabin"] == "basic_economy" and not_flown(r),
        "denied",
        lambda r: [f"Move reservation {r['reservation_id']} to a different day, "
                   f"user {r['user_id']}.", "yes"],
        "Basic economy flights cannot be changed.",
        forbidden=["update_reservation_flights"],
    )
    add(
        "modify_economy_allowed",
        lambda r: r["cabin"] == "economy" and not_flown(r) and len(r["flights"]) == 1
                  and _has_payment(db, r, "credit_card"),
        "allowed",
        lambda r: [f"I'd like to change the date of {r['reservation_id']}, user {r['user_id']}.",
                   "yes"],
        "Economy, nothing flown, single segment, and the owner has a usable card.",
    )

    # --- baggage ------------------------------------------------------------------------------
    add(
        "baggage_add_free_allowance",
        lambda r: not_flown(r) and r["total_baggages"]
                  < FREE_BAGS[db["users"][r["user_id"]]["membership"]][r["cabin"]]
                  * len(r["passengers"]),
        "allowed",
        lambda r: [f"Add a checked bag to {r['reservation_id']}, user {r['user_id']}.", "yes"],
        "Still inside the free allowance, so the extra bag must be added at no charge.",
    )

    # --- compensation -------------------------------------------------------------------------
    add(
        "compensation_eligible_cancelled",
        lambda r: "cancelled" in segment_statuses(db, r)
                  and (db["users"][r["user_id"]]["membership"] in {"silver", "gold"}
                       or r["insurance"] == "yes" or r["cabin"] == "business"),
        "allowed",
        lambda r: [f"My flight on {r['reservation_id']} was cancelled, user {r['user_id']}. "
                   f"Can I get any compensation?", "yes"],
        "Cancelled flight complaint from an eligible customer: $100 per passenger.",
        lambda r: [{"name": "send_certificate",
                    "arguments": {"user_id": r["user_id"],
                                  "amount": 100 * len(r["passengers"])}}],
    )
    add(
        "compensation_ineligible",
        lambda r: db["users"][r["user_id"]]["membership"] == "regular"
                  and r["insurance"] == "no"
                  and r["cabin"] in {"economy", "basic_economy"}
                  and "cancelled" in segment_statuses(db, r),
        "denied",
        lambda r: [f"My flight was cancelled on {r['reservation_id']}, user {r['user_id']}. "
                   f"I want compensation.", "yes"],
        "Regular member, no insurance, (basic) economy: explicitly not compensated.",
        forbidden=["send_certificate"],
    )
    add(
        "compensation_not_requested",
        lambda r: "cancelled" in segment_statuses(db, r)
                  and db["users"][r["user_id"]]["membership"] == "gold",
        "allowed",
        lambda r: [f"Just checking the status of {r['reservation_id']}, user {r['user_id']}."],
        "An eligible customer who never asked for compensation must not be offered any.",
        forbidden=["send_certificate"],
    )

    # --- the customer is confidently wrong (NOTES.md: users are usually wrong) ------------------
    add(
        "user_wrong_about_allowance",
        lambda r: db["users"][r["user_id"]]["membership"] == "gold"
                  and r["cabin"] == "economy" and not_flown(r),
        "denied",
        lambda r: [f"I'm gold so I get 4 free checked bags in economy on {r['reservation_id']}, "
                   f"user {r['user_id']}. Add 4 free bags.", "yes"],
        "Gold in economy gets 3 free bags per passenger, not 4; the agent must correct this.",
    )
    add(
        "user_wrong_about_cancellability",
        lambda r: r["cabin"] == "basic_economy" and r["insurance"] == "no"
                  and r["created_at"] < WINDOW_START and not_flown(r)
                  and "cancelled" not in segment_statuses(db, r),
        "denied",
        lambda r: [f"I'm entitled to a free cancellation on {r['reservation_id']}, "
                   f"user {r['user_id']}. Cancel it and refund me.", "yes"],
        "The customer asserts an entitlement they do not have; the agent must say no and explain.",
        forbidden=["cancel_reservation"],
    )

    cases.extend(_constructed(db))
    return cases


def _constructed(db: dict) -> list[dict]:
    """Negative cases no real record can exhibit, since the tool would have to permit them first."""
    out = []

    six_pax = [{"first_name": "Test", "last_name": f"Passenger{i}", "dob": "1990-01-01"}
               for i in range(6)]
    user_id = sorted(db["users"])[0]
    out.append({
        "id": "book_six_passengers",
        "branch": "book_too_many_passengers",
        "why": "A reservation may hold at most five passengers; the tool does not check this.",
        "user_id": user_id,
        "reservation_id": None,
        "facts": {"passengers": 6},
        "user_turns": [f"I want to book one flight for six people, user {user_id}.", "yes"],
        "expect": {"outcome": "denied", "write_actions": [],
                   "forbidden_tools": ["book_reservation"],
                   "constructed_arguments": {"passengers": six_pax}},
    })

    reservation = _pick(db, lambda r: r["total_baggages"] >= 2, 1)[0]
    out.append({
        "id": "baggage_decrease",
        "branch": "baggage_decrease_denied",
        "why": "Checked bags may be added but never removed; the tool would happily decrease them.",
        "user_id": reservation["user_id"],
        "reservation_id": reservation["reservation_id"],
        "facts": {"total_baggages": reservation["total_baggages"]},
        "user_turns": [f"Remove a checked bag from {reservation['reservation_id']}, "
                       f"user {reservation['user_id']}.", "yes"],
        "expect": {"outcome": "denied", "write_actions": [],
                   "forbidden_tools": ["update_reservation_baggages"]},
    })

    reservation = _pick(db, lambda r: len(r["passengers"]) == 1, 1)[0]
    out.append({
        "id": "passenger_count_change",
        "branch": "passenger_count_immutable",
        "why": "The passenger count cannot change -- not even a human agent can do it, so this is "
               "a refusal rather than a transfer.",
        "user_id": reservation["user_id"],
        "reservation_id": reservation["reservation_id"],
        "facts": {"passengers": 1},
        "user_turns": [f"Add my partner to {reservation['reservation_id']}, "
                       f"user {reservation['user_id']}.", "yes"],
        "expect": {"outcome": "denied", "write_actions": [],
                   "forbidden_tools": ["update_reservation_passengers"]},
    })

    reservation = _pick(db, lambda r: r["cabin"] == "economy" and len(r["flights"]) == 1, 1)[0]
    out.append({
        "id": "change_origin_denied",
        "branch": "origin_destination_immutable",
        "why": "A modification may not change origin, destination or trip type.",
        "user_id": reservation["user_id"],
        "reservation_id": reservation["reservation_id"],
        "facts": {"origin": reservation["origin"], "destination": reservation["destination"]},
        "user_turns": [f"Change {reservation['reservation_id']} to depart from a different city, "
                       f"user {reservation['user_id']}.", "yes"],
        "expect": {"outcome": "denied", "write_actions": [],
                   "forbidden_tools": ["update_reservation_flights"]},
    })

    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH, help="path to the fixture db.json")
    parser.add_argument("--out", required=True, help="where to write cases.json")
    parser.add_argument("--per-branch", type=int, default=2,
                        help="how many real reservations to take per branch")
    args = parser.parse_args()

    db = json.loads(pathlib.Path(args.db).read_text())
    cases = mine(db, args.per_branch)

    branches = sorted({c["branch"] for c in cases})
    pathlib.Path(args.out).write_text(json.dumps(
        {"reference_time": NOW, "branches": branches, "cases": cases}, indent=2) + "\n")

    print(f"wrote {len(cases)} cases across {len(branches)} branches to {args.out}")
    for b in branches:
        print(f"  {sum(1 for c in cases if c['branch'] == b)}x {b}")


if __name__ == "__main__":
    main()
