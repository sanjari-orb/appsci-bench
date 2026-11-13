"""Unit tests for the per-case task-env reset seam (`orchestrator.run._make_env_reset_hooks`).

The seam is what makes a DB/ACTION reward measurable at all, and it is shared by every task that sets
`evaluation_data.reset_env_per_sample`. These tests fake `subprocess.run`, so they exercise the real
closures -- including the exact admin routes called, in order -- with no Docker and no credentials.

The load-bearing behaviours pinned here:

  * a case carrying an `initial_state` gets POST /admin/seed AND the `seeded_initial_state` flag,
    because `task_success` refuses to score a seeded case without it (comparing an unseeded agent run
    against a seeded golden replay is a guaranteed false negative, not a real failure);
  * a case carrying none never calls seed at all -- airline's behaviour must be bit-identical to
    before this seam grew;
  * an env with no /admin/seed route (404) warns and leaves the flag OFF rather than raising or
    pretending: the case then scores honestly unmeasurable;
  * the two optional read routes (/admin/retrieved_docs, /admin/action_trace) are merged when present
    and silently skipped on 404, so an older env keeps working;
  * a failing reset / state_hash raises, because scoring against an unknown env state is worse than
    failing the run.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import pytest

from appsci_bench.orchestrator.run import (
    _make_env_reset_hooks,
    _sample_initial_state,
    _sample_oracle,
)


# --------------------------------------------------------------------------- fixtures / fakes

@dataclass
class _Expected:
    json: str | None


@dataclass
class _Sample:
    """The slice of an EvalSample the seam reads."""
    expected_output: _Expected | None = None


def _sample_with(oracle: dict | None) -> _Sample:
    if oracle is None:
        return _Sample(expected_output=None)
    return _Sample(expected_output=_Expected(json=json.dumps(oracle)))


class _FakeDocker:
    """Stands in for `subprocess.run`, answering admin calls from a route->(status, body) table.

    Records every (method, path) so a test can assert both WHICH routes were hit and in what order --
    the ordering matters: seeding must happen after reset, or the seed is wiped by it.
    """

    def __init__(self, responses: dict[tuple[str, str], tuple[int, str]], *, returncode: int = 0):
        self.responses = responses
        self.returncode = returncode
        self.calls: list[tuple[str, str]] = []
        self.payloads: dict[str, str] = {}

    def __call__(self, argv, **kwargs):
        env = {}
        for i, token in enumerate(argv):
            if token == "-e":
                key, _, value = argv[i + 1].partition("=")
                env[key] = value
        method, url = env["ADMIN_METHOD"], env["ADMIN_URL"]
        path = url.split("8080", 1)[1]
        self.calls.append((method, path))
        if kwargs.get("input"):
            self.payloads[path] = kwargs["input"]
        status, body = self.responses.get((method, path), (404, '{"detail":"Not Found"}'))
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout=f"{status}\n{body}", stderr="boom" if self.returncode else ""
        )


def _hooks(monkeypatch, fake: _FakeDocker):
    monkeypatch.setattr(subprocess, "run", fake)
    return _make_env_reset_hooks("test-workspace-1", "admin-token")


_OK_RESET = {("POST", "/admin/reset"): (200, '{"status":"reset"}')}
_OK_HASH = {("GET", "/admin/state_hash"): (200, '{"state_hash":"deadbeef"}')}
_OK_SEED = {("POST", "/admin/seed"): (200, '{"status":"seeded"}')}


# --------------------------------------------------------------------------- oracle extraction

def test_oracle_reads_expected_output_json() -> None:
    assert _sample_oracle(_sample_with({"initial_state": {"a": 1}})) == {"initial_state": {"a": 1}}


@pytest.mark.parametrize(
    "sample",
    [
        _Sample(expected_output=None),                       # no oracle at all
        _Sample(expected_output=_Expected(json=None)),        # oracle slot, no payload
        _Sample(expected_output=_Expected(json="")),          # empty payload
        _Sample(expected_output=_Expected(json="not json")),  # unparseable
        _Sample(expected_output=_Expected(json="[1, 2]")),    # parses, but not a mapping
    ],
)
def test_oracle_degrades_to_empty_rather_than_raising(sample: _Sample) -> None:
    """This runs for every reset-seam task; a missing or odd oracle is a shape, not a run failure."""
    assert _sample_oracle(sample) == {}
    assert _sample_initial_state(sample) is None


def test_initial_state_ignores_a_non_dict_value() -> None:
    """A non-mapping initial_state cannot be POSTed as a seed body, so treat it as absent."""
    assert _sample_initial_state(_sample_with({"initial_state": "everything"})) is None
    assert _sample_initial_state(_sample_with({"initial_state": None})) is None


def test_initial_state_absent_when_oracle_has_other_keys() -> None:
    """Airline's shape: real oracle, no starting state."""
    assert _sample_initial_state(_sample_with({"actions": [{"name": "book"}]})) is None


# --------------------------------------------------------------------------- setup_state (Seam A)

def test_seeded_case_resets_then_seeds_and_records_the_flag(monkeypatch) -> None:
    fake = _FakeDocker({**_OK_RESET, **_OK_SEED, **_OK_HASH})
    setup_state, read_final_state = _hooks(monkeypatch, fake)
    initial_state = {"agent_data": {"users": {"data": {"u1": {"name": "Ada"}}}}}

    setup_state(_sample_with({"initial_state": initial_state}))

    assert fake.calls == [("POST", "/admin/reset"), ("POST", "/admin/seed")], \
        "seed must follow reset -- seeding first would be wiped by the reset"
    assert json.loads(fake.payloads["/admin/seed"])["initial_state"] == initial_state
    assert read_final_state(_sample_with(None))["seeded_initial_state"] is True


