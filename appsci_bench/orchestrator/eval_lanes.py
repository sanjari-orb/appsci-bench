"""Stand up the parallel (task-env + artifact) stacks the eval phase scores against.

A task that resets its environment before every eval case used to score its dataset one case at a
time, because there was one environment and a reset is a whole-store operation. The constraint is the
environment, not the benchmark: a case needs a CLEAN store, not the SAME one. So this module builds W
copies of the stack -- a task environment plus the artifact bound to it -- and hands them to the
generation loop as a `LanePool`, which lets one case occupy one copy for its whole life.

**Lane 0 is whatever the caller already brought up.** Its task environment and its artifact container
keep the names they have always had (`airline-workspace-1`, `appsci-artifact-airline_rebooking`), and
at width 1 this module starts and stops nothing at all. That is deliberate and load-bearing: the
eval-only CLI's PID lock, its orphan-detection decision table, container teardown and the tracer all
address the environment by that name, and a width-1 run must keep meaning exactly what it meant.

**Lanes 1..W-1 are new.** Each gets its own compose project (`<project>-l<n>`, so its own containers
and volumes), its own generated tool token, and its own artifact container run from the image lane 0
already built. Their per-case hooks are bound to their own workspace container, which is what makes
the reset a copy-local operation instead of a global one.

**Two silent failures this refuses rather than risks.** A task whose spec pins the environment address
literally would hand every copy's artifact copy 0's address, and they would all transact on one store
-- so a task that has not templated `agent_env` is refused above width 1 (`parallel_env_blockers`). An
artifact that hardcoded the address or token instead of reading it gets a 401 from the copy it was
handed, because each copy's token differs -- loud, and attributable, instead of a plausible wrong
number. And any lane that fails to start, deploy or answer aborts the whole eval: scoring on fewer
copies than were asked for produces a number nobody can compare.

**Cleanup, which is a sweep concern rather than a laptop one.** A run that unwinds normally -- including
one cancelled by SIGINT, which is what the queue worker sends first -- tears down every stack it
created in a `finally`. A run SIGKILLed at the end of its grace period runs none of that, and nothing
else on the host reaps it: the worker does no docker cleanup between cells. So each pool STARTS by
removing any lane stack left behind for its task (`reap_orphan_lanes`), which is safe to do
unconditionally because lanes above 0 are owned by exactly one eval phase and never shared.
"""
from __future__ import annotations

import contextlib
import logging
import re
import secrets
import subprocess
from pathlib import Path
from typing import Iterator

from ..eval.artifact_client import HttpArtifactClient
from ..eval.lanes import EvalLane, LanePool
from ..task_runner import PHASE_EVAL, TaskRunner
from ..task_runner.config import TaskConfig

logger = logging.getLogger(__name__)


class LaneSetupError(RuntimeError):
    """A lane could not be brought up, so the requested width cannot be served."""


def _lane_env_dict(task_env: dict | None, workspace_container: str, task_config: TaskConfig) -> dict:
    """The environment dict shape `evaluate_deployed`'s hook builders read, for one lane.

    Mirrors what `TaskRunner.start` returns. Built here for lanes this module started, and copied from
    the caller's dict for lane 0, so the hook builders cannot tell the two apart.
    """
    return {
        "workspace_container": workspace_container,
        "env_admin_token_var": task_config.env_admin_token_var,
        "env_tool_token_var": task_config.env_tool_token_var,
        "phase": (task_env or {}).get("phase", PHASE_EVAL),
    }


