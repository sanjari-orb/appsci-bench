from __future__ import annotations

import copy
import hashlib
import json
import pathlib

import pytest

from appsci_bench.eval import eval_system
from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.datatypes import ArtifactAgentOutput
from appsci_bench.eval.eval_system import run_evaluation


def spec():
    return {"task_id": "rerun_test", "evaluation_data": {
        "type": "single_turn", "repeat": 2, "reward_objective": "match",
        "data": [{"data_point_id": s, "single_turn_input": {"text": s},
                  "expected_output": {"text": s.upper()}} for s in ("a", "b", "c")],
        "objectives": [
            {"name": "match", "eval_function_path": "eval_task.eval_fns.text_exact_match",
             "positive": True, "weight": 3.0},
            {"name": "penalty", "eval_function_path": "eval_task.eval_fns.text_exact_match",
             "positive": False, "weight": 1.0},
        ],
    }}


def evaluate(path, *, task=None, respond=None, **kwargs):
    return run_evaluation(task or spec(), InProcessArtifactClient(
        respond or (lambda q: ArtifactAgentOutput(text=q.text.upper()))),
        package="tests.fixtures", workdir=str(path), **kwargs)


def test_replaces_selected_repeats_with_worse_results_and_recomputes_full_suite(tmp_path, monkeypatch):
    source, target = tmp_path / "original/eval", tmp_path / "revision/eval"
    original = evaluate(source)
    old_files = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    calls = []
    monkeypatch.setattr(eval_system, "_score_elicitation_safely", lambda *a: pytest.fail("rescored build"))
    monkeypatch.setattr(eval_system, "_read_optimization_safely", lambda *a: pytest.fail("rescored build"))
    def respond(q):
        calls.append(q.text)
        return ArtifactAgentOutput(text="wrong")
    merged = evaluate(target, respond=respond, sample_ids=["b", "c"], rerun_source=str(source))
    assert sorted(calls) == ["b", "b", "c", "c"]
    assert original.final_score == pytest.approx(.75)
    assert merged.final_score == pytest.approx((.75 + .25 + .25) / 3)
    assert merged.cases_scored == merged.cases_total == 6
    assert len(merged.results) == len(original.results) == 12
    assert merged.views["reward"]["passes"] == {"a": True, "b": False, "c": False}
    assert merged.elicitation == original.elicitation
    assert merged.optimization == original.optimization
    for rel, data in old_files.items():
        assert (source / rel).read_bytes() == data
    for folder in ("results", "conversations"):
        for path in (source / folder).glob("a__*.json"):
            assert (target / folder / path.name).read_bytes() == path.read_bytes()
    assert merged.sample_rerun["source_trace_id"] == "original"
    # A second revision starts from the revised full suite, not the original subset's fingerprint.
    second = evaluate(tmp_path / "second/eval", sample_ids=["b"], rerun_source=str(target))
    assert second.views["reward"]["passes"] == {"a": True, "b": True, "c": False}


@pytest.mark.parametrize("ids", [[], ["a", "a"], ["missing"], "a", [1]])
def test_invalid_selection_runs_no_queries(tmp_path, ids):
    source = tmp_path / "old"
    evaluate(source)
    with pytest.raises(ValueError):
        evaluate(tmp_path / "new", sample_ids=ids, rerun_source=str(source),
                 respond=lambda q: pytest.fail("queried before validation"))


@pytest.mark.parametrize("change", ["dataset", "weight", "repeat", "reward", "missing_transcript", "partial", "hash"])
def test_incompatible_source_refused_before_queries(tmp_path, change):
    source = tmp_path / "old"
    evaluate(source)
    task = copy.deepcopy(spec())
    extra = {}
    if change == "dataset":
        task["evaluation_data"]["data"][0]["expected_output"]["text"] = "changed"
    if change == "weight":
        task["evaluation_data"]["objectives"][0]["weight"] = 1
    if change == "repeat":
        task["evaluation_data"]["repeat"] = 1
    if change == "reward":
        task["evaluation_data"]["reward_objective"] = "penalty"
    if change == "missing_transcript":
        next((source / "conversations").glob("*.json")).unlink()
    if change == "partial":
        report = json.loads((source / "report.json").read_text())
        report["partial"] = True
        (source / "report.json").write_text(json.dumps(report))
    if change == "hash":
        extra["rerun_source_sha256"] = "wrong"
    with pytest.raises(ValueError):
        evaluate(tmp_path / "new", task=task, sample_ids=["b"], rerun_source=str(source),
                 respond=lambda q: pytest.fail("queried before validation"), **extra)


