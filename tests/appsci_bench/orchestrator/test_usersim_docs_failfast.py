"""The orchestrator's per-task fail-fast on a bad document declaration (issue #76, D2).

Two properties, and the first matters more than the exit code:

  1. NOTHING starts. No submission server, no task environment, no user-sim, no agent container. A
     validation error is an authoring mistake known before any Docker work, so paying for that work and
     then failing is pure waste.
  2. It fails THIS task only. The result is an ordinary failed BenchmarkResult (exit 1 via main), never
     the exit-3 environment-blocker path -- scripts/run_tasks.sh treats 3 as abort-the-whole-batch, and
     one task's bad declaration says nothing about the others.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib

import pytest

from appsci_bench.orchestrator.run import BenchmarkOrchestrator

SPEC = """
task_id: demo
name: Demo
description: a demo task
business_goals: ["ship it"]
environment:
  resources: []
artifacts:
  output_dir: /work/out
task_runner:
  network_name: bench-network
  workspace_name: demo-workspace
evaluation_data:
  dataset: tasks/demo/eval_data
  objectives: []
user_personas:
  - tasks/demo/user_personas/l1.yaml
"""


@pytest.fixture
def broken_task(tmp_path: pathlib.Path):
    """A task whose L1 persona declares a document that is not there."""
    tasks_dir = tmp_path / "tasks"
    task = tasks_dir / "demo"
    (task / "user_personas").mkdir(parents=True)
    (task / "spec.yaml").write_text(SPEC)
    (task / "user_sim_docs").mkdir()
    (task / "user_personas" / "l1.yaml").write_text(
        "persona_id: l1\ntask_id: demo\nlevel: L1\nrole: Manager\nknown_docs:\n  - policy.md\n"
    )
    return tasks_dir, task


def _orchestrator(tasks_dir: pathlib.Path, tmp_path: pathlib.Path) -> BenchmarkOrchestrator:
    return BenchmarkOrchestrator(tasks_dir=tasks_dir, runs_dir=tmp_path / "runs")


def _forbid_all_startup(monkeypatch) -> list[str]:
    """Make every startup path explode, so 'nothing started' is proven rather than assumed."""
    started: list[str] = []

    def boom(name):
        def _f(*a, **kw):
            started.append(name)
            raise AssertionError(f"{name} must not start when the docs declaration is invalid")
        return _f

    import appsci_bench.orchestrator.run as run_mod

    monkeypatch.setattr(run_mod, "ArtifactSubmissionServer", boom("submission_server"))
    monkeypatch.setattr(run_mod, "DockerAppsciAgentUserSimService", boom("usersim"))
    monkeypatch.setattr(run_mod, "AppSciAgentContainerManager", boom("agent_container"))
    monkeypatch.setattr(run_mod, "write_agent_spec", boom("write_agent_spec"))
    return started


def test_bad_declaration_starts_nothing(broken_task, tmp_path, monkeypatch) -> None:
    tasks_dir, task = broken_task
    started = _forbid_all_startup(monkeypatch)
    orch = _orchestrator(tasks_dir, tmp_path)

    result = asyncio.run(orch.run(task_id="demo",
                                  persona_path=task / "user_personas" / "l1.yaml"))

    assert started == []
    assert result.status == "failed"


def test_failure_is_per_task_not_an_environment_blocker(broken_task, tmp_path, monkeypatch) -> None:
    """The result must flow through the ordinary failed-task path (main exits 1). Exit 3 is reserved
    for blockers that will hit every remaining row, and would abort the batch."""
    tasks_dir, task = broken_task
    _forbid_all_startup(monkeypatch)
    orch = _orchestrator(tasks_dir, tmp_path)

    result = asyncio.run(orch.run(task_id="demo",
                                  persona_path=task / "user_personas" / "l1.yaml"))

    assert result.status == "failed"          # -> sys.exit(1)
    assert result.task_id == "demo"
    assert result.scores is None


def test_error_message_and_metadata_name_the_problem(broken_task, tmp_path, monkeypatch) -> None:
    tasks_dir, task = broken_task
    _forbid_all_startup(monkeypatch)
    orch = _orchestrator(tasks_dir, tmp_path)

    result = asyncio.run(orch.run(task_id="demo",
                                  persona_path=task / "user_personas" / "l1.yaml"))

    assert "user-sim document" in result.error
    assert "policy.md" in result.error
    assert result.metadata["usersim_docs_errors"]


def test_every_error_is_logged_not_just_the_first(broken_task, tmp_path, monkeypatch, caplog) -> None:
    """Fixing one error per re-run, each costing a container start, is miserable."""
    tasks_dir, task = broken_task
    for name in ("l2", "l3"):
        (task / "user_personas" / f"{name}.yaml").write_text(
            f"persona_id: {name}\ntask_id: demo\nlevel: L1\nrole: M\nknown_docs:\n  - {name}_doc.md\n"
        )
    _forbid_all_startup(monkeypatch)
    orch = _orchestrator(tasks_dir, tmp_path)

    with caplog.at_level(logging.ERROR):
        asyncio.run(orch.run(task_id="demo",
                             persona_path=task / "user_personas" / "l1.yaml"))

    logged = "\n".join(r.message for r in caplog.records)
    assert "policy.md" in logged and "l2_doc.md" in logged and "l3_doc.md" in logged


def test_warning_does_not_stop_the_run(tmp_path, monkeypatch, caplog) -> None:
    """An over-suggested-budget declaration is VALID: it must warn and let the run proceed.

    Proof that it proceeded: execution reaches `load_task_spec`, which resolves against the repo-global
    tasks dir (not the orchestrator's `tasks_dir` -- a pre-existing quirk) and so cannot find this
    synthetic task. Getting that error rather than a failed BenchmarkResult is exactly what shows
    validation did not block.
    """
    from appsci_bench.appsci_agent_user_sim.core.persona import SUGGESTED_DOC_TOKENS

    tasks_dir = tmp_path / "tasks"
    task = tasks_dir / "demo"
    (task / "user_personas").mkdir(parents=True)
    (task / "spec.yaml").write_text(SPEC)
    (task / "user_sim_docs").mkdir()
    (task / "user_sim_docs" / "big.md").write_text("x" * ((SUGGESTED_DOC_TOKENS + 500) * 4))
    (task / "user_personas" / "l1.yaml").write_text(
        "persona_id: l1\ntask_id: demo\nlevel: L1\nrole: M\nknown_docs:\n  - big.md\n"
    )
    _forbid_all_startup(monkeypatch)
    orch = _orchestrator(tasks_dir, tmp_path)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(FileNotFoundError):
            asyncio.run(orch.run(task_id="demo",
                                 persona_path=task / "user_personas" / "l1.yaml"))

    warnings = "\n".join(r.message for r in caplog.records if r.levelno == logging.WARNING)
    assert "over the suggested" in warnings
    assert not any(r.levelno >= logging.ERROR for r in caplog.records), "a warning must not log ERROR"
