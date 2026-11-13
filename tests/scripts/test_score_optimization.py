"""Tests for scripts/score_optimization.py -- the hand-operated entry to the optimization diagnostic.

The script lives in scripts/ (a runnable tool, not an importable package), so it is loaded by path,
mirroring test_score_elicitation.py. Nothing here reaches a model: the diagnostic only counts records
a run already wrote.

Three behaviours get most of the attention. Exit status is about whether the tool could RUN, never
about what it found -- a diagnostic that failed the shell on an unreadable run would get wrapped in
`|| true` and stop being read. `--update-report` has to ADD a section to a run's existing report
without taking anything out of it. And `--runs-dir` has to pick real runs out of a directory that
also holds other things, rather than reporting a README as a run with no optimization.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "score_optimization.py"


def _load():
    spec = importlib.util.spec_from_file_location("_score_optimization_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script():
    return _load()


def _events(*, calls: int, saturated: bool, rollback_at: int | None = None) -> list[dict]:
    """A loop's trace in the shape `optimize_agent` actually writes it.

    A run that SATURATED ends on a call that measured and changed nothing, and only then does the
    status say `saturated`. The tool scores the deployed candidate before it edits, so a cycle that
    changed and redeployed leaves a score nobody has taken yet and reports `awaiting_verification` --
    which wins over saturation in the tool's own status field. A call claiming both at once is one the
    tool cannot produce, and a fixture that claimed it was what let the reader's own ordering go
    unnoticed.

    Every call after one that changed the candidate carries a VERDICT on it, because that is the only
    shape the tool produces: the change is measured by the next call, which then keeps it or restores
    the snapshot. `rollback_at` makes that call reject instead of keep.
    """
    out: list[dict] = []
    for i in range(1, calls + 1):
        span = f"s{i}"
        out.append({"source": "appsci_agent", "event_type": "agent.tool_invocation",
                    "span_id": span, "timestamp": f"2026-09-01T12:{2 * i:02d}:00+00:00",
                    "task_id": "order_to_cash", "payload": {"tool": "optimize_agent"}})
        last = i == calls
        measured_only = last and saturated
        cycle = {"changed": False} if measured_only else {"changed": True, "redeployed": True}
        if i > 1:
            cycle["verified_previous"] = (
                {"accepted": False, "reverted": True, "revert": "reverted",
                 "score_to_beat": 0.4 + 0.1 * (i - 1), "new_score": 0.4 + 0.1 * i}
                if i == rollback_at else
                {"accepted": True, "reverted": False,
                 "score_to_beat": 0.4 + 0.1 * (i - 1), "new_score": 0.4 + 0.1 * i})
        status = ("saturated" if measured_only else "awaiting_verification")
        out.append({"source": "appsci_agent", "event_type": "agent.tool_result", "span_id": span,
                    "timestamp": f"2026-09-01T12:{2 * i + 1:02d}:00+00:00",
                    "task_id": "order_to_cash",
                    "payload": {"success": True, "status": status,
                                "iteration": i, "headline_metric": 0.4 + 0.1 * i,
                                "saturated": bool(last and saturated), "budget_exhausted": False,
                                "cycles_used": i - 1, "cycles_allowed": 5,
                                "cycle": cycle}})
    return out


@pytest.fixture
def run(tmp_path):
    """A run directory carrying an agent event log and the build.start flag."""
    def _build(name: str = "order_to_cash_20260901_120000", *, calls: int = 2,
               saturated: bool = True, rollback_at: int | None = None,
               checkpoints: list[dict] | None = None) -> pathlib.Path:
        run_dir = tmp_path / "runs" / name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "events-appsci_agent.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in _events(
                calls=calls, saturated=saturated, rollback_at=rollback_at)))
        (run_dir / "events-orchestrator.jsonl").write_text(json.dumps(
            {"source": "orchestrator", "event_type": "build.start",
             "timestamp": "2026-09-01T12:00:00+00:00", "task_id": "order_to_cash",
             "payload": {"optimization_enabled": True, "optimize_cycles": 5}}) + "\n")
        if checkpoints is not None:
            (run_dir / "checkpoints_manifest.json").write_text(
                json.dumps({"trace_id": name, "checkpoints": checkpoints}))
        return run_dir
    return _build


def test_writes_the_result_beside_the_run(script, run, capsys):
    run_dir = run(checkpoints=[{"ordinal": 1, "name": "artifact-001", "outcome": "deployed",
                                "created_at": "2026-09-01T12:02:30+00:00"}])

    assert script.main(["--run", str(run_dir)]) == 0

    payload = json.loads((run_dir / "optimization.json").read_text())
    assert payload["status"] == "scored"
    assert payload["calls"] == 2
    assert payload["stop_reason"] == "saturated"
    assert payload["early_stopped_on_saturation"] is True
    # One: the call that saturated the loop is the one that measured and changed nothing.
    assert payload["deploys_succeeded"] == 1
    assert payload["checkpoints_valid"] == 1
    out = capsys.readouterr().out
    assert "optimization runs" in out
    assert str(run_dir / "optimization.json") in out


def test_no_write_leaves_the_run_untouched(script, run):
    run_dir = run()

    assert script.main(["--run", str(run_dir), "--no-write"]) == 0
    assert not (run_dir / "optimization.json").exists()


def test_an_unreadable_run_is_reported_and_still_exits_zero(script, tmp_path, capsys):
    """Exit status is about whether the tool could run. A missing trace is a finding, not a failure
    of the tool."""
    missing = tmp_path / "not_a_run"
    missing.mkdir()

    assert script.main(["--run", str(missing), "--no-write"]) == 0
    assert "unmeasurable" in capsys.readouterr().out


def test_naming_no_input_is_a_usage_error(script):
    assert script.main([]) == 2


def test_a_runs_dir_that_is_not_a_directory_is_a_usage_error(script, tmp_path):
    assert script.main(["--runs-dir", str(tmp_path / "nope")]) == 2


def test_a_runs_dir_with_no_runs_in_it_yet_is_not_a_crash(script, tmp_path, capsys):
    """The first thing anyone points this at -- a sweep directory not yet populated -- and it used to
    raise out of the table's column-width calculation. Exit status here is about whether the tool
    could run, and it could: the directory exists and holds no runs, which is an answer."""
    empty = tmp_path / "runs"
    empty.mkdir()
    dest = tmp_path / "out" / "optimization.csv"

    assert script.main(["--runs-dir", str(empty), "--csv", str(dest)]) == 0

    captured = capsys.readouterr()
    assert "nothing to score" in captured.err
    assert "0 run(s) read, 0 with an optimization loop" in captured.out
    # The CSV still gets its header row: a file with no columns would break whatever reads it next,
    # and "this sweep found nothing" is a row count of zero, not a missing table.
    rows = dest.read_text().splitlines()
    assert len(rows) == 1 and rows[0].startswith("run,task_id,status")


def test_runs_dir_scores_every_run_and_skips_what_is_not_one(script, run, tmp_path, capsys):
    first = run("order_to_cash_20260901_120000", calls=2, saturated=True)
    second = run("airline_rebooking_20260901_130000", calls=1, saturated=False)
    # Things that live under runs/ and are not runs: a README, and a directory with no event log.
    (first.parent / "README.md").write_text("notes")
    (first.parent / "checkpoint_scoring").mkdir()

    assert script.main(["--runs-dir", str(tmp_path / "runs"), "--no-write"]) == 0

    out = capsys.readouterr().out
    assert first.name in out and second.name in out
    assert "checkpoint_scoring" not in out
    assert "2 run(s) read, 2 with an optimization loop" in out
    # The rollup counts RUNS, not cycles: only the first one stopped on saturation.
    assert "1 stopped early on saturation" in out


def test_the_detail_view_prints_the_loops_own_trajectory(script, run, capsys):
    run_dir = run(calls=2)

    assert script.main(["--run", str(run_dir), "--no-write"]) == 0

    out = capsys.readouterr().out
    assert "internal eval first   0.500" in out
    assert "internal eval best    0.600 (call 2)" in out
    assert "internal eval final   0.600" in out
    assert "internal eval change  +0.100" in out


def test_the_benchmark_score_is_printed_beside_the_loops_final_one(script, run, capsys):
    run_dir = run(calls=2)
    (run_dir / "eval").mkdir()
    (run_dir / "eval" / "report.json").write_text(json.dumps(
        {"task_id": "order_to_cash", "final_score": 0.25}))

    assert script.main(["--run", str(run_dir), "--no-write"]) == 0

    out = capsys.readouterr().out
    assert "benchmark score       0.250 (+0.350 from the loop's final)" in out
    # Never as an error or an agreement score: the two measure different things.
    assert "a distance, not an error" in out


def test_a_run_with_no_readable_report_simply_omits_the_comparison(script, run, capsys):
    """A build-only cell never reached an eval, so the absence is ordinary and must cost only the one
    line. A corrupt report is the same case, not a crash."""
    run_dir = run(calls=2)
    (run_dir / "eval").mkdir()
    (run_dir / "eval" / "report.json").write_text("{not json")

    assert script.main(["--run", str(run_dir), "--no-write"]) == 0

    out = capsys.readouterr().out
    assert "benchmark score" not in out
    assert "internal eval final   0.600" in out


def test_the_batch_table_and_rollup_carry_the_trajectory(script, run, tmp_path, capsys):
    run("order_to_cash_20260901_120000", calls=2)
    run("airline_rebooking_20260901_130000", calls=1, saturated=False)

    assert script.main(["--runs-dir", str(tmp_path / "runs"), "--no-write"]) == 0

    out = capsys.readouterr().out
    assert "int.end" in out and "int.chg" in out
    assert "0.600" in out and "+0.100" in out
    # The one-call run measured once, so it has a final but no change to report.
    assert "run(s) with a measured trajectory improved on their own eval" in out


def test_the_detail_view_counts_what_the_loop_did_with_its_own_changes(script, run, capsys):
    run_dir = run(calls=3, saturated=True, rollback_at=2)

    assert script.main(["--run", str(run_dir), "--no-write"]) == 0

    out = capsys.readouterr().out
    assert "changes kept          1 of 2 judged" in out
    assert "changes rolled back   1" in out
    assert "roll-back redeploys   1" in out
    assert "rolled the previous change back (reverted)" in out


def test_the_detail_view_reconciles_the_deploy_and_checkpoint_tallies(script, run, capsys):
    """A restore is a redeploy that is not a change, so it leaves a checkpoint the deploy tally does
    not explain. This is the line that says so with the arithmetic."""
    run_dir = run(calls=3, saturated=True, rollback_at=2, checkpoints=[
        {"ordinal": 1, "name": "artifact-001", "outcome": "deployed",
         "created_at": "2026-09-01T12:02:30+00:00"},
        {"ordinal": 2, "name": "artifact-002", "outcome": "deployed",
         "created_at": "2026-09-01T12:04:30+00:00"},
    ])

    assert script.main(["--run", str(run_dir), "--no-write"]) == 0

    out = capsys.readouterr().out
    assert "2 redeploy(s) of a change + 1 roll-back redeploy(s) = 3" in out


def test_a_loop_with_no_rollbacks_prints_no_reconciliation_line(script, run, capsys):
    run_dir = run(calls=2, checkpoints=[
        {"ordinal": 1, "name": "artifact-001", "outcome": "deployed",
         "created_at": "2026-09-01T12:02:30+00:00"}])

    assert script.main(["--run", str(run_dir), "--no-write"]) == 0

    assert "roll-back redeploy(s) =" not in capsys.readouterr().out


def test_the_rollup_separates_a_worse_candidate_from_the_eval_re_measuring(script, run, tmp_path,
                                                                          capsys):
    """Two different findings. Only one is about the artifact; the other is the loop's own eval
    returning a different number for the same program, and reporting them as one number was wrong on
    four of the six runs it flagged in the sweep."""
    run("order_to_cash_20260901_120000", calls=2)
    run("airline_rebooking_20260901_130000", calls=1, saturated=False)

    assert script.main(["--runs-dir", str(tmp_path / "runs"), "--no-write"]) == 0

    out = capsys.readouterr().out
    assert "ended on a WORSE candidate than their best" in out
    assert "scored below their best on the same candidate" in out


def test_a_deploy_between_calls_is_reported_as_outside_the_loop(script, run, capsys):
    run_dir = run(calls=2, checkpoints=[
        {"ordinal": 1, "name": "artifact-001", "outcome": "deployed",
         "created_at": "2026-09-01T12:02:30+00:00"},
        # Between call 1 (ends 12:03) and call 2 (starts 12:04): the agent's own deploy.
        {"ordinal": 2, "name": "artifact-002", "outcome": "deployed",
         "created_at": "2026-09-01T12:03:30+00:00"},
    ])

    assert script.main(["--run", str(run_dir), "--no-write"]) == 0

    out = capsys.readouterr().out
    assert "deploys outside loop  1" in out
    assert "the keep-or-roll-back rule does not cover them" in out


def test_the_batch_rollup_counts_rollbacks_and_says_whether_the_tallies_close(script, run,
                                                                             tmp_path, capsys):
    run("order_to_cash_20260901_120000", calls=3, rollback_at=2, checkpoints=[
        {"ordinal": 1, "name": "artifact-001", "outcome": "deployed",
         "created_at": "2026-09-01T12:02:30+00:00"},
        {"ordinal": 2, "name": "artifact-002", "outcome": "deployed",
         "created_at": "2026-09-01T12:04:30+00:00"},
    ])
    run("airline_rebooking_20260901_130000", calls=1, saturated=False)

    assert script.main(["--runs-dir", str(tmp_path / "runs"), "--no-write"]) == 0

    out = capsys.readouterr().out
    assert "1 rolled back at least one change, 1 roll-back(s) in total" in out
    assert "0 had a roll-back that did not take" in out
    assert "reconcile: successful redeploys plus roll-back redeploys" in out


def test_the_csv_carries_one_row_per_run(script, run, tmp_path):
    run("order_to_cash_20260901_120000")
    run("airline_rebooking_20260901_130000", calls=1, saturated=False)
    dest = tmp_path / "out" / "optimization.csv"

    assert script.main(["--runs-dir", str(tmp_path / "runs"), "--no-write",
                        "--csv", str(dest)]) == 0

    rows = dest.read_text().splitlines()
    assert rows[0].startswith("run,task_id,status")
    assert "internal_eval_final,internal_eval_change" in rows[0]
    assert "changes_kept,changes_rolled_back,rollback_redeploys,rollbacks_failed" in rows[0]
    assert len(rows) == 3


def test_the_csv_writes_the_score_list_as_one_parseable_cell(script, run, tmp_path):
    """A Python repr there would need its own parser and would break on the comma when a spreadsheet
    re-quotes the file."""
    run("order_to_cash_20260901_120000", calls=3)
    dest = tmp_path / "out" / "optimization.csv"

    assert script.main(["--runs-dir", str(tmp_path / "runs"), "--no-write",
                        "--csv", str(dest)]) == 0

    rows = dest.read_text().splitlines()
    assert "internal_eval_all_scores" in rows[0]
    assert "0.500000|0.600000|0.700000" in rows[1]
    assert "[" not in rows[1]


def test_update_report_adds_the_section_without_dropping_the_transcripts(script, run):
    """The failure the sibling script shipped with: re-rendering from the report alone silently
    strips every case transcript, so an "update" shrinks the report it was asked to add to."""
    run_dir = run()
    eval_dir = run_dir / "eval"
    (eval_dir / "conversations").mkdir(parents=True)
    (eval_dir / "conversations" / "dp1.json").write_text(json.dumps({
        "data_point_id": "dp1", "repeat_index": 0,
        "conversation": [{"query": {"text": "the only line in this transcript"},
                          "response": {"text": "ok"}}],
    }))
    (eval_dir / "report.json").write_text(json.dumps({
        "task_id": "order_to_cash", "final_score": 0.75,
        "results": [{"data_point_id": "dp1", "objective": "o1", "repeat_index": 0, "score": 0.75}],
        "views": {"per_data_point": [{"data_point_id": "dp1", "score": 0.75}],
                  "per_metric": [{"objective": "o1", "score": 0.75, "weight": 1.0}]},
    }))

    assert script.main(["--run", str(run_dir), "--update-report"]) == 0

    after = json.loads((eval_dir / "report.json").read_text())
    assert after["optimization"]["calls"] == 2
    # Nothing else moved.
    assert after["final_score"] == 0.75
    assert len(after["results"]) == 1
    html = (eval_dir / "report.html").read_text()
    assert "Optimization diagnostic" in html
    assert "the only line in this transcript" in html


def test_update_report_without_a_report_is_a_note_not_a_crash(script, run, capsys):
    run_dir = run()

    assert script.main(["--run", str(run_dir), "--update-report"]) == 0
    assert "no report to update" in capsys.readouterr().err
