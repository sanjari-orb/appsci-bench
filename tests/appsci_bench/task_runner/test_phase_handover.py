"""The build->eval environment handover: does each phase get its OWN data, and do the two failures
that would corrupt a run silently actually get caught?

A run brings the task environment up twice (docs/task_env_split.md): the build env the AppSci agent
explores, holding only records no eval case touches, and then -- after that one is torn down -- the
eval env the artifact is scored against. Both come up from the same compose file under the same
project name, which is what lets the artifact keep resolving the same address across the handover, and
is also what makes the failures below possible.

Docker is never invoked here: the container manager is faked, so these assert the decisions (which
bundle, which script, was the container actually replaced) rather than the containers.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import subprocess
from pathlib import Path
from unittest import mock

import pytest
import yaml

from appsci_bench.task_runner import PHASE_BUILD, PHASE_EVAL, TaskRunner
from appsci_bench.task_runner.containers import ContainerManager

#: Repo root, so the tests that drive a real task spec do not depend on pytest's cwd. Run from
#: anywhere else, `Path("tasks")` resolves to nothing, the run swallows the missing task dir and
#: returns status="failed", and the assertions then fail on a KeyError that reads like the plumbing
#: regressed rather than like a bad path. Same derivation as
#: tests/appsci_bench/orchestrator/test_run_signals.py.
REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_spec(tasks_dir: Path, task_id: str, environments: dict) -> None:
    task_dir = tasks_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "spec.yaml").write_text(yaml.safe_dump({
        "task_id": task_id,
        "name": "t",
        "description": "d",
        "task_source_type": "synthetic",
        "environment": {
            "dockerfile": "env/Dockerfile",
            "docker_compose": "env/docker-compose.yml",
        },
        "task_runner": {
            "network_name": "bench-network",
            "workspace_name": "widget-workspace",
            "environments": environments,
        },
    }, sort_keys=False))


_SPLIT = {
    "build": {
        "bundle": [{"src": "environment/build/fixture/", "dest": "/fixture/"}],
        "setup_script": "/scripts/setup.sh",
    },
    "eval": {
        "bundle": [{"src": "environment/eval/fixture/", "dest": "/fixture/"}],
        "setup_script": "/scripts/setup.sh",
    },
}


def _runner(tasks_dir: Path, *, ids=("aaa", "bbb"), real_network: bool = False) -> TaskRunner:
    """A TaskRunner whose container + bundle managers are fakes.

    `ids` are the container ids returned by successive container_id() calls: the runner reads it once
    before `compose up` and once after, so ("aaa", "bbb") models a normal start (nothing there, then
    a new container) only if the first is None -- see the individual tests.

    `real_network` keeps the genuine `_create_network`, for the tests that are ABOUT it and patch
    `subprocess` themselves. Everything else gets it faked (see below).
    """
    runner = TaskRunner(tasks_dir)
    runner._container_manager = mock.MagicMock()
    runner._container_manager.container_id.side_effect = list(ids)
    runner._container_manager.exists.return_value = False       # no db service
    runner._container_manager.wait_until_ready.return_value = True
    # `_create_network` is NOT part of the container manager -- it shells out to `docker network
    # create` directly, so faking only the manager left every start() in this file creating a real
    # bench-network on whatever machine ran the suite, including CI runners, and never removing it.
    # That contradicted this file's own docstring ("Docker is never invoked here").
    if not real_network:
        runner._create_network = mock.MagicMock()
    runner._bundle_manager = mock.MagicMock()
    runner._bundle_manager.create_bundle.return_value = tasks_dir / "bundle.tar.gz"
    (tasks_dir / "bundle.tar.gz").write_bytes(b"x")
    return runner


# --- each phase loads its own data -------------------------------------------

@pytest.mark.parametrize(
    "phase,expected_src",
    [(PHASE_BUILD, "environment/build/fixture/"), (PHASE_EVAL, "environment/eval/fixture/")],
)
def test_each_phase_bundles_only_its_own_data(tmp_path, phase, expected_src):
    """The whole point: the eval records must never enter the build phase's container."""
    _write_spec(tmp_path, "widget_task", _SPLIT)
    runner = _runner(tmp_path, ids=(None, "new"))

    env = runner.start("widget_task", phase=phase)

    bundle_arg = runner._bundle_manager.create_bundle.call_args.args[1]
    assert bundle_arg == [{"src": expected_src, "dest": "/fixture/"}]
    assert env["phase"] == phase


