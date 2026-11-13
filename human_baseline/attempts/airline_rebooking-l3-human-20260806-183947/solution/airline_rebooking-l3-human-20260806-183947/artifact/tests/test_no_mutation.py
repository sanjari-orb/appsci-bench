"""Assert that requests which should not write anything really do not write anything.

This is the cheap catch-all for the failure the KPIs care most about: the agent quietly changing a
record nobody asked it to change. It runs against an isolated in-process copy of the real
environment when that package is importable (inside the environment container), and is skipped
elsewhere so the default suite stays runnable anywhere.

No LLM is involved: the scripted tool calls stand in for a model's choices, so the assertions are
about the guard and the loop rather than about a model's judgement.
"""
from __future__ import annotations

import json
import pathlib

import pytest

policy = pytest.importorskip("policy")
env_module = pytest.importorskip(
    "tasks.airline_rebooking.environment",
    reason="the environment package is only importable inside the task container",
)

from fastapi.testclient import TestClient  # noqa: E402

from env_client import EnvClient  # noqa: E402

CASES = json.loads(
    (pathlib.Path(__file__).resolve().parent / "cases.json").read_text())["cases"]


@pytest.fixture()
def isolated():
    """An isolated environment copy plus a client pointed at it. Never the live database."""
    import os

    os.environ.setdefault("AIRLINE_ENV_TOKEN", "test-token")
    os.environ.setdefault("AIRLINE_ENV_ADMIN_TOKEN", "test-admin")
    from tasks.airline_rebooking.environment.service import create_app

    base = env_module.AirlineEnvironment.from_fixture()
    app = create_app(base_env=base)
    client = EnvClient(base_url="", token=os.environ["AIRLINE_ENV_TOKEN"],
                       client=TestClient(app))
    return app.state.env, client


def _context(client: EnvClient, reservation_id: str) -> policy.Context:
    """Build a guard context from the isolated environment, as the agent does at runtime."""
    reservation = client.get_reservation(reservation_id)
    user = client.get_user(reservation["user_id"])
    statuses = {}
    for flight in reservation["flights"]:
        key = (flight["flight_number"], flight["date"])
        statuses[key] = client.get_flight_status(*key)
    return policy.Context(reservation=reservation, user=user, flight_statuses=statuses)


def _cases_expecting_no_write():
    """Mined cases whose outcome forbids any database change."""
    return [c for c in CASES
            if c["expect"]["outcome"] in {"denied", "transferred"} and c.get("reservation_id")]


def test_reads_never_change_state(isolated):
    """Establishing the facts must be free of side effects."""
    env, client = isolated
    before = env.state_hash()
    reservation = client.get_reservation(CASES[0]["reservation_id"])
    client.get_user(reservation["user_id"])
    for flight in reservation["flights"]:
        client.get_flight_status(flight["flight_number"], flight["date"])
    client.call("list_all_airports", {})
    assert env.state_hash() == before


@pytest.mark.parametrize("case", _cases_expecting_no_write(),
                         ids=lambda c: c["id"])
def test_guard_blocks_the_writes_these_cases_forbid(isolated, case):
    """For every case whose answer is no, the guard must block the write it forbids.

    The check is run against real records fetched from an isolated copy, and the state hash is
    asserted unchanged afterwards -- so a rule that silently allowed the write would fail here.
    """
    env, client = isolated
    before = env.state_hash()
    ctx = _context(client, case["reservation_id"])

    for tool in case["expect"]["forbidden_tools"]:
        arguments = _forbidden_arguments(tool, case, ctx)
        if arguments is None:
            continue
        violation = policy.check(tool, arguments, ctx)
        assert violation is not None, (
            f"{case['id']}: {tool} should have been blocked ({case['why']})")

    assert env.state_hash() == before, "judging a request must not change the database"


def _forbidden_arguments(tool: str, case: dict, ctx: policy.Context) -> dict | None:
    """The call this case says must be refused, shaped as the model would emit it."""
    reservation = ctx.reservation
    if tool == "cancel_reservation":
        return {"reservation_id": case["reservation_id"]}
    if tool == "update_reservation_flights":
        return {
            "reservation_id": case["reservation_id"],
            "cabin": reservation["cabin"],
            # A different date is the minimal "changed flights" request.
            "flights": [{"flight_number": f["flight_number"], "date": "2024-05-25"}
                        for f in reservation["flights"]],
            "payment_id": next(iter((ctx.user or {}).get("payment_methods", {})), "none"),
        }
    if tool == "update_reservation_baggages":
        return {
            "reservation_id": case["reservation_id"],
            "total_baggages": max(0, reservation["total_baggages"] - 1),
            "nonfree_baggages": reservation["nonfree_baggages"],
            "payment_id": next(iter((ctx.user or {}).get("payment_methods", {})), "none"),
        }
    if tool == "update_reservation_passengers":
        extra = dict(reservation["passengers"][0])
        return {"reservation_id": case["reservation_id"],
                "passengers": reservation["passengers"] + [extra]}
    if tool == "send_certificate":
        return {"user_id": case["user_id"], "amount": 100}
    return None


def test_a_permitted_cancellation_is_not_blocked(isolated):
    """The suite must also prove the guard is not simply refusing everything."""
    env, client = isolated
    allowed = [c for c in CASES if c["branch"] == "cancel_within_24h"]
    assert allowed, "expected the miner to find a cancellable reservation"
    for case in allowed:
        ctx = _context(client, case["reservation_id"])
        assert policy.check("cancel_reservation",
                            {"reservation_id": case["reservation_id"]}, ctx) is None