@contextlib.contextmanager
def eval_lane_pool(
    *,
    task_config: TaskConfig | None,
    tasks_dir: Path | None,
    width: int,
    endpoint_url: str,
    task_env: dict | None,
    artifact_dir: Path | None,
    make_lane,
    query_timeout_s: float | None = None,
) -> Iterator[LanePool]:
    """Yield a `LanePool` of `width` stacks, tearing down everything this call created.

    Args:
        task_config: The task's parsed `task_runner` block. Required only above width 1; at width 1
            nothing is started, so a caller that has no reason to load a spec need not load one.
        tasks_dir: Where task specs live, for the `TaskRunner` that starts extra environments.
            Required only above width 1.
        width: How many stacks to serve cases from. 1 starts nothing.
        endpoint_url: The host-reachable artifact URL for lane 0 -- already deployed by the caller.
        task_env: Lane 0's running eval environment dict, or None when the task declares it needs no
            environment.
        artifact_dir: The artifact SOURCE directory, needed only above width 1 (each extra lane runs
            its own container from it). Required there, because there is nothing to replicate without
            it.
        make_lane: `(client, env_dict) -> EvalLane` -- supplied by the caller so the per-case hook
            wiring stays in one place (`orchestrator/run.evaluate_deployed`) rather than being
            duplicated here with its own chance to drift.
        query_timeout_s: Per-turn ceiling for each extra lane's client, matching lane 0's.

    Raises:
        LaneSetupError: The task cannot serve this width, or a lane failed to come up. Never yields a
            narrower pool than asked for.
    """
    lane0_client = HttpArtifactClient(endpoint_url=endpoint_url, timeout=query_timeout_s)
    lanes = [make_lane(lane0_client, task_env, "lane-0")]

    if width <= 1:
        # Exactly what this code path did before lanes existed: one environment, so one case at a
        # time when there are per-case hooks, and unbounded concurrency on the single client when
        # there are not.
        has_hooks = lanes[0].setup_state is not None or lanes[0].read_final_state is not None
        yield LanePool.exclusive(lanes) if has_hooks else LanePool.shared(lanes[0])
        return

    if task_config is None or tasks_dir is None:
        raise LaneSetupError(
            f"--eval-lanes {width} needs the task's spec to start more environments, and this caller "
            "did not supply one."
        )
    _refuse_unless_replicable(task_config, task_env, artifact_dir, width)

    runner = TaskRunner(tasks_dir)
    # Before starting anything: remove any lane stack a previous run died without unwinding. See
    # `reap_orphan_lanes` -- a sweep SIGKILLs cells that overrun, and nothing else on the host reaps
    # what that leaves.
    reap_orphan_lanes(runner, task_config)
    started: list[dict] = []
    artifact_containers: list[str] = []
    try:
        for index in range(1, width):
            container, env_dict = _start_lane_env(
                runner, task_config, index, started,
            )
            endpoint, name = _deploy_lane_artifact(
                task_config, artifact_dir, index, container,
            )
            artifact_containers.append(name)
            client = HttpArtifactClient(endpoint_url=endpoint, timeout=query_timeout_s)
            lanes.append(make_lane(client, env_dict, f"lane-{index}"))
            logger.info("eval lane-%d ready: env=%s artifact=%s", index, container, name)
        logger.info(
            "Parallel eval: %d stacks (lane-0 env %s, plus %d more)",
            width, (task_env or {}).get("workspace_container", "none"), width - 1,
        )
        yield LanePool.exclusive(lanes)
    finally:
        _teardown(runner, started, artifact_containers)


def _refuse_unless_replicable(
    task_config: TaskConfig, task_env: dict | None, artifact_dir: Path | None, width: int,
) -> None:
    """Fail before anything starts if this task/run cannot honestly serve `width` stacks."""
    if task_env is None:
        raise LaneSetupError(
            f"--eval-lanes {width} was requested but {task_config.task_id} is running without a task "
            "environment, so there is nothing to give a case its own copy of. Drop the flag."
        )
    if artifact_dir is None:
        raise LaneSetupError(
            f"--eval-lanes {width} needs the artifact source directory to stand up the extra copies, "
            "and this caller did not provide one."
        )
    blockers = task_config.parallel_env_blockers()
    if blockers:
        raise LaneSetupError(
            f"--eval-lanes {width} was requested but {task_config.task_id} cannot run several copies "
            "of its environment:\n  - " + "\n  - ".join(blockers)
        )


def _existing_lane_indices(task_config: TaskConfig) -> list[int]:
    """Lane indices of this task that still have a workspace container on the host.

    Enumerated from docker rather than from the width being requested, so a run at width 2 also finds
    what a previous width-8 run left behind.
    """
    base = task_config.default_workspace_container.removesuffix("-workspace-1")
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}",
             "--filter", f"name=^{base}-l[0-9]+-workspace-1$"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:  # noqa: BLE001 -- a listing failure must not block the run
        logger.warning("Could not list existing eval lanes for %s: %s", task_config.task_id, e)
        return []
    if result.returncode != 0:
        return []
    found = []
    pattern = re.compile(rf"^{re.escape(base)}-l(\d+)-workspace-1$")
    for name in result.stdout.split():
        match = pattern.match(name)
        if match:
            found.append(int(match.group(1)))
    return sorted(found)