def test_both_phases_use_the_same_container_name(tmp_path):
    """Why the artifact needs no re-pointing across the handover."""
    _write_spec(tmp_path, "widget_task", _SPLIT)

    names = []
    for phase in (PHASE_BUILD, PHASE_EVAL):
        runner = _runner(tmp_path, ids=(None, "new"))
        names.append(runner.start("widget_task", phase=phase)["workspace_container"])

    assert names[0] == names[1] == "widget-workspace-1"


def test_a_lane_gets_its_own_compose_project_and_lane_zero_keeps_the_old_name(tmp_path):
    """Compose keys containers, volumes and networks off the project name, so a suffixed project IS
    the isolation. Lane 0 must stay unsuffixed: teardown, the eval-only CLI's orphan detection and the
    tracer all address the environment by that name."""
    _write_spec(tmp_path, "widget_task", _SPLIT)

    names = []
    for lane in (0, 1, 2):
        runner = _runner(tmp_path, ids=(None, "new"))
        env = runner.start("widget_task", phase=PHASE_EVAL, lane=lane)
        names.append(env["workspace_container"])
        assert env["lane"] == lane
        assert runner.teardown_descriptor("widget_task", lane=lane)["compose_project"] == (
            "widget" if lane == 0 else f"widget-l{lane}"
        )

    assert names == ["widget-workspace-1", "widget-l1-workspace-1", "widget-l2-workspace-1"]


def test_a_lanes_compose_env_overlay_reaches_the_compose_command(tmp_path):
    """How two copies of one compose file end up with different tool tokens: compose expands `${VAR}`
    against the environment of the process that started it."""
    _write_spec(tmp_path, "widget_task", _SPLIT)
    runner = _runner(tmp_path, ids=(None, "new"))

    runner.start("widget_task", phase=PHASE_EVAL, lane=1, env_overlay={"WIDGET_TOKEN": "lane1-tok"})

    assert runner._container_manager.start.call_args.args[2] == {"WIDGET_TOKEN": "lane1-tok"}


def test_a_phase_declared_none_starts_nothing(tmp_path):
    _write_spec(tmp_path, "widget_task", {**_SPLIT, "eval": "none"})
    runner = _runner(tmp_path, ids=(None, "new"))

    assert runner.start("widget_task", phase=PHASE_EVAL) is None
    runner._container_manager.start.assert_not_called()
    runner._bundle_manager.create_bundle.assert_not_called()
    # And stop() tolerates it, so a caller can pass start()'s result straight through.
    runner.stop(None)


def test_the_network_can_be_created_without_starting_a_phase(tmp_path):
    """What a caller needs when the phase it wanted declares no environment.

    Nothing else creates the network then, and an eval trial of such a task still deploys the artifact
    onto it. Idempotent, because the usual case is that some phase already made it.
    """
    _write_spec(tmp_path, "widget_task", {**_SPLIT, "eval": "none"})
    runner = _runner(tmp_path, ids=(None, "new"), real_network=True)

    with mock.patch.object(subprocess, "run") as run:
        assert runner.ensure_network("widget_task") == "bench-network"

    assert run.call_args.args[0] == ["docker", "network", "create", "bench-network"]
    # Nothing was brought up: this creates the network and only the network.
    runner._container_manager.start.assert_not_called()


def test_an_already_existing_network_is_not_an_error(tmp_path):
    _write_spec(tmp_path, "widget_task", _SPLIT)
    runner = _runner(tmp_path, ids=(None, "new"), real_network=True)

    # Docker's real wording, not a paraphrase, so the substring match is pinned to what the daemon
    # actually emits.
    already = subprocess.CalledProcessError(
        1, "docker",
        stderr=b"Error response from daemon: network with name bench-network already exists",
    )
    with mock.patch.object(subprocess, "run", side_effect=already):
        assert runner.ensure_network("widget_task") == "bench-network"


def test_a_failing_network_create_is_raised(tmp_path):
    """Anything other than "already exists" is a real docker failure, and the caller deploys onto this
    network moments later -- swallowing it would surface as an unexplained deploy failure instead."""
    _write_spec(tmp_path, "widget_task", _SPLIT)
    runner = _runner(tmp_path, ids=(None, "new"), real_network=True)

    boom = subprocess.CalledProcessError(1, "docker", stderr=b"permission denied")
    with mock.patch.object(subprocess, "run", side_effect=boom):
        with pytest.raises(subprocess.CalledProcessError):
            runner.ensure_network("widget_task")


def test_an_unknown_phase_raises(tmp_path):
    _write_spec(tmp_path, "widget_task", _SPLIT)
    runner = _runner(tmp_path, ids=(None, "new"))
    with pytest.raises(ValueError, match="unknown run phase"):
        runner.start("widget_task", phase="evaluation")


# --- failure 1: compose attaching to a build env that never came down --------

