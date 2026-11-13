"""Integration tests: run_evaluation end to end + artifact_to_client adapter."""
from __future__ import annotations

import json
import pathlib

import pytest

from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval import eval_system
from appsci_bench.eval.eval_system import artifact_to_client, run_evaluation
from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, EvalReport
from appsci_bench.eval.artifact_agent_user_sim import ScriptedArtifactAgentUserSim

_PKG = "tests.fixtures"  # eval_task.eval_fns


def _single_spec():
    return {"task_id": "t_single", "evaluation_data": {
        "type": "single_turn",
        "data": [{"data_point_id": "d1", "single_turn_input": {"text": "go"},
                  "expected_output": {"text": "GO"}}],
        "objectives": [{"name": "match", "eval_function_path": "eval_task.eval_fns.text_exact_match",
                        "positive": True, "weight": 1.0}],
    }}


def _echo(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
    return ArtifactAgentOutput(text=(q.text or "").upper())


def test_run_evaluation_single_turn(tmp_path):
    rep = run_evaluation(_single_spec(), InProcessArtifactClient(_echo),
                         package=_PKG, workdir=str(tmp_path))
    assert isinstance(rep, EvalReport)
    assert rep.final_score == pytest.approx(1.0)        # "go" -> "GO" matches expected
    assert rep.views["per_metric"][0]["objective"] == "match"
    assert rep.run_id and len(list((tmp_path / "conversations").glob("*.json"))) == 1
    # the transcript store root is recorded on the report, so it's findable after the run
    assert rep.store_root == str(tmp_path)


def test_disabled_objective_skips_calls_and_report_rows_until_reenabled(tmp_path, monkeypatch):
    from unittest.mock import Mock

    diagnostic = Mock(return_value=(0.0, {}))
    monkeypatch.setattr("tests.fixtures.eval_task.eval_fns.always_one", diagnostic)
    spec = _single_spec()
    optional = {"name": "diagnostic", "eval_function_path": "eval_task.eval_fns.always_one",
                "positive": True, "weight": 0.0, "enabled": False}
    spec["evaluation_data"]["objectives"].append(optional)
    disabled = run_evaluation(spec, InProcessArtifactClient(_echo), package=_PKG,
                             workdir=str(tmp_path / "disabled"))
    diagnostic.assert_not_called()
    assert {r.objective for r in disabled.results} == {"match"}
    assert [m["objective"] for m in disabled.views["per_metric"]] == ["match"]

    optional["enabled"] = True
    enabled = run_evaluation(spec, InProcessArtifactClient(_echo), package=_PKG,
                            workdir=str(tmp_path / "enabled"))
    diagnostic.assert_called_once()
    assert {r.objective for r in enabled.results} == {"match", "diagnostic"}
    assert disabled.final_score == enabled.final_score == 1.0
    assert disabled.run_id.split("_")[1] != enabled.run_id.split("_")[1]


def test_store_root_recorded_and_exists_without_workdir(tmp_path):
    # No workdir -> store falls back to a temp dir, but its path is still surfaced on the report
    # (the run-id-tagged dir) so the transcript deliverable can be located.
    import pathlib
    rep = run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG)
    assert rep.store_root and pathlib.Path(rep.store_root).is_dir()
    assert rep.run_id in rep.store_root                       # temp dir tagged with the run id
    assert (pathlib.Path(rep.store_root) / "conversations").is_dir()


def test_run_id_unique_per_execution_but_groupable_by_config():
    a = run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG)
    b = run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG)
    # unique per execution -> never collide (safe for archiving reports keyed on run_id)
    assert a.run_id != b.run_id
    # ...but prefixed with a stable config hash, so the same config stays groupable
    assert a.run_id.startswith("run_")
    cfg_a = a.run_id.split("_")[1]
    cfg_b = b.run_id.split("_")[1]
    assert cfg_a == cfg_b


def test_run_id_config_prefix_changes_with_weights():
    # The prefix is a full config hash -> it moves when weights/paths/data change (old run_id didn't).
    spec_w1 = _single_spec()
    spec_w2 = _single_spec()
    spec_w2["evaluation_data"]["objectives"][0]["weight"] = 0.5
    a = run_evaluation(spec_w1, InProcessArtifactClient(_echo), package=_PKG)
    b = run_evaluation(spec_w2, InProcessArtifactClient(_echo), package=_PKG)
    assert a.run_id.split("_")[1] != b.run_id.split("_")[1]


