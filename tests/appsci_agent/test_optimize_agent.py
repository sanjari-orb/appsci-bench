"""Tests for the `optimize_agent` tool. Offline: no LLM, no docker, no orchestrator, no keys.

A cycle has TWO seams and both are faked here. The tool measures in code -- it runs the harness
itself as a fixed subprocess -- so `_run_harness` is stubbed to return a score. It then hands that
score to one `mini_swe_agent` run whose job is only to improve the candidate, so a stand-in writes the
change note and optionally edits the source with bash, as the real sub-agent does. `_patch_swe`
installs both from one `_swe(scores=...)` call. `deploy_artifact` and the orchestrator's deploy-status
route are patched too.

The cycle spans TWO calls by design -- a change is measured by the call after the one that makes it,
because the eval queries the deployed container and only the tool can redeploy -- so most of these
drive two calls and assert what happened across the pair.
"""
from __future__ import annotations

import contextlib
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest

from appsci_agent.tools import OptimizeAgentTool
from appsci_agent.tools.base import ToolResult
from appsci_agent.tools.optimize_agent import (
    HISTORY_FILENAME,
    MAX_BRIEF_REJECTED,
    MIN_IMPROVEMENT,
    NOTE_INBOX_DIRNAME,
    OPTIMIZE_DIRNAME,
    PENDING_FILENAME,
    REPORT_FILENAME,
    SATURATION_PATIENCE,
)

ENDPOINT = "http://cand:8080/respond"


# --- layout ---------------------------------------------------------------------------------------

@pytest.fixture
def artifact_dir(tmp_path):
    """The deployed layout: the artifact directory sits INSIDE the workspace.

    Mirrors the real one (`/workspace` and `/workspace/artifact`), so this tool's own files land
    outside the artifact directory exactly as they do in a run.
    """
    path = tmp_path / "artifact"
    path.mkdir()
    return path


def _write_eval(artifact_dir, *, sanction=True) -> str:
    """The harness `eval_generator` leaves behind, in the WORKSPACE (not the artifact dir).

    Its contents do not matter here -- the sub-agent is what runs it, and that is faked -- but the tool
    checks the files exist and that the harness matches what eval_generator blessed. `sanction=False`
    writes a harness nothing blessed, which is what an agent hand-rolling its own eval looks like.
    """
    from appsci_agent.tools.eval_generator import record_sanctioned_harness
    workspace = str(artifact_dir.parent)
    eval_dir = artifact_dir.parent / "eval"
    eval_dir.mkdir(exist_ok=True)
    (eval_dir / "eval_data.json").write_text("[]")
    (eval_dir / "README.md").write_text("eval")
    (eval_dir / "run.py").write_text("print('{}')\n")
    if sanction:
        record_sanctioned_harness(workspace, str(eval_dir))
    return str(eval_dir)


def _artifact(artifact_dir) -> None:
    (artifact_dir / "server.py").write_text("# v1\n")
    (artifact_dir / "Dockerfile").write_text("FROM python:3.12-slim\n")