def reap_orphan_lanes(runner: TaskRunner, task_config: TaskConfig) -> list[int]:
    """Tear down every leftover lane stack for this task. Returns the lane indices removed.

    Lanes above 0 are wholly owned by one eval phase -- no operator command brings one up, nothing
    attaches to one, and the run that created it removes it on the way out. So a lane stack found
    BEFORE a run starts is unambiguously an orphan from a run that died without unwinding, and the
    only safe thing to do with it is remove it. That is a stronger claim than the eval-only CLI can
    make about lane 0, which an operator may legitimately have left running with `up`.

    Two failures this prevents, both of which a sweep hits rather than a laptop:

      * **Accumulation.** The queue worker SIGKILLs a cell that overruns its grace period, which runs
        none of the teardown, and the worker itself does no docker cleanup between cells. Without this
        every hard-killed cell would leave its extra environments running for the life of the box --
        and on banking each one is a workspace plus a confined shell sidecar.
      * **Adoption.** `docker compose up` under an existing project attaches to what is already there
        instead of creating it, so the next cell could silently inherit a dead run's store. The
        per-lane token usually forces a recreate, but "usually" is not a property to rest a score on.

    Best-effort per lane and logged rather than raised: failing to remove someone else's leftover is
    not a reason to refuse to run.
    """
    reaped = []
    for index in _existing_lane_indices(task_config):
        try:
            runner.stop(runner.teardown_descriptor(task_config.task_id, lane=index))
            reaped.append(index)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not remove orphaned eval lane-%d for %s: %s",
                           index, task_config.task_id, e)
        # The artifact container is not part of the compose project, so it needs removing separately.
        subprocess.run(["docker", "rm", "-f", f"{task_config.artifact_container}-l{index}"],
                       capture_output=True, timeout=30)
    if reaped:
        logger.warning(
            "Removed %d orphaned eval lane(s) for %s (%s) left by a run that did not shut down "
            "cleanly.", len(reaped), task_config.task_id,
            ", ".join(f"lane-{i}" for i in reaped),
        )
    return reaped


def _start_lane_env(
    runner: TaskRunner, task_config: TaskConfig, index: int, started: list[dict],
) -> tuple[str, dict]:
    """Bring up one extra copy of the eval environment and return `(container, env_dict)`.

    The teardown descriptor is recorded BEFORE the start, so a start that throws part-way still leaves
    something that can bring its containers down -- the same ordering the orchestrator uses at the
    build/eval handover.
    """
    started.append(runner.teardown_descriptor(task_config.task_id, lane=index))
    overlay = None
    if task_config.lane_token_var:
        # A distinct token per copy. This is the guard that turns "the artifact ignored the address we
        # gave it" from a contaminated store into a 401 on the copy it was actually handed.
        overlay = {task_config.lane_token_var: f"lane{index}-{secrets.token_urlsafe(16)}"}
    try:
        env = runner.start(task_config.task_id, phase=PHASE_EVAL, lane=index, env_overlay=overlay)
    except Exception as e:  # noqa: BLE001 -- turned into an abort with the lane named
        raise LaneSetupError(f"eval lane-{index}: task environment failed to start: {e}") from e
    if env is None:
        raise LaneSetupError(
            f"eval lane-{index}: {task_config.task_id} declares no eval environment"
        )
    if env.get("reused_existing_container"):
        raise LaneSetupError(
            f"eval lane-{index}: compose adopted an existing container ({env['workspace_container']}) "
            "instead of creating one, so this copy holds whatever a previous run left in it. Remove "
            "it and re-run."
        )
    return env["workspace_container"], _lane_env_dict(env, env["workspace_container"], task_config)


def _deploy_lane_artifact(
    task_config: TaskConfig, artifact_dir: Path, index: int, workspace_container: str,
) -> tuple[str, str]:
    """Run one more artifact container, pointed at this lane's environment. Returns (endpoint, name).

    Reuses the image lane 0 already built rather than rebuilding per lane: it is the same source
    directory, so a rebuild would produce an identical image, and two builds writing one tag is a race
    whose loser's container starts the wrong code.
    """
    from .run import deploy_artifact_dir  # local: run.py imports this module's caller chain

    name = f"{task_config.artifact_container}-l{index}"
    endpoint, _container, error = deploy_artifact_dir(
        task_config, artifact_dir,
        workspace_container=workspace_container,
        container_name=name,
        image_name=f"{task_config.artifact_container}:latest",
        build=False,
    )
    if error or not endpoint:
        raise LaneSetupError(f"eval lane-{index}: artifact deploy failed: {error}")
    return endpoint, name


def _teardown(runner: TaskRunner, started: list[dict], artifact_containers: list[str]) -> None:
    """Remove every container this call created, in artifact-then-environment order.

    Best-effort per item and logged rather than raised: a teardown failure must not replace the run's
    own outcome (a real score, or the lane failure that aborted it) with a docker error. Lane 0 is
    untouched -- this call never created it.
    """
    for name in artifact_containers:
        try:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            logger.info("Removed eval lane artifact container: %s", name)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to remove eval lane artifact container %s: %s", name, e)
    for env in started:
        try:
            runner.stop(env)
            logger.info("Stopped eval lane environment: %s", env["workspace_container"])
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to stop eval lane environment %s: %s",
                           env.get("workspace_container"), e)