def test_run_evaluation_multi_turn():
    spec = {"task_id": "t_multi", "evaluation_data": {
        "type": "multi_turn", "max_turns": 3,
        "data": [{"data_point_id": "m1",
                  "multi_turn_input": {"user_task": "do", "user_persona": "p"}}],
        "objectives": [{"name": "always", "eval_function_path": "eval_task.eval_fns.always_one",
                        "positive": True, "weight": 1.0}],
    }}
    rep = run_evaluation(spec, InProcessArtifactClient(_echo),
                         user_sim=ScriptedArtifactAgentUserSim(["hi", "more"]), package=_PKG)
    assert rep.final_score == pytest.approx(1.0)


def test_limit_caps_data_points():
    spec = _single_spec()
    spec["evaluation_data"]["data"].append(
        {"data_point_id": "d2", "single_turn_input": {"text": "x"}, "expected_output": {"text": "X"}})
    rep = run_evaluation(spec, InProcessArtifactClient(_echo), package=_PKG, limit=1)
    assert len(rep.views["per_data_point"]) == 1


def test_subsampling_keeps_the_simulation_budget(tmp_path):
    import json
    from pathlib import Path
    spec = _single_spec()
    spec["evaluation_data"].update(type="multi_turn", max_steps=200, data=[{
        "data_point_id": "steps", "multi_turn_input": {"user_task": "t", "user_persona": "p"},
    }])
    rep = run_evaluation(spec, InProcessArtifactClient(_echo),
                         user_sim=ScriptedArtifactAgentUserSim(["hi"] * 25),
                         package=_PKG, limit=1, workdir=tmp_path)
    rec = json.loads(next((Path(rep.store_root) / "conversations").glob("*.json")).read_text())
    assert len(rec["conversation"]) == 25
    assert rec["simulation_steps"] == {"steps": 51, "max_steps": 200}
    assert rec["termination_reason"] == "user_stop"


def test_artifact_to_client_passthrough_and_callable():
    client = InProcessArtifactClient(_echo)
    assert artifact_to_client(client) is client
    wrapped = artifact_to_client(_echo)                 # plain respond callable
    sid = wrapped.create_session()
    assert wrapped.query_session(sid, ArtifactAgentQuery(text="hi")).text == "HI"


def test_artifact_to_client_bridges_legacy_artifact():
    from appsci_bench.eval.artifact import InProcessArtifact, ArtifactResponse
    legacy = InProcessArtifact(lambda q: ArtifactResponse(output={"text": "from-legacy"}))
    client = artifact_to_client(legacy)
    sid = client.create_session()
    assert client.query_session(sid, ArtifactAgentQuery(text="anything")).text == "from-legacy"


def test_config_fingerprint_does_not_read_file_path_sourced_files(tmp_path):
    # _config_fingerprint runs before generation, on every sample, to build run_id -- it must not
    # force a read of a lazily-sourced file (that would defeat lazy loading). Proof: the file is
    # deleted after the sample is loaded (so the *pointer* still exists on the File, but reading it
    # would now raise), yet fingerprinting still succeeds.
    import json

    from appsci_bench.eval.dataset import load_dataset
    from appsci_bench.eval.objectives import load_objectives
    from appsci_bench.eval.eval_system import _config_fingerprint

    p = tmp_path / "attachment.txt"
    p.write_bytes(b"hi")
    sample_dir = tmp_path / "d1"
    sample_dir.mkdir()
    (sample_dir / "sample.json").write_text(json.dumps({
        "data_point_id": "d1", "single_turn_input": {"text": "q", "files": [
            {"file_type": "txt", "file_name": "a.txt", "file_path": str(p)}]}}))
    spec = {"task_id": "t", "evaluation_data": {
        "type": "single_turn", "dataset": str(tmp_path),
        "objectives": [{"name": "always", "eval_function_path": "eval_task.eval_fns.always_one",
                        "positive": True, "weight": 1.0}]}}
    dataset = load_dataset(spec)
    objectives = load_objectives(spec, package=_PKG)
    p.unlink()  # the pointer is now dangling; reading it would raise FileNotFoundError

    fp = _config_fingerprint("t", dataset, objectives)
    assert fp.startswith("cfg_")


# --- user-sim parity note on the report ----------------------------------------------------------