def _ctx(artifact_dir, **overrides):
    base = {
        "workspace_dir": str(artifact_dir.parent), "artifact_dir": str(artifact_dir),
        "environment": {}, "conversation_history": [], "config": None,
        "artifact_endpoint": ENDPOINT, "artifact_idx": 1, "build_complete": False,
        "eval_ready": True, "parent_span_id": None, "optimize_cycles_allowed": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _report(score: float, *, cases=None, breakdown=None) -> dict:
    return {
        "headline_metric": score,
        "metrics_breakdown": breakdown if breakdown is not None
        else [{"metric_name": "accuracy", "score": score}],
        "cases": cases if cases is not None
        else [{"id": "c1", "passed": False, "detail": "wrong total"}],
    }


STATUS_OK = {
    "available": True, "configured": True, "deployed": True, "stale": False,
    "endpoint_url": ENDPOINT, "deployed_fingerprint": "abc123",
    "current_fingerprint": "abc123",
    "checkpoint": {"name": "artifact-o2c-002", "ordinal": 2,
                   "content_hash": "abc123", "outcome": "deployed"},
}


@pytest.fixture(autouse=True)
def _alive():
    """Make the candidate answer, for every test that does not say otherwise.

    Patches the HTTP call rather than `_probe_endpoint`, so the real probe code runs in every test.
    """
    with mock.patch("appsci_agent.tools.optimize_agent.urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value = SimpleNamespace(status=200)
        yield urlopen


def _patch_status(status: dict):
    return mock.patch.object(OptimizeAgentTool, "_deploy_status", return_value=status)


# --- sub-agent stand-ins --------------------------------------------------------------------------

def _swe(*, scores, edit_marker=None, write_report=True, note="targeted accuracy", fail=False):
    """A `mini_swe_agent` stand-in that behaves like the real one: it writes the change note and
    (optionally) edits the candidate's source with bash.

    It no longer writes the eval report -- the tool measures in code before the sub-agent starts, so
    `scores` is consumed by the `_run_harness` double that `_patch_swe` installs alongside this one,
    one score per harness run. `write_report=False` now means "the harness produces nothing usable",
    which is where that failure actually lives.
    """
    seen = []

    def _execute(self, params, context):
        task = params["task"]
        seen.append(task)
        if fail:
            return ToolResult(success=False, output={}, error="step limit reached")
        inbox = os.path.join(context.workspace_dir, NOTE_INBOX_DIRNAME)
        os.makedirs(inbox, exist_ok=True)
        with open(os.path.join(inbox, "change_note.md"), "w") as f:
            f.write(note)
        # Only edit when the brief actually allowed it, mirroring a sub-agent that follows its brief.
        if edit_marker and "Step 3 -- improve the candidate" in task:
            with open(os.path.join(context.artifact_dir, "server.py"), "w") as f:
                f.write(edit_marker)
        return ToolResult(success=True, output={"result": "done"})

    _execute.seen = seen
    _execute.scores = scores
    _execute.no_report = not write_report
    return _execute


def _deploy_ok(new_endpoint=None):
    def _execute(self, params, context):
        if new_endpoint:
            context.artifact_endpoint = new_endpoint
        context.artifact_idx += 1
        return ToolResult(success=True, output={"endpoint_url": context.artifact_endpoint})
    return _execute


def _patch_swe(fn):
    """Patch BOTH seams of a cycle: the in-code measurement, and the sub-agent that improves.

    The tool runs the harness itself now, so a test that wants a score has to feed `_run_harness`, not
    the sub-agent. Both come from the same `_swe(scores=...)` call so a test still declares its scores
    once, in one place, and reads the same as before.
    """
    return _BothPatches(fn)


class _BothPatches:
    def __init__(self, fn):
        self._stack = contextlib.ExitStack()
        self._fn = fn

    def __enter__(self):
        self._stack.enter_context(
            mock.patch("appsci_agent.tools.optimize_agent.MiniSweAgentTool.execute", self._fn))
        self._stack.enter_context(
            mock.patch("appsci_agent.tools.optimize_agent.OptimizeAgentTool._run_harness",
                       _harness_from(self._fn)))
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


def _fake_popen(seen, *, stdout="", stderr="", returncode=0, raise_timeout=False):
    """A `subprocess.Popen` stand-in recording how the harness was launched.

    `raise_timeout` makes the first `communicate` raise, as a real timeout does, and the second
    (the one after the group kill) return what the process had already written.
    """
    import subprocess as sp

    class _P:
        pid = 31337

        def __init__(self, command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            self.returncode = returncode
            self._raised = False

        def communicate(self, timeout=None):
            if raise_timeout and not self._raised:
                self._raised = True
                raise sp.TimeoutExpired(cmd=seen["command"], timeout=timeout or 1)
            return stdout, stderr

        def kill(self):
            seen["direct_kill"] = True

    return _P


def _carry(fn, source):
    """Give a bespoke sub-agent double the score attributes `_patch_swe` reads to build the harness."""
    fn.scores = getattr(source, "scores", None)
    fn.no_report = getattr(source, "no_report", False)
    fn.seen = getattr(source, "seen", [])
    return fn


def _harness_from(fn):
    """A `_run_harness` stand-in returning the scores its paired sub-agent double was given.

    `no_report=True` stands for a harness that runs but prints nothing usable -- the failure the
    sub-agent used to cause by not saving its report, which is now the harness's to cause.
    """
    it = iter(getattr(fn, "scores", None) or [])

    def _run(self, eval_dir, endpoint_url):
        if getattr(fn, "no_report", False):
            return 1, "", "the harness produced no output"
        try:
            return 0, json.dumps(_report(next(it))), ""
        except StopIteration:
            raise AssertionError(
                "the harness was run more times than the test supplied scores for") from None
    return _run


def _patch_deploy(fn):
    return mock.patch("appsci_agent.tools.optimize_agent.DeployArtifactTool.execute", fn)


def _history(artifact_dir) -> list[dict]:
    path = os.path.join(str(artifact_dir.parent), OPTIMIZE_DIRNAME, HISTORY_FILENAME)
    with open(path) as f:
        return json.load(f)["records"]


def _pending(artifact_dir):
    path = os.path.join(str(artifact_dir.parent), OPTIMIZE_DIRNAME, PENDING_FILENAME)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


# --- preconditions --------------------------------------------------------------------------------

def test_refuses_without_an_internal_eval(artifact_dir):
    ctx = _ctx(artifact_dir, eval_ready=False)
    swe = _swe(scores=[0.5])
    with _patch_status(STATUS_OK), _patch_swe(swe):
        res = OptimizeAgentTool().execute({}, ctx)
    assert swe.seen == []                                 # refused before spending a sub-agent
    assert not res.success
    assert "eval_generator" in res.error and "No internal eval" in res.error


def test_refuses_when_the_eval_dir_is_incomplete(artifact_dir):
    """eval_ready stays True after a later sub-agent task deletes part of the harness."""
    (artifact_dir / "eval").mkdir()
    (artifact_dir / "eval" / "run.py").write_text("print()")
    with _patch_status(STATUS_OK):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert not res.success and "incomplete" in res.error
    assert "eval_data.json" in res.error and "README.md" in res.error


def test_refuses_without_a_deployed_candidate(artifact_dir):
    _write_eval(artifact_dir)
    with _patch_status(STATUS_OK):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir, artifact_endpoint=None))
    assert not res.success and "deploy_artifact" in res.error


def test_refuses_when_the_artifact_changed_since_the_deploy(artifact_dir):
    """Scoring code that is not the code running is worse than not scoring at all."""
    _write_eval(artifact_dir)
    with _patch_status({**STATUS_OK, "stale": True, "current_fingerprint": "def456"}):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert not res.success and "changed since your last deploy" in res.error


def test_refuses_when_the_endpoint_is_not_answering(artifact_dir):
    """A dead container would score 0 on every case and read as a quality regression."""
    _write_eval(artifact_dir)
    with _patch_status(STATUS_OK), \
         mock.patch.object(OptimizeAgentTool, "_probe_endpoint",
                           return_value=(False, "URLError: refused")):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert not res.success and "not answering" in res.error


# --- the one sub-agent run ------------------------------------------------------------------------

def test_one_call_is_one_sub_agent_run(artifact_dir):
    """The whole point of the shape: measure and improve in a single loop, not one run each."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    swe = _swe(scores=[0.4], edit_marker="# v2\n")
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert res.success, res.error
    assert len(swe.seen) == 1


def test_the_brief_hands_over_a_score_instead_of_asking_for_one(artifact_dir):
    """The tool measures; the sub-agent improves. So the brief carries the numbers and the report path.

    It must also tell the sub-agent NOT to run the harness again: a second run would measure the same
    container the tool already measured, spending steps needed for the fix and producing a score the
    cycle will not use.
    """
    _artifact(artifact_dir)
    eval_dir = _write_eval(artifact_dir)
    swe = _swe(scores=[0.4], edit_marker="# v2\n")
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    brief = swe.seen[0]
    assert eval_dir in brief
    assert "headline_metric = 0.4" in brief
    assert REPORT_FILENAME in brief
    # The invocation is stated for reference -- the sub-agent should know what it is being measured
    # by -- while re-running it is still discouraged, and probing one case is explicitly encouraged.
    assert "python run.py" in brief
    assert "Do not run it again" in brief
    assert "talking to the candidate directly" in brief
    assert ENDPOINT in brief


def test_the_report_is_on_disk_before_the_sub_agent_starts(artifact_dir):
    """The measurement cannot be lost to the improvement step, because it precedes it.

    This is the whole point of measuring in code: the old brief asked the sub-agent to save the report
    first and admitted that a cycle which ran out of steps before doing so had thrown the measurement
    away.
    """
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    report_path = os.path.join(str(artifact_dir.parent), OPTIMIZE_DIRNAME, REPORT_FILENAME)
    seen_at_start = {}

    swe = _swe(scores=[0.4])

    def _spy(self, params, context):
        with open(report_path) as f:
            seen_at_start["headline"] = json.load(f)["headline_metric"]
        return swe(self, params, context)

    with _patch_status(STATUS_OK), _patch_swe(_carry(_spy, swe)), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert seen_at_start["headline"] == 0.4
    assert res.output["headline_metric"] == 0.4


def test_a_sub_agent_that_fails_does_not_lose_the_measurement(artifact_dir):
    """The score was taken before the improvement step, so losing that step does not lose the score."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[0.4], fail=True)), \
         _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert res.success
    assert res.output["headline_metric"] == 0.4
    assert res.output["cycle"]["changed"] is False
    assert (artifact_dir / "server.py").read_text() == "# v1\n"


def test_the_brief_forbids_editing_the_eval_and_deploying(artifact_dir):
    """The sub-agent changes the AGENT, never the thing that measures it; deploying is the tool's job
    and it has no docker socket anyway."""
    _artifact(artifact_dir)
    eval_dir = _write_eval(artifact_dir)
    swe = _swe(scores=[0.4])
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    brief = swe.seen[0]
    assert "Do NOT modify ANYTHING under " + eval_dir in brief
    assert "you change the AGENT, never the thing that measures it" in brief
    assert "Do NOT run docker" in brief
    # Installing a dependency is allowed; editing a harness file is not -- and regenerating is
    # eval_generator's job, not the sub-agent's. (The dependency AFFORDANCE now lives in the repair
    # brief, which only runs when the harness would not start; this brief just states the boundary.)
    assert "Installing a dependency INTO THE ENVIRONMENT is fine" in brief
    assert "eval_generator's job" in brief


def test_a_harness_that_produces_no_score_is_a_failure_not_a_zero(artifact_dir):
    """A harness that ran and printed nothing usable must not be scored as 0.0.

    0.0 and "not measured" are the same number to every comparison downstream, so the loop would keep
    whatever change it last made and call an unmeasurable cycle progress.
    """
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[], write_report=False)):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert not res.success
    assert "without producing a score" in res.error


def test_a_stale_report_is_never_read_as_a_fresh_measurement(artifact_dir):
    """The trap: last cycle's report is still on disk, and reading it would score the previous
    candidate and attribute it to this one. The tool clears it before measuring."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    opt = os.path.join(str(artifact_dir.parent), OPTIMIZE_DIRNAME)
    os.makedirs(opt, exist_ok=True)
    with open(os.path.join(opt, REPORT_FILENAME), "w") as f:
        json.dump(_report(0.99), f)                       # a previous cycle's score

    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[], write_report=False)):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert not res.success
    assert "0.99" not in (res.error or "")
    assert res.output.get("headline_metric") is None


@pytest.mark.parametrize("bad", ['"0.5"', "null", "true", "1.5", "-0.1"])
def test_a_headline_metric_that_is_not_a_0_to_1_number_is_rejected(artifact_dir, bad):
    """Every decision downstream is arithmetic on this value; a bad one fails comparisons silently."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)

    def _harness_bad(self, eval_dir, endpoint_url):
        return 0, '{"headline_metric": ' + bad + "}", ""

    with _patch_status(STATUS_OK), \
         mock.patch("appsci_agent.tools.optimize_agent.MiniSweAgentTool.execute",
                    _swe(scores=[])), \
         mock.patch("appsci_agent.tools.optimize_agent.OptimizeAgentTool._run_harness",
                    _harness_bad):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert not res.success and "headline_metric" in res.error


def test_a_harness_printing_junk_names_the_contract_it_broke(artifact_dir):
    """Stdout IS the report, so one stray log line breaks it -- the error has to say that plainly."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)

    def _harness_chatty(self, eval_dir, endpoint_url):
        return 0, 'loading cases...\n{"headline_metric": 0.5}', ""

    with _patch_status(STATUS_OK), \
         mock.patch("appsci_agent.tools.optimize_agent.MiniSweAgentTool.execute",
                    _swe(scores=[])), \
         mock.patch("appsci_agent.tools.optimize_agent.OptimizeAgentTool._run_harness",
                    _harness_chatty):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert not res.success
    assert "not the contracted JSON report" in res.error


# --- the score and its checkpoint -----------------------------------------------------------------

def test_the_score_is_recorded_against_the_checkpoint_it_was_measured_on(artifact_dir):
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[1.0])), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert res.success, res.error
    assert res.output["checkpoint"]["name"] == "artifact-o2c-002"
    assert res.output["checkpoint_note"] is None
    assert _history(artifact_dir)[0]["checkpoint"]["ordinal"] == 2


def test_a_missing_checkpoint_says_why_rather_than_being_a_bare_null(artifact_dir):
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    with _patch_status({"available": False, "reason": "no orchestrator (local run)"}), \
         _patch_swe(_swe(scores=[1.0])), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert res.output["checkpoint"] is None
    assert "no orchestrator" in res.output["checkpoint_note"]


def test_scoring_never_writes_into_the_artifact_directory(artifact_dir):
    """The artifact directory's hash IS the candidate's identity, so a measurement must not change it."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)

    def tree():
        out = {}
        for root, _d, files in os.walk(artifact_dir):
            for f in files:
                if not f.endswith(".pyc") and "__pycache__" not in root:
                    p = os.path.join(root, f)
                    with open(p) as fh:
                        out[os.path.relpath(p, artifact_dir)] = fh.read()
        return out

    before = tree()
    ctx = _ctx(artifact_dir)
    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[1.0, 1.0])), _patch_deploy(_deploy_ok()):
        OptimizeAgentTool().execute({}, ctx)
        OptimizeAgentTool().execute({}, ctx)
    assert tree() == before
    assert len(_history(artifact_dir)) == 2
    assert OPTIMIZE_DIRNAME not in os.listdir(artifact_dir)


