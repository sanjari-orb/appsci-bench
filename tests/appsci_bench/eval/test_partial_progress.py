"""Unit + integration tests: a killed eval keeps the cases it finished.

THE FAILURE THESE GUARD. A 97-case banking eval was SIGKILLed by its worker timeout with 80 cases
generated. Nothing was on disk: the transcript store was written only after the last unit completed,
scoring had not started at all (it was a second phase), and the store root was a temp dir the
orchestrator copied out at the end of a path that never finished. 4.3M tokens and four hours produced
zero recorded cases.

So the tests below assert the three properties that together make that impossible, and each is written
to fail if its own half regresses:

  * a case's transcript AND its scores are on disk before the next case is collected;
  * the report on disk describes the cases finished so far and says it is partial;
  * a partial report can never be read as a complete one -- not by the report reader, not by the
    experiment metrics, not by the worker deciding whether a cell passed.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

import pytest

from appsci_bench.eval import eval_system
from appsci_bench.eval.artifact_agent_user_sim import ScriptedArtifactAgentUserSim
from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.dataset import parse_dataset
from appsci_bench.eval.datatypes import (
    ArtifactAgentOutput, ArtifactAgentQuery, DataPointMetricResult, EvalReport, GenerationRecord,
    Objective,
)
from appsci_bench.eval.eval_system import run_evaluation
from appsci_bench.eval.generation import generate
from appsci_bench.eval.scoring import StreamingScorer, score_records
from appsci_bench.eval.store import TranscriptStore
from appsci_bench.eval.workers import worker_pool

_PKG = "tests.fixtures"  # eval_task.eval_fns


def _echo(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
    return ArtifactAgentOutput(text=(q.text or "").upper())


def _single_ds(n=3, repeat=1):
    samples = [{"data_point_id": f"d{i}", "single_turn_input": {"text": f"q{i}"},
                "expected_output": {"text": f"Q{i}"}} for i in range(n)]
    return parse_dataset({"type": "single_turn", "repeat": repeat}, samples)


def _spec(n=3):
    return {"task_id": "t_partial", "evaluation_data": {
        "type": "single_turn",
        "data": [{"data_point_id": f"d{i}", "single_turn_input": {"text": f"q{i}"},
                  "expected_output": {"text": f"Q{i}"}} for i in range(n)],
        "objectives": [{"name": "match", "eval_function_path": "eval_task.eval_fns.text_exact_match",
                        "positive": True, "weight": 1.0}],
    }}


def _objectives():
    from appsci_bench.eval.objectives import load_objectives
    return load_objectives(_spec(1), package=_PKG)


# --- the store: per-unit results, written atomically ------------------------------------------

def test_save_results_writes_one_file_per_unit(tmp_path):
    store = TranscriptStore(tmp_path)
    rec = GenerationRecord("dp1", 0)
    results = [DataPointMetricResult("dp1", "match", 0, 1.0, {"note": "ok"})]
    path = store.save_results(rec, results)
    doc = json.loads(path.read_text())
    assert doc["data_point_id"] == "dp1" and doc["repeat_index"] == 0
    assert doc["results"][0]["objective"] == "match" and doc["results"][0]["score"] == 1.0
    assert store.list_results() == [path]


def test_writes_are_atomic_and_leave_no_temp_files(tmp_path):
    # The reader parses these files while the run is still writing them, so a half-written file would
    # be read rather than skipped. os.replace means a kill leaves the previous version, never a torn one.
    store = TranscriptStore(tmp_path)
    store.write_json("report.json", {"a": 1})
    store.write_json("report.json", {"a": 2})            # overwrite in place
    store.write_text("report.html", "<p>hi</p>")
    assert json.loads((tmp_path / "report.json").read_text()) == {"a": 2}
    assert (tmp_path / "report.html").read_text() == "<p>hi</p>"
    assert list(tmp_path.glob("*.tmp")) == []            # nothing left behind


def test_a_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    store = TranscriptStore(tmp_path)

    def boom(self, data):        # noqa: ARG001 -- stand-in for a full disk
        raise OSError("no space left on device")

    monkeypatch.setattr(pathlib.Path, "write_bytes", boom)
    with pytest.raises(OSError):
        store.write_json("report.json", {"a": 1})
    assert list(tmp_path.glob("*.tmp")) == []


# --- generation: each unit lands before the next is collected ----------------------------------

def test_each_conversation_is_persisted_as_its_unit_completes(tmp_path):
    # The regression guard for the original bug: assert the store already holds the FINISHED units
    # while generation is still going, rather than only checking the count at the end (which the old
    # write-everything-at-the-end code also passed).
    store = TranscriptStore(tmp_path)
    seen: list[int] = []

    def on_record(rec):
        seen.append(len(list(store.list_conversations())))

    generate(_single_ds(3), InProcessArtifactClient(_echo), store=store, on_record=on_record)
    # 1 file after the first unit, 2 after the second, 3 after the third -- never 0, 0, 0.
    assert seen == [1, 2, 3]


def test_on_record_is_called_once_per_unit_with_its_record(tmp_path):
    got: list[tuple[str, int]] = []
    generate(_single_ds(2, repeat=2), InProcessArtifactClient(_echo),
             on_record=lambda r: got.append((r.data_point_id, r.repeat_index)))
    assert sorted(got) == [("d0", 0), ("d0", 1), ("d1", 0), ("d1", 1)]


def test_a_raising_hook_does_not_lose_the_remaining_cases(tmp_path):
    # The hook is an observer. A bug in the progress bookkeeping must cost that case's bookkeeping,
    # never the generation of the other 96.
    def boom(rec):
        raise RuntimeError("bookkeeping is broken")

    recs = generate(_single_ds(3), InProcessArtifactClient(_echo), on_record=boom)
    assert len(recs) == 3 and all(r.error is None for r in recs)


def test_a_store_that_cannot_write_does_not_lose_the_run(tmp_path, monkeypatch):
    store = TranscriptStore(tmp_path)
    monkeypatch.setattr(TranscriptStore, "save", lambda self, rec: (_ for _ in ()).throw(OSError("ro")))
    recs = generate(_single_ds(2), InProcessArtifactClient(_echo), store=store)
    assert len(recs) == 2 and all(r.error is None for r in recs)


# --- StreamingScorer: same answers as the batch scorer ----------------------------------------

def test_streaming_and_batch_scorers_agree(tmp_path):
    # The equivalence that makes interleaving safe to adopt: scoring as cases arrive must produce
    # exactly what scoring them all at the end did, including order.
    ds, objectives = _single_ds(4), _objectives()
    recs = generate(ds, InProcessArtifactClient(_echo))
    batch = score_records(recs, ds, objectives)
    with worker_pool(8) as pool:
        scorer = StreamingScorer(ds, objectives, pool=pool)
        for rec in recs:
            scorer.submit(rec)
        streamed = scorer.drain()
    assert [(r.data_point_id, r.objective, r.score) for r in streamed] == \
           [(r.data_point_id, r.objective, r.score) for r in batch]


def test_streaming_scorer_reports_a_generation_failure_as_a_graded_zero(tmp_path):
    ds, objectives = _single_ds(1), _objectives()
    rec = GenerationRecord("d0", 0, error="TimeoutError('query_session timed out')")
    with worker_pool(2) as pool:
        scorer = StreamingScorer(ds, objectives, pool=pool)
        scorer.submit(rec)
        results = scorer.drain()
    assert len(results) == 1 and results[0].score == 0.0
    assert "generation failed" in results[0].error


def test_on_unit_scored_fires_once_per_unit(tmp_path):
    ds, objectives = _single_ds(3), _objectives()
    recs = generate(ds, InProcessArtifactClient(_echo))
    calls: list[str] = []
    with worker_pool(8) as pool:
        scorer = StreamingScorer(ds, objectives, pool=pool,
                                 on_unit_scored=lambda rec, unit, all_: calls.append(rec.data_point_id))
        for rec in recs:
            scorer.submit(rec)
        scorer.drain()
    assert sorted(calls) == ["d0", "d1", "d2"]
    assert scorer.units_scored == 3


def test_a_raising_unit_hook_does_not_lose_the_scores(tmp_path):
    ds, objectives = _single_ds(2), _objectives()
    recs = generate(ds, InProcessArtifactClient(_echo))

    def boom(rec, unit, all_):
        raise RuntimeError("progress writer is broken")

    with worker_pool(4) as pool:
        scorer = StreamingScorer(ds, objectives, pool=pool, on_unit_scored=boom)
        for rec in recs:
            scorer.submit(rec)
        results = scorer.drain()
    assert len(results) == 2 and all(r.score == 1.0 for r in results)


# --- run_evaluation: the report on disk tracks the run ----------------------------------------

def _noop_setup(sample):        # noqa: ARG001
    """A `setup_state` hook that does nothing but force `workers=1`.

    Generation serialises whenever an env hook is present, which is how banking and airline really run
    (`reset_env_per_sample` is set on both) and therefore the shape that gets killed part-way. Tests
    that care about ordering pass this so they exercise that path rather than the parallel one."""


class _SlowSnapshotting(InProcessArtifactClient):
    """A client that takes a beat per query and records the on-disk report while it does.

    The delay is what makes the observation meaningful rather than flaky. With an instant client the
    single generation worker runs every unit back-to-back before the collecting thread is scheduled at
    all, so nothing has been flushed yet and no observation point inside the run can see progress --
    an artifact of a microsecond-long "eval", not of the code. A real case takes minutes; 20ms is
    enough to reproduce the ordering that produces.
    """

    def __init__(self, fn, report_path, snapshots):
        super().__init__(fn)
        self._report_path = report_path
        self._snapshots = snapshots

    def query_session(self, session_id, query):
        time.sleep(0.02)
        if self._report_path.is_file():
            self._snapshots.append(json.loads(self._report_path.read_text()))
        return super().query_session(session_id, query)


def test_report_on_disk_grows_as_cases_are_scored(tmp_path):
    """The core property: while the run is going, the report on disk describes the cases finished so
    far -- which is what a killed run leaves behind."""
    snapshots: list[dict] = []
    client = _SlowSnapshotting(_echo, tmp_path / "report.json", snapshots)
    rep = run_evaluation(_spec(5), client, package=_PKG, workdir=str(tmp_path),
                         setup_state=_noop_setup)

    assert snapshots, "no report.json existed mid-run -- partial progress is not being saved"
    counts = [s["cases_scored"] for s in snapshots]
    assert all(s["partial"] is True for s in snapshots)          # never readable as a finished run
    assert all(s["cases_scored"] == len(s["views"]["per_data_point"]) for s in snapshots)
    assert counts == sorted(counts)                              # monotonic: cases only accumulate
    assert max(counts) < 5                                       # a genuine prefix of the dataset
    assert all(s["cases_total"] == 5 for s in snapshots)          # ...against a known denominator
    # ...and the finished report is complete, with the counts to prove it.
    assert rep.partial is False and rep.cases_scored == 5 and rep.cases_total == 5
    final = json.loads((tmp_path / "report.json").read_text())
    assert final["partial"] is False and final["cases_scored"] == 5


def test_partial_report_carries_a_score_over_the_cases_it_has(tmp_path):
    # The owner's decision: a partial report DOES carry final_score, as a live progress figure. What
    # keeps that honest is the flag beside it, not the absence of the number.
    snapshots: list[dict] = []
    client = _SlowSnapshotting(_echo, tmp_path / "report.json", snapshots)
    run_evaluation(_spec(4), client, package=_PKG, workdir=str(tmp_path), setup_state=_noop_setup)
    assert snapshots, "expected at least one mid-run report"
    assert all(s["final_score"] is not None for s in snapshots)
    assert all(s["partial"] is True for s in snapshots)


# --- the flagship case: an actual SIGKILL, in an actual subprocess ------------------------------

_KILLED_RUN_CHILD = """
import pathlib, sys, time
sys.path.insert(0, {repo_root!r})
from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.datatypes import ArtifactAgentOutput
from appsci_bench.eval.eval_system import run_evaluation