class _SimWithModel:
    def __init__(self, model): self.model = model


def _report():
    from appsci_bench.eval.datatypes import EvalReport
    return EvalReport(task_id="t", results=[], views={}, final_score=0.0)


def test_report_records_a_diverged_user_sim_model():
    from appsci_bench.eval.eval_system import _note_non_parity_user_sim
    r = _report()
    _note_non_parity_user_sim(r, _SimWithModel("anthropic/claude-opus-4-8"))
    assert len(r.warnings) == 1
    assert "NOT tau-bench-comparable" in r.warnings[0]


def test_report_stays_clean_for_the_pinned_model_and_its_gateway_alias():
    # The alias is the documented internal path; an entry on every internal report would train
    # readers to skip the field, costing the signal exactly where it matters.
    from appsci_bench.eval.artifact_agent_user_sim import TAU_BENCH_USER_SIM_MODEL
    from appsci_bench.eval.eval_system import _note_non_parity_user_sim
    for model in (TAU_BENCH_USER_SIM_MODEL, "openai/openai-gpt-5.2"):
        r = _report()
        _note_non_parity_user_sim(r, _SimWithModel(model))
        assert r.warnings == [], model


def test_report_note_is_a_noop_without_a_model_attribute():
    # ScriptedArtifactAgentUserSim (and a single_turn run's None) have no model to report.
    from appsci_bench.eval.eval_system import _note_non_parity_user_sim
    r = _report()
    _note_non_parity_user_sim(r, object())
    _note_non_parity_user_sim(r, None)
    assert r.warnings == []


# --- the elicitation event on the unified trace -----------------------------------------------
#
# Emitted once per run whether or not the task has a rubric. A timeline that shows nothing for a
# rubric-less task leaves the reader unable to tell "not measured" from "the emitter is broken", and
# that ambiguity costs more than one extra event.

def _events(monkeypatch) -> list[tuple]:
    """Capture what eval_system emits, without standing up a real publisher."""
    seen: list[tuple] = []
    monkeypatch.setattr(eval_system, "_emit_obs",
                        lambda event_type, payload=None, **kw: seen.append((event_type, payload)))
    return seen


def _elicitation_events(seen) -> list[dict]:
    from shared.observability.events import EventType
    return [p for et, p in seen if et == EventType.EVAL_ELICITATION]


def test_the_elicitation_event_carries_the_summary_when_a_rubric_was_scored(monkeypatch):
    from appsci_bench.eval.datatypes import ElicitationResult, EvalReport, ItemResult

    seen = _events(monkeypatch)
    report = EvalReport(task_id="demo_task", final_score=0.8, elicitation=ElicitationResult(
        task_id="demo_task", status="scored", total_items=3, elicited_rate=1 / 3,
        asked_not_elicited_rate=1 / 3, missed_rate=1 / 3, error_items=0, num_exchanges=9,
        items=[ItemResult(item_id="a", verdict="elicited", evidence="sim > yes")]))
    eval_system._emit_elicitation(report)

    (payload,) = _elicitation_events(seen)
    assert payload["status"] == "scored"
    assert payload["elicited_rate"] == pytest.approx(1 / 3)
    assert payload["total_items"] == 3 and payload["num_exchanges"] == 9
    assert set(payload) == {"status", "elicited_rate", "asked_not_elicited_rate", "missed_rate",
                            "judged_items", "total_items", "error_items", "num_exchanges"}
    # Summary only: fifty per-item verdicts on the timeline would bury everything else, and the
    # report already carries them.
    assert "items" not in payload and "evidence" not in payload


def test_the_event_is_emitted_with_no_rubric_and_null_rates_by_default(tmp_path, monkeypatch):
    seen = _events(monkeypatch)
    run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG,
                   workdir=str(tmp_path))
    (payload,) = _elicitation_events(seen)
    assert payload["status"] == "no_rubric"
    assert payload["elicited_rate"] is None and payload["missed_rate"] is None
    assert payload["total_items"] == 0 and payload["num_exchanges"] == 0


def test_the_event_comes_after_the_scores_so_the_timeline_reads_in_order(tmp_path, monkeypatch):
    from shared.observability.events import EventType
    seen = _events(monkeypatch)
    run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG,
                   workdir=str(tmp_path))
    order = [et for et, _ in seen]
    assert order.index(EventType.EVAL_COMPLETE) < order.index(EventType.EVAL_ELICITATION)