def test_unseeded_case_never_calls_seed(monkeypatch) -> None:
    """Airline's path: no initial_state, so the seam is exactly the reset it always was."""
    fake = _FakeDocker({**_OK_RESET, **_OK_HASH})
    setup_state, read_final_state = _hooks(monkeypatch, fake)

    setup_state(_sample_with({"actions": []}))

    assert fake.calls == [("POST", "/admin/reset")]
    assert "seeded_initial_state" not in read_final_state(_sample_with(None))


def test_missing_seed_route_warns_and_leaves_the_case_unmeasurable(monkeypatch, caplog) -> None:
    """An env predating /admin/seed must not raise -- and must not claim the seed happened."""
    fake = _FakeDocker({**_OK_RESET, **_OK_HASH})  # seed -> default 404
    setup_state, read_final_state = _hooks(monkeypatch, fake)

    with caplog.at_level("WARNING"):
        setup_state(_sample_with({"initial_state": {"a": 1}}))

    assert ("POST", "/admin/seed") in fake.calls
    assert "seeded_initial_state" not in read_final_state(_sample_with(None))
    assert "no POST /admin/seed" in caplog.text


def test_seed_failure_raises(monkeypatch) -> None:
    """A 500 from seed is a real failure: the env is in an unknown state, so stop."""
    fake = _FakeDocker({**_OK_RESET, ("POST", "/admin/seed"): (500, '{"detail":"bad seed"}')})
    setup_state, _ = _hooks(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="env seed failed"):
        setup_state(_sample_with({"initial_state": {"a": 1}}))


def test_reset_failure_raises(monkeypatch) -> None:
    fake = _FakeDocker({("POST", "/admin/reset"): (503, "unavailable")})
    setup_state, _ = _hooks(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="env reset failed"):
        setup_state(_sample_with(None))


def test_pending_flag_does_not_leak_between_cases(monkeypatch) -> None:
    """Case N+1 must not inherit case N's seeded flag -- that would score an unseeded run as seeded."""
    fake = _FakeDocker({**_OK_RESET, **_OK_SEED, **_OK_HASH})
    setup_state, read_final_state = _hooks(monkeypatch, fake)

    setup_state(_sample_with({"initial_state": {"a": 1}}))
    assert read_final_state(_sample_with(None))["seeded_initial_state"] is True

    setup_state(_sample_with({"actions": []}))          # unseeded case follows a seeded one
    assert "seeded_initial_state" not in read_final_state(_sample_with(None))


# --------------------------------------------------------------------------- read_final_state

def test_final_state_always_carries_the_state_hash(monkeypatch) -> None:
    fake = _FakeDocker({**_OK_RESET, **_OK_HASH})
    _, read_final_state = _hooks(monkeypatch, fake)

    assert read_final_state(_sample_with(None)) == {"env_state_hash": "deadbeef"}


def test_optional_read_routes_are_merged_when_present(monkeypatch) -> None:
    """Seam B's action trace and the retrieved-doc log, which upgrade ACTION scoring and recall."""
    fake = _FakeDocker({
        **_OK_HASH,
        ("GET", "/admin/retrieved_docs"): (200, '{"retrieved_documents":["doc_a","doc_b"]}'),
        ("GET", "/admin/action_trace"): (200, '{"agent_actions":[{"name":"log_verification"}]}'),
    })
    _, read_final_state = _hooks(monkeypatch, fake)

    final = read_final_state(_sample_with(None))

    assert final["retrieved_documents"] == ["doc_a", "doc_b"]
    assert final["agent_actions"] == [{"name": "log_verification"}]


def test_optional_read_routes_are_skipped_on_404(monkeypatch) -> None:
    """Airline exposes neither route; its final_state must stay exactly the state hash."""
    fake = _FakeDocker({**_OK_HASH})
    _, read_final_state = _hooks(monkeypatch, fake)

    final = read_final_state(_sample_with(None))

    assert final == {"env_state_hash": "deadbeef"}
    assert ("GET", "/admin/retrieved_docs") in fake.calls, "the route is probed, just not required"


def test_unparseable_optional_route_is_skipped_not_fatal(monkeypatch, caplog) -> None:
    fake = _FakeDocker({**_OK_HASH, ("GET", "/admin/retrieved_docs"): (200, "<html>nope</html>")})
    _, read_final_state = _hooks(monkeypatch, fake)

    with caplog.at_level("WARNING"):
        final = read_final_state(_sample_with(None))

    assert "retrieved_documents" not in final
    assert "unparseable" in caplog.text


def test_missing_state_hash_raises(monkeypatch) -> None:
    """Scoring a DB reward against an absent hash would silently mis-score, so fail loudly."""
    fake = _FakeDocker({("GET", "/admin/state_hash"): (200, '{"unexpected":"shape"}')})
    _, read_final_state = _hooks(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="no hash"):
        read_final_state(_sample_with(None))


def test_docker_exec_failure_raises(monkeypatch) -> None:
    fake = _FakeDocker({**_OK_RESET}, returncode=1)
    setup_state, _ = _hooks(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="env admin exec failed"):
        setup_state(_sample_with(None))