# --- a change, and its verification on the next call ----------------------------------------------

def test_a_change_is_redeployed_and_left_awaiting_verification(artifact_dir):
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[0.4], edit_marker="# v2\n")), \
         _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))

    assert res.output["status"] == "awaiting_verification"
    assert res.output["awaiting_verification"] is True
    assert res.output["cycle"]["changed"] is True and res.output["cycle"]["redeployed"] is True
    assert (artifact_dir / "server.py").read_text() == "# v2\n"
    # The agent must be told not to finish here -- this is the weak point of verifying next call.
    assert "Do NOT call complete" in res.output["message"]
    pending = _pending(artifact_dir)
    assert pending["score_to_beat"] == 0.4


def test_an_improving_change_is_kept_on_the_next_call(artifact_dir):
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    ctx = _ctx(artifact_dir)
    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[0.4, 0.9], edit_marker="# v2\n")), \
         _patch_deploy(_deploy_ok()):
        OptimizeAgentTool().execute({}, ctx)
        res = OptimizeAgentTool().execute({}, ctx)

    verdict = res.output["cycle"]["verified_previous"]
    assert verdict["accepted"] is True and verdict["reverted"] is False
    assert res.output["headline_metric"] == 0.9
    assert (artifact_dir / "server.py").read_text() == "# v2\n"        # change kept
    assert _pending(artifact_dir) is None
    stages = [r["stage"] for r in _history(artifact_dir)]
    assert stages == ["baseline", "attempt"]


def test_a_change_that_did_not_help_is_rolled_back_and_redeployed(artifact_dir):
    """Restoring the files is not enough: the rejected candidate is still the one serving."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    ctx = _ctx(artifact_dir)
    deploys = []

    def _deploy(self, params, context):
        with open(os.path.join(context.artifact_dir, "server.py")) as f:
            deploys.append(f.read())
        context.artifact_idx += 1
        return ToolResult(success=True, output={"endpoint_url": ENDPOINT})

    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[0.4, 0.2], edit_marker="# v2\n")), \
         _patch_deploy(_deploy):
        OptimizeAgentTool().execute({}, ctx)
        res = OptimizeAgentTool().execute({}, ctx)

    verdict = res.output["cycle"]["verified_previous"]
    assert verdict["accepted"] is False and verdict["reverted"] is True
    assert (artifact_dir / "server.py").read_text() == "# v1\n"        # incumbent restored
    assert deploys == ["# v2\n", "# v1\n"]                            # and REDEPLOYED, not just restored


def test_a_rolled_back_change_reports_the_incumbents_score(artifact_dir):
    """The rejected candidate is no longer running, so reporting its score would claim a regression
    in code the agent is not serving."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    ctx = _ctx(artifact_dir)
    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[0.4, 0.2], edit_marker="# v2\n")), \
         _patch_deploy(_deploy_ok()):
        OptimizeAgentTool().execute({}, ctx)
        res = OptimizeAgentTool().execute({}, ctx)
    assert res.output["headline_metric"] == 0.4                      # what is deployed
    # ...while the history still records what the rejected candidate actually scored.
    assert [r["headline_metric"] for r in _history(artifact_dir)] == [0.4, 0.2]


def test_the_sub_agent_is_told_the_score_to_beat(artifact_dir):
    """So it can skip editing when its measurement means the previous change is being discarded --
    the branch lives inside its loop rather than costing a wasted edit."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    ctx = _ctx(artifact_dir)
    swe = _swe(scores=[0.4, 0.2], edit_marker="# v2\n")
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        OptimizeAgentTool().execute({}, ctx)
        OptimizeAgentTool().execute({}, ctx)
    # Nothing pending on the first call, so no decide-step at all.
    assert "## Step 2 -- decide whether to go on" not in swe.seen[0]
    # On the second, the score to beat and what happens below it are both stated.
    assert "## Step 2 -- decide whether to go on" in swe.seen[1]
    assert "0.400" in swe.seen[1] and "rolled back" in swe.seen[1]


def test_a_change_the_sub_agent_only_claimed_does_not_start_a_cycle(artifact_dir):
    """Whether the candidate changed is decided by comparing the source, not by asking. Otherwise a
    sub-agent that says it fixed something and did not would spend a budget cycle on nothing."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    # note claims a change; edit_marker is None so nothing is actually written
    swe = _swe(scores=[0.4], note="I rewrote the prompt")
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert res.output["cycle"]["changed"] is False
    assert res.output["awaiting_verification"] is False
    assert _pending(artifact_dir) is None
    assert _cycles(artifact_dir) == 0


def test_a_change_that_will_not_deploy_restores_the_working_source(artifact_dir):
    _artifact(artifact_dir)
    _write_eval(artifact_dir)

    def _deploy_fails(self, params, context):
        return ToolResult(success=False, output={}, error="build step failed")

    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[0.4], edit_marker="# v2\n")), \
         _patch_deploy(_deploy_fails):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert res.output["cycle"]["redeployed"] is False
    assert res.output["cycle"]["restored"] is True
    assert (artifact_dir / "server.py").read_text() == "# v1\n"
    assert _pending(artifact_dir) is None                            # nothing to verify next call


# --- stopping: nothing to fix, saturation, budget -------------------------------------------------

def _cycles(artifact_dir) -> int:
    return sum(1 for r in _history(artifact_dir) if r.get("stage") == "attempt")


def test_a_perfect_score_still_measures_but_the_brief_says_not_to_edit(artifact_dir):
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    swe = _swe(scores=[1.0])
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert res.output["headline_metric"] == 1.0
    assert "Every case passes" in res.output["message"]
    assert "nothing to target" in swe.seen[0]


def test_score_only_runs_no_sub_agent_at_all(artifact_dir):
    """Measuring is now code, so a score-only call spawns nothing that could edit the candidate.

    Stronger than the old assertion that the brief said "measurement only": a sub-agent that is never
    invoked cannot ignore its brief, and the call costs no sub-agent tokens either.
    """
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    swe = _swe(scores=[0.4], edit_marker="# v2\n")
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({"score_only": True}, _ctx(artifact_dir))
    assert swe.seen == []
    assert res.output["headline_metric"] == 0.4
    assert res.output["cycle"]["changed"] is False
    assert (artifact_dir / "server.py").read_text() == "# v1\n"
    assert _cycles(artifact_dir) == 0


def test_a_zero_budget_forbids_editing_but_still_measures(artifact_dir):
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    swe = _swe(scores=[0.4], edit_marker="# v2\n")
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir, optimize_cycles_allowed=0))
    assert swe.seen == []                        # nothing that could edit was ever started
    assert res.output["headline_metric"] == 0.4
    assert res.output["budget_exhausted"] is True
    assert (artifact_dir / "server.py").read_text() == "# v1\n"