def test_a_failing_emit_does_not_break_the_run(tmp_path, monkeypatch):
    # Observability is best-effort everywhere else in this module; the diagnostic is no exception.
    from shared.observability.events import EventType

    def boom(event_type, payload=None, **kw):
        if event_type == EventType.EVAL_ELICITATION:
            raise RuntimeError("publisher exploded")

    monkeypatch.setattr(eval_system, "_emit_obs", boom)
    rep = run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG,
                        workdir=str(tmp_path))
    assert rep.final_score == pytest.approx(1.0)


def test_the_event_type_is_registered_on_the_shared_enum():
    from shared.observability.events import EventType
    assert EventType.EVAL_ELICITATION.value == "eval.elicitation"


# --- the elicitation diagnostic wired into a real eval run ------------------------------------
#
# The property under test in most of these is that the diagnostic cannot cost a scored run. Everything
# about it -- a rubric that will not load, a transcript path that will not read, a judge that explodes --
# has to land as a status on the report rather than as an exception out of run_evaluation.

def _spec_with_rubric(tmp_path, items=None, *, objectives=None):
    """The single-turn fixture spec plus a rubric on disk, pointed at absolutely (no repo_root games)."""
    import yaml

    spec = _single_spec()
    spec["task_id"] = "t_single"
    if items is None:
        items = [{"item_id": "fault_taxonomy",
                  "fact": "The six fault_type values, spelled exactly as the finance team spells them.",
                  "objectives": objectives or ["match"]}]
    rubric = tmp_path / "elicitation_rubric.yaml"
    rubric.write_text(yaml.safe_dump({"task_id": "t_single", "items": items}, sort_keys=False))
    spec["evaluation_data"]["elicitation_rubric"] = str(rubric)
    return spec


def _transcript(n=2):
    from appsci_bench.eval.build_transcript import STATUS_OK, BuildTranscript, Exchange
    return BuildTranscript(
        exchanges=tuple(Exchange(turn=i, question=f"q{i}", answer=f"a{i}") for i in range(1, n + 1)),
        source="runs/demo_20260823", status=STATUS_OK)


def _verdict(asked=True, elicited=True):
    from appsci_bench.eval.elicitation import ElicitationVerdict
    return ElicitationVerdict(reasoning="because", asked=asked, elicited=elicited, evidence="sim > a1")


def test_a_transcript_plus_a_rubric_attaches_a_scored_result(tmp_path, monkeypatch):
    monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item",
                        lambda item, text, config=None: _verdict())
    rep = run_evaluation(_spec_with_rubric(tmp_path), InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"), build_transcript=_transcript())
    assert rep.elicitation.status == "scored"
    assert rep.elicitation.elicited_rate == 1.0
    assert rep.elicitation.num_exchanges == 2
    assert rep.elicitation.task_id == "t_single"
    # and the objectives are untouched
    assert rep.final_score == pytest.approx(1.0)


def test_no_transcript_with_a_rubric_is_unmeasurable_not_zero(tmp_path, monkeypatch):
    # "Nobody said where the conversation is" and "the agent never had one" are different findings. The
    # transcript is passed in, never guessed at from an ambient publisher or an inferred run directory:
    # a wrong guess would score one run's conversation against another's rubric.
    judged = []
    monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item",
                        lambda item, text, config=None: judged.append(item) or _verdict())
    rep = run_evaluation(_spec_with_rubric(tmp_path), InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"))
    assert rep.elicitation.status == "unmeasurable"
    assert rep.elicitation.elicited_rate is None
    assert judged == []                       # no calls spent on a conversation we do not have
    assert rep.final_score == pytest.approx(1.0)


def test_no_rubric_reports_no_rubric_and_still_names_the_task(tmp_path):
    rep = run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path), build_transcript=_transcript(3))
    assert rep.elicitation.status == "no_rubric"
    assert rep.elicitation.task_id == "t_single"
    # The exchange count survives: it says the conversation WAS read and only the rubric is missing.
    assert rep.elicitation.num_exchanges == 3


