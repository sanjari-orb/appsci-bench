"""The airline BUILD-phase seed store must be loadable, closed, disjoint and complete.

WHY THIS FILE EXISTS AT ALL, given docs/task_env_carve_spec.md says a task owner writes data and scripts
and never test code. That principle rests on the repo-wide guard in test_carve_guards.py -- which decides
a task has split by comparing its two phases' declared sources, and SKIPS every task whose phases still
point at the same data. This PR ships the build store without wiring the phase declarations, so airline is
still "not split" as far as that guard can tell, and the guard skips it. Without this file nothing checks
the authored data at all.

So the checks that CAN be shared are shared: closure and coverage call the guard's own
`check_closure`/`check_coverage`, and disjointness uses its `identifier_tokens`. What is local is only what
the guard cannot express yet -- that the store loads through the real environment, that every tool works
against it, and that it avoids the two id reservoirs the tools mint from.

When the phase declarations do get wired, the guard starts covering this task and the overlapping checks
here become redundant. Delete them then; keep the tool-exercise and reservoir ones, which are about the
service rather than the carve.
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import pathlib
import sys as _sys

import pytest

from appsci_bench.core.spec import TASKS_DIR
from tasks.airline_rebooking.environment import AirlineEnvironment
from tasks.airline_rebooking.environment.db import AirlineDB
from tasks.airline_rebooking.environment.tools import ToolError

# The carve guard lives in `scripts/` -- a runnable tool, not part of the importable package -- so it is
# loaded by path. Copied from tests/appsci_bench/core/test_carve_guards.py rather than invented, so both
# files find it the same way. (It moved there mid-review; importing it as `appsci_bench.core.carve_guard`
# passed locally against the older base and failed on the merged tree, which is why the path is derived
# from the repo root rather than assumed.)
_GUARD = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "carve_guard.py"
_spec = _ilu.spec_from_file_location("_carve_guard_for_build_seed", _GUARD)
cg = _ilu.module_from_spec(_spec)
# Registered before exec: the module defines a dataclass, and @dataclass resolves type hints through
# sys.modules[cls.__module__], which raises if the module is absent from it.
_sys.modules[_spec.name] = cg
_spec.loader.exec_module(cg)

_TASK = TASKS_DIR / "airline_rebooking"
_BUILD_DB = _TASK / "environment" / "build" / "data" / "db.json"
# The EVAL store, which is where the full fixture now lives: the carve moved it out of
# `environment/db/` so that directory holds no data at all and each phase owns its own.
_EVAL_DB = _TASK / "environment" / "eval" / "data" / "db.json"

#: Ids the tools mint at runtime (see tools.py's two fixed reservoirs). A seed holding one of these
#: silently changes which id a live booking or certificate receives, which breaks golden-action replay --
#: a failure with no error anywhere, which is why it is worth a test of its own.
_MINTED_RESERVATIONS = {"HATHAT", "HATHAU", "HATHAV"}
_MINTED_CERTIFICATES = {"certificate_3221322", "certificate_3221323", "certificate_3221324"}


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def build() -> dict:
    return _load(_BUILD_DB)


@pytest.fixture(scope="module")
def full() -> dict:
    return _load(_EVAL_DB)


def _as_records(store: dict) -> dict[str, list[dict]]:
    """The shape the carve guard's pure checks take: collection name -> list of records."""
    return {name: list(coll.values()) for name, coll in store.items()}


# --- it exists, and it is small on purpose -----------------------------------------------------

def test_the_build_seed_is_present_and_small(build: dict) -> None:
    assert set(build) == {"flights", "users", "reservations"}
    assert len(build["users"]) == 5
    assert len(build["flights"]) == 10
    assert len(build["reservations"]) == 10


def test_the_seed_loads_through_the_real_environment(build: dict) -> None:
    """`AirlineDB.load` reads only db.json, so this is the file that has to work."""
    env = AirlineEnvironment.from_fixture(_BUILD_DB)
    assert len(env.db.users) == 5
    # `fresh()` is the per-case isolation primitive the service resets with; a store that cannot
    # round-trip through it would break between-case resets rather than failing at load.
    assert env.fresh().state_hash() == env.state_hash()


# --- the three carve invariants, via the guard's own functions ----------------------------------

def test_the_seed_is_referentially_closed(build: dict, full: dict) -> None:
    result = cg.check_closure(_as_records(build), _as_records(full))
    assert result.ok, str(result)


def test_the_seed_covers_every_category_the_full_store_enumerates(build: dict, full: dict) -> None:
    """A small store's likeliest defect: the agent builds against a world missing a category it is
    scored on. Coverage is asserted against the EVAL store's enumerations, not a hand-written list, so
    a category added upstream is covered here without this test being edited."""
    result = cg.check_coverage(_as_records(build), _as_records(full))
    assert result.ok, str(result)