def test_reusing_a_live_container_is_reported(tmp_path):
    """`compose up` under an existing project ATTACHES instead of recreating.

    If the build env is still standing when the eval env goes up, the eval data loads on top of the
    build data and every other surface still reports healthy: health passes, setup runs, nothing
    raises. Comparing the container id across the call is the only thing that notices.
    """
    _write_spec(tmp_path, "widget_task", _SPLIT)
    runner = _runner(tmp_path, ids=("same-id", "same-id"))     # id unchanged => attached

    env = runner.start("widget_task", phase=PHASE_EVAL)

    assert env["reused_existing_container"] is True


def test_a_freshly_created_container_is_not_reported_as_reused(tmp_path):
    _write_spec(tmp_path, "widget_task", _SPLIT)
    runner = _runner(tmp_path, ids=(None, "brand-new"))         # nothing there before

    env = runner.start("widget_task", phase=PHASE_BUILD)

    assert env["reused_existing_container"] is False


def test_a_replaced_container_is_not_reported_as_reused(tmp_path):
    """The normal handover: a container existed, compose recreated it, so the id changed."""
    _write_spec(tmp_path, "widget_task", _SPLIT)
    runner = _runner(tmp_path, ids=("old-id", "new-id"))

    env = runner.start("widget_task", phase=PHASE_EVAL)

    assert env["reused_existing_container"] is False


# --- failure 2: data that did not load --------------------------------------

def test_the_phase_setup_script_runs(tmp_path):
    """One script per phase, and it is the phase's own.

    A task asserts its data actually landed at the END of this script rather than in a check of its
    own: a failing setup script aborts the run, which is the right outcome when the records are
    missing, and it is one less concept for a task owner to learn.
    """
    _write_spec(tmp_path, "widget_task", _SPLIT)
    runner = _runner(tmp_path, ids=(None, "new"))

    runner.start("widget_task", phase=PHASE_EVAL)

    scripts = [c.args[1] for c in runner._container_manager.execute_command.call_args_list
               if c.args[1][:1] == ["bash"]]
    assert scripts == [["bash", "/scripts/setup.sh"]]


def test_a_failing_setup_script_aborts_the_phase(tmp_path):
    """Nothing should come up holding no data and report itself fine."""
    _write_spec(tmp_path, "widget_task", _SPLIT)
    runner = _runner(tmp_path, ids=(None, "new"))

    def _exec(container, command):
        if command == ["bash", "/scripts/setup.sh"]:
            raise RuntimeError("expected 2000 reservations, found 0")
        return ""
    runner._container_manager.execute_command.side_effect = _exec

    with pytest.raises(RuntimeError, match="found 0"):
        runner.start("widget_task", phase=PHASE_EVAL)


# --- an eval trial's environment ---------------------------------------------
#
# Driven for real, unlike the ordering assertions further down: an eval-only run starts no agent and no
# user-sim, so with the task runner faked and the deploy + scoring stubbed, `run()` executes end to end
# with no Docker. That is what makes these assert the CALL rather than the source, which matters here --
# reverting the branch to the build phase passed the entire suite before this existed.

_EVAL_ENV = {
    "workspace_container": "o2c-workspace-1",
    "compose_project": "o2c",
    "compose_file": "x",
    "network_name": "bench-network",
    "phase": PHASE_EVAL,
}

_TEARDOWN = {
    "workspace_container": "o2c-workspace-1",
    "compose_project": "o2c",
    "compose_file": "x",
    "network_name": "bench-network",
}


def _drive_trial(tmp_path, *, env_started, reused=False):
    """Run one eval-only trial against a real task spec, with Docker faked out.

    Returns (result, fake task runner, deploy mock, evaluate mock). `env_started` False models a task
    whose eval phase declares no environment (`eval: none`), which start() reports by returning None.
    """
    from appsci_bench.orchestrator import run as run_mod

    env = None
    if env_started:
        env = {**_EVAL_ENV, "reused_existing_container": reused}

    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir(exist_ok=True)

    orch = run_mod.BenchmarkOrchestrator(
        tasks_dir=REPO_ROOT / "tasks", observability=False, runs_dir=tmp_path / "runs",
    )
    orch.task_runner = mock.MagicMock()
    orch.task_runner.start.return_value = env
    orch.task_runner.teardown_descriptor.return_value = dict(_TEARDOWN)

    report = mock.MagicMock(views={"per_metric": []}, final_score=0.5, store_root=None)
    with mock.patch.object(
        run_mod, "deploy_artifact_dir", return_value=("http://localhost:1/respond", "c", None),
    ) as deploy, mock.patch.object(
        run_mod, "evaluate_deployed", return_value=report,
        # returncode=0 rather than a bare MagicMock, whose truthy `.returncode` reads as a FAILURE to
        # any docker query added to this path later while these tests stayed green.
    ) as evaluate, mock.patch.object(
        run_mod.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr=""),
    ):
        result = asyncio.run(orch.run(
            task_id="order_to_cash",
            persona_path=Path("unused-in-eval-only.yaml"),
            artifact_dir=artifact_dir,
        ))
    return result, orch.task_runner, deploy, evaluate