def test_limited_source_keeps_original_suite_and_artifact_refs(tmp_path):
    source = tmp_path / "old"
    original = evaluate(source, limit=2, sample_seed=7)
    ids = sorted({r.data_point_id for r in original.results})
    unchanged = ids[1]
    artifact = source / "artifacts" / f"{unchanged}__r0" / "file.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"keep these bytes")
    target = tmp_path / "new"
    report = evaluate(target, sample_ids=[ids[0]], rerun_source=str(source))
    assert report.cases_total == 4
    assert {r.data_point_id for r in report.results} == set(ids)
    assert (target / artifact.relative_to(source)).read_bytes() == b"keep these bytes"


def test_interruption_leaves_source_untouched(tmp_path, monkeypatch):
    source = tmp_path / "old"
    evaluate(source)
    digest = hashlib.sha256((source / "report.json").read_bytes()).hexdigest()
    def stop(*a, **kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(eval_system, "generate", stop)
    with pytest.raises(KeyboardInterrupt):
        evaluate(tmp_path / "new", sample_ids=["a"], rerun_source=str(source))
    assert hashlib.sha256((source / "report.json").read_bytes()).hexdigest() == digest


def test_selected_repeats_keep_the_shared_step_limit_and_reject_a_changed_limit(tmp_path):
    from appsci_bench.eval.artifact_agent_user_sim import ScriptedArtifactAgentUserSim
    from .test_accounting_recovery import _spec

    task = _spec("selected", "unchanged", repeat=2)
    task["evaluation_data"]["max_steps"] = 4
    source, target = tmp_path / "original/eval", tmp_path / "revision/eval"
    calls = []

    def run(path, **kwargs):
        return evaluate(path, task=task, respond=lambda q: calls.append(q.text) or "ok",
                        user_sim=ScriptedArtifactAgentUserSim(["hello"] * 10),
                        conformance="off", **kwargs)

    original = run(source)
    calls.clear()
    report = run(target, sample_ids=["selected"], rerun_source=str(source))
    assert len(calls) == 4  # two exchanges per repeat; the unselected case is not queried
    assert report.cases_scored == report.cases_total == original.cases_total == 4
    assert not report.partial
    for path in (target / "conversations").glob("selected__*.json"):
        record = json.loads(path.read_text())
        assert record["simulation_steps"] == {"steps": 4, "max_steps": 4}
        assert record["termination_reason"] == "max_steps"
    task["evaluation_data"]["max_steps"] = 6
    calls.clear()
    with pytest.raises(ValueError, match="differ from the source eval"):
        run(tmp_path / "changed/eval", sample_ids=["selected"], rerun_source=str(source))
    assert not calls


@pytest.mark.parametrize("persistent", [False, True])
def test_sample_rerun_recovers_accounting_or_saves_partial_without_replacing_source(tmp_path, persistent):
    from appsci_bench.eval.generation import GenerationIncompleteError
    from appsci_bench.eval.lanes import LanePool
    from .test_accounting_recovery import _Environment, _spec

    task = _spec("bad", "healthy", "untouched", repeat=2)
    source, target = tmp_path / "original/eval", tmp_path / "revision/eval"

    def run(path, env, **kwargs):
        return run_evaluation(task, env.client, user_sim=env.sim,
                              lanes=LanePool.exclusive([env.lane]), package="tests.fixtures",
                              workdir=str(path), conformance="off", **kwargs)

    original = run(source, _Environment())
    old_files = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    env = _Environment(persistent=persistent)
    kwargs = {"sample_ids": ["bad", "healthy"], "rerun_source": str(source)}
    if persistent:
        with pytest.raises(GenerationIncompleteError, match="completed 2/4"):
            run(target, env, **kwargs)
        doc = json.loads((target / "report.json").read_text())
        assert doc["partial"] and doc["cases_scored"] == 2 and doc["cases_total"] == 4
        assert {r["data_point_id"] for r in doc["results"]} == {"healthy"}
        assert env.attempts == {"bad": 4, "healthy": 2}
        assert len(list((target / "infrastructure_attempts").glob("*.json"))) == 4
        assert any("original trial was not replaced" in w for w in doc["warnings"])
    else:
        report = run(target, env, **kwargs)
        assert not report.partial
        assert report.cases_scored == report.cases_total == original.cases_total == 6
        assert env.attempts == {"bad": 3, "healthy": 2}
        assert any("infrastructure retry" in w for w in report.warnings)
    for rel, data in old_files.items():
        assert (source / rel).read_bytes() == data


@pytest.mark.parametrize("via_orchestrator", [False, True])
def test_explicit_config_change_runs_200_steps_and_tracks_retained_configs_across_revisions(
        tmp_path, monkeypatch, via_orchestrator):
    from contextlib import nullcontext
    from functools import partial
    from types import SimpleNamespace
    from unittest.mock import Mock

    import appsci_bench.eval.evaluator as evaluator_mod
    import appsci_bench.orchestrator.run as run_mod
    from appsci_bench.eval.artifact_agent_user_sim import ScriptedArtifactAgentUserSim
    from appsci_bench.eval.lanes import EvalLane, LanePool
    from .test_accounting_recovery import _spec

    task = _spec("first", "second", "third", repeat=2)
    task["evaluation_data"].pop("max_steps")
    task["evaluation_data"]["max_turns"] = 20
    source = tmp_path / "original/eval"

    def run(path, **kwargs):
        if via_orchestrator:
            # Exercise both production adapters with a real evaluator. Replace only Docker/HTTP
            # and resolve the in-process test objective from the fixture package.
            client = InProcessArtifactClient(lambda q: "ok")
            sim = ScriptedArtifactAgentUserSim(["hello"] * 25)
            lanes = LanePool.exclusive([EvalLane(client=client, user_sim=sim)])
            monkeypatch.setattr(run_mod, "HttpArtifact", Mock())
            monkeypatch.setattr(run_mod, "eval_lane_pool", lambda **kw: nullcontext(lanes))
            monkeypatch.setattr(evaluator_mod, "run_evaluation",
                                partial(run_evaluation, package="tests.fixtures"))
            return run_mod.evaluate_deployed(SimpleNamespace(**task), "http://test", None,
                                             workdir=str(path), **kwargs)
        return evaluate(path, task=task, respond=lambda q: "ok",
                        user_sim=ScriptedArtifactAgentUserSim(["hello"] * 25),
                        conformance="off", **kwargs)

    old = run(source)
    # Historical reports have no new provenance fields. Their existing fingerprint is sufficient.
    raw = json.loads((source / "report.json").read_text())
    raw.pop("sample_rerun", None)
    (source / "report.json").write_text(json.dumps(raw))
    old_files = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    task["evaluation_data"].pop("max_turns")
    task["evaluation_data"]["max_steps"] = 200
    with pytest.raises(ValueError, match="--allow-config-change"):
        run(tmp_path / "refused/eval", sample_ids=["first"], rerun_source=str(source))
    target = tmp_path / "revision/eval"
    report = run(target, sample_ids=["first"], rerun_source=str(source), allow_config_change=True)
    assert report.cases_total == report.cases_scored == old.cases_total == 6
    assert not report.partial and report.sample_rerun["allow_config_change"]
    provenance = report.sample_rerun
    original_cfg = provenance["source_config_fingerprint"]
    new_cfg = provenance["current_config_fingerprint"]
    assert original_cfg != new_cfg
    assert provenance["sample_config_fingerprints"] == {
        "first": new_cfg, "second": original_cfg, "third": original_cfg}
    assert provenance["configurations"][original_cfg] is None
    assert provenance["configurations"][new_cfg]["max_steps"] == 200
    assert provenance["config_changed"] and provenance["mixed_configurations"]
    assert any("different configurations" in w for w in report.warnings)
    for i in range(2):
        rec = json.loads((target / f"conversations/first__r{i}.json").read_text())
        assert rec["simulation_steps"] == {"steps": 51, "max_steps": 200}
        assert len(rec["conversation"]) == 25  # the former 20-turn ceiling no longer applies
        assert (target / f"conversations/second__r{i}.json").read_bytes() == old_files[
            pathlib.Path(f"conversations/second__r{i}.json")]
    for rel, data in old_files.items():
        assert (source / rel).read_bytes() == data
    # Same-current-config reruns remain possible, without forgetting older retained samples.
    second = tmp_path / "second/eval"
    report2 = run(second, sample_ids=["second"], rerun_source=str(target))
    assert not report2.sample_rerun["allow_config_change"]
    assert report2.sample_rerun["mixed_configurations"]
    assert any("different configurations" in w for w in report2.warnings)
    report3 = run(tmp_path / "third/eval", sample_ids=["third"], rerun_source=str(second))
    assert not report3.sample_rerun["mixed_configurations"]
    assert set(report3.sample_rerun["sample_config_fingerprints"].values()) == {new_cfg}


@pytest.mark.parametrize("change", ["weight", "direction", "repeat", "reward", "task", "missing_case", "hash"])
def test_config_override_retains_aggregation_and_source_integrity_guards(tmp_path, change):
    source = tmp_path / "old/eval"
    evaluate(source)
    task = spec()
    kwargs = {}
    if change == "weight":
        task["evaluation_data"]["objectives"][0]["weight"] = 2
    elif change == "direction":
        task["evaluation_data"]["objectives"][0]["positive"] = False
    elif change == "repeat":
        task["evaluation_data"]["repeat"] = 3
    elif change == "reward":
        task["evaluation_data"]["reward_objective"] = "penalty"
    elif change == "task":
        task["task_id"] = "other"
    elif change == "missing_case":
        task["evaluation_data"]["data"].pop()
    else:
        kwargs["rerun_source_sha256"] = "changed"
    with pytest.raises(ValueError):
        evaluate(tmp_path / "new/eval", task=task, sample_ids=["a"], rerun_source=str(source),
                 allow_config_change=True, respond=lambda q: pytest.fail("query before validation"), **kwargs)