def test_no_eval_identifier_appears_in_the_build_seed(build: dict, full: dict) -> None:
    """The leak the split exists to close, checked on the record keys directly.

    The guard's own disjointness check reads the phase sources out of the spec, which this PR has not
    wired, so it cannot run yet -- but the property it asserts is checkable here without it.
    """
    for collection in ("flights", "users", "reservations"):
        clash = set(build[collection]) & set(full[collection])
        assert not clash, f"{collection}: build seed reuses eval ids {sorted(clash)[:5]}"

    build_pay = {pid for u in build["users"].values() for pid in u["payment_methods"]}
    eval_pay = {pid for u in full["users"].values() for pid in u["payment_methods"]}
    assert not (build_pay & eval_pay), "payment ids collide with the eval store"


def test_no_token_from_an_eval_case_appears_in_the_build_seed(build: dict) -> None:
    """Stronger than key disjointness: an identifier an eval case NAMES must not appear anywhere in the
    build store, including inside a nested field. Uses the guard's own token heuristic so both agree on
    what counts as an identifier."""
    eval_tokens: set[str] = set()
    for path in cg.eval_case_files("airline_rebooking", TASKS_DIR):
        eval_tokens |= cg.identifier_tokens(path.read_text())
    assert eval_tokens, "no identifier tokens extracted from the eval cases -- the check would be vacuous"

    blob = json.dumps(build)
    leaked = sorted(t for t in eval_tokens if t in blob)
    assert not leaked, f"build seed contains identifiers the eval cases use: {leaked[:10]}"


def test_the_seed_avoids_the_ids_the_tools_mint(build: dict) -> None:
    assert not set(build["reservations"]) & _MINTED_RESERVATIONS
    pay = {pid for u in build["users"].values() for pid in u["payment_methods"]}
    assert not pay & _MINTED_CERTIFICATES


# --- usability: the store must not merely be well-shaped ---------------------------------------

def test_every_bookable_instance_carries_all_three_cabins(build: dict) -> None:
    """`tools.py` subscripts `inst["prices"][cabin]` bare, so a missing cabin is a KeyError the service
    turns into an HTTP 500 -- not a clean refusal the agent can read."""
    cabins = {"basic_economy", "economy", "business"}
    for number, flight in build["flights"].items():
        for date, inst in flight["dates"].items():
            if inst["status"] != "available":
                continue
            for field in ("prices", "available_seats"):
                assert set(inst[field]) == cabins, f"{number} {date}: {field} is {sorted(inst[field])}"


def test_no_reservation_is_pre_seeded_with_a_status(build: dict) -> None:
    """`status` is what `cancel_reservation` adds; pre-seeding it would make a live cancellation
    indistinguishable from the initial state."""
    for rid, r in build["reservations"].items():
        assert "status" not in r, f"reservations[{rid}] carries a status key"


def test_fares_and_payment_amounts_are_ints(build: dict) -> None:
    """`book_reservation` compares the payment total with exact equality, so a float fare makes the
    correct payment a float and moves the DB-state hash even when the arithmetic is right."""
    for rid, r in build["reservations"].items():
        for seg in r["flights"]:
            assert isinstance(seg["price"], int), f"reservations[{rid}]: fare {seg['price']!r}"
        for p in r["payment_history"]:
            assert isinstance(p["amount"], int), f"reservations[{rid}]: amount {p['amount']!r}"


def test_every_tool_succeeds_against_the_seed(build: dict) -> None:
    """Each of the 14 tools must actually SUCCEED, not merely avoid crashing.

    Accepting a `ToolError` here would let a store where nothing works pass: every call below is aimed at
    records authored to satisfy it, so a refusal means the data stopped supporting the operation. This is
    also the only check that would catch a bookable instance missing a cabin, which is a 500 rather than a
    schema error.
    """
    base = AirlineEnvironment(AirlineDB(build["flights"], build["users"], build["reservations"]))
    uid, rid, card = "nora_castillo_9412", "TRN001", "credit_card_50000001"
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
                                         "nonfree_baggages": 1, "payment_id": card}),
        ("update_reservation_passengers", {"reservation_id": rid, "passengers": [
            {"dob": "1986-03-14", "first_name": "Nora", "last_name": "Castillo"}]}),
        ("update_reservation_flights", {"reservation_id": rid, "cabin": "business", "flights": [
            {"flight_number": "HAT901", "date": "2024-05-20"},
            {"flight_number": "HAT902", "date": "2024-05-26"}], "payment_id": card}),
        ("cancel_reservation", {"reservation_id": rid}),
        ("send_certificate", {"user_id": uid, "amount": 100}),
        ("book_reservation", {
            "user_id": uid, "origin": "ORD", "destination": "DEN", "flight_type": "one_way",
            "cabin": "economy", "flights": [{"flight_number": "HAT906", "date": "2024-05-19"}],
            "passengers": [{"dob": "1986-03-14", "first_name": "Nora", "last_name": "Castillo"}],
            "payment_methods": [{"payment_id": "gift_card_50000002", "amount": 150}],
            "total_baggages": 1, "nonfree_baggages": 0, "insurance": "no"}),
    ]
    assert len(calls) == 14, "every tool must be exercised"
    for name, args in calls:
        env = base.fresh()          # fresh per call, so one write cannot disturb the next
        try:
            env.tools.execute(name, args)
        except ToolError as exc:                       # a refusal means the data stopped supporting it
            pytest.fail(f"{name} refused against the build seed: {exc}")
        except Exception as exc:                       # noqa: BLE001 -- malformed data -> HTTP 500
            pytest.fail(f"{name} raised {type(exc).__name__}: {exc}")


