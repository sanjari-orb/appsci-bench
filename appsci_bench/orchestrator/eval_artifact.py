"""Standalone eval-only pipeline: re-score an already-built artifact directory.

This is **Caller B** of the shared deploy capability (see docs/artifact_design.md); the agent's
`deploy_artifact` tool -> `POST /deploy` route is Caller A. Both stand an artifact up with the SAME
host-side `DockerArtifactDeployer`, and both score with the SAME `evaluate_deployed` routine, so an
eval-only rerun reproduces the orchestrator's score *by construction* rather than by hope. (The old
`scripts/eval_built_artifact.py` prototype this replaces wrapped the endpoint in the lossy single-shot
`HttpArtifact` bridge, silently collapsing multi-turn tasks to one turn -- the exact fidelity trap
this module closes.)

The **task id is the key**. On its own an artifact directory is inert -- nothing in it says which task
it implements, how its container should be named/networked, or how it should be scored. Every one of
those facts is keyed by the task id and read from the task's `spec.yaml`:

    deploy contract   task_runner.artifact_container / artifact_port / network_name / agent_env
    eval config       the task's single evaluation_data block (type -> user-sim; reset_env_per_sample
                       + env_admin_token_var -> per-sample reset hooks)

So the pipeline takes the task id AND the artifact dir as required arguments, and the only runtime knobs
are how MANY cases to score and WHICH ones: `--limit` (a debug cap; omit it and the run scores the full
block, reproducing the orchestrator) and `--sample-seed`, which replaces the cap's first-N draw with a
seeded random one. There is still deliberately no OBJECTIVE selector -- scoring a subset of objectives
would silently diverge from the orchestrator's weighted score, which is a different and unrecoverable
kind of divergence. A case subset is safe by comparison only because it is recorded and reproducible: the
seed is stated on the command line, the report lists every case it scored, and the run fingerprint is
computed over the sampled cases, so two differently-sampled runs cannot be mistaken for each other. Note
that a seeded run is NOT comparable with an orchestrator run at the same limit; it is comparable with
other runs carrying the same limit and seed, which is what a checkpoint series needs.

Subcommands:
    eval --task <id> --artifact-dir <dir> [--attach] [--limit N] [--sample-seed S]
                                                                   deploy + score an artifact
    up   --phase {build,eval} --task <id>                          bring a task-env up, leave running
    down --task <id>                                               stop a task-env brought up with `up`

The expensive, repeated part is the **task-env**, not the deploy -- so `up` once, then repeated
`eval ... --attach`, then `down` is the pattern for scoring one artifact many times. Default `eval`
(no `--attach`) is always-correct and self-cleaning: it brings its own env up and tears everything
down in a `finally`.

Usage:
    python -m appsci_bench.orchestrator.eval_artifact eval --task order_to_cash --artifact-dir runs/<trace>/artifact
    python -m appsci_bench.orchestrator.eval_artifact up   --phase eval --task order_to_cash
    python -m appsci_bench.orchestrator.eval_artifact eval --task order_to_cash --artifact-dir <dir> --attach --limit 2
    python -m appsci_bench.orchestrator.eval_artifact down --task order_to_cash
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from ..core.spec import load_task_spec
from ..task_runner import PHASE_BUILD, PHASE_EVAL, PHASES, TaskRunner
from ..task_runner.config import TaskConfig
from .run import (
    _cleanup_artifact_containers,
    deploy_artifact_dir,
    evaluate_deployed,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("eval_artifact")


# -- persisted task-env state (so `up` / `eval --attach` / `down` share one running env) -----------
#
# `up` writes the env dict TaskRunner.start() returns to a per-task JSON file; `eval --attach` reads
# it to reuse the hot env; `down` reads it to stop the env and then deletes the file. A machine-stable
# temp location keyed by task id keeps this out of the repo working tree (this branch tracks leakage
# surfaces) while staying deterministic across separate CLI invocations.
_STATE_DIR = Path(tempfile.gettempdir()) / "appsci-task-env"


def _state_path(task_id: str) -> Path:
    return _STATE_DIR / f"{task_id}.json"


def _write_env_state(task_id: str, env: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(task_id).write_text(json.dumps(env, indent=2))


def _read_env_state(task_id: str) -> dict | None:
    path = _state_path(task_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _clear_env_state(task_id: str) -> None:
    _state_path(task_id).unlink(missing_ok=True)


def _spec_path(task_id: str, tasks_dir: Path) -> Path:
    """Resolve a task's spec path under `tasks_dir`, honoring a non-default --tasks-dir."""
    return tasks_dir / task_id / "spec.yaml"