def test_a_run_directory_path_is_read(tmp_path, monkeypatch, write_usersim_events,
                                     usersim_exchange):
    monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item",
                        lambda item, text, config=None: _verdict())
    run_dir = write_usersim_events(tmp_path / "run", [
        *usersim_exchange(1, "which fault types?", "six of them, spelled exactly"),
        *usersim_exchange(2, "what tolerance?", "$500 dollar impact"),
    ])
    rep = run_evaluation(_spec_with_rubric(tmp_path), InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"), build_transcript=run_dir)
    assert rep.elicitation.status == "scored" and rep.elicitation.num_exchanges == 2


def test_a_transcript_file_path_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item",
                        lambda item, text, config=None: _verdict())
    captured = tmp_path / "transcript.md"
    captured.write_text("you > which fault types?\nsim > six of them, spelled exactly\n")
    rep = run_evaluation(_spec_with_rubric(tmp_path), InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"), build_transcript=captured)
    assert rep.elicitation.status == "scored" and rep.elicitation.num_exchanges == 1


def test_an_unreadable_transcript_path_is_unmeasurable_and_the_run_survives(tmp_path):
    rep = run_evaluation(_spec_with_rubric(tmp_path), InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"),
                         build_transcript=tmp_path / "does_not_exist.md")
    assert rep.elicitation.status == "unmeasurable"
    assert rep.final_score == pytest.approx(1.0)


def test_one_pool_serves_both_fan_outs(tmp_path, monkeypatch):
    """The shared-pool guarantee, observed rather than reasoned about: the executor that scored the
    objectives is the same object the rubric items were submitted into.

    The observation point is the SCORER'S CONSTRUCTOR rather than a `score_records` call, because
    scoring is no longer a phase that runs after generation -- `StreamingScorer` is handed the pool up
    front and each case is submitted into it as generation produces it. The guarantee being asserted is
    unchanged: one concurrency budget for the whole eval phase, not two that cannot see each other."""
    from concurrent.futures import ThreadPoolExecutor

    seen: list[object] = []
    real_scorer = eval_system.StreamingScorer
    real_score_elicitation = eval_system.score_elicitation

    def spy_scorer(*a, pool=None, **kw):
        seen.append(("scoring", pool))
        return real_scorer(*a, pool=pool, **kw)

    def spy_elicitation(*a, pool=None, **kw):
        seen.append(("elicitation", pool))
        return real_score_elicitation(*a, pool=pool, **kw)

    monkeypatch.setattr(eval_system, "StreamingScorer", spy_scorer)
    monkeypatch.setattr(eval_system, "score_elicitation", spy_elicitation)
    monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item",
                        lambda item, text, config=None: _verdict())

    run_evaluation(_spec_with_rubric(tmp_path, items=[
        {"item_id": f"item_{i}", "fact": "A fact long enough to clear the schema's length floor.",
         "objectives": ["match"]} for i in range(3)]),
        InProcessArtifactClient(_echo), package=_PKG, workdir=str(tmp_path / "wd"),
        build_transcript=_transcript())

    assert [label for label, _ in seen] == ["scoring", "elicitation"]
    pools = [pool for _, pool in seen]
    assert all(isinstance(p, ThreadPoolExecutor) for p in pools)
    assert pools[0] is pools[1], "the two fan-outs must share one concurrency budget"