def test_a_two_leg_itinerary_is_constructible(build: dict) -> None:
    """`search_onestop_flight` is a stub aliasing the direct search, so a connection is only findable by
    two `search_direct_flight` calls through the hub. If no such pair exists the store quietly cannot
    express a connecting trip at all."""
    env = AirlineEnvironment.from_fixture(_BUILD_DB)
    first = env.tools.execute("search_direct_flight",
                              {"origin": "SFO", "destination": "ORD", "date": "2024-05-20"})
    second = env.tools.execute("search_direct_flight",
                               {"origin": "ORD", "destination": "JFK", "date": "2024-05-20"})
    assert first and second, "no SFO->ORD->JFK connection available in the build seed"


# --- the agent-facing description must hold in BOTH phases -------------------------------------

def test_the_agent_facing_store_description_states_no_record_counts(build: dict, full: dict) -> None:
    """A description the agent reads must be true of the environment the agent actually meets.

    The agent explores the BUILD environment; the artifact is scored against the EVAL one. Any record
    count in an agent-facing description is therefore false in one of the two phases. This caught a real
    defect: the `airline_db` resource read "300 flights, 500 users, 2000 reservations", which was accurate
    only while both phases loaded the same store and describes an environment the agent never sees once
    the build phase loads its own seed.

    Checked against BOTH stores' counts rather than a hardcoded list, so re-sizing either one keeps the
    guard honest. Naming the build volume instead would be equally wrong at eval time -- and would invite
    an artifact built for a tiny store, since a full-table scan per request survives 10 reservations and
    not 2000.
    """
    import yaml

    from appsci_bench.core.spec import build_agent_spec, resolve_spec_path

    raw = yaml.safe_load(resolve_spec_path("airline_rebooking").read_text())
    view = build_agent_spec(raw)

    counts = {str(len(coll)) for store in (build, full) for coll in store.values()}
    assert counts, "no counts derived -- the check would be vacuous"

    agent_facing = [
        *(r.get("description", "") for r in view.get("environment", {}).get("resources", [])),
        *(d.get("description", "") for d in view.get("dataset_files", [])),
        *(d.get("description", "") for d in view.get("technical_docs", [])),
        view.get("description", ""),
        view.get("interface_design", ""),
    ]
    for text in agent_facing:
        leaked = sorted(c for c in counts if c in (text or ""))
        assert not leaked, (
            f"agent-facing text states record counts {leaked}, which hold in at most one phase: {text!r}")


def test_the_agent_is_told_to_ask_for_ids_but_never_given_one(build: dict, full: dict) -> None:
    """The spec must say where an id comes from, and must not be the place one comes from.

    Both halves matter and they pull against each other. Nothing in this environment enumerates users
    or reservations -- every read tool takes an id the caller already holds, setup.sh deletes the raw
    fixture once loaded, and the load/state routes are admin-gated -- so an agent with no id cannot
    call a single read tool. Told nothing, it fills the gap from its own memory: two live runs fell
    back on ids from the public tau-bench dataset (all 404 against this carve) and then on invented
    ones, reading the resulting emptiness as a broken environment.

    But the ids themselves belong to the stakeholders, because eliciting them is the work being
    measured. Naming one here would hand the agent for free what the elicitation score exists to
    assess. So: say that asking is the route, and name nothing.
    """
    import yaml

    from appsci_bench.core.spec import build_agent_spec, resolve_spec_path

    raw = yaml.safe_load(resolve_spec_path("airline_rebooking").read_text())
    view = build_agent_spec(raw)

    agent_facing = [
        *(r.get("description", "") for r in view.get("environment", {}).get("resources", [])),
        *(d.get("description", "") for d in view.get("dataset_files", [])),
        view.get("description", ""),
        view.get("interface_design", ""),
    ]
    blob = " ".join(t or "" for t in agent_facing)

    assert "ask the stakeholders" in blob.lower(), (
        "the agent-facing spec never says asking is how to get a record id, so an agent that needs "
        "one has no sanctioned route to it")

    keys = {k for store in (build, full) for coll in store.values() for k in coll}
    assert keys, "no record keys derived -- the check would be vacuous"
    leaked = sorted(k for k in keys if k in blob)
    assert not leaked, (
        f"agent-facing text names record ids {leaked}; those are the stakeholders' to give when "
        "asked, and naming them here skips the elicitation being measured")