# -- env ownership -------------------------------------------------------------------------------
#
# The task-env is named GLOBALLY per task: TaskRunner derives one compose project from
# `task_runner.workspace_name` ("airline-workspace" -> project "airline" -> container
# "airline-workspace-1"), and the artifact lands under the spec-declared `artifact_container`. So
# every caller of the same task -- a second `eval`, an `up`, or a full orchestrator run -- competes
# for the SAME containers.
#
# That matters because `runner.start()` is `docker compose up -d --build`, which SILENTLY ATTACHES to
# an already-running project instead of failing. Left unchecked, `eval` marks an env it did not create
# as its own and destroys it in `finally` -- pulling containers out from under whoever did own it. The
# victim sees exactly the teardown order of that finally block: artifact removed first (its next query
# gets "connection closed" then "refused"), then `compose down` removes the workspace container (every
# later case fails its per-sample reset with "No such container", scored 0.0).
#
# The discriminator is NOT "is a container running" -- a leftover container from a crashed run is an
# ORPHAN that nobody owns, and refusing on it would break the documented self-contained command. What
# matters is whether a LIVE run owns the env, which we track with a PID lockfile:
#
#   live lock held by another pid   -> refuse (rc 2); that run is mid-scoring.
#   stale lock (pid dead)           -> recover: drop the lock and continue.
#   env up + `up` state file        -> deliberately kept by the operator -> tell them to use --attach.
#   env up, no lock, no state file  -> ORPHAN from a crashed run -> tear it down, start fresh.


def _workspace_container_for(runner: TaskRunner, config: TaskConfig) -> str:
    """Name of the task-env workspace container, derived exactly as TaskRunner.start() derives it.

    Reuses the runner's own derivation rather than re-deriving it here, so the preflight check can
    never drift from the name that `start()`/`stop()` actually act on.
    """
    project = runner._derive_project_name(config.workspace_name)
    return f"{project}-workspace-1"


def _env_already_running(runner: TaskRunner, config: TaskConfig) -> str | None:
    """Return the workspace container name if a task-env for this task already exists, else None."""
    container = _workspace_container_for(runner, config)
    return container if runner._container_manager.exists(container) else None


# -- run lock (which live process owns this task's env) --------------------------------------------


def _lock_path(task_id: str) -> Path:
    return _STATE_DIR / f"{task_id}.eval.lock"


def _live_lock_holder(task_id: str) -> int | None:
    """PID of a still-running `eval` that holds this task, or None (no lock / stale lock).

    A stale lockfile (process died without releasing) is deleted here, so a crashed run can never
    permanently block the next one.
    """
    path = _lock_path(task_id)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except (ValueError, OSError):
        path.unlink(missing_ok=True)      # unreadable/corrupt -> treat as stale
        return None
    try:
        os.kill(pid, 0)                   # signal 0: liveness probe, does not touch the process
    except ProcessLookupError:
        log.info("Clearing stale eval lock for %s (pid %d is gone).", task_id, pid)
        path.unlink(missing_ok=True)
        return None
    except PermissionError:
        return pid                        # alive but owned by another user
    return pid


def _acquire_lock(task_id: str) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _lock_path(task_id).write_text(str(os.getpid()))


def _release_lock(task_id: str) -> None:
    _lock_path(task_id).unlink(missing_ok=True)



# -- infrastructure-failure detection ------------------------------------------------------------
#
# A unit that never reached the artifact (dead task-env, unreachable/killed artifact, bad model
# credentials) is an INFRASTRUCTURE failure, not evidence about artifact quality -- but the evaluator
# grades it 0.0 like any other miss, so a broken harness silently yields a plausible-looking low
# score. That is the worst possible failure mode for a benchmark: it reads as "the artifact is bad".
# We classify those errors and refuse to present the run as a valid score.
_INFRA_ERROR_MARKERS = (
    "No such container",              # task-env removed mid-run
    "env admin exec failed",          # per-sample reset couldn't reach the task-env
    "Connection refused",             # artifact container gone
    "Remote end closed connection",   # artifact killed mid-request
    "AuthenticationError",            # model creds missing/expired (user-sim or judge)
    "invalid or expired token",
    "Max retries exceeded",
    "Failed to establish a new connection",
)


