"""Tests for the eval_generator tool. Offline: mini_swe_agent is faked, no LLM/keys."""
from __future__ import annotations

import os
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from appsci_agent.tools import EvalGeneratorTool, ToolResult
from appsci_agent.tools.workspace_layout import HANDOFF_DIRNAME
from appsci_agent.tools.eval_generator import (
    HANDOFF_FILENAME,
    handoff_path,
    REQUIRED_EVAL_FILES,
    eval_harness_dir,
    read_sanctioned_harness,
)


def _ctx(tmp_path):
    """The deployed layout: the handoff and the harness both sit under the workspace."""
    artifact = tmp_path / "artifact"
    artifact.mkdir(exist_ok=True)
    return SimpleNamespace(
        artifact_dir=str(artifact), workspace_dir=str(tmp_path), eval_ready=False,
    )


def _handoff_file(ctx) -> pathlib.Path:
    """The handoff at whatever path the layout says, created parents and all.

    Resolved through `handoff_path` rather than rebuilt here: a test that hardcodes the location stops
    testing the tool the moment the layout moves, which is exactly what happened when the handoff came
    out of the artifact directory.
    """
    path = pathlib.Path(handoff_path(ctx))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_eval_files(eval_dir):
    os.makedirs(eval_dir, exist_ok=True)
    with open(os.path.join(eval_dir, "eval_data.json"), "w") as f:
        f.write('[{"id": "1", "scenario": "s", "ground_truth": "g", "metrics": "exact"}]')
    with open(os.path.join(eval_dir, "run.py"), "w") as f:
        f.write('print(\'{"headline_metric": 1.0, "metrics_breakdown": [], "cases": []}\')')
    with open(os.path.join(eval_dir, "README.md"), "w") as f:
        f.write("# Eval\n")


def test_fails_when_handoff_missing(tmp_path):
    tool = EvalGeneratorTool()
    ctx = _ctx(tmp_path)

    result = tool.execute({}, ctx)

    assert not result.success
    assert HANDOFF_FILENAME in result.error
    assert ctx.eval_ready is False


def test_handoff_in_the_artifact_dir_does_not_count(tmp_path):
    """A handoff in the artifact directory is not the handoff -- it must live under the workspace.

    Guards the move out of the artifact directory. Everything there is hashed into the fingerprint
    that identifies the candidate, so writing the handoff there made the next optimize_agent call
    report the artifact as stale and cost a redeploy; it was also snapshotted and rolled back with
    the candidate, silently reverting handoff corrections the tools ask the agent to make.
    """
    tool = EvalGeneratorTool()
    ctx = _ctx(tmp_path)
    (pathlib.Path(ctx.artifact_dir) / HANDOFF_FILENAME).write_text("task: does X\n")

    result = tool.execute({}, ctx)

    assert not result.success
    assert result.output["handoff_path"] == handoff_path(ctx)
    assert HANDOFF_DIRNAME in result.output["handoff_path"]
    assert ctx.eval_ready is False


def test_happy_path_sets_eval_ready(tmp_path):
    ctx = _ctx(tmp_path)
    _handoff_file(ctx).write_text(
        "task: does X\ncriterion: response matches Y\n")

    def fake_execute(self, params, context):
        # The harness goes where the tool told it to: under the WORKSPACE, not the artifact dir.
        _write_eval_files(eval_harness_dir(context))
        return ToolResult(success=True, output={"status": "completed", "result": "wrote eval"})

    with patch("appsci_agent.tools.eval_generator.MiniSweAgentTool.execute", fake_execute):
        result = EvalGeneratorTool().execute({}, ctx)

    assert result.success
    assert ctx.eval_ready is True
    eval_dir = os.path.join(str(tmp_path), "eval")
    assert result.output["eval_dir"] == eval_dir
    assert set(result.output["files"]) == {os.path.join(eval_dir, f) for f in REQUIRED_EVAL_FILES}
    # Not in the deliverable: the artifact dir is the Docker build context.
    assert not os.path.exists(os.path.join(ctx.artifact_dir, "eval"))
    # And the harness it wrote is now the sanctioned one.
    assert result.output["eval_fingerprint"]
    assert read_sanctioned_harness(str(tmp_path)) == result.output["eval_fingerprint"]


def test_fails_when_delegate_does_not_write_files(tmp_path):
    ctx = _ctx(tmp_path)
    _handoff_file(ctx).write_text("task: does X\n")

    def fake_execute(self, params, context):
        # Reports success but never actually writes the eval/ files.
        return ToolResult(success=True, output={"status": "completed"})

    with patch("appsci_agent.tools.eval_generator.MiniSweAgentTool.execute", fake_execute):
        result = EvalGeneratorTool().execute({}, ctx)

    assert not result.success
    assert "missing" in result.error.lower()
    for fname in REQUIRED_EVAL_FILES:
        assert fname in result.error
    assert ctx.eval_ready is False


def _agent_facing_surfaces() -> dict[str, str]:
    """Everything that tells the agent where the handoff goes, as the model sees it."""
    from appsci_agent.core.agent import AppSciAgent
    from appsci_agent.prompts import build_system_prompt
    from appsci_agent.tools import EvalGeneratorTool
    from appsci_agent.tools.workspace_layout import OPTIMIZE_DIRNAME

    bare = AppSciAgent.__new__(AppSciAgent)
    return {
        "eval_generator tool description": EvalGeneratorTool().description,
        "system prompt": build_system_prompt(
            task_brief="t", workspace_dir="/workspace",
            handoff_dirname=HANDOFF_DIRNAME, optimize_dirname=OPTIMIZE_DIRNAME),
        "dynamic task message": AppSciAgent._format_task_message(
            bare, "t", environment=None, optimize_cycles=3),
    }


def test_no_agent_facing_surface_names_a_stale_handoff_path() -> None:
    """Every surface that names the handoff must name its CURRENT directory.

    This has been wrong twice in one change. The prompt kept saying "your workspace directory" after
    the file moved out of the artifact directory, and the tool description kept the pre-move path.
    Both sit in front of the model every turn, so the agent writes where the surface says and
    `eval_generator` then refuses with a different path -- the exact failure the move set out to fix.
    """
    for name, text in _agent_facing_surfaces().items():
        if HANDOFF_FILENAME not in text:
            continue
        assert HANDOFF_DIRNAME in text, (
            f"{name} names {HANDOFF_FILENAME} but never its directory ({HANDOFF_DIRNAME}), so the "
            f"agent has to guess where it goes")


@pytest.mark.parametrize("stale", [
    "your workspace directory",          # the vague noun that was read as /work
    "<workspace>/eval_handoff.md",       # the un-rendered placeholder
    "/workspace/eval_handoff.md",        # the pre-move workspace-root location
    "artifact output directory. Start",  # the original artifact-directory instruction
])
def test_no_agent_facing_surface_carries_a_superseded_location(stale) -> None:
    """Each of these was a real location at some point, which is what makes them easy to leave behind."""
    for name, text in _agent_facing_surfaces().items():
        assert stale not in text, f"{name} still carries the superseded location {stale!r}"
