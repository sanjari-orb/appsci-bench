"""The read-log allowlist must be applied on BOTH sides, or a perfect agent scores zero.

Banking's discoverable-tool mechanic logs a call into the HASHED `agent_discoverable_tools` table when
the underlying tool mutates state OR when its name is in the per-case read-log allowlist. That
allowlist is what restores upstream's discrimination: an agent that reaches the right end state
without doing the reads the task required lands on a different hash and scores 0.

The hazard is asymmetry. If the allowlist is set on the golden replay but not on the live environment
(or vice versa), one side logs required reads the other never does, so IDENTICAL behaviour hashes
differently and a flawless agent scores 0 -- a harness bug wearing an agent failure's clothes. 51 of
the 88 DB-basis cases carry a non-empty allowlist, so this is the common path, not a corner.

These tests pin the three things that keep it safe:
  1. both sides derive the allowlist from ONE shared function, so they agree by construction;
  2. a golden trajectory replayed the way the live env replays it scores 1.0 (the false-negative
     guard -- this is the test that would have caught a one-sided wiring);
  3. when the live env did NOT apply the same allowlist, `task_success` refuses to score (None)
     instead of blaming the agent for a mismatch the harness caused.

Creds-free: real BankingEnvironment, real sample oracles, no LLM and no Docker.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn
from tasks.banking_support import eval as bk
from tasks.banking_support.environment import BankingEnvironment
from tasks.banking_support.environment.service import create_app

_EVAL_DATA = pathlib.Path(__file__).resolve().parents[3] / "tasks" / "banking_support" / "eval_data"

#: A real DB-basis case whose golden trajectory calls a NON-mutating discoverable tool, so its
#: allowlist is non-empty and the symmetry actually matters.
_CASE = "tau2_banking_100"


def _oracle(dpid: str) -> dict:
    raw = json.loads((_EVAL_DATA / dpid / "sample.json").read_text())
    return json.loads(raw["expected_output"]["json"])


def _expected_of(dpid: str) -> ArtifactAgentOutput:
    raw = json.loads((_EVAL_DATA / dpid / "sample.json").read_text())
    return ArtifactAgentOutput(json=raw["expected_output"]["json"])


def _turn(text: str) -> ConversationTurn:
    return ConversationTurn(query=ArtifactAgentQuery(text="customer message"),
                            response=ArtifactAgentOutput(text=text))


@pytest.fixture(scope="module")
def base_env() -> BankingEnvironment:
    return BankingEnvironment.from_fixture()


def _live_run_hash(base: BankingEnvironment, oracle: dict, *, allowlist) -> str:
    """The hash a PERFECT agent leaves behind: the live env is reset, given `allowlist`, seeded, and
    then driven through exactly the golden trajectory. This mirrors what the task-env service does
    per case (POST /admin/reset -> POST /admin/seed -> the artifact's tool calls)."""
    env = base.fresh()
    env.set_read_log_allowlist(allowlist)
    env.seed(oracle.get("initial_state"))
    env.apply_actions(oracle.get("actions") or [])
    return env.state_hash()


# --------------------------------------------------------------------------- the shared derivation

def test_the_case_actually_exercises_a_non_empty_allowlist(base_env) -> None:
    """Without this, every assertion below would pass vacuously on an empty allowlist."""
    reads = bk._required_reads(_oracle(_CASE)["actions"])
    assert reads, f"{_CASE} no longer has required reads -- pick another case for this file"
    assert all(isinstance(r, str) for r in reads)


def test_both_sides_use_one_derivation(base_env) -> None:
    """The eval helper and the env method are the same function, so they cannot drift apart."""
    actions = _oracle(_CASE)["actions"]
    assert bk._required_reads(actions) == base_env.read_log_allowlist_for(actions)


def test_derivation_keeps_only_non_mutating_discoverable_reads(base_env) -> None:
    """Mutating discoverable tools are logged regardless, so allowlisting them would be a no-op; and
    only `call_discoverable_agent_tool` actions name a discoverable tool at all."""
    discoverable = base_env.agent_tools.get_discoverable_tools()
    reads = base_env.read_log_allowlist_for(_oracle(_CASE)["actions"])
    for name in reads:
        assert name in discoverable
        assert not getattr(discoverable[name], "__mutates_state__", False)


def test_derivation_ignores_unrelated_and_unknown_actions(base_env) -> None:
    assert base_env.read_log_allowlist_for([]) == []
    assert base_env.read_log_allowlist_for([{"name": "log_verification", "arguments": {}}]) == []
    assert base_env.read_log_allowlist_for(
        [{"name": "call_discoverable_agent_tool", "arguments": {"agent_tool_name": "no_such_tool"}}]
    ) == []
    assert base_env.read_log_allowlist_for(
        [{"name": "call_discoverable_agent_tool", "arguments": {}}]     # no tool named
    ) == []


def test_derivation_is_deterministic_and_sorted(base_env) -> None:
    """Sorted + JSON-serializable: it crosses the wire to /admin/seed and shows up in traces."""
    actions = _oracle("tau2_banking_43")["actions"]     # this one needs four distinct reads
    reads = base_env.read_log_allowlist_for(actions)
    assert reads == sorted(reads) and len(reads) > 1
    assert json.loads(json.dumps(reads)) == reads


# --------------------------------------------------------------------------- the symmetry guard

def test_a_perfect_agent_scores_one_when_both_sides_match(base_env) -> None:
    """THE false-negative guard: golden behaviour, replayed the way the live env replays it, must
    score 1.0. A one-sided allowlist makes this assertion fail -- which is the whole point."""
    oracle = _oracle(_CASE)
    allowlist = bk._required_reads(oracle["actions"])
    live_hash = _live_run_hash(base_env, oracle, allowlist=allowlist)

    score, meta = bk.task_success(
        data_point_id=_CASE, conversation=[_turn("Done.")], expected_output=_expected_of(_CASE),
        final_state={"env_state_hash": live_hash, "seeded_initial_state": True,
                     "read_log_allowlist": allowlist},
    )

    assert score == 1.0, f"perfect agent scored {score}: {meta}"
    assert meta["db_ok"] is True


def test_an_asymmetric_allowlist_really_does_change_the_hash(base_env) -> None:
    """Proves the risk is real rather than theoretical: the same trajectory, differing only in the
    allowlist, produces two different hashes. This is why the gate below exists."""
    oracle = _oracle(_CASE)
    with_reads = _live_run_hash(base_env, oracle, allowlist=bk._required_reads(oracle["actions"]))
    without_reads = _live_run_hash(base_env, oracle, allowlist=[])

    assert with_reads != without_reads


def test_missing_allowlist_is_refused_not_scored_zero(base_env) -> None:
    """A live env that never applied the allowlist yields an unmeasurable case, NOT a 0. Scoring 0
    here would attribute a harness gap to the agent -- exactly the mistake this seam must not make."""
    oracle = _oracle(_CASE)
    live_hash = _live_run_hash(base_env, oracle, allowlist=[])     # env forgot the allowlist

    score, meta = bk.task_success(
        data_point_id=_CASE, conversation=[_turn("Done.")], expected_output=_expected_of(_CASE),
        final_state={"env_state_hash": live_hash, "seeded_initial_state": True},
    )

    assert score is None and meta["measurable"] is False
    assert any("allowlist" in r for r in meta["reasons"])


def test_wrong_allowlist_is_also_refused(base_env) -> None:
    """Not just absent -- a DIFFERENT allowlist is equally asymmetric."""
    oracle = _oracle(_CASE)
    score, meta = bk.task_success(
        data_point_id=_CASE, conversation=[_turn("Done.")], expected_output=_expected_of(_CASE),
        final_state={"env_state_hash": "whatever", "seeded_initial_state": True,
                     "read_log_allowlist": ["some_other_tool"]},
    )
    assert score is None and any("allowlist" in r for r in meta["reasons"])


def test_cases_needing_no_reads_are_not_gated(base_env) -> None:
    """When the golden trajectory requires no discoverable reads, both sides are trivially empty, so
    the gate must stay open -- otherwise airline (which has no such concept) would stop scoring."""
    oracle = {"reward_basis": ["DB"], "actions": [], "communicate_info": []}
    assert bk._required_reads(oracle["actions"]) == []
    golden = bk._golden_state_hash([], None)

    score, meta = bk.task_success(
        data_point_id="synthetic", conversation=[_turn("Done.")],
        expected_output=ArtifactAgentOutput(json=json.dumps(oracle)),
        final_state={"env_state_hash": golden},          # no allowlist key at all
    )

    assert score == 1.0, meta


# --------------------------------------------------------------------------- the service side

def test_seed_route_derives_the_same_allowlist(base_env, monkeypatch) -> None:
    """The live half of the symmetry: POST /admin/seed, handed the golden trajectory, must apply the
    same list the eval side derived -- and report it back so the evaluator can verify rather than
    assume."""
    monkeypatch.setenv("BANKING_ENV_TOKEN", "tool-tok")
    monkeypatch.setenv("BANKING_ENV_ADMIN_TOKEN", "admin-tok")
    client = TestClient(create_app(base_env))
    oracle = _oracle(_CASE)

    resp = client.post(
        "/admin/seed",
        headers={"Authorization": "Bearer admin-tok"},
        json={"initial_state": oracle["initial_state"], "golden_actions": oracle["actions"]},
    )

    assert resp.status_code == 200
    assert resp.json()["read_log_allowlist"] == bk._required_reads(oracle["actions"])


def test_seed_route_honours_an_explicit_allowlist(base_env, monkeypatch) -> None:
    monkeypatch.setenv("BANKING_ENV_TOKEN", "tool-tok")
    monkeypatch.setenv("BANKING_ENV_ADMIN_TOKEN", "admin-tok")
    client = TestClient(create_app(base_env))

    resp = client.post(
        "/admin/seed",
        headers={"Authorization": "Bearer admin-tok"},
        json={"initial_state": None, "read_log_allowlist": ["explicit_tool"]},
    )

    assert resp.status_code == 200 and resp.json()["read_log_allowlist"] == ["explicit_tool"]


def test_seed_route_is_admin_only(base_env, monkeypatch) -> None:
    """The artifact holds the TOOL token; if it could reach /admin/seed it could read oracle-derived
    information back out of the response."""
    monkeypatch.setenv("BANKING_ENV_TOKEN", "tool-tok")
    monkeypatch.setenv("BANKING_ENV_ADMIN_TOKEN", "admin-tok")
    client = TestClient(create_app(base_env))

    resp = client.post("/admin/seed", headers={"Authorization": "Bearer tool-tok"},
                       json={"initial_state": None})

    assert resp.status_code == 401