def test_the_budget_stops_editing_once_spent(artifact_dir):
    """One cycle allowed: the first call may change the candidate, the second only measures it."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    ctx = _ctx(artifact_dir, optimize_cycles_allowed=1)
    swe = _swe(scores=[0.4, 0.9], edit_marker="# v2\n")
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        first = OptimizeAgentTool().execute({}, ctx)
        second = OptimizeAgentTool().execute({}, ctx)

    assert first.output["cycle"]["changed"] is True
    assert "Step 3 -- improve" in swe.seen[0]
    # The change was measured and kept, and the budget is now spent.
    assert second.output["cycle"]["verified_previous"]["accepted"] is True
    assert second.output["cycles_used"] == 1 and second.output["cycles_allowed"] == 1
    assert second.output["budget_exhausted"] is True
    assert second.output["cycle"]["changed"] is False
    assert len(swe.seen) == 1                     # the spent-budget call started no sub-agent


def test_the_call_parameter_can_only_narrow_the_configured_budget():
    """A budget the spender can raise is not a budget."""
    from appsci_agent.tools.optimize_agent import _cycle_budget
    ctx = SimpleNamespace(optimize_cycles_allowed=1)
    assert _cycle_budget({"iteration_cycles_allowed": 99}, ctx) == 1
    assert _cycle_budget({"iteration_cycles_allowed": 0}, ctx) == 0
    assert _cycle_budget({}, SimpleNamespace(optimize_cycles_allowed=None)) is None


@pytest.mark.parametrize("bad", [-1, "3", None, True, 2.5])
def test_an_unusable_cycle_parameter_is_ignored(bad):
    from appsci_agent.tools.optimize_agent import _cycle_budget
    ctx = SimpleNamespace(optimize_cycles_allowed=4)
    assert _cycle_budget({"iteration_cycles_allowed": bad}, ctx) == 4


# --- saturation -----------------------------------------------------------------------------------

def _rec(score, stage="attempt"):
    return {"headline_metric": score, "stage": stage}


def test_not_saturated_before_enough_measured_changes():
    assert OptimizeAgentTool._is_saturated([]) is False
    assert OptimizeAgentTool._is_saturated([_rec(0.4, "baseline")]) is False
    assert OptimizeAgentTool._is_saturated([_rec(0.4, "baseline"), _rec(0.4)]) is False


def test_saturated_after_consecutive_changes_stop_improving_the_best():
    history = [_rec(0.4, "baseline")] + [_rec(0.4)] * SATURATION_PATIENCE
    assert OptimizeAgentTool._is_saturated(history) is True


def test_a_real_improvement_in_the_window_is_not_saturation():
    history = [_rec(0.4, "baseline"), _rec(0.4), _rec(0.4 + MIN_IMPROVEMENT * 2)]
    assert OptimizeAgentTool._is_saturated(history) is False


def test_a_dip_and_recovery_is_not_mistaken_for_progress():
    """Compared against the best so far, not the previous record -- 0.4 -> 0.2 -> 0.4 gained nothing."""
    assert OptimizeAgentTool._is_saturated(
        [_rec(0.4, "baseline"), _rec(0.2), _rec(0.4)]) is True


def test_noise_below_the_improvement_floor_is_saturation():
    history = [_rec(0.4, "baseline")] + [_rec(0.4 + MIN_IMPROVEMENT / 10)] * SATURATION_PATIENCE
    assert OptimizeAgentTool._is_saturated(history) is True


# --- registration and the prompt ------------------------------------------------------------------

def test_the_agent_registers_optimize_agent_and_not_the_old_tool():
    from appsci_agent.core.agent import AppSciAgent
    agent = AppSciAgent(system_prompt="x")
    assert "optimize_agent" in agent.tools
    assert "optimize_candidate" not in agent.tools


@pytest.mark.parametrize("cycles,expected", [
    (None, "saturated"),
    (3, "3 optimization cycle(s)"),
    (0, "NO optimization cycles"),
])
def test_the_task_message_states_the_right_stopping_rule(cycles, expected):
    from appsci_agent.core.agent import AppSciAgent
    agent = AppSciAgent.__new__(AppSciAgent)
    message = AppSciAgent._format_task_message(
        agent, "Build an agent.", environment=None, optimize_cycles=cycles)
    assert expected in message


def test_the_task_message_warns_against_completing_an_unverified_change():
    """The one guard-rail for verifying on the next call, so a prompt edit that drops it fails here."""
    from appsci_agent.core.agent import AppSciAgent
    agent = AppSciAgent.__new__(AppSciAgent)
    message = AppSciAgent._format_task_message(agent, "Build an agent.", environment=None)
    assert "awaiting_verification" in message
    assert "NEVER call complete" in message


def test_the_cycle_budget_precedence():
    """0 must survive resolution: `or` would turn "no optimization" into the config default."""
    from appsci_agent.core.agent import _resolve_optimize_cycles
    cfg = SimpleNamespace(optimize_cycles=7)
    assert _resolve_optimize_cycles({"optimize_cycles": 0}, cfg) == 0
    assert _resolve_optimize_cycles({"optimize_cycles": 2}, cfg) == 2
    assert _resolve_optimize_cycles({}, cfg) == 7
    assert _resolve_optimize_cycles({"optimize_cycles": None}, cfg) == 7
    assert _resolve_optimize_cycles({"optimize_cycles": -1}, cfg) == 7
    assert _resolve_optimize_cycles({}, SimpleNamespace(optimize_cycles=None)) is None


# --- the loop's length is min(budget, saturation) --------------------------------------------------

def test_saturation_stops_the_loop_even_with_budget_left(artifact_dir):
    """A generous budget does not license more attempts once the trajectory has flattened.

    Seeded with two flat measured changes -- which is what saturation is -- and a budget of 10. The
    loop must stop on saturation, spending neither the sub-agent's edit step nor the remaining cycles.
    """
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    _seed_history(artifact_dir, [
        {"headline_metric": 0.4, "stage": "baseline"},
        {"headline_metric": 0.4, "stage": "attempt"},
        {"headline_metric": 0.4, "stage": "attempt"},
    ])
    swe = _swe(scores=[0.4], edit_marker="# v2\n")
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir, optimize_cycles_allowed=10))

    assert res.output["saturated"] is True
    assert res.output["budget_exhausted"] is False            # budget was NOT the limit
    assert swe.seen == []                                     # nothing that could edit ran
    assert res.output["cycle"]["changed"] is False
    assert (artifact_dir / "server.py").read_text() == "# v1\n"


def test_the_budget_stops_the_loop_even_when_not_saturated(artifact_dir):
    """The mirror case: the scores are still moving, but the allowance is gone."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    _seed_history(artifact_dir, [
        {"headline_metric": 0.2, "stage": "baseline"},
        {"headline_metric": 0.6, "stage": "attempt"},         # a real improvement -> not saturated
    ])
    swe = _swe(scores=[0.6], edit_marker="# v2\n")
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir, optimize_cycles_allowed=1))

    assert res.output["budget_exhausted"] is True
    assert res.output["saturated"] is False                   # saturation was NOT the limit
    assert swe.seen == []                                     # nothing that could edit ran
    assert res.output["cycle"]["changed"] is False


def test_the_two_limits_are_reported_separately(artifact_dir):
    """Never collapsed: "we ran out of cycles" is not evidence a candidate cannot be improved, so a
    reader must be able to tell which limit ended the run."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    _seed_history(artifact_dir, [
        {"headline_metric": 0.4, "stage": "baseline"},
        {"headline_metric": 0.4, "stage": "attempt"},
        {"headline_metric": 0.4, "stage": "attempt"},
    ])
    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[0.4])), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir, optimize_cycles_allowed=2))
    # Both true here, and both readable.
    assert res.output["saturated"] is True
    assert res.output["budget_exhausted"] is True


def _seed_history(artifact_dir, records):
    path = os.path.join(str(artifact_dir.parent), OPTIMIZE_DIRNAME, HISTORY_FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"records": records}, f)


# --- the brief's ordering, which a real run showed matters -----------------------------------------

# --- only eval_generator may create or change the eval --------------------------------------------

def test_an_unsanctioned_harness_is_refused(artifact_dir):
    """A harness eval_generator never produced is not something to be measured against."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir, sanction=False)
    swe = _swe(scores=[0.4])
    with _patch_status(STATUS_OK), _patch_swe(swe):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert swe.seen == []                                  # refused before spending a sub-agent
    assert not res.success
    assert "not produced by eval_generator" in res.error


def test_a_harness_edited_after_generation_is_refused(artifact_dir):
    """The rule that a live run violated: the agent expanded its own eval from 32 to 46 cases.

    Refused rather than warned, because a score taken against a changed eval is not comparable to the
    ones already recorded -- so the whole trajectory saturation is judged on would be meaningless.
    """
    _artifact(artifact_dir)
    eval_dir = _write_eval(artifact_dir)
    # The agent hand-edits its own scorer, exactly as observed.
    with open(os.path.join(eval_dir, "eval_data.json"), "w") as f:
        f.write('[{"id": "easy"}]')

    swe = _swe(scores=[0.4])
    with _patch_status(STATUS_OK), _patch_swe(swe):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert swe.seen == []
    assert not res.success
    assert "changed since eval_generator wrote it" in res.error
    assert "optimize_agent changes the AGENT, never the eval" in res.error


def test_regenerating_the_eval_makes_it_scorable_again(artifact_dir):
    """The sanctioned path back: eval_generator blesses the new harness, so scoring resumes."""
    from appsci_agent.tools.eval_generator import record_sanctioned_harness
    _artifact(artifact_dir)
    eval_dir = _write_eval(artifact_dir)
    with open(os.path.join(eval_dir, "eval_data.json"), "w") as f:
        f.write('[{"id": "harder"}]')
    # What eval_generator does on its way out.
    record_sanctioned_harness(str(artifact_dir.parent), eval_dir)

    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[0.4])), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert res.success, res.error
    assert res.output["headline_metric"] == 0.4