def test_a_broken_rubric_pointer_cannot_fail_the_run(tmp_path):
    # A declared-but-missing rubric is a task bug and load_rubric raises on it -- but a run whose
    # artifact scored fine must not be thrown away over a diagnostic that could not be computed.
    spec = _single_spec()
    spec["evaluation_data"]["elicitation_rubric"] = str(tmp_path / "nope.yaml")
    rep = run_evaluation(spec, InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"), build_transcript=_transcript())
    assert rep.final_score == pytest.approx(1.0)
    assert rep.elicitation.status == "unmeasurable"
    assert any("could not run" in w for w in rep.elicitation.warnings)


def test_an_exploding_judge_cannot_fail_the_run(tmp_path, monkeypatch):
    def boom(item, text, config=None):
        raise RuntimeError("gateway down")

    monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item", boom)
    rep = run_evaluation(_spec_with_rubric(tmp_path), InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"), build_transcript=_transcript())
    assert rep.final_score == pytest.approx(1.0)
    assert rep.elicitation.items[0].verdict == "error"


def test_a_rubric_naming_an_objective_the_task_does_not_score_warns_and_scores_anyway(
        tmp_path, monkeypatch):
    # A mistyped objective name is worth seeing. Suppressing the whole diagnostic over it would hide
    # both the typo and the score.
    monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item",
                        lambda item, text, config=None: _verdict())
    rep = run_evaluation(_spec_with_rubric(tmp_path, objectives=["not_a_real_objective"]),
                         InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"), build_transcript=_transcript())
    assert rep.elicitation.status == "scored"
    assert rep.elicitation.elicited_rate == 1.0
    assert any("not_a_real_objective" in w for w in rep.elicitation.warnings)


def test_rubric_can_reference_a_disabled_objective_without_resolving_it(tmp_path, monkeypatch):
    monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item",
                        lambda item, text, config=None: _verdict())
    spec = _spec_with_rubric(tmp_path, objectives=["disabled"])
    spec["evaluation_data"]["objectives"].append({
        "name": "disabled", "eval_function_path": "missing.module.function",
        "positive": True, "weight": 0.0, "enabled": False,
    })
    rep = run_evaluation(spec, InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"), build_transcript=_transcript())
    assert rep.elicitation.status == "scored"
    assert rep.elicitation.warnings == []
    assert {r.objective for r in rep.results} == {"match"}


def test_the_diagnostic_reaches_the_report_artifacts(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item",
                        lambda item, text, config=None: _verdict())
    wd = tmp_path / "wd"
    run_evaluation(_spec_with_rubric(tmp_path), InProcessArtifactClient(_echo), package=_PKG,
                   workdir=str(wd), build_transcript=_transcript())
    assert json.loads((wd / "elicitation.json").read_text())["status"] == "scored"
    assert json.loads((wd / "report.json").read_text())["elicitation"]["status"] == "scored"
    assert "Elicitation diagnostic" in (wd / "report.html").read_text()


def test_the_elicitation_event_survives_a_real_publisher(tmp_path, monkeypatch):
    """The other tests capture the emit call and inspect the dict, which proves nothing about whether the
    event can actually be WRITTEN. Emission is wrapped best-effort and only logs on failure, so a payload
    carrying something unserializable would vanish silently and the timeline would simply be missing the
    diagnostic. This one drives the real publisher and reads the line back off disk.
    """
    import json

    import shared.observability.publisher as pub
    from appsci_bench.eval.datatypes import ElicitationResult, EvalReport, ItemResult
    from shared.observability.events import Source
    from shared.observability.storage import get_source_log_path

    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OBSERVABILITY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("OBSERVABILITY_TRACE_ID", "tr")
    monkeypatch.setenv("OBSERVABILITY_TASK", "demo_task")

    report = EvalReport(task_id="demo_task", final_score=0.8, elicitation=ElicitationResult(
        task_id="demo_task", status="scored", total_items=3, judged_items=2, error_items=1,
        elicited_rate=0.5, asked_not_elicited_rate=0.5, missed_rate=0.0, num_exchanges=4,
        items=[ItemResult(item_id="a", verdict="elicited", evidence="sim > yes")],
        warnings=["1 of 3 items could not be judged"]))
    try:
        pub.init_publisher(source=Source.EVALUATOR)
        eval_system._emit_elicitation(report)
    finally:
        p = pub.get_publisher()
        if p is not None:
            p.close()
        pub._publisher = None

    lines = [json.loads(x) for x in
             get_source_log_path("tr", Source.EVALUATOR, str(tmp_path)).read_text().splitlines()
             if x.strip()]
    (event,) = [e for e in lines if e["event_type"] == "eval.elicitation"]
    assert event["payload"]["status"] == "scored"
    assert event["payload"]["elicited_rate"] == 0.5
    assert event["payload"]["judged_items"] == 2
    # Round-trips through JSON on disk -- which is what "the timeline shows it" actually requires.
    assert json.loads(json.dumps(event["payload"])) == event["payload"]


def test_the_shared_pool_is_sized_for_the_rubric_too(tmp_path, monkeypatch):
    """The two fan-outs are not the same shape: scoring scales with the dataset, the diagnostic with the
    rubric. Sizing on scoring alone starved the diagnostic on exactly the small runs people iterate with
    -- one data point and two objectives gave two workers for a rubric of a dozen items."""
    import os
    from unittest.mock import patch

    sizes: list[int] = []
    real_worker_pool = eval_system.worker_pool

    def spy(n_items, max_workers=None):
        sizes.append(n_items)
        return real_worker_pool(n_items, max_workers)

    monkeypatch.setattr(eval_system, "worker_pool", spy)
    monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item",
                        lambda item, text, config=None: _verdict())
    # 12 rubric items against a single-case, single-objective dataset: scoring work is 1.
    items = [{"item_id": f"item_{i:02d}",
              "fact": "A fact long enough to clear the schema's length floor for items.",
              "objectives": ["match"]} for i in range(12)]
    with patch.object(os, "cpu_count", return_value=16):
        run_evaluation(_spec_with_rubric(tmp_path, items=items), InProcessArtifactClient(_echo),
                       package=_PKG, workdir=str(tmp_path / "wd"), build_transcript=_transcript())

    assert sizes, "the eval phase did not open a pool"
    assert sizes[0] >= 12, f"pool sized {sizes[0]} for a 12-item rubric -- the diagnostic is starved"


def test_a_broken_rubric_cannot_break_pool_sizing(tmp_path):
    """Sizing reads the rubric before the scorer's own guard runs, so its failure path has to be just as
    safe -- otherwise a task bug would take the whole eval run down from the sizing call."""
    spec = _single_spec()
    spec["evaluation_data"]["elicitation_rubric"] = str(tmp_path / "not_here.yaml")
    assert eval_system._rubric_size(spec) == 0
    rep = run_evaluation(spec, InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"), build_transcript=_transcript())
    assert rep.final_score == pytest.approx(1.0)
    assert rep.elicitation.status == "unmeasurable"


def test_rubric_size_is_zero_for_a_task_without_one(tmp_path):
    assert eval_system._rubric_size(_single_spec()) == 0
    assert eval_system._rubric_size({}) == 0


# --- the optimization diagnostic wired into a real eval run -----------------------------------
#
# Same property as the elicitation block above: nothing about the diagnostic may cost a scored run,
# and "nobody told us where the build is" has to land as a status rather than as a count of zero.

def _run_dir_with_one_optimize_call(tmp_path) -> pathlib.Path:
    """A minimal finished build: one optimize_agent call that changed and redeployed the candidate.

    Its status is `awaiting_verification` and not `saturated`, even though the call also reported the
    scores as flat. Both were true at once and the tool resolves that in favour of the unmeasured
    change -- it scores the candidate BEFORE it edits, so the redeployed one has no score at all.
    """
    run = tmp_path / "build_run"
    run.mkdir(parents=True, exist_ok=True)
    events = [
        {"source": "appsci_agent", "event_type": "agent.tool_invocation", "span_id": "s1",
         "timestamp": "2026-09-01T12:01:00+00:00", "task_id": "t_single",
         "payload": {"tool": "optimize_agent"}},
        {"source": "appsci_agent", "event_type": "agent.tool_result", "span_id": "s1",
         "timestamp": "2026-09-01T12:02:00+00:00", "task_id": "t_single",
         "payload": {"success": True, "status": "awaiting_verification", "iteration": 1,
                     "headline_metric": 0.6, "saturated": True, "budget_exhausted": False,
                     "cycles_used": 1, "cycles_allowed": 5,
                     "cycle": {"changed": True, "redeployed": True}}},
    ]
    (run / "events-appsci_agent.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))
    (run / "events-orchestrator.jsonl").write_text(json.dumps(
        {"source": "orchestrator", "event_type": "build.start",
         "timestamp": "2026-09-01T12:00:00+00:00", "task_id": "t_single",
         "payload": {"optimization_enabled": True, "optimize_cycles": 5}}) + "\n")
    return run


def test_a_build_run_dir_attaches_the_optimization_diagnostic(tmp_path):
    rep = run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"),
                         build_run_dir=_run_dir_with_one_optimize_call(tmp_path))

    assert rep.optimization.status == "scored"
    assert rep.optimization.calls == 1
    assert rep.optimization.cycles_used == 1
    assert rep.optimization.early_stopped_on_saturation is True
    assert rep.optimization.deploys_succeeded == 1
    # The loop's own eval, which is not this run's score and is never mixed into it.
    assert rep.optimization.internal_eval_final == pytest.approx(0.6)
    assert rep.optimization.internal_eval_best_call == 1
    assert rep.optimization.internal_eval_all_scores == [pytest.approx(0.6)]
    # One call, so there was no previous change for it to judge.
    assert rep.optimization.changes_kept == 0
    assert rep.optimization.changes_rolled_back == 0
    # and the objectives are untouched -- the diagnostic is structurally outside the score
    assert rep.final_score == pytest.approx(1.0)


def test_no_build_run_dir_is_unmeasurable_not_a_run_without_optimization(tmp_path):
    rep = run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path))

    assert rep.optimization.status == "unmeasurable"
    assert rep.optimization.calls == 0
    assert any("NOT a run without optimization" in w for w in rep.optimization.warnings)