def test_a_reused_environment_is_recorded_where_a_sweep_can_find_it(tmp_path):
    """The contamination signal has to outlive the run, and the event log is the only thing that does.

    `BenchmarkResult.metadata` is a plain dataclass field nothing persists or reads, and the ERROR line
    fires near the start of a cell whose log the worker keeps only the tail of. So a trial that adopted a
    leftover container scored normally, exited 0, produced a valid report and entered the pass^k
    estimator with nothing machine-readable saying its store was wrong.

    Asserted against the file on disk rather than a mocked publisher, because the file is what gets
    mirrored to S3 and what `grep task_env.reused` over a sweep actually reads.
    """
    from shared.observability.events import EventType

    from appsci_bench.orchestrator import run as run_mod

    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    runs_dir = tmp_path / "runs"
    orch = run_mod.BenchmarkOrchestrator(
        tasks_dir=REPO_ROOT / "tasks", observability=True, runs_dir=runs_dir,
    )
    orch.task_runner = mock.MagicMock()
    orch.task_runner.start.return_value = {**_EVAL_ENV, "reused_existing_container": True}
    orch.task_runner.teardown_descriptor.return_value = dict(_TEARDOWN)

    report = mock.MagicMock(views={"per_metric": []}, final_score=0.5, store_root=None)
    with mock.patch.object(
        run_mod, "_start_tracer_with_retry",
        new=mock.AsyncMock(return_value=(mock.MagicMock(), "http://tracer.test", 8080)),
    ), mock.patch.object(
        run_mod, "deploy_artifact_dir", return_value=("http://localhost:1/respond", "c", None),
    ), mock.patch.object(run_mod, "evaluate_deployed", return_value=report), \
            mock.patch.object(run_mod, "_register_eval_publisher"), \
            mock.patch.object(
                run_mod.subprocess, "run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ):
        result = asyncio.run(orch.run(
            task_id="order_to_cash",
            persona_path=Path("unused-in-eval-only.yaml"),
            artifact_dir=artifact_dir,
        ))

    assert result.status == "completed"
    logs = list(runs_dir.glob("*/events-orchestrator.jsonl"))
    assert logs, "the run wrote no orchestrator event log"
    events = [json.loads(line) for line in logs[0].read_text().splitlines() if line.strip()]
    reused = [e for e in events if e.get("event_type") == EventType.TASK_ENV_REUSED.value]
    assert len(reused) == 1, (
        "a trial whose environment came up on a pre-existing container must leave a durable, "
        f"machine-readable marker. Events found: {[e.get('event_type') for e in events]}"
    )
    assert reused[0]["payload"]["phase"] == PHASE_EVAL


def test_a_clean_environment_records_no_reuse_event(tmp_path):
    """The other half: the marker has to mean something, so it must not fire on a healthy trial."""
    from shared.observability.events import EventType

    from appsci_bench.orchestrator import run as run_mod

    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    runs_dir = tmp_path / "runs"
    orch = run_mod.BenchmarkOrchestrator(
        tasks_dir=REPO_ROOT / "tasks", observability=True, runs_dir=runs_dir,
    )
    orch.task_runner = mock.MagicMock()
    orch.task_runner.start.return_value = {**_EVAL_ENV, "reused_existing_container": False}
    orch.task_runner.teardown_descriptor.return_value = dict(_TEARDOWN)

    report = mock.MagicMock(views={"per_metric": []}, final_score=0.5, store_root=None)
    with mock.patch.object(
        run_mod, "_start_tracer_with_retry",
        new=mock.AsyncMock(return_value=(mock.MagicMock(), "http://tracer.test", 8080)),
    ), mock.patch.object(
        run_mod, "deploy_artifact_dir", return_value=("http://localhost:1/respond", "c", None),
    ), mock.patch.object(run_mod, "evaluate_deployed", return_value=report), \
            mock.patch.object(run_mod, "_register_eval_publisher"), \
            mock.patch.object(
                run_mod.subprocess, "run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ):
        asyncio.run(orch.run(
            task_id="order_to_cash",
            persona_path=Path("unused-in-eval-only.yaml"),
            artifact_dir=artifact_dir,
        ))

    logs = list(runs_dir.glob("*/events-orchestrator.jsonl"))
    events = [
        json.loads(line)
        for log in logs for line in log.read_text().splitlines() if line.strip()
    ]
    assert not [e for e in events if e.get("event_type") == EventType.TASK_ENV_REUSED.value]


def test_a_trial_starts_the_eval_environment_and_never_the_build_one(tmp_path):
    """The defect this file's orchestrator section exists for.

    A trial scores an already-built artifact, so it must come up on the store the eval cases are drawn
    from. The trial branch used to sit after the build-env start and reuse that dict, so every trial of
    a `builds`/`trials` sweep scored against the build share -- for order_to_cash, roughly a fifth of
    the records, with a case naming an absent record erroring into a confident-looking low score.
    """
    result, runner, _deploy, evaluate = _drive_trial(tmp_path, env_started=True)

    assert result.status == "completed"
    assert runner.start.call_args_list == [mock.call("order_to_cash", phase=PHASE_EVAL)]
    # And the environment the scorer is handed is that one: `evaluate_deployed` reads the per-sample
    # reset token off the container in this dict, so passing the build phase's would reset the wrong
    # store even where both phases hold the same records.
    assert evaluate.call_args.args[2]["phase"] == PHASE_EVAL


def test_a_trial_scores_elicitation_against_its_builds_transcript(tmp_path):
    """A trial has no build phase, so the conversation the diagnostic scores is in another run's dir.

    Passed nothing, the diagnostic reports `unmeasurable` -- and since a build cell never scores at all
    (`--skip-eval`), a `builds`/`trials` sweep computed elicitation NOWHERE while a plain build+eval run
    still did. Confirmed against a real trial: `"transcript_source": "(no build transcript provided)"`.
    """
    from appsci_bench.orchestrator import run as run_mod

    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    build_run = tmp_path / "runs" / "build-trace-1"
    build_run.mkdir(parents=True)
    (build_run / "events-usersim.jsonl").write_text("{}\n", encoding="utf-8")

    orch = run_mod.BenchmarkOrchestrator(
        tasks_dir=REPO_ROOT / "tasks", observability=False, runs_dir=tmp_path / "runs",
    )
    orch.task_runner = mock.MagicMock()
    orch.task_runner.start.return_value = dict(_EVAL_ENV)
    orch.task_runner.teardown_descriptor.return_value = dict(_TEARDOWN)

    report = mock.MagicMock(views={"per_metric": []}, final_score=0.5, store_root=None)
    with mock.patch.object(
        run_mod, "deploy_artifact_dir", return_value=("http://localhost:1/respond", "c", None),
    ), mock.patch.object(run_mod, "evaluate_deployed", return_value=report) as evaluate, \
            mock.patch.object(
                run_mod.subprocess, "run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ):
        asyncio.run(orch.run(
            task_id="order_to_cash",
            persona_path=Path("unused-in-eval-only.yaml"),
            artifact_dir=artifact_dir,
            build_run_dir=build_run,
        ))

    assert evaluate.call_args.kwargs.get("build_transcript") == build_run, (
        "the trial did not hand the scorer its build's run dir, so elicitation is unmeasurable"
    )


def test_a_trial_without_a_build_run_dir_still_scores_everything_else(tmp_path):
    """Reaped, or built on another host. Elicitation is lost; nothing else may be."""
    from appsci_bench.orchestrator import run as run_mod

    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    orch = run_mod.BenchmarkOrchestrator(
        tasks_dir=REPO_ROOT / "tasks", observability=False, runs_dir=tmp_path / "runs",
    )
    orch.task_runner = mock.MagicMock()
    orch.task_runner.start.return_value = dict(_EVAL_ENV)
    orch.task_runner.teardown_descriptor.return_value = dict(_TEARDOWN)

    report = mock.MagicMock(views={"per_metric": []}, final_score=0.5, store_root=None)
    with mock.patch.object(
        run_mod, "deploy_artifact_dir", return_value=("http://localhost:1/respond", "c", None),
    ), mock.patch.object(run_mod, "evaluate_deployed", return_value=report) as evaluate, \
            mock.patch.object(
                run_mod.subprocess, "run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ):
        result = asyncio.run(orch.run(
            task_id="order_to_cash",
            persona_path=Path("unused-in-eval-only.yaml"),
            artifact_dir=artifact_dir,
        ))

    assert result.status == "completed"
    assert evaluate.call_args.kwargs.get("build_transcript") is None


def test_a_trial_records_a_teardown_descriptor_before_starting_the_env(tmp_path):
    """So a start that throws part-way through is still torn down.

    The next trial of the same build runs on this same host minutes later, and `compose up` adopts a
    half-created environment instead of making a fresh one -- which is the poisoned-store case the
    reused-container check reports.
    """
    _result, runner, _deploy, _evaluate = _drive_trial(tmp_path, env_started=True)

    runner.teardown_descriptor.assert_called_once_with("order_to_cash")
    assert runner.method_calls.index(mock.call.teardown_descriptor("order_to_cash")) < \
        runner.method_calls.index(mock.call.start("order_to_cash", phase=PHASE_EVAL))


def test_a_trial_of_a_task_with_no_eval_environment_still_gets_a_network(tmp_path):
    """`eval: none` starts nothing, so nothing creates the network the artifact is deployed on.

    The eval inputs of such a task travel on the cases themselves (slides_generator is one), but the
    artifact container is still run with `--network <task network>`, and `docker run` fails outright if
    it does not exist. It used to exist only as a side effect of the build environment this branch no
    longer starts.
    """
    result, runner, deploy, evaluate = _drive_trial(tmp_path, env_started=False)

    assert result.status == "completed"
    runner.ensure_network.assert_called_once_with("order_to_cash")
    # Nothing came up, so nothing is torn down, and the scorer is told there is no environment.
    runner.stop.assert_not_called()
    assert evaluate.call_args.args[2] is None
    assert deploy.call_count == 1


def test_a_trial_on_a_reused_container_says_so_in_its_result(tmp_path):
    """Not only in the log. A trial's score is the thing that outlives the run.

    An environment that came up on a container that already existed holds whatever the last run left
    in it, which invalidates the trial -- so the flag has to reach the record a sweep is read from,
    under the same key the build-only path uses.
    """
    clean, _r, _d, _e = _drive_trial(tmp_path, env_started=True, reused=False)
    poisoned, _r2, _d2, _e2 = _drive_trial(tmp_path, env_started=True, reused=True)

    assert clean.metadata["task_env_reused_existing_container"] is False
    assert poisoned.metadata["task_env_reused_existing_container"] is True


# --- the orchestrator's handover ordering ------------------------------------

def _strip_comments(src: str) -> str:
    """`src` with `#` comments blanked out, positions preserved.

    Every source-order assertion below is really a claim about CODE, and this method's comments quote
    code constantly -- so without this, adding one comment that mentions a call can fail a test that
    nothing about the behaviour changed. Each comment becomes spaces of equal length rather than being
    deleted, so every index stays comparable to the unstripped source.

    Naive on purpose: a `#` inside a string literal would be treated as a comment. There is no such
    line in the method under inspection, and a real tokenizer here would be a second thing to maintain
    for no behavioural gain.
    """
    out = []
    for line in src.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        newline = line[len(body):]
        hash_at = body.find("#")
        if hash_at != -1:
            body = body[:hash_at] + " " * (len(body) - hash_at)
        out.append(body + newline)
    return "".join(out)


def test_the_orchestrator_halts_the_agent_before_starting_the_eval_env():
    """Source-order assertion; driving a real run needs Docker, an agent container and a task env.

    Three orderings in the handover are load-bearing, and each has a distinct failure:

      * halt the agent BEFORE the eval env starts -- the eval env comes up at the same container name
        and network alias the agent used all through the build phase, so a background process the
        coding subagent left behind would reach it;
      * tear the build env down BEFORE the eval env goes up -- otherwise compose attaches to it (see
        test_reusing_a_live_container_is_reported);
      * halt, don't remove -- `docker cp` works on a stopped container but not a removed one, and the
        snapshots still have to run.
    """
    import inspect

    from appsci_bench.orchestrator import run as run_mod

    src = inspect.getsource(run_mod.BenchmarkOrchestrator.run)
    at_halt = src.index("agent_svc.halt()")
    at_teardown = src.index("self.task_runner.stop(task_env)")
    # Anchor on the HANDOVER's own eval start, which assigns `eval_task_env`, not on the first
    # `phase=PHASE_EVAL` in the method: an eval-only trial also brings the eval env up, earlier and
    # legitimately, because it has no build phase and no agent at all.
    at_eval_start = src.index("eval_task_env = self.task_runner.start(task_id, phase=PHASE_EVAL)")
    at_agent_start = src.index("agent_svc.start()")
    assert at_halt < at_eval_start, (
        "the agent container must be halted BEFORE the eval environment starts; it would otherwise "
        "be live on the network at the address the eval env comes up on"
    )
    assert at_teardown < at_eval_start, (
        "the build environment must be torn down BEFORE the eval environment starts, or compose "
        "attaches to it and the eval data loads on top of the build data"
    )
    # The invariant the first assertion is a special case of: no eval environment may come up while a
    # live agent container exists. Stated separately because anchoring on one call site cannot see a
    # THIRD eval start added later somewhere inside that window.
    #
    # Matched on the CALL, not on the bare phase constant, and over source with comments stripped.
    # Both failure modes of the looser form were real: a comment mentioning `phase=PHASE_EVAL` inside
    # the window failed the test with no environment started (and this method's comments are dense with
    # literal code references), while a positionally-written `start(task_id, PHASE_EVAL)` inside it
    # passed. This pattern accepts either spelling and ignores prose.
    code = _strip_comments(src)
    at_agent_start_code = code.index("agent_svc.start()")
    at_halt_code = code.index("agent_svc.halt()")
    assert at_agent_start_code < at_halt_code
    for m in re.finditer(r"self\.task_runner\.start\([^)]*PHASE_EVAL", code):
        assert m.start() < at_agent_start_code or m.start() > at_halt_code, (
            "an eval environment is started while the agent container is live; it would come up at "
            "the container name and network alias the agent spent the build phase talking to"
        )
    assert "agent_svc.remove()" not in src, (
        "the handover must HALT the agent, not remove it -- the artifact and filesystem snapshots "
        "still need a container to copy out of"
    )


def test_a_trial_whose_env_start_throws_still_tears_the_stack_down(tmp_path):
    """The teardown descriptor's entire purpose, stated behaviourally rather than as call ordering.

    `start()` raises after `compose up` has already brought containers up -- a failed health check, a
    setup script that asserts its records and exits 1, a bundle that will not extract. Without a
    descriptor recorded first there is nothing for `finally` to stop, and the half-created environment
    is still standing when the next trial of this same build lands on this host minutes later, where
    `compose up` adopts it instead of making a fresh one.
    """
    from appsci_bench.orchestrator import run as run_mod

    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    orch = run_mod.BenchmarkOrchestrator(
        tasks_dir=REPO_ROOT / "tasks", observability=False, runs_dir=tmp_path / "runs",
    )
    orch.task_runner = mock.MagicMock()
    orch.task_runner.teardown_descriptor.return_value = dict(_TEARDOWN)
    orch.task_runner.start.side_effect = RuntimeError("health check never passed")

    with mock.patch.object(run_mod, "deploy_artifact_dir") as deploy, mock.patch.object(
        run_mod.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr=""),
    ):
        result = asyncio.run(orch.run(
            task_id="order_to_cash",
            persona_path=Path("unused-in-eval-only.yaml"),
            artifact_dir=artifact_dir,
        ))

    orch.task_runner.stop.assert_called_once_with(_TEARDOWN)
    assert result.status == "failed"
    # And nothing was deployed onto an environment that never came up.
    deploy.assert_not_called()


def test_no_eval_environment_is_started_while_the_agent_is_live(tmp_path):
    """The behavioural mirror of the source-order invariant above.

    A source-position check is satisfied today only because the trial branch ends in an unconditional
    `return`. Condition or remove that return -- to keep one artifact container alive across k trials,
    say -- and the eval store would be up and loaded when the agent container starts, with the text
    assertion still green. This drives a real build+eval run instead: at the moment `agent_svc.start()`
    is called, the only environment the runner has been asked for must be the BUILD one.
    """
    from appsci_bench.orchestrator import run as run_mod

    orch = run_mod.BenchmarkOrchestrator(
        tasks_dir=REPO_ROOT / "tasks", observability=False, runs_dir=tmp_path / "runs",
    )
    orch.task_runner = mock.MagicMock()
    orch.task_runner.start.return_value = {**_EVAL_ENV, "phase": PHASE_BUILD}
    orch.task_runner.teardown_descriptor.return_value = dict(_TEARDOWN)

    phases_at_agent_start: list[str] = []

    def record_then_fail():
        phases_at_agent_start.extend(
            c.kwargs.get("phase") or (c.args[1] if len(c.args) > 1 else None)
            for c in orch.task_runner.start.call_args_list
        )
        # Stop the run here: everything after this point is the agent's build phase, which needs a real
        # container. What the test is about has already happened.
        raise RuntimeError("stop the run at the moment the agent would start")

    agent = mock.MagicMock()
    agent.start.side_effect = record_then_fail

    # The submission server's start/stop are awaited, so they need async doubles rather than a plain
    # MagicMock (which raises "can't be used in 'await' expression" before the agent is ever reached).
    submission = mock.MagicMock()
    submission.start = mock.AsyncMock(return_value="http://submit.test/artifact")
    submission.stop = mock.AsyncMock()

    with mock.patch.object(run_mod, "AppSciAgentContainerManager", return_value=agent), \
            mock.patch.object(run_mod, "DockerAppsciAgentUserSimService"), \
            mock.patch.object(run_mod, "ArtifactSubmissionServer", return_value=submission), \
            mock.patch.object(run_mod, "write_agent_spec", return_value=tmp_path / "spec.yaml"), \
            mock.patch.object(
                run_mod.subprocess, "run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ):
        asyncio.run(orch.run(
            task_id="order_to_cash",
            persona_path=REPO_ROOT / "tasks/order_to_cash/user_personas/l3.yaml",
        ))

    assert agent.start.called, "the run never reached the agent start this test is anchored on"
    assert phases_at_agent_start == [PHASE_BUILD], (
        "an eval environment was brought up before the agent container started; it would come up at "
        f"the container name and network alias the agent talks to all through the build phase. "
        f"Phases started: {phases_at_agent_start}"
    )


def test_the_orchestrator_scores_against_the_eval_environment():
    """The per-case reset token is read off whichever env dict reaches the scorer.

    Hand it the build env's dict and reset would target a container that no longer exists -- the DB
    half of every case would go unmeasured, or worse, be measured against a stale container.
    """
    import inspect

    from appsci_bench.orchestrator import run as run_mod

    src = inspect.getsource(run_mod.BenchmarkOrchestrator.run)
    # The HANDOVER's own eval start, not the first `phase=PHASE_EVAL` in the method: the trial branch
    # added one earlier and legitimately, which made the plain anchor match it and left this test's
    # ordering assertion vacuous while widening its window to most of the method.
    at_eval_start = src.index("eval_task_env = self.task_runner.start(task_id, phase=PHASE_EVAL)")
    at_score = src.index("report = evaluate_deployed(")
    assert at_eval_start < at_score
    # The variable handed to the scorer is reassigned from the eval start, not left as the build env.
    between = src[at_eval_start:at_score]
    assert "task_env = eval_task_env" in between, (
        "the eval environment's dict must replace the build one before scoring, so per-case reset "
        "targets the container that is actually running"
    )


# --- failure 2b: the assertion text surviving the harness --------------------

def test_a_failing_setup_script_reports_its_own_message(tmp_path):
    """The whole point of asserting your data landed is the sentence saying what was missing.

    `docker exec` output is captured, and CalledProcessError's message is only the argv plus the exit
    status -- so a script ending `echo "expected 2000 reservations, found 0" >&2; exit 1` used to reach
    the operator as `returned non-zero exit status 1` and nothing else. The orchestrator puts `str(e)`
    into the run record, so the text has to be in the message, not only in a log line.
    """
    manager = ContainerManager()
    completed = subprocess.CompletedProcess(
        args=["docker", "exec", "widget-workspace-1", "bash", "/scripts/setup.sh"],
        returncode=1, stdout="", stderr="expected 2000 reservations, found 0\n",
    )
    with mock.patch("subprocess.run", return_value=completed):
        with pytest.raises(subprocess.CalledProcessError) as caught:
            manager.execute_command("widget-workspace-1", ["bash", "/scripts/setup.sh"])

    assert "expected 2000 reservations, found 0" in str(caught.value)
    # still a CalledProcessError, so existing handlers keep working
    assert caught.value.returncode == 1


def test_a_failed_build_teardown_does_not_abort_the_run():
    """docs/task_env_split.md commits to "logged, run continues" for a handover failure.

    A whole build phase is already paid for by this point, so throwing it away over a teardown hiccup
    buys nothing -- and the consequence is already covered elsewhere: a build env left standing gets
    adopted by `compose up`, which start() reports and the run record keeps. Source-order assertion,
    since driving the handover needs Docker, an agent container and a task env.
    """
    from appsci_bench.orchestrator import run as run_mod

    src = inspect.getsource(run_mod.BenchmarkOrchestrator.run)
    teardown = src.index("Tearing down the build task environment")
    eval_start = src.index("Starting eval task environment", teardown)
    between = src[teardown:eval_start]

    assert "try:" in between and "except Exception" in between, (
        "the handover's teardown must not be able to abort the run"
    )


def test_compose_failures_carry_their_own_output():
    """`compose up`'s build log and `compose down`'s reason were captured and dropped.

    Same defect as the setup-script one above, in the two calls that bracket the handover.
    """
    manager = ContainerManager()
    compose = Path(__file__)                       # any existing file; the run is patched out
    for call, argv_marker in (
        (lambda: manager.start(compose, "proj"), "up"),
        (lambda: manager.stop(compose, "proj"), "down"),
    ):
        completed = subprocess.CompletedProcess(
            args=["docker", "compose", argv_marker], returncode=1,
            stdout="", stderr="network bench-network has active endpoints\n",
        )
        with mock.patch("subprocess.run", return_value=completed):
            with pytest.raises(subprocess.CalledProcessError) as caught:
                call()
        assert "active endpoints" in str(caught.value), argv_marker