def test_running_the_harness_is_not_editing_it(artifact_dir):
    """Executing run.py writes __pycache__; that must not read as tampering."""
    _artifact(artifact_dir)
    eval_dir = _write_eval(artifact_dir)
    os.makedirs(os.path.join(eval_dir, "__pycache__"), exist_ok=True)
    with open(os.path.join(eval_dir, "__pycache__", "run.cpython-312.pyc"), "wb") as f:
        f.write(b"\x00compiled")

    with _patch_status(STATUS_OK), _patch_swe(_swe(scores=[0.4])), _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert res.success, res.error


def test_a_sub_agent_that_edits_the_eval_has_its_score_discarded(artifact_dir):
    """Same rule, other actor. The brief forbids it, but a brief cannot bind -- so it is checked, and
    the measurement is thrown away because it was not taken against the sanctioned eval."""
    _artifact(artifact_dir)
    eval_dir = _write_eval(artifact_dir)

    def _swe_tampers(self, params, context):
        with open(os.path.join(eval_dir, "eval_data.json"), "w") as f:
            f.write('[{"id": "trivially_passing"}]')       # rewrites the cases it is scored on
        with open(os.path.join(context.artifact_dir, "server.py"), "w") as f:
            f.write("# v2\n")
        return ToolResult(success=True, output={})

    # Measuring first means tampering can no longer inflate THIS cycle's score -- the number was taken
    # against the sanctioned harness before the sub-agent started. The check still has to fire: a
    # harness nobody blessed would corrupt every later cycle, and edits made alongside tampering are
    # not edits to trust.
    with _patch_status(STATUS_OK), _patch_swe(_carry(_swe_tampers, _swe(scores=[0.4]))), \
         _patch_deploy(_deploy_ok()):
        res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))

    assert not res.success
    assert "was modified during this call" in res.error
    # The score is gone and the candidate is back, so the tampering bought nothing.
    assert res.output["restored_candidate"] is True
    assert (artifact_dir / "server.py").read_text() == "# v1\n"
    assert not os.path.exists(os.path.join(str(artifact_dir.parent), OPTIMIZE_DIRNAME,
                                           HISTORY_FILENAME))


# --- the ablation: running as if these tools never landed -----------------------------------------

def _agent(**config_kwargs):
    from appsci_agent.core.agent import AppSciAgent
    from appsci_agent.core.config import AgentConfig
    return AppSciAgent(config=AgentConfig(**config_kwargs), system_prompt="x")


def test_optimization_enabled_is_the_default():
    assert set(_agent().tools) >= {"eval_generator", "optimize_agent"}


def test_the_ablation_removes_both_tools_from_the_registry():
    """From the REGISTRY, not just the prompt: an unadvertised tool the model happens to name still
    executes, so a prompt-only ablation would not be one."""
    tools = _agent(optimization_enabled=False).tools
    assert "eval_generator" not in tools
    assert "optimize_agent" not in tools
    assert set(tools) == {"talk_to_user", "mini_swe_agent", "deploy_artifact", "complete"}


def test_the_ablation_task_message_never_mentions_the_removed_tools():
    """An agent told to call eval_generator and handed no such tool burns turns on "Unknown tool"."""
    from appsci_agent.core.agent import AppSciAgent
    agent = AppSciAgent.__new__(AppSciAgent)
    message = AppSciAgent._format_task_message(
        agent, "Build an agent.", environment=None, optimization_enabled=False)
    assert "eval_generator" not in message
    assert "optimize_agent" not in message
    assert "saturated" not in message
    assert "deploy_artifact" in message and "complete" in message


def test_the_ablation_system_prompt_never_mentions_the_removed_tools():
    from appsci_agent.prompts import build_system_prompt
    off = build_system_prompt(task_brief="t", optimization_enabled=False)
    assert "eval_generator" not in off
    assert "optimize_agent" not in off
    assert "four tools" in off
    for name in ("talk_to_user", "mini_swe_agent", "deploy_artifact", "complete"):
        assert name in off
    # The numbering has no hole in it -- `complete` becomes 4, not a dangling 6.
    assert "## 4. `complete`" in off
    assert "## 5." not in off and "## 6." not in off


def test_the_full_system_prompt_still_documents_all_six():
    from appsci_agent.prompts import build_system_prompt
    on = build_system_prompt(task_brief="t")
    assert "six tools" in on
    assert "## 4. `eval_generator`" in on
    assert "## 5. `optimize_agent`" in on
    assert "## 6. `complete`" in on


class TestThePromptNamesTheHandoffDirectory:
    """The eval handoff's location must be a PATH in the prompt, never a description of one.

    It used to read "in your workspace directory". The agent's WORKDIR is /work, so on a live run it
    read that as /work, wrote /work/eval_handoff.md, and eval_generator -- which looks in
    /workspace -- rejected the call with "does not exist yet". It recovered by moving the file, but
    only after spending turns on it. The instruction it replaced named the artifact output directory
    concretely, which is why the old wording never had this problem.
    """

    def test_the_rendered_prompt_carries_the_path(self):
        from appsci_agent.prompts import build_system_prompt

        prompt = build_system_prompt(task_brief="t", workspace_dir="/somewhere")
        assert "`/somewhere/.appsci/handoff/eval_handoff.md`" in prompt
        assert "your workspace directory" not in prompt

    def test_an_omitted_variable_still_renders_a_real_path(self):
        """This Environment leaves a missing variable Undefined, which renders as the empty string --
        so without the template's default an unaware caller would print `/eval_handoff.md`."""
        from appsci_agent.prompts import build_system_prompt

        prompt = build_system_prompt(task_brief="t")
        assert "`/workspace/.appsci/handoff/eval_handoff.md`" in prompt
        assert "`/eval_handoff.md`" not in prompt
        assert "`/workspace//eval_handoff.md`" not in prompt

    def test_both_production_prompt_paths_pass_it(self):
        """`applied_science_agent` hands AppSciAgent a pre-built prompt, so the default inside that
        class never applies on the path production actually takes -- the same trap `optimization_enabled`
        already fell into. Asserted by rendering with a non-default workspace and looking for it."""
        import inspect

        from appsci_agent import applied_science_agent
        from appsci_agent.core import agent as core_agent

        for module in (applied_science_agent, core_agent):
            source = inspect.getsource(module)
            assert "workspace_dir=self.config.workspace_dir" in source, (
                f"{module.__name__} builds a system prompt without passing workspace_dir")


def test_the_prompt_describes_the_surface_the_agent_actually_has():
    """Rendered off the REGISTRY, not the config, so the two cannot disagree."""
    for enabled in (True, False):
        agent = _agent(optimization_enabled=enabled)
        agent._system_prompt = None
        prompt = agent._build_system_prompt(task_description="t")
        assert ("optimize_agent" in prompt) == ("optimize_agent" in agent.tools)


@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("no", False), ("off", False), ("FALSE", False),
    ("1", True), ("true", True), ("yes", True), ("on", True),
])
def test_the_env_var_parses_both_ways(monkeypatch, raw, expected):
    from appsci_agent.core.config import AgentConfig
    monkeypatch.setenv("AGENT_OPTIMIZATION_ENABLED", raw)
    assert AgentConfig.from_env().optimization_enabled is expected


def test_an_unparseable_env_var_is_refused_rather_than_guessed(monkeypatch):
    """Silently defaulting would run an ablation nobody asked for, or skip one that was."""
    from appsci_agent.core.config import AgentConfig
    monkeypatch.setenv("AGENT_OPTIMIZATION_ENABLED", "maybe")
    with pytest.raises(ValueError, match="boolean-ish"):
        AgentConfig.from_env()


def test_the_env_var_unset_leaves_the_tools_in(monkeypatch):
    from appsci_agent.core.config import AgentConfig
    monkeypatch.delenv("AGENT_OPTIMIZATION_ENABLED", raising=False)
    assert AgentConfig.from_env().optimization_enabled is True


# --- a roll-back colliding with a fresh edit in the same call -------------------------------------

