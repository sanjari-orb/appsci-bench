"""Unit tests: artifact-agent conformance probe + contract publication."""
from __future__ import annotations

import pytest

from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.conformance import ConformanceError, check_conformance
from appsci_bench.eval.contract import latest_version, session_api_contract
from appsci_bench.eval.eval_system import run_evaluation
from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery

_PKG = "tests.fixtures"


def _conformant():
    return InProcessArtifactClient(lambda q: ArtifactAgentOutput(text="ok", json='{"a": 1}'))


def test_conformant_client_passes():
    rep = check_conformance(_conformant())
    assert rep.passed
    assert {c.name for c in rep.checks} >= {"create_session", "query_session", "delete_session"}


def test_bad_query_return_type_fails():
    rep = check_conformance(InProcessArtifactClient(lambda q: 123))  # not coercible
    assert not rep.passed
    assert any(c.name == "query_session" and not c.passed for c in rep.checks)


def test_invalid_json_framing_is_dropped_not_fatal():
    # _coerce_output drops a malformed `json` field with a warning rather than raising, so by the
    # time it reaches the probe it's already sanitized -- conformance has no json_framing check to
    # pass or fail; it just sees a normal ArtifactAgentOutput.
    rep = check_conformance(InProcessArtifactClient(lambda q: ArtifactAgentOutput(json="{not json")))
    assert rep.passed
    assert not any(c.name == "query_session.json_framing" for c in rep.checks)


def test_bad_create_session_fails():
    class BadCreate:
        def create_session(self, context=None): return 123       # not a str
        def query_session(self, s, q): return ArtifactAgentOutput(text="x")
        def delete_session(self, s): return True
    rep = check_conformance(BadCreate())
    assert not rep.passed
    assert any(c.name == "create_session" and not c.passed for c in rep.checks)


def test_create_session_rejecting_context_fails():
    # The probe passes an empty reserved `context`; an artifact whose create_session can't accept it
    # must fail conformance NOW (loud) rather than silently when per-case starting state lands.
    class NoContext:
        def create_session(self): return "s1"                    # rejects the reserved context arg
        def query_session(self, s, q): return ArtifactAgentOutput(text="x")
        def delete_session(self, s): return True
    rep = check_conformance(NoContext())
    assert not rep.passed
    assert any(c.name == "create_session" and not c.passed for c in rep.checks)


def test_non_bool_delete_fails():
    class BadDelete:
        def create_session(self, context=None): return "s1"
        def query_session(self, s, q): return ArtifactAgentOutput(text="x")
        def delete_session(self, s): return "yes"                # not a bool
    rep = check_conformance(BadDelete())
    assert not rep.passed
    assert any(c.name == "delete_session" and not c.passed for c in rep.checks)


def test_delete_raising_fails_gracefully():
    class DeleteRaises:
        def create_session(self, context=None): return "s1"
        def query_session(self, s, q): return ArtifactAgentOutput(text="x")
        def delete_session(self, s): raise RuntimeError("nope")
    rep = check_conformance(DeleteRaises())                       # must not raise
    assert not rep.passed


def test_run_evaluation_aborts_on_nonconformance():
    spec = {"task_id": "t", "evaluation_data": {
        "type": "single_turn",
        "data": [{"data_point_id": "d1", "single_turn_input": {"text": "go"}}],
        "objectives": [{"name": "m", "eval_function_path": "eval_task.eval_fns.always_one",
                        "positive": True, "weight": 1.0}]}}
    bad = InProcessArtifactClient(lambda q: 123)                  # non-conformant
    with pytest.raises(ConformanceError):
        run_evaluation(spec, bad, package=_PKG, conformance="on")  # force probe on in-process


def test_run_evaluation_auto_skips_inprocess():
    # auto mode skips the probe for an in-process client (conformant by construction), so even a
    # quirky-but-coercible client runs; here a normal one completes without ConformanceError.
    spec = {"task_id": "t", "evaluation_data": {
        "type": "single_turn",
        "data": [{"data_point_id": "d1", "single_turn_input": {"text": "go"}}],
        "objectives": [{"name": "m", "eval_function_path": "eval_task.eval_fns.always_one",
                        "positive": True, "weight": 1.0}]}}
    rep = run_evaluation(spec, _conformant(), package=_PKG)        # conformance="auto" default
    assert rep.final_score == pytest.approx(1.0)


def test_contract_published_and_has_endpoints():
    c = session_api_contract()
    assert set(c["endpoints"]) == {"create_session", "query_session", "delete_session"}
    # payload shapes are the single source of truth (machine-readable $defs, not a parallel summary)
    assert set(c["payloads"]["$defs"]) >= {"ArtifactAgentQuery", "ArtifactAgentOutput", "File"}
    # versioned + reserved create_session context (additive-evolution slots)
    assert c["version"] == latest_version()
    assert "context" in c["endpoints"]["create_session"]["input"]


def test_contract_registry_is_versioned_and_reversible():
    from appsci_bench.eval.contract import ContractError, available_versions

    versions = available_versions()
    assert versions and versions == sorted(versions)
    # loading by explicit version returns that version (lets the evaluator "reverse" to an old contract)
    for v in versions:
        assert session_api_contract(version=v)["version"] == v
    # default == latest
    assert session_api_contract()["version"] == versions[-1]
    # unknown version fails loudly rather than silently picking another
    with pytest.raises(ContractError):
        session_api_contract(version=999)


def test_contract_files_validate_against_meta_schema():
    # Every session_api.v<N>.json must satisfy the registry meta-schema -- session_api_contract()
    # validates on load, so a malformed/new contract file is caught here.
    from appsci_bench.eval.contract import available_versions
    for v in available_versions():
        session_api_contract(version=v)  # raises ContractError if it violates the meta-schema
