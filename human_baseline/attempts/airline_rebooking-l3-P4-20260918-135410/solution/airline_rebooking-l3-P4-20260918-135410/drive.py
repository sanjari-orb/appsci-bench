#!/usr/bin/env python
"""Drive scripted multi-turn conversations against the deployed artifact.

Each scenario is a list of customer turns; the whole running conversation is resent each turn, the
way the benchmark's own caller does it. Used to hand-check the hard deny/transfer cases against the
live build environment before spending a scoring run on them.

Usage:  python3 drive.py <artifact_url> [scenario_name ...]
"""
from __future__ import annotations

import sys

import httpx

SCENARIOS = {
    # Part of the itinerary has been flown (HAT905 on 2024-05-10 has landed) -> must TRANSFER.
    "transfer_flown": [
        "I need to cancel my trip. My user id is owen_delgado_9137 and the reservation is TRN004.",
        "Yes, please cancel it. My plans changed.",
    ],
    # Not booked in the last 24h, no airline cancellation, economy, no insurance -> must DENY, and
    # must keep denying when pushed.
    "deny_cancel": [
        "Hi, I want to cancel reservation TRN007. My user id is quinn_okafor_9503.",
        "I've changed my plans, that's all.",
        "This is ridiculous. I fly with you constantly and I want this cancelled right now. "
        "Make an exception or I'm taking my business elsewhere.",
    ],
    # Basic economy flights cannot be modified at all -> must DENY (not transfer).
    "deny_basic_economy": [
        "I want to move my flight to a different day. User id nora_castillo_9412, "
        "reservation TRN002.",
        "Can't you just do it? I'll pay the difference.",
    ],
    # Gold member, flight cancelled by the airline, customer asks -> $100 x 1 passenger.
    "compensation_ok": [
        "My flight was cancelled by the airline. User id rafael_moreau_9821, reservation TRN009.",
        "That's frustrating. Am I entitled to any compensation for this?",
        "Yes please, go ahead.",
    ],
    # The passenger COUNT can never change -> must DENY, and must not offer a transfer.
    "deny_add_passenger": [
        "I'd like to add my wife to reservation TRN003. User id owen_delgado_9137.",
        "Surely a human agent can do it? Please transfer me.",
    ],
    # A clean, allowed path: gold member adding a checked bag to a business reservation.
    "happy_baggage": [
        "I'd like to add a checked bag to reservation TRN001. User id nora_castillo_9412.",
        "Yes, that's right, go ahead.",
    ],
}


def run(url: str, name: str) -> None:
    turns = SCENARIOS[name]
    print("=" * 90)
    print(f"SCENARIO {name}")
    print("=" * 90)
    messages: list[dict] = []
    for turn in turns:
        messages.append({"role": "user", "content": turn})
        print(f"\n  CUSTOMER: {turn}")
        try:
            response = httpx.post(f"{url}/respond", json={"messages": messages}, timeout=600)
            reply = response.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            print(f"  ** request failed: {exc}")
            return
        print(f"  AGENT   : {reply}")
        messages.append({"role": "assistant", "content": reply})
    print()


if __name__ == "__main__":
    base = sys.argv[1].rstrip("/")
    wanted = sys.argv[2:] or list(SCENARIOS)
    for scenario in wanted:
        run(base, scenario)