def _infra_failures(report) -> dict[str, int]:
    """Count per-case infrastructure errors on a report, keyed by marker (most specific first)."""
    counts: dict[str, int] = {}
    for row in getattr(report, "results", []) or []:
        error = getattr(row, "error", None)
        if error is None and isinstance(row, dict):
            error = row.get("error")
        if not error:
            continue
        for marker in _INFRA_ERROR_MARKERS:
            if marker in str(error):
                counts[marker] = counts.get(marker, 0) + 1
                break
    return counts


def _print_infra_failure(counts: dict[str, int], total: int) -> None:
    """Explain that the run is invalid (harness broke) instead of letting a 0.0 read as a score."""
    print("\n" + "!" * 64)
    print("INVALID RUN -- NOT A SCORE")
    print("!" * 64)
    print(
        f"{sum(counts.values())} of {total} scored cases failed for INFRASTRUCTURE reasons, so the\n"
        "weighted score printed above measures the harness, not the artifact. Breakdown:"
    )
    for marker, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}x  {marker}")
    print("-" * 64)
    print("Common causes:")
    print("  - another run of this task tore the shared task-env down mid-scoring")
    print("  - model credentials missing/expired -> re-source the setup steps in README.md")
    print("  - the artifact crashed or was killed mid-request (check its container logs)")
    print("!" * 64)


# -- report printing (same shape the prototype produced) -------------------------------------------


def _print_report(report, eval_dir: Path | None = None) -> None:
    print("\n" + "=" * 64)
    print(f"TASK: {report.task_id}")
    print(f"FINAL SCORE: {report.final_score}")
    print("-" * 64)
    print(f"{'OBJECTIVE':<24}{'SCORE':<12}{'WEIGHT':<10}POSITIVE")
    for row in report.views.get("per_metric", []):
        print(
            f"{str(row['objective']):<24}{str(row['score']):<12}"
            f"{str(row['weight']):<10}{row['positive']}"
        )
    if report.warnings:
        print("-" * 64)
        print("WARNINGS:")
        for w in report.warnings:
            print("  -", w)
    print("=" * 64)
    # Point at the durable location the evaluator wrote straight into (next to the artifact) rather
    # than a tempfile store -- report.html is a self-contained page you can open in a browser, and
    # conversations/ holds the per-case transcript for every scored objective (open these to see WHY
    # a score is what it is -- e.g. why a 0.0 happened).
    if eval_dir is not None:
        print(f"Full report (open in a browser):  {eval_dir / 'report.html'}")
        print(f"Per-case transcripts:             {eval_dir / 'conversations'}/")
    else:
        print(f"(transcripts: {report.store_root})")


# -- subcommands -----------------------------------------------------------------------------------


def _cmd_up(args: argparse.Namespace) -> int:
    tasks_dir = Path(args.tasks_dir)
    if _read_env_state(args.task) is not None:
        log.warning(
            "A task-env state file for %s already exists (%s). Bringing up a new env will "
            "overwrite it; run `down %s` first if an env is still running.",
            args.task, _state_path(args.task), args.task,
        )
    runner = TaskRunner(tasks_dir)
    # Whichever phase the caller asked for. `up` serves two workflows with opposite needs -- a human
    # baseline authoring against the build share, and an operator preparing to re-score against the
    # full store -- so the phase is required rather than defaulted (docs/task_env_split.md).
    log.info("Starting %s task environment for %s ...", args.phase, args.task)
    env = runner.start(args.task, phase=args.phase)
    if env is None:
        log.info(
            "Task %s declares no %s environment; there is nothing to bring up. Score directly "
            "with: eval --task %s --artifact-dir <artifact_dir>", args.task, args.phase, args.task,
        )
        return 0
    _write_env_state(args.task, env)
    log.info(
        "Task env ready: %s (phase=%s, network=%s).", env["workspace_container"], args.phase,
        env["network_name"],
    )
    if args.phase == PHASE_EVAL:
        log.info(
            "Re-score with: eval --task %s --artifact-dir <artifact_dir> --attach", args.task,
        )
    else:
        log.warning(
            "This is the BUILD environment -- it holds only the records no eval case touches. Do NOT "
            "score against it; `eval --attach` would measure the artifact on the wrong store. Bring "
            "it down and re-run with --phase eval when you are ready to score."
        )
    log.info("Stop it when done with: down --task %s", args.task)
    return 0