def test_an_edit_made_during_a_rollback_cycle_is_discarded(artifact_dir):
    """Found by self-review, not by a test -- so here is the test.

    The sub-agent's brief tells it not to edit when its measurement comes in below the score to beat,
    because the previous change is being rolled back. A brief cannot bind it. If it edits anyway, the
    roll-back has ALREADY overwritten those edits (it restores the whole source tree), so there is
    nothing left to deploy.

    Left as a `changed` cycle, the tool would deploy the restored incumbent while recording a pending
    change against the REJECTED score -- and the next call would measure the incumbent, find it better
    than that rejected score, and "accept" a change that never existed: a spent budget cycle and a
    bogus `attempt` in the trajectory saturation is judged on.
    """
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    ctx = _ctx(artifact_dir)

    def _swe_edits_regardless(self, params, context):
        """Ignores the "do not edit" branch, which is exactly the case being guarded."""
        verifying = "## Step 2 -- decide whether to go on" in params["task"]
        with open(os.path.join(context.artifact_dir, "server.py"), "w") as f:
            f.write("# v%d\n" % (3 if verifying else 2))
        return ToolResult(success=True, output={})

    deploys = []

    def _deploy(self, params, context):
        with open(os.path.join(context.artifact_dir, "server.py")) as f:
            deploys.append(f.read())
        context.artifact_idx += 1
        return ToolResult(success=True, output={"endpoint_url": ENDPOINT})

    with _patch_status(STATUS_OK), \
         _patch_swe(_carry(_swe_edits_regardless, _swe(scores=[0.4, 0.1]))), \
         _patch_deploy(_deploy):
        first = OptimizeAgentTool().execute({}, ctx)     # scores 0.4, edits to v2, redeploys
        second = OptimizeAgentTool().execute({}, ctx)    # scores 0.1 -> roll back, but also edits v3

    assert first.output["awaiting_verification"] is True
    verdict = second.output["cycle"]["verified_previous"]
    assert verdict["reverted"] is True

    # The v3 edit is gone, and was never deployed or recorded as a change.
    assert (artifact_dir / "server.py").read_text() == "# v1\n"
    assert second.output["cycle"]["changed"] is False
    assert second.output["cycle"]["edit_discarded_by_rollback"] is True
    assert second.output["awaiting_verification"] is False
    assert _pending(artifact_dir) is None
    # Deploys: v2 (the attempt), then v1 (the roll-back). Never v3.
    assert deploys == ["# v2\n", "# v1\n"]
    # And the reported score is the incumbent's, not the rejected candidate's.
    assert second.output["headline_metric"] == 0.4


# --- the PRODUCTION path, which is where the ablation actually leaked ------------------------------

