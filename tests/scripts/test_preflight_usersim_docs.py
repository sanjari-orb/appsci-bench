"""The batch preflight (issue #76, D3).

Contract with scripts/run_tasks.sh:
  - report EVERY bad task before the first run starts, so one fix pass clears them all;
  - exit 0 even when tasks fail -- a bad declaration is per-task, and the caller skips just those rows.
    Exit 2 means the preflight itself could not run, which IS worth aborting the batch on;
  - emit machine-readable `OK  /WARN/FAIL <task>` lines the shell can parse with awk.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "preflight_usersim_docs.py"

SPEC = """
task_id: {tid}
name: Demo
description: a demo task
business_goals: ["ship"]
environment:
  resources: []
artifacts:
  output_dir: /work/out
"""


def _run(*args: str, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(cwd or REPO),
    )


def test_all_real_tasks_pass_today() -> None:
    """No task has adopted known_docs, so every one must report OK."""
    result = _run("--all")

    assert result.returncode == 0, result.stderr
    assert "FAIL" not in result.stdout
    assert "OK   airline_rebooking" in result.stdout
    assert "OK   order_to_cash" in result.stdout


def test_named_tasks_only() -> None:
    result = _run("airline_rebooking")

    assert result.returncode == 0
    assert "airline_rebooking" in result.stdout
    assert "order_to_cash" not in result.stdout


def test_unknown_task_reports_fail_but_exits_zero() -> None:
    """Exit 0 is deliberate: the caller skips this row rather than aborting the batch."""
    result = _run("no_such_task")

    assert result.returncode == 0
    assert "FAIL no_such_task" in result.stdout


def test_no_arguments_is_exit_two() -> None:
    """Nothing to check means the preflight could not do its job -- a blocker, not a skip."""
    result = _run()

    assert result.returncode == 2
    assert "no tasks given" in result.stderr


def test_duplicate_task_ids_are_deduped() -> None:
    result = _run("airline_rebooking", "airline_rebooking")

    assert result.stdout.count("OK   airline_rebooking") == 1


def test_output_lines_are_parseable_by_the_shell() -> None:
    """run_tasks.sh does `awk '/^FAIL /{print $2}'`, so the shape is load-bearing."""
    result = _run("--all")

    for line in result.stdout.splitlines():
        assert line[:4] in ("OK  ", "WARN", "FAIL"), line
        parts = line.split()
        assert len(parts) >= 2


class TestAgainstSyntheticTasks:
    """The real coverage: a repo where a task IS broken. Runs the validator directly (the CLI resolves
    tasks/ relative to the repo root, which we must not mutate)."""

    def _task(self, tasks_dir: pathlib.Path, tid: str) -> pathlib.Path:
        task = tasks_dir / tid
        (task / "user_personas").mkdir(parents=True)
        (task / "spec.yaml").write_text(SPEC.format(tid=tid))
        return task

    def test_reports_every_bad_task_not_just_the_first(self, tmp_path) -> None:
        from appsci_bench.core.usersim_docs import validate_many

        for tid in ("bad_a", "bad_b", "good"):
            task = self._task(tmp_path, tid)
            if tid.startswith("bad"):
                (task / "user_sim_docs").mkdir()
                (task / "user_personas" / "l1.yaml").write_text(
                    "persona_id: l1\ntask_id: x\nlevel: L1\nrole: M\nknown_docs:\n  - gone.md\n"
                )
            else:
                (task / "user_personas" / "l1.yaml").write_text(
                    "persona_id: l1\ntask_id: x\nlevel: L1\nrole: M\n"
                )

        results = validate_many(["bad_a", "bad_b", "good"], tmp_path)

        assert [r.ok for r in results] == [False, False, True]
        assert all(r.errors for r in results if not r.ok)

    def test_a_good_task_is_unaffected_by_a_bad_sibling(self, tmp_path) -> None:
        """The whole point of per-task scoping: one authoring mistake must not cost the other rows."""
        from appsci_bench.core.usersim_docs import validate_many

        bad = self._task(tmp_path, "bad")
        (bad / "user_sim_docs").mkdir()
        (bad / "user_personas" / "l1.yaml").write_text(
            "persona_id: l1\ntask_id: x\nlevel: L1\nrole: M\nknown_docs:\n  - gone.md\n"
        )
        good = self._task(tmp_path, "good")
        (good / "user_sim_docs").mkdir()
        (good / "user_sim_docs" / "policy.md").write_text("RULES")
        (good / "user_personas" / "l1.yaml").write_text(
            "persona_id: l1\ntask_id: x\nlevel: L1\nrole: M\nknown_docs:\n  - policy.md\n"
        )

        results = {r.task_id: r for r in validate_many(["bad", "good"], tmp_path)}

        assert not results["bad"].ok
        assert results["good"].ok