def _cmd_down(args: argparse.Namespace) -> int:
    tasks_dir = Path(args.tasks_dir)
    env = _read_env_state(args.task)
    if env is None:
        log.warning(
            "No running task-env recorded for %s (no state file at %s); nothing to stop.",
            args.task, _state_path(args.task),
        )
        return 0
    runner = TaskRunner(tasks_dir)
    log.info("Stopping task environment for %s ...", args.task)
    try:
        runner.stop(env)
    finally:
        _clear_env_state(args.task)
    log.info("Task env for %s stopped.", args.task)
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    tasks_dir = Path(args.tasks_dir)
    artifact_dir = Path(args.artifact_dir).resolve()
    if not artifact_dir.exists():
        log.error("Artifact directory not found: %s", artifact_dir)
        return 1

    # A seed only chooses WHICH cases a cap keeps, so on its own it does nothing. Refuse rather than
    # ignore: the cost of being wrong here is a scoring that looks seeded, is recorded as seeded by
    # whoever launched it, and actually scored the full block -- which is a silently non-comparable
    # point on a curve. Cheap to state, expensive to discover later.
    if args.sample_seed is not None and args.limit is None:
        log.error(
            "--sample-seed %d was given without --limit, which would do nothing: the seed decides "
            "WHICH cases a cap keeps, not how many. Pass --limit N as well, or drop the seed to score "
            "the full block.", args.sample_seed,
        )
        return 2

    # task id -> ONE spec -> both the deploy contract and the eval config.
    spec = load_task_spec(_spec_path(args.task, tasks_dir))
    config = TaskConfig.from_spec(args.task, tasks_dir)

    runner = TaskRunner(tasks_dir)

    # Mutual exclusion: two runs of the same task share its container names, so a second run's
    # teardown would destroy this one's env mid-scoring. Refuse while a LIVE run holds the task; a
    # stale lock from a crashed run is cleared automatically by _live_lock_holder.
    holder = _live_lock_holder(args.task)
    if holder is not None:
        log.error(
            "Another eval run (pid %d) is already scoring %s.\n"
            "  Refusing to start: both runs would share this task's containers, and whichever "
            "finished first would tear the env down under the other.\n"
            "  Wait for it to finish, or kill pid %d if it is hung.",
            holder, args.task, holder,
        )
        return 2

    # We only tear the task-env down if we brought it up. Under --attach we reuse (and leave running)
    # an env a prior `up` started.
    started_env_here = False
    task_env: dict | None = None
    deployed_container: str | None = None
    # Whether we got as far as invoking the deployer -- gates artifact-container teardown so we never
    # remove an artifact this run did not deploy (see the `finally` block).
    deploy_attempted = False
    _acquire_lock(args.task)
    try:
        if args.attach:
            task_env = _read_env_state(args.task)
            if task_env is None:
                log.error(
                    "--attach given but no running task-env for %s was found (no state file at "
                    "%s). Run `up --phase eval --task %s` first, or omit --attach to bring one up "
                    "for this run.",
                    args.task, _state_path(args.task), args.task,
                )
                return 1
            # Refuse to score against the BUILD environment. It holds only the records no eval case
            # touches, so every case would be measured against a store its data is missing from --
            # producing a plausible, entirely wrong number rather than an error. The other half of the
            # same mistake (standing up `eval` to author against) is guarded by `up` requiring a phase.
            attached_phase = task_env.get("phase")
            if attached_phase == PHASE_BUILD:
                log.error(
                    "The running task-env for %s is the BUILD environment, which holds only the "
                    "records no eval case touches. Scoring against it would measure the artifact on "
                    "the wrong store. Bring it down (`down --task %s`) and start the eval one "
                    "(`up --phase eval --task %s`).",
                    args.task, args.task, args.task,
                )
                return 2
            log.info(
                "Attached to running task env: %s (phase=%s, network=%s)",
                task_env["workspace_container"], attached_phase or "unknown", task_env["network_name"],
            )
        else:
            # Preflight: only refuse for an env a LIVE run owns; recover an orphan so the documented
            # self-contained command keeps working after a crashed/interrupted run.
            existing = _env_already_running(runner, config)
            if existing is not None:
                if _read_env_state(args.task) is not None:
                    log.error(
                        "A task environment for %s was brought up with `up` (%s) and is still "
                        "running.\n"
                        "  Refusing to start: this command tears the env down when it finishes, which "
                        "would destroy an env you asked to keep.\n"
                        "  To score against it -> re-run with --attach (leaves it running).\n"
                        "  To discard it       -> `down --task %s` first, then re-run.",
                        args.task, existing, args.task,
                    )
                    return 2
                # No live lock (checked above) and no `up` state file => nobody owns it. It is a
                # leftover from a crashed run: remove it so we start from a known-good env rather
                # than silently inheriting whatever state it was left in.
                log.warning(
                    "Found an ORPHANED task-env for %s (%s) with no owning run -- removing it and "
                    "starting fresh.", args.task, existing,
                )
                try:
                    runner.stop(runner.teardown_descriptor(args.task))
                except Exception as e:  # noqa: BLE001 -- fall through; start() will surface any issue
                    log.warning("Could not remove orphaned env (%s); continuing.", e)
            log.info("Starting eval task environment for %s ...", args.task)
            task_env = runner.start(args.task, phase=PHASE_EVAL)
            started_env_here = task_env is not None
            if task_env is None:
                log.info("Task %s declares no eval environment; scoring without one.", args.task)
                # Nothing came up, so nothing created the network -- and the deploy below still runs
                # the artifact with `--network <config.network_name>`, which docker refuses when it is
                # missing. It survives today only because every task's compose file declares the
                # network `external: true`, so `compose down` never removes it and some earlier run's
                # copy is usually still there. On a fresh host, or after `docker network prune`,
                # `eval --task slides_generator` failed with nothing pointing at the cause.
                runner.ensure_network(args.task)
            else:
                log.info(
                    "Task env ready: %s (network=%s)",
                    task_env["workspace_container"], task_env["network_name"],
                )

        # Deploy with the SAME host-side deployer the agent's /deploy route uses, through the SAME
        # helper the orchestrator's own eval-only path uses: spec-declared container name / port /
        # network, injecting the task's agent_env (task-API URLs/tokens the artifact needs) plus the
        # LLM/proxy credentials the artifact (itself an agent) needs to reach its model provider. One
        # helper rather than a second copy of the same constructor call, so "a rerun stands the
        # artifact up the same way the orchestrator does" stays true by construction rather than by
        # two call sites being kept in step by hand.
        log.info(
            "Deploying artifact from %s as %s:%d on %s ...",
            artifact_dir, config.artifact_container, config.artifact_port, config.network_name,
        )
        deploy_attempted = True  # set BEFORE the call: a mid-deploy raise may still leave a container
        host_endpoint, deployed_container, deploy_error = deploy_artifact_dir(config, artifact_dir)
        if deploy_error or not host_endpoint:
            # Deployment is "not the thing under test": surface exactly which step broke.
            log.error("Deploy failed: %s", deploy_error)
            return 1

        # `deploy_artifact_dir` has already translated the container-DNS URL the deployer returns into
        # the localhost URL the host-vantage evaluator can reach (via `docker port`).
        log.info("Artifact deployed; scoring at %s ...", host_endpoint)

        # Write the report + transcripts straight into a durable dir next to the artifact instead of a
        # throwaway /tmp store: for a run's artifact/, that's runs/<trace>/eval/ -- exactly where the
        # tracer looks for a native Eval tab. The evaluator's own "report -> ..." log line then points
        # at this real location, not /var/folders/...
        eval_dir = artifact_dir.parent / "eval"

        # Score with the SAME routine the orchestrator uses -> identical eval sequence by construction.
        # Above one lane it also stands up the extra (task-env, artifact) copies and tears them down;
        # at the default of one it starts nothing, which is why `--attach` and the lock above keep
        # meaning exactly what they did.
        report = evaluate_deployed(spec, host_endpoint, task_env, limit=args.limit,
                                   sample_seed=args.sample_seed, workdir=str(eval_dir),
                                   eval_lanes=args.lanes, task_config=config,
                                   artifact_dir=artifact_dir, tasks_dir=tasks_dir)
        _print_report(report, eval_dir)

        # A run whose cases died on infrastructure (dead env, killed artifact, bad creds) must NOT be
        # reported as a score -- the evaluator grades those units 0.0, so the weighted number measures
        # the harness. Say so loudly and exit non-zero so scripted callers can't mistake it for a
        # result. The report is still on disk for diagnosis.
        infra = _infra_failures(report)
        if infra:
            _print_infra_failure(infra, len(getattr(report, "results", []) or []))
            return 1
        return 0
    finally:
        # Teardown always runs -- but only for what THIS run actually created. Same ownership rule as
        # the task-env above: the artifact container name is a global per-task constant, so removing it
        # unconditionally would kill a concurrent run's artifact (e.g. on the refuse-to-start path,
        # where we never deployed anything). `deploy_attempted` still covers a partial deploy, where
        # the container exists but `deployed_container` was never set because health-check failed.
        if deploy_attempted:
            _cleanup_artifact_containers(deployed_container or config.artifact_container, None, None)
        # Stop the task-env only if we started it. Under --attach it stays up for the next rerun.
        if started_env_here and task_env is not None:
            try:
                runner.stop(task_env)
                log.info("Task env for %s stopped.", args.task)
            except Exception as e:  # noqa: BLE001
                log.warning("Task env stop failed: %s", e)
        elif args.attach:
            log.info("Left task env for %s running (--attach). Stop it with: down --task %s",
                     args.task, args.task)
        _release_lock(args.task)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_artifact",
        description="Deploy + score an already-built artifact against a task's real eval config.",
    )
    parser.add_argument(
        "--tasks-dir", default="tasks", help="Path to the tasks directory (default: tasks)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_eval = sub.add_parser("eval", help="deploy an artifact dir and score it")
    p_eval.add_argument(
        "--task", required=True,
        help="Task ID (e.g. airline_rebooking) -- resolves the deploy + eval config",
    )
    p_eval.add_argument(
        "--artifact-dir", required=True,
        help="Artifact directory to deploy (e.g. runs/<trace>/artifact)",
    )
    p_eval.add_argument(
        "--attach", action="store_true",
        help="Reuse a task-env already brought up with `up` (do not start/stop it here).",
    )
    p_eval.add_argument(
        # Two spellings of the same knob: --limit is the original (and matches the orchestrator's
        # eval_limit); --num-samples reads better for "just score a few". argparse takes `dest` from
        # the first long option, so both land in args.limit and nothing downstream changes.
        "--limit", "--num-samples", type=int, default=None, metavar="N",
        help=(
            "Score only the first N eval samples (debug; e.g. --num-samples 5). "
            "Omit to score every sample and reproduce the orchestrator's full score."
        ),
    )
    p_eval.add_argument(
        "--sample-seed", type=int, default=None, metavar="S",
        help=(
            "With --limit, choose WHICH samples by seeded random draw instead of taking the first N. "
            "Eval data is stored in id order and that order carries structure -- on order_to_cash the "
            "first 20 of 60 over-represent one fault type fourfold and omit another entirely -- so a "
            "first-N cap can hide an improvement in the categories it never exercises. The seed must be "
            "held FIXED across every artifact being compared, or they are not measured on the same cases."
        ),
    )
    p_eval.add_argument(
        "--lanes", type=int, default=1, metavar="N",
        help=(
            "Score against N copies of the task environment, each with its own artifact container, so "
            "N cases run at once (default 1). Only does anything for a task that resets its "
            "environment per case: with one environment those run strictly one case at a time, "
            "because a reset is a whole-store operation. Copy 0 is the environment this command "
            "already uses (including under --attach); the other N-1 are started here and torn down "
            "when it finishes. Recorded on the report -- scores are strictly comparable only at equal "
            "N."
        ),
    )
    p_eval.set_defaults(func=_cmd_eval)

    p_up = sub.add_parser(
        "up",
        help="bring a task-env up and leave it running (--phase is required)",
        description=(
            "Bring one PHASE of a task environment up and leave it running. --phase is mandatory "
            "because the right answer depends on why you are running it, and both mistakes are "
            "silent: `build` holds only the records no eval case touches and is what a human "
            "baseline attempt authors against; `eval` holds every record and is what an artifact is "
            "scored against. Standing up `eval` to build against leaks the scored data; standing up "
            "`build` to score against measures the artifact on the wrong store."
        ),
    )
    p_up.add_argument("--task", required=True, help="Task ID (e.g. airline_rebooking)")
    p_up.add_argument(
        "--phase", required=True, choices=list(PHASES),
        help="build = author/explore against it (human baseline); eval = score an artifact against it",
    )
    p_up.set_defaults(func=_cmd_up)

    p_down = sub.add_parser("down", help="stop a task-env brought up with `up`")
    p_down.add_argument("--task", required=True, help="Task ID (e.g. airline_rebooking)")
    p_down.set_defaults(func=_cmd_down)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