def _task_context(tmp_path):
    from appsci_agent.core import TaskContext
    return TaskContext(
        description="Review supplier invoices and flag the ones a human should check.",
        environment={}, budgets={}, artifacts={"output_dir": str(tmp_path)},
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_the_production_wrapper_builds_a_prompt_matching_its_registry(tmp_path, enabled, monkeypatch):
    """`AppliedScienceAgent` is what `appsci_agent/run.py` (the container entrypoint) uses, and it hands
    `AppSciAgent` a PRE-BUILT prompt -- so the registry-derived default inside that class never applies.

    A live ablation run caught this the hard way: the wrapper built its prompt without
    `optimization_enabled`, so the prompt advertised `eval_generator` and `optimize_agent` while the
    registry had dropped them. The agent called eval_generator twice, got "Unknown tool" twice, and
    adapted around it -- measuring neither arm of the experiment. Every earlier test drove `AppSciAgent`
    or `build_system_prompt` directly and so could not see it.
    """
    from appsci_agent.applied_science_agent import AppliedScienceAgent
    from appsci_agent.core.config import AgentConfig

    captured = {}

    def _fake_run(self, *a, **kw):
        captured["tools"] = set(self.tools)
        captured["prompt"] = self._system_prompt
        # The wrapper logs several of these on the way out, so return the real shape.
        return {"status": "complete", "artifact_endpoint": None, "turns_used": 1,
                "tokens_used": 0, "wall_clock_s": 0.0, "stop_reason": "complete", "messages": []}

    monkeypatch.setattr("appsci_agent.core.agent.AppSciAgent.run", _fake_run)
    wrapper = AppliedScienceAgent(config=AgentConfig(optimization_enabled=enabled))
    wrapper.build(_task_context(tmp_path))

    prompt, tools = captured["prompt"], captured["tools"]
    for name in ("eval_generator", "optimize_agent"):
        assert (name in tools) is enabled
        # The prompt must describe exactly the tools that exist. This is the assertion that failed.
        assert (name in prompt) is enabled, f"{name}: prompt and registry disagree"


def test_a_prompt_advertising_an_absent_tool_is_logged_loudly(tmp_path, caplog):
    """Defence in depth for the class of bug above: a pre-built prompt bypasses the gating entirely,
    so drift between prompt and registry has to be visible rather than silent."""
    import logging
    from appsci_agent.core.agent import AppSciAgent
    from appsci_agent.core.config import AgentConfig

    with caplog.at_level(logging.ERROR):
        AppSciAgent(
            config=AgentConfig(optimization_enabled=False),
            system_prompt="You have a tool called optimize_agent. Use it.",
        )
    assert "does not have" in caplog.text
    assert "optimize_agent" in caplog.text


def test_a_matching_prompt_logs_nothing(caplog):
    import logging
    from appsci_agent.core.agent import AppSciAgent
    from appsci_agent.core.config import AgentConfig

    with caplog.at_level(logging.ERROR):
        AppSciAgent(config=AgentConfig(optimization_enabled=False),
                    system_prompt="You have deploy_artifact and complete.")
    assert "does not have" not in caplog.text


# --- what the loop has already tried and thrown away ----------------------------------------------
#
# Raised in review on #135: on a roll-back the sub-agent is told not to edit, but nothing carried the
# rejected approach into the NEXT cycle, so it could propose the same thing again and spend a second
# budget cycle rediscovering that it does not work.

def _attempts(artifact_dir) -> list[dict]:
    return [r for r in _history(artifact_dir) if r["stage"] == "attempt"]


def test_a_rejected_change_is_recorded_as_rejected(artifact_dir):
    """The verdict lives on the attempt record, not re-derived later from adjacent scores.

    Whether a change survives is decided against `score_to_beat` plus `MIN_IMPROVEMENT`; a reader
    reconstructing that from the history would have to re-implement the rule and would drift from it.
    """
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    ctx = _ctx(artifact_dir)
    swe = _swe(scores=[0.4, 0.1], edit_marker="# v2\n", note="added a rounding rule")

    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        OptimizeAgentTool().execute({}, ctx)   # baseline 0.4, change made
        OptimizeAgentTool().execute({}, ctx)   # measures 0.1 -> rolled back

    attempt = _attempts(artifact_dir)[0]
    assert attempt["verdict"] == "rejected"
    assert attempt["reverted"] is True
    # The note is what the change actually was, which is the part worth telling the next cycle.
    assert attempt["change_note"] == "added a rounding rule"


def test_a_kept_change_is_recorded_as_kept(artifact_dir):
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    ctx = _ctx(artifact_dir)
    swe = _swe(scores=[0.4, 0.9], edit_marker="# v2\n")

    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        OptimizeAgentTool().execute({}, ctx)
        OptimizeAgentTool().execute({}, ctx)

    attempt = _attempts(artifact_dir)[0]
    assert attempt["verdict"] == "kept"
    assert attempt["reverted"] is False


def test_the_next_brief_names_what_was_already_tried_and_rejected(artifact_dir):
    """The point of the whole change: cycle 3 is told what cycle 1 tried and cycle 2 threw away."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    ctx = _ctx(artifact_dir)
    swe = _swe(scores=[0.4, 0.1, 0.4], edit_marker="# v2\n", note="added a rounding rule")

    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        for _ in range(3):
            OptimizeAgentTool().execute({}, ctx)

    third = swe.seen[2]
    assert "## Already tried and rejected" in third
    assert "accuracy" in third                 # the technique label the attempt was recorded under
    assert "added a rounding rule" in third    # and what was actually done
    assert "Take a different angle." in third


def test_a_first_brief_carries_no_rejection_section(artifact_dir):
    """Nothing has been rejected yet, so the section is absent rather than empty."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    swe = _swe(scores=[0.4], edit_marker="# v2\n")
    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        OptimizeAgentTool().execute({}, _ctx(artifact_dir))
    assert "Already tried and rejected" not in swe.seen[0]


def test_a_measure_only_brief_carries_no_rejection_section(artifact_dir):
    """A cycle that may not edit has nothing to avoid re-proposing, so the list is only prompt weight."""
    _artifact(artifact_dir)
    _write_eval(artifact_dir)
    ctx = _ctx(artifact_dir)
    swe = _swe(scores=[0.4, 0.1, 0.4], edit_marker="# v2\n")

    with _patch_status(STATUS_OK), _patch_swe(swe), _patch_deploy(_deploy_ok()):
        OptimizeAgentTool().execute({}, ctx)
        OptimizeAgentTool().execute({}, ctx)                      # produces a rejection
        OptimizeAgentTool().execute({"score_only": True}, ctx)     # ...which this must not be told about

    # The third call never started a sub-agent at all, so there is no brief to carry the list.
    assert len(swe.seen) == 2


def test_a_rejection_that_could_not_be_reverted_is_flagged_as_still_present():
    """Rejected and reverted are not the same thing, and the brief describes code the sub-agent reads.

    A change judged no improvement whose snapshot could not be restored is still in the candidate; a
    blanket "these are not in the code" would be wrong about the files in front of it.
    """
    section = OptimizeAgentTool._rejected_section(
        [{"technique": "accuracy", "note": "n", "score": 0.2, "reverted": False}]
    )
    assert "still in the code" in section

    reverted = OptimizeAgentTool._rejected_section(
        [{"technique": "accuracy", "note": "n", "score": 0.2, "reverted": True}]
    )
    assert "still in the code" not in reverted


def test_the_rejected_list_is_bounded_and_newest_first():
    """This text goes into every later brief, so it cannot grow with the run."""
    history = [
        {"verdict": "rejected", "technique": f"m{i}", "headline_metric": 0.1, "change_note": "n",
         "reverted": True}
        for i in range(MAX_BRIEF_REJECTED + 3)
    ]
    out = OptimizeAgentTool._rejected_changes(history)
    assert len(out) == MAX_BRIEF_REJECTED
    assert out[0]["technique"] == f"m{MAX_BRIEF_REJECTED + 2}"   # newest first


def test_kept_and_untitled_rejections_are_not_listed():
    """A rejected record with no technique names nothing to avoid; a kept one is not a warning at all."""
    history = [
        {"verdict": "kept", "technique": "accuracy", "headline_metric": 0.9},
        {"verdict": "rejected", "technique": None, "headline_metric": 0.1},
        {"verdict": "rejected", "technique": "coverage", "headline_metric": 0.2},
    ]
    assert [r["technique"] for r in OptimizeAgentTool._rejected_changes(history)] == ["coverage"]


# --- measuring in code ----------------------------------------------------------------------------

class TestTheHarnessInvocation:
    """The one command the contract allows, and the sandbox it runs under."""

    def test_the_entry_point_has_a_single_definition(self):
        """Two halves of one contract read this name: the generator checks the file exists after
        writing the harness, and this tool builds the command that runs it. Held as two literals they
        could be renamed apart, leaving the existence check passing while the invocation looked for a
        file that was never written."""
        from appsci_agent.tools import eval_generator, optimize_agent

        assert optimize_agent.RUN_SCRIPT_FILENAME is eval_generator.RUN_SCRIPT_FILENAME
        assert eval_generator.RUN_SCRIPT_FILENAME in eval_generator.REQUIRED_EVAL_FILES
        command = OptimizeAgentTool()._harness_command("/workspace/eval", ENDPOINT)
        assert eval_generator.RUN_SCRIPT_FILENAME in command

    def test_it_uses_the_pinned_entry_point_and_env_var(self):
        command = OptimizeAgentTool()._harness_command("/workspace/eval", ENDPOINT)
        assert "cd /workspace/eval" in command
        assert f"CANDIDATE_ENDPOINT_URL={ENDPOINT}" in command
        assert command.endswith("python run.py")

    def test_paths_and_urls_are_quoted(self):
        """A directory or endpoint with a shell metacharacter must not become part of the command."""
        command = OptimizeAgentTool()._harness_command("/tmp/a b; rm -rf /", "http://x/?a=1&b=2")
        assert "; rm -rf /" not in command.replace("'/tmp/a b; rm -rf /'", "")
        assert "&b=2" not in command.replace("'http://x/?a=1&b=2'", "")

    def test_the_harness_runs_unprivileged(self, monkeypatch):
        """It is LLM-authored code. Run from this process unwrapped it would execute as root and could
        read the meta-agent's own source at /app -- the exposure the sandbox wrapper exists to close."""
        seen = {}
        monkeypatch.setattr("appsci_agent.tools.optimize_agent.subprocess.Popen",
                            _fake_popen(seen, stdout='{"headline_metric": 0.5}'))
        monkeypatch.setattr("appsci_agent.tools.mini_swe_agent.can_drop_privileges", lambda: True)

        code, stdout, _ = OptimizeAgentTool()._run_harness("/workspace/eval", ENDPOINT)

        assert code == 0 and "headline_metric" in stdout
        assert "setpriv" in seen["command"]
        assert "--reuid=swe" in seen["command"]

    def test_the_harness_gets_its_own_process_group(self, monkeypatch):
        """Without this a timeout kills only the outer shell.

        The command is layered -- `env` -> `setpriv` -> `sh -c` -> `python` -- so the harness itself is
        a grandchild. Orphaned it keeps making model calls with an inherited key while `_measure`
        retries, so two runs hit the same candidate at once.
        """
        seen = {}
        monkeypatch.setattr("appsci_agent.tools.optimize_agent.subprocess.Popen",
                            _fake_popen(seen, stdout='{"headline_metric": 0.5}'))
        OptimizeAgentTool()._run_harness("/workspace/eval", ENDPOINT)
        assert seen["kwargs"].get("start_new_session") is True

    def test_a_timeout_kills_the_whole_group_and_fails(self, monkeypatch):
        import signal

        seen = {}
        monkeypatch.setattr("appsci_agent.tools.optimize_agent.subprocess.Popen",
                            _fake_popen(seen, stdout="partial", raise_timeout=True))
        killed = {}
        monkeypatch.setattr("appsci_agent.tools.optimize_agent.os.getpgid", lambda pid: 4242)
        monkeypatch.setattr("appsci_agent.tools.optimize_agent.os.killpg",
                            lambda pgid, sig: killed.update(pgid=pgid, sig=sig))

        code, stdout, stderr = OptimizeAgentTool()._run_harness("/workspace/eval", ENDPOINT)

        assert code != 0
        assert "did not finish" in stderr
        assert stdout == "partial"       # what it had written is still diagnosable
        assert killed["pgid"] == 4242    # the GROUP, not just the direct child
        assert killed["sig"] == signal.SIGKILL


class TestAScoreIsJoinedToTheContainerThatProducedIt:
    """The checkpoint is captured when the score is TAKEN, not when it is written.

    The sub-agent edits the artifact source between those two points, so the deployed fingerprint no
    longer matches any checkpoint by the time the record is written and the join silently fails. Every
    baseline in the runs on disk carried `checkpoint: null` with a note claiming the run kept none,
    while three sat in the manifest -- a real, traceable score attributed to nothing, which is what
    makes a baseline impossible to re-score against the real eval later.
    """

    def _status_that_goes_stale(self, artifact_dir):
        """Deploy status as it really behaves: it matches until the SOURCE is edited.

        Keyed off the artifact's actual contents rather than a call count, because the tool asks for
        status more than once per call (the preconditions check asks first) and both of those come
        before the sub-agent edits anything. A count-based stub would go stale at the wrong moment and
        test nothing.
        """
        calls = []

        def _status(_self):
            calls.append(1)
            edited = (artifact_dir / "server.py").read_text() != "# v1\n"
            if not edited:
                return STATUS_OK
            # Post-edit: still deployed, but the fingerprint has moved on, so nothing matches.
            return {**STATUS_OK, "checkpoint": None, "stale": True,
                    "current_fingerprint": "edited-since", "deployed_fingerprint": "abc123"}

        return _status, calls

    def test_the_baseline_keeps_its_checkpoint_despite_the_later_edit(self, artifact_dir):
        _artifact(artifact_dir)
        _write_eval(artifact_dir)
        status, calls = self._status_that_goes_stale(artifact_dir)

        with mock.patch.object(OptimizeAgentTool, "_deploy_status", status), \
             _patch_swe(_swe(scores=[0.4], edit_marker="# v2\n")), \
             _patch_deploy(_deploy_ok()):
            res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))

        assert res.success, res.error
        record = _history(artifact_dir)[0]
        assert record["stage"] == "baseline"
        assert record["checkpoint"] is not None, (
            "the baseline score was recorded with no checkpoint, so it cannot be traced to the files "
            "that produced it -- the status was read after the sub-agent edited the source")
        assert record["checkpoint"]["name"] == STATUS_OK["checkpoint"]["name"]
        assert record.get("checkpoint_note") is None
        assert calls, "the deploy status was never consulted"

    def test_a_genuine_mismatch_says_only_what_is_known(self, artifact_dir):
        """When nothing really matches, the note must not claim the run keeps no checkpoints."""
        _artifact(artifact_dir)
        _write_eval(artifact_dir)
        no_match = {**STATUS_OK, "checkpoint": None}

        with mock.patch.object(OptimizeAgentTool, "_deploy_status", lambda _s: no_match), \
             _patch_swe(_swe(scores=[0.4])), _patch_deploy(_deploy_ok()):
            OptimizeAgentTool().execute({}, _ctx(artifact_dir))

        note = _history(artifact_dir)[0]["checkpoint_note"]
        assert "matched no checkpoint" in note
        assert "keeps no checkpoints" not in note