def slow_echo(q):
    time.sleep(0.35)                       # stands in for a case that takes minutes
    return ArtifactAgentOutput(text=(q.text or "").upper())

spec = {{"task_id": "t_killed", "evaluation_data": {{
    "type": "single_turn",
    "data": [{{"data_point_id": "d%d" % i, "single_turn_input": {{"text": "q%d" % i}},
              "expected_output": {{"text": "Q%d" % i}}}} for i in range(40)],
    "objectives": [{{"name": "match",
                    "eval_function_path": "eval_task.eval_fns.text_exact_match",
                    "positive": True, "weight": 1.0}}],
}}}}

run_evaluation(spec, InProcessArtifactClient(slow_echo), package="tests.fixtures",
               workdir={workdir!r}, setup_state=lambda sample: None)
"""


def test_a_sigkilled_run_leaves_the_cases_it_scored(tmp_path):
    """SIGKILL a real eval mid-generation and assert the finished cases are on disk and scored.

    This is the failure the whole change exists for, so it is tested the way it happened rather than
    by simulating it in-process: nothing inside the evaluator can emulate SIGKILL, because a signal
    that runs no cleanup is exactly what the old code could not survive. An in-process stand-in would
    pass against an implementation that flushed in a `finally` block, which a real kill never reaches.
    """
    import os
    import signal
    import subprocess

    repo_root = str(pathlib.Path(__file__).resolve().parents[3])
    script = _KILLED_RUN_CHILD.format(repo_root=repo_root, workdir=str(tmp_path))
    proc = subprocess.Popen([sys.executable, "-c", script], cwd=repo_root,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        results_dir = tmp_path / "results"
        deadline = time.time() + 60
        while time.time() < deadline:
            if len(list(results_dir.glob("*.json"))) >= 3:
                break
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"child exited early ({proc.returncode}): {err.decode()[-2000:]}")
            time.sleep(0.05)
        else:
            proc.kill()
            pytest.fail("no per-case results appeared within 60s")
        os.kill(proc.pid, signal.SIGKILL)      # no cleanup, no atexit, no finally
    finally:
        proc.wait(timeout=30)

    scored = sorted(p.name for p in (tmp_path / "results").glob("*.json"))
    assert len(scored) >= 3, "a SIGKILLed run left no scored cases"
    assert len(list((tmp_path / "conversations").glob("*.json"))) >= len(scored)
    report = json.loads((tmp_path / "report.json").read_text())     # parses: the write was atomic
    assert report["partial"] is True                               # ...and says what it is
    assert report["cases_total"] == 40
    assert report["cases_scored"] == len(report["views"]["per_data_point"]) >= 3
    assert report["final_score"] is not None                       # a score over what it got through
    assert (tmp_path / "report.html").is_file()                    # the tracer has something to serve
    progress = json.loads((tmp_path / TranscriptStore.PROGRESS).read_text())
    assert progress["partial"] is True and progress["cases_scored"] >= 3


# --- the progress recorder on its own, with no threading in the way -----------------------------

def test_progress_recorder_rewrites_the_report_per_case(tmp_path):
    from appsci_bench.eval.eval_system import _ProgressRecorder

    store = TranscriptStore(tmp_path)
    objectives = _objectives()
    recorder = _ProgressRecorder(store, task_id="t", run_id="run_x", objectives=objectives,
                                 cases_total=3, reward_objective=None)
    accumulated: list[DataPointMetricResult] = []
    for i in range(2):
        rec = GenerationRecord(f"d{i}", 0)
        unit = [DataPointMetricResult(f"d{i}", "match", 0, 1.0, {})]
        accumulated.extend(unit)
        recorder.unit_scored(rec, unit, accumulated)
        report = json.loads((tmp_path / "report.json").read_text())
        assert report["partial"] is True
        assert report["cases_scored"] == i + 1 and report["cases_total"] == 3
        assert len(list((tmp_path / "results").glob("*.json"))) == i + 1
        progress = json.loads((tmp_path / TranscriptStore.PROGRESS).read_text())
        assert progress["cases_scored"] == i + 1 and progress["last_data_point_id"] == f"d{i}"


def test_per_case_results_and_transcripts_both_land_in_the_store(tmp_path):
    run_evaluation(_spec(3), InProcessArtifactClient(_echo), package=_PKG, workdir=str(tmp_path))
    assert len(list((tmp_path / "conversations").glob("*.json"))) == 3
    assert len(list((tmp_path / "results").glob("*.json"))) == 3
    # Each results file is readable on its own -- the durable record a report rebuild would use.
    doc = json.loads(sorted((tmp_path / "results").glob("*.json"))[0].read_text())
    assert doc["results"] and doc["results"][0]["objective"] == "match"


def test_progress_file_tracks_the_case_count(tmp_path):
    run_evaluation(_spec(3), InProcessArtifactClient(_echo), package=_PKG, workdir=str(tmp_path))
    doc = json.loads((tmp_path / TranscriptStore.PROGRESS).read_text())
    assert doc["cases_scored"] == 3 and doc["cases_total"] == 3
    assert doc["task_id"] == "t_partial" and doc["last_data_point_id"]


def test_cases_total_counts_units_not_data_points(tmp_path):
    spec = _spec(2)
    spec["evaluation_data"]["repeat"] = 3
    rep = run_evaluation(spec, InProcessArtifactClient(_echo), package=_PKG, workdir=str(tmp_path))
    assert rep.cases_total == 6 and rep.cases_scored == 6     # 2 data points x 3 repeats


def test_case_score_events_are_emitted_during_the_run_not_after(tmp_path, monkeypatch):
    """The tracer timeline is the other half of "show the samples that were scored": emitting the
    per-case events after aggregation meant a killed run's timeline showed none of them."""
    emitted: list[str] = []
    seen_during_the_run: list[int] = []

    def fake_emit(event_type, payload):
        emitted.append(str(getattr(event_type, "value", event_type)))

    monkeypatch.setattr(eval_system, "_emit_obs", fake_emit)

    class Watching(InProcessArtifactClient):
        """Counts case events already emitted, from inside a query -- i.e. mid-run."""

        def query_session(self, session_id, query):
            time.sleep(0.02)
            seen_during_the_run.append(emitted.count("eval.case_score"))
            return super().query_session(session_id, query)

    run_evaluation(_spec(4), Watching(_echo), package=_PKG, workdir=str(tmp_path),
                   setup_state=_noop_setup)
    assert emitted.count("eval.case_score") == 4     # one per case, no duplicate final pass
    assert emitted.count("eval.complete") == 1
    assert max(seen_during_the_run) > 0, "case events still only arrive after the last case"