def test_an_unreadable_build_run_dir_cannot_cost_the_scored_run(tmp_path):
    rep = run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path / "wd"), build_run_dir=tmp_path / "gone")

    assert rep.optimization.status == "unmeasurable"
    assert rep.final_score == pytest.approx(1.0)


def _optimization_events(seen) -> list[dict]:
    from shared.observability.events import EventType
    return [p for et, p in seen if et == EventType.EVAL_OPTIMIZATION]


def test_the_optimization_event_carries_the_summary(tmp_path, monkeypatch):
    seen = _events(monkeypatch)
    run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG,
                   workdir=str(tmp_path / "wd"),
                   build_run_dir=_run_dir_with_one_optimize_call(tmp_path))

    (payload,) = _optimization_events(seen)
    assert payload["status"] == "scored"
    assert payload["calls"] == 1 and payload["cycles_used"] == 1
    # The change nobody measured outranks the flat scores beside it, and both survive on the event.
    assert payload["stop_reason"] == "unverified_change"
    assert payload["saturated"] is True
    assert payload["early_stopped_on_saturation"] is True
    # Whether the loop got anywhere, not only how long it ran. One call measured, so there is a final
    # score and no change to report.
    assert payload["internal_eval_final"] == pytest.approx(0.6)
    assert payload["internal_eval_change"] is None
    # A roll-back redeploys the previous candidate, so it leaves a checkpoint the deploy counts do
    # not explain. Carried so the timeline's two tallies can be reconciled without the report.
    assert payload["changes_rolled_back"] == 0
    assert payload["rollback_redeploys"] == 0
    assert payload["rollbacks_failed"] == 0
    # Summary only: the per-call table is on the report, and putting it on the timeline would bury
    # everything else. The whole score list is left off for the same reason.
    assert "call_details" not in payload
    assert "internal_eval_all_scores" not in payload