class TestTheTamperCheckCoversMeasuringToo:
    """Measuring is an actor, not just a read: `_measure` can delegate a dependency repair.

    The check used to run only after the improvement step, so every path that measures without editing
    -- `score_only`, a spent budget, saturation -- had no check at all. And with a budget of N the call
    that resolves the last change always has the budget spent, so every budgeted run ended on that
    path. A repair sub-agent could rewrite the harness and the score was still recorded and used to
    accept or roll back a change.
    """

    def _tampering_harness(self, eval_dir):
        def _run(self, _eval_dir, _endpoint_url):
            with open(os.path.join(eval_dir, "eval_data.json"), "w") as f:
                f.write('[{"id": "trivially_passing"}]')
            return 0, json.dumps(_report(1.0)), ""
        return _run

    @pytest.mark.parametrize("params,ctx_kwargs", [
        ({"score_only": True}, {}),
        ({}, {"optimize_cycles_allowed": 0}),
    ])
    def test_a_measure_only_call_still_refuses_a_tampered_harness(
        self, artifact_dir, params, ctx_kwargs,
    ):
        _artifact(artifact_dir)
        eval_dir = _write_eval(artifact_dir)

        with _patch_status(STATUS_OK), \
             mock.patch("appsci_agent.tools.optimize_agent.OptimizeAgentTool._run_harness",
                        self._tampering_harness(eval_dir)), \
             mock.patch("appsci_agent.tools.optimize_agent.MiniSweAgentTool.execute",
                        _swe(scores=[])), \
             _patch_deploy(_deploy_ok()):
            res = OptimizeAgentTool().execute(params, _ctx(artifact_dir, **ctx_kwargs))

        assert not res.success
        assert "was modified during this call" in res.error
        # A suspiciously perfect score from an unblessed harness must not reach the record.
        assert res.output.get("headline_metric") is None
        assert not os.path.exists(os.path.join(str(artifact_dir.parent), OPTIMIZE_DIRNAME,
                                               HISTORY_FILENAME))

    def test_nothing_is_claimed_restored_when_no_change_was_in_flight(self, artifact_dir):
        """Caught before any snapshot exists, so there is nothing to put back -- and saying otherwise
        would be a claim about work that never happened."""
        _artifact(artifact_dir)
        eval_dir = _write_eval(artifact_dir)

        with _patch_status(STATUS_OK), \
             mock.patch("appsci_agent.tools.optimize_agent.OptimizeAgentTool._run_harness",
                        self._tampering_harness(eval_dir)), \
             mock.patch("appsci_agent.tools.optimize_agent.MiniSweAgentTool.execute",
                        _swe(scores=[])), \
             _patch_deploy(_deploy_ok()):
            res = OptimizeAgentTool().execute({"score_only": True}, _ctx(artifact_dir))

        assert res.output["restored_candidate"] is None
        assert "nothing needed restoring" in res.error


class TestANonZeroExitIsNeverScored:
    """A harness that scores what it could bind and exits non-zero to flag the rest.

    That is the shape the generator brief now asks for, and the old condition (`exit != 0` AND empty
    stdout) accepted it -- the partial score was used to accept or roll back a change and the exit code
    never reached the decision.
    """

    def test_a_well_formed_report_with_a_non_zero_exit_is_refused(self, artifact_dir):
        _artifact(artifact_dir)
        _write_eval(artifact_dir)
        runs = []

        def _run(self, eval_dir, endpoint_url):
            runs.append(1)
            return 3, json.dumps(_report(0.9)), "2 of 7 cases could not be bound"

        with _patch_status(STATUS_OK), \
             mock.patch("appsci_agent.tools.optimize_agent.OptimizeAgentTool._run_harness", _run), \
             mock.patch("appsci_agent.tools.optimize_agent.MiniSweAgentTool.execute",
                        _swe(scores=[])), \
             _patch_deploy(_deploy_ok()):
            res = OptimizeAgentTool().execute({"score_only": True}, _ctx(artifact_dir))

        assert not res.success
        assert "exited 3" in res.error
        assert res.output.get("headline_metric") is None
        # One repair attempt plus one retry, then it gives up -- it does not loop on a broken harness.
        assert len(runs) == 2


class TestTheDependencyRepairFallback:
    """Measuring moved into code, but the reason it was ever agentic -- a harness missing a package --
    still has to work. It is now a fallback rather than the normal path."""

    def test_a_failed_first_run_is_retried_after_a_repair(self, artifact_dir):
        _artifact(artifact_dir)
        _write_eval(artifact_dir)
        runs = []
        repairs = []

        def _run(self, eval_dir, endpoint_url):
            runs.append(1)
            if len(runs) == 1:
                return 1, "", "ModuleNotFoundError: no module named 'httpx'"
            return 0, json.dumps(_report(0.6)), ""

        def _swe_repairs(self, params, context):
            repairs.append(params["task"])
            return ToolResult(success=True, output={})

        with _patch_status(STATUS_OK), \
             mock.patch("appsci_agent.tools.optimize_agent.OptimizeAgentTool._run_harness", _run), \
             mock.patch("appsci_agent.tools.optimize_agent.MiniSweAgentTool.execute", _swe_repairs), \
             _patch_deploy(_deploy_ok()):
            res = OptimizeAgentTool().execute({"score_only": True}, _ctx(artifact_dir))

        assert res.success, res.error
        assert res.output["headline_metric"] == 0.6
        assert len(runs) == 2                       # one failure, one retry after the repair
        assert "make it runnable" in repairs[0]
        assert "ModuleNotFoundError" in repairs[0]  # the repair brief carries the actual failure

    def test_a_repair_that_edits_the_candidate_is_undone(self, artifact_dir):
        """The repair brief says not to touch the candidate, and a brief cannot bind it.

        This path runs on measure-only calls too, where the caller takes no snapshot and reports
        `changed: False` -- so an edit here would be live, unrecorded and impossible to roll back,
        surfacing on the next call as a stale artifact with nothing explaining why.
        """
        _artifact(artifact_dir)
        _write_eval(artifact_dir)
        runs = []

        def _run(self, eval_dir, endpoint_url):
            runs.append(1)
            if len(runs) == 1:
                return 1, "", "ModuleNotFoundError: no module named 'httpx'"
            return 0, json.dumps(_report(0.6)), ""

        def _swe_edits_the_candidate(self, params, context):
            with open(os.path.join(context.artifact_dir, "server.py"), "w") as f:
                f.write("# smuggled\n")
            return ToolResult(success=True, output={})

        with _patch_status(STATUS_OK), \
             mock.patch("appsci_agent.tools.optimize_agent.OptimizeAgentTool._run_harness", _run), \
             mock.patch("appsci_agent.tools.optimize_agent.MiniSweAgentTool.execute",
                        _swe_edits_the_candidate), \
             _patch_deploy(_deploy_ok()):
            res = OptimizeAgentTool().execute({"score_only": True}, _ctx(artifact_dir))

        assert res.success, res.error
        assert (artifact_dir / "server.py").read_text() == "# v1\n"
        assert res.output["cycle"]["changed"] is False

    def test_the_repair_brief_forbids_touching_the_harness_or_the_candidate(self):
        brief = OptimizeAgentTool()._repair_brief("/workspace/eval", ENDPOINT, "boom")
        assert "may NOT edit any file under /workspace/eval" in brief
        assert "Do NOT touch the candidate" in brief
        assert "Do NOT write a report" in brief
        assert "install missing dependencies" in brief

    def test_a_run_that_scored_badly_is_not_repaired(self, artifact_dir):
        """A harness that ran and returned a low number is working. Installing packages at it would
        burn a sub-agent run on nothing, and 0.0 is a legitimate score."""
        _artifact(artifact_dir)
        _write_eval(artifact_dir)
        runs = []

        def _run(self, eval_dir, endpoint_url):
            runs.append(1)
            return 0, json.dumps(_report(0.0)), ""

        def _no_swe(self, params, context):
            raise AssertionError("a working harness must not trigger a repair")

        with _patch_status(STATUS_OK), \
             mock.patch("appsci_agent.tools.optimize_agent.OptimizeAgentTool._run_harness", _run), \
             mock.patch("appsci_agent.tools.optimize_agent.MiniSweAgentTool.execute", _no_swe), \
             _patch_deploy(_deploy_ok()):
            res = OptimizeAgentTool().execute({"score_only": True}, _ctx(artifact_dir))

        assert res.success, res.error
        assert res.output["headline_metric"] == 0.0
        assert len(runs) == 1

    def test_the_harness_output_is_kept_even_when_it_is_not_json(self, artifact_dir):
        """Stdout IS the report, so when it is junk the bytes are the only record of what went wrong."""
        _artifact(artifact_dir)
        _write_eval(artifact_dir)

        def _run(self, eval_dir, endpoint_url):
            return 0, "Traceback (most recent call last): boom", "stderr detail"

        with _patch_status(STATUS_OK), \
             mock.patch("appsci_agent.tools.optimize_agent.OptimizeAgentTool._run_harness", _run), \
             mock.patch("appsci_agent.tools.optimize_agent.MiniSweAgentTool.execute",
                        _swe(scores=[])), \
             _patch_deploy(_deploy_ok()):
            res = OptimizeAgentTool().execute({}, _ctx(artifact_dir))

        assert not res.success
        opt = os.path.join(str(artifact_dir.parent), OPTIMIZE_DIRNAME)
        with open(os.path.join(opt, REPORT_FILENAME)) as f:
            assert "boom" in f.read()
        with open(os.path.join(opt, "eval_stderr.log")) as f:
            assert "stderr detail" in f.read()