def test_multi_turn_runs_keep_the_same_guarantee(tmp_path):
    spec = {"task_id": "t_mt", "evaluation_data": {
        "type": "multi_turn", "max_turns": 2,
        "data": [{"data_point_id": f"m{i}",
                  "multi_turn_input": {"user_task": "t", "user_persona": "p"}} for i in range(2)],
        "objectives": [{"name": "match", "eval_function_path": "eval_task.eval_fns.text_exact_match",
                        "positive": True, "weight": 1.0}],
    }}
    rep = run_evaluation(spec, InProcessArtifactClient(_echo), package=_PKG,
                         user_sim=ScriptedArtifactAgentUserSim(["hi"]), workdir=str(tmp_path))
    assert rep.cases_scored == 2 and rep.partial is False
    assert len(list((tmp_path / "results").glob("*.json"))) == 2


# --- the report is not readable as a complete one ---------------------------------------------

def test_eval_report_round_trips_the_partial_fields():
    rep = EvalReport(task_id="t", partial=True, cases_scored=80, cases_total=97)
    back = EvalReport.from_dict(rep.to_dict())
    assert back.partial is True and back.cases_scored == 80 and back.cases_total == 97


def test_a_report_without_the_fields_reads_as_unknown_not_complete():
    # A hand-built or pre-existing report: partial defaults False (it is not evidence of a kill) but
    # the counts stay None rather than becoming 0, which would render as "scored nothing".
    back = EvalReport.from_dict({"task_id": "t", "final_score": 0.5})
    assert back.partial is False and back.cases_scored is None and back.cases_total is None


def test_headline_carries_partial_and_the_counts():
    from shared.observability import eval_report as reader
    head = reader.headline({"task_id": "t", "final_score": 0.41, "partial": True,
                            "cases_scored": 80, "cases_total": 97,
                            "views": {"per_data_point": [{"data_point_id": "d0", "score": 0.4}]},
                            "results": []})
    assert head["partial"] is True and head["cases_scored"] == 80 and head["cases_total"] == 97


@pytest.mark.parametrize("value", ["80", -1, None, True])
def test_headline_rejects_an_unusable_case_count(value):
    from shared.observability import eval_report as reader
    head = reader.headline({"task_id": "t", "cases_scored": value, "views": {}, "results": []})
    assert head["cases_scored"] is None          # unknown, never a confident zero




