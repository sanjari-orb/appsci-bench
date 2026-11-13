#!/usr/bin/env python
"""End-to-end harness: drive the real agent against an isolated copy of the environment.

This is the layer that answers "does the assembled agent reach the right outcome on a real
conversation", which the pure unit tests cannot. It needs an ANTHROPIC_API_KEY, so it is not part
of the default test run -- it is a tool to invoke deliberately.

The live database is never touched. `AirlineEnvironment.from_fixture().fresh()` is a deepcopy, and
the service is served in-process over an ASGI transport, so there is no socket and no container
involved. Every run also asserts the state hash, which is the cheapest possible catch-all for "the
agent wrote something nobody expected".

Run inside the environment container (where the environment package and fixture live):

    docker run --rm -v "$PWD":/artifact -w /artifact \
      -e PYTHONPATH=/app:/artifact -e ANTHROPIC_API_KEY=... \
      <test-image> python tests/harness.py --case cancel_within_24h_0

    ... --all          run every mined case
    ... --branch cancel_flown_segment
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent import Agent  # noqa: E402
from env_client import EnvClient  # noqa: E402

CASES_PATH = pathlib.Path(__file__).resolve().parent / "cases.json"

# Read-only outcomes: the database must be byte-identical afterwards.
NO_WRITE_OUTCOMES = {"denied", "transferred"}


def run_case(case: dict, verbose: bool = True) -> dict:
    """Drive one case's scripted turns through the agent and check the resulting state."""
    os.environ.setdefault("AIRLINE_ENV_TOKEN", "harness-token")
    os.environ.setdefault("AIRLINE_ENV_ADMIN_TOKEN", "harness-admin")

    from tasks.airline_rebooking.environment import AirlineEnvironment
    from tasks.airline_rebooking.environment.service import create_app

    base = AirlineEnvironment.from_fixture()
    app = create_app(base_env=base)
    live = app.state.env                      # the isolated per-run copy the tools mutate
    before_hash = live.state_hash()

    # TestClient is a synchronous client over an ASGI app: no socket, no container, no live DB.
    client = TestClient(app)
    env_client = EnvClient(base_url="", token=os.environ["AIRLINE_ENV_TOKEN"], client=client)
    agent = Agent(env=env_client)

    messages: list[dict] = []
    transcript = []
    for turn in case["user_turns"]:
        messages.append({"role": "user", "content": turn})
        reply = agent.run(messages)
        messages.append({"role": "assistant", "content": reply})
        transcript.append((turn, reply))
        if verbose:
            print(f"  customer: {turn}")
            print(f"  agent:    {reply}\n")

    after_hash = live.state_hash()
    expected = case["expect"]
    problems = []

    # 1. Nothing forbidden may have been written.
    reservation_id = case.get("reservation_id")
    reservation = live.db.reservations.get(reservation_id) if reservation_id else None
    if "cancel_reservation" in expected["forbidden_tools"]:
        if reservation and reservation.get("status") == "cancelled":
            problems.append("reservation was cancelled but cancellation was forbidden")

    # 2. A denied or transferred request must leave the database untouched.
    if expected["outcome"] in NO_WRITE_OUTCOMES and after_hash != before_hash:
        problems.append("database changed on a request that should not have written anything")

    # 3. An expected write must actually have happened.
    for action in expected["write_actions"]:
        if action["name"] == "cancel_reservation":
            if not (reservation and reservation.get("status") == "cancelled"):
                problems.append("expected the reservation to be cancelled, but it was not")

    # 4. A transfer must say the exact required words.
    if expected["outcome"] == "transferred":
        last = transcript[-1][1] if transcript else ""
        if "YOU ARE BEING TRANSFERRED TO A HUMAN AGENT" not in last.upper():
            problems.append("expected the transfer message, which was not sent")

    return {
        "id": case["id"],
        "branch": case["branch"],
        "outcome_expected": expected["outcome"],
        "state_changed": after_hash != before_hash,
        "problems": problems,
        "passed": not problems,
        "transcript": transcript,
    }


def main() -> None:
    """Select cases from cases.json, run them, and report a pass/fail summary."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", help="run a single case by id")
    parser.add_argument("--branch", help="run every case of one branch")
    parser.add_argument("--all", action="store_true", help="run every case")
    parser.add_argument("--quiet", action="store_true", help="summary only")
    args = parser.parse_args()

    cases = json.loads(CASES_PATH.read_text())["cases"]
    if args.case:
        selected = [c for c in cases if c["id"] == args.case]
    elif args.branch:
        selected = [c for c in cases if c["branch"] == args.branch]
    elif args.all:
        selected = cases
    else:
        parser.error("choose --case, --branch or --all")
        return

    if not selected:
        parser.error("no matching cases")

    results = []
    for case in selected:
        if not args.quiet:
            print(f"\n=== {case['id']} ({case['branch']}) -- expect "
                  f"{case['expect']['outcome']} ===")
            print(f"    {case['why']}")
        results.append(run_case(case, verbose=not args.quiet))

    print("\n--- summary ---")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{status}  {r['id']:<40} expect={r['outcome_expected']:<12} "
              f"wrote={r['state_changed']}")
        for problem in r["problems"]:
            print(f"      - {problem}")
    passed = sum(1 for r in results if r["passed"])
    print(f"\n{passed}/{len(results)} cases passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