def test_the_optimization_event_is_emitted_even_with_nothing_to_count(tmp_path, monkeypatch):
    """A timeline that shows nothing leaves "not measured" and "the emitter is broken" identical."""
    seen = _events(monkeypatch)
    run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG,
                   workdir=str(tmp_path))

    (payload,) = _optimization_events(seen)
    assert payload["status"] == "unmeasurable"
    assert payload["calls"] == 0 and payload["cycles_used"] is None


def test_a_failing_optimization_emit_does_not_break_the_run(tmp_path, monkeypatch):
    from shared.observability.events import EventType

    def boom(event_type, payload=None, **kw):
        if event_type == EventType.EVAL_OPTIMIZATION:
            raise RuntimeError("publisher exploded")

    monkeypatch.setattr(eval_system, "_emit_obs", boom)
    rep = run_evaluation(_single_spec(), InProcessArtifactClient(_echo), package=_PKG,
                         workdir=str(tmp_path))
    assert rep.final_score == pytest.approx(1.0)


def test_the_optimization_event_type_is_registered_on_the_shared_enum():
    from shared.observability.events import EventType
    assert EventType.EVAL_OPTIMIZATION.value == "eval.optimization"


def _record(*rejected_per_turn):
    from types import SimpleNamespace as NS
    return NS(conversation=[NS(response=ArtifactAgentOutput(rejected_files=list(r)))
                            for r in rejected_per_turn])


def test_rejected_files_warning_names_each_refused_file_and_counts_cases():
    exe = {"file_type": "application/x-msdownload", "file_name": "bad.exe"}
    bin_ = {"file_type": "weird", "file_name": "deck.bin"}
    warning = eval_system._rejected_files_warning(
        [_record([exe], []), _record([bin_, exe]), _record([])])
    assert warning == ("3 returned file(s) in 2 case(s) were rejected for an unsupported file_type "
                       "and ignored: bad.exe (application/x-msdownload), deck.bin (weird)")


def test_rejected_files_warning_is_absent_when_nothing_was_refused():
    assert eval_system._rejected_files_warning([_record([]), _record()]) is None
