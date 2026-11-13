"""BenchmarkOrchestrator: coordinates a full benchmark run for a task.

This module provides the main orchestrator that coordinates:
1. Starting the submission server for agent artifact submissions
2. Starting the task environment (workspace container, DB, etc.)
3. Starting the appsci agent user sim container
4. Starting the appsci-agent container
5. Waiting for the agent to complete and submit its artifact
6. Stopping the appsci agent user sim (agent is done talking to the user)
7. Running the evaluator on the submitted artifact
8. Cleaning up all containers and observability publishers

If observability is enabled, it also starts a tracer/aggregator server up front (retrying
on later ports if the default is occupied) and, around step 7, registers a global
EVALUATOR-source publisher and persists the eval report next to the run's other data --
see appsci_bench/tracer/ and docs/benchmark-orchestrator-design.md for the full picture.

Example usage:
    orchestrator = BenchmarkOrchestrator(tasks_dir=Path("tasks"))
    result = await orchestrator.run(
        task_id="order_to_cash",
        persona_path=Path("tasks/order_to_cash/user_personas/l3.yaml"),
    )
    print(result.scores)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.agent_container import (
    AppSciAgentContainerManager,
    AppSciAgentContainerConfig,
    fireworks_routing_error,
)
from appsci_bench.eval.artifact import HttpArtifact
from appsci_bench.eval.datatypes import _expected
from ..eval.build_optimization import read_optimization
from ..eval.redteam_trace import assemble_trace_dir
from ..core.spec import TaskSpec, load_task_spec, resolve_spec_path, write_agent_spec
from ..core.harness import HarnessConfig
from ..core.harness_inputs import write_harness_inputs
from ..core.usersim_docs import (
    POLICIES_DIRNAME,
    DocsValidation,
    persona_policies_dir,
    validate_policies_dir,
    validate_usersim_docs,
)
from ..core.usersim_docs import usersim_docs_dir as _core_usersim_docs_dir
from ..core.appsci_agent_usersim import DockerAppsciAgentUserSimService
from ..eval.artifact_client import HttpArtifactClient
from ..eval.build_transcript import BuildTranscript, no_stakeholder_transcript
from ..eval.lanes import EvalLane
from ..eval.store import TranscriptStore
from ..eval.evaluator import Evaluator
from shared.observability.events import EventType, Source
from shared.observability.publisher import EventPublisher
from shared.observability.s3_store import (
    DEFAULT_BUCKET as DEFAULT_S3_BUCKET,
    DEFAULT_PREFIX as DEFAULT_S3_PREFIX,
    S3Config,
)
from ..eval.artifact_agent_user_sim import ArtifactAgentUserSim
from ..task_runner import PHASE_BUILD, PHASE_EVAL, TaskRunner
from ..task_runner.config import TaskConfig
from . import checkpoint as artifact_checkpoint
from .deployer import (
    DockerArtifactDeployer, LLM_CREDENTIAL_ENV_VARS, SERVER_ENTRY_CANDIDATES, llm_credential_env,
)
from .eval_lanes import eval_lane_pool
from .submission_server import ArtifactSubmissionServer, DeployContext

logger = logging.getLogger(__name__)

#: Repo root, for invoking scripts/score_redteam_trace.py as a subprocess (see
#: _maybe_build_redteam_trace) -- three parents up from appsci_bench/orchestrator/run.py.
REPO_ROOT = Path(__file__).resolve().parents[2]


#: Characters a run id may contain, mirroring `shared.observability.validate_run_id`'s rule. Every
#: run id becomes a DIRECTORY NAME under runs/, an S3 key component, and -- in the tracer -- a value
#: interpolated into JS template literals, so anything outside this set is replaced rather than
#: passed through. A model id in particular arrives as `anthropic/claude-opus-4-8`, whose slash would
#: silently create a nested directory.
_RUN_NAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

#: Per-component and whole-name length caps. A run id is ONE path component, and most filesystems cap
#: that at 255 bytes -- `validate_run_id` bounds the character set and says nothing about length, so
#: without these a long model id or persona name produces an id that validates and then cannot be
#: created. The whole-name cap trims from the FRONT so the timestamp, which is what makes the name
#: unique, always survives.
_MAX_COMPONENT_CHARS = 48
_MAX_NAME_CHARS = 200


def _slug(value: str, *, keep_last_segment: bool = False) -> str:
    """One path-safe, length-capped component of a run name, or "" when there is nothing usable.

    `keep_last_segment` takes only the final `/`-separated piece, which is what makes a model id
    readable: `anthropic/claude-opus-4-8` reads as `claude-opus-4-8` rather than
    `anthropic-claude-opus-4-8`, and the provider prefix is recorded on the manifest anyway.

    THE LENGTH CAP IS NOT COSMETIC. `validate_run_id` bounds the character SET, not the length, so a
    400-character component sails through it and then fails at `mkdir` with `ENAMETOOLONG` -- most
    filesystems cap a single path component at 255 bytes. That failure arrives after the run has
    started, which is the worst place for it.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if keep_last_segment:
        text = text.rsplit("/", 1)[-1]
    return _RUN_NAME_SAFE.sub("-", text).strip("-._")[:_MAX_COMPONENT_CHARS].strip("-._")


#: What the NO-USER arm puts in a run name's persona slot. That arm mounts the whole user_sim_docs/
#: tree rather than one persona's share, so the bound persona decides nothing and a run named `l3`
#: would read as a persona-level result it is not. See `BenchmarkOrchestrator.run`.
NO_USER_RUN_SLUG = "nouser"


def compose_run_name(
    task_id: str,
    *,
    agent_model: str | None = None,
    persona_path: str | None = None,
    label: str | None = None,
    now: datetime | None = None,
) -> str:
    """The run id for a new run: `<task>_<model>_<label>_<persona>_<timestamp>`.

    Was `<task>_<timestamp>`, which made a directory listing of a replicate sweep unreadable: fifteen
    runs per arm distinguished only by the second they started, so telling b0's second trial from b2's
    first meant opening both. Every part except the timestamp is dropped when it is unknown, so a
    plain `python -m appsci_bench.orchestrator.run --task X` still yields a short name.

    `label` is the piece only the caller can know -- the replicate coordinates (`b0`, `b0.t1`) held by
    the experiment runner, which the orchestrator has no concept of. Passed in rather than inferred.

    The timestamp stays LAST and stays second-resolution. Last because everything before it is what a
    reader scans for and a leading timestamp buries it; second-resolution because two cells of one
    sweep are strictly serial, so a collision needs two runs of the same task, model, label and
    persona to start within the same second.

    A COLLISION IS NOT GUARDED, and the earlier claim here that a task-env lock would surface it was
    simply wrong: `BenchmarkOrchestrator.run()` takes no lock (`docs/concurrency.md` section 6 says so
    explicitly). The real symptom, recorded in that doc's section 5, is two runs silently sharing one
    trace directory and merging into one trace. Second resolution is therefore a bet on the serial
    worker, not a protection -- if concurrent runs on one host ever become possible, this needs a
    uniqueness suffix rather than a finer clock.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    parts = [
        _slug(task_id),
        _slug(agent_model, keep_last_segment=True),
        _slug(label),
        _slug(Path(str(persona_path)).stem) if persona_path else "",
    ]
    name = "_".join([p for p in parts if p] + [stamp])
    if len(name) > _MAX_NAME_CHARS:
        # Trim from the front: the timestamp is the part that makes this unique, so it is the part
        # that must not be lost. `lstrip` of the separator keeps the result from starting with "_".
        name = name[len(name) - _MAX_NAME_CHARS:].lstrip("_-.")
    return name or stamp


def _docker_container_name_from_url(url: str) -> str | None:
    """Extract the Docker container-DNS name from a container URL, if present.

    The deployed artifact endpoint is a container-DNS URL (e.g.
    http://<artifact-container>:8080/respond) whose host is the container's own
    name, so any such URL yields a real container name to recover here. A
    localhost/loopback host has no container name and returns None.

    Returns:
        The container name, or None if the URL doesn't match the pattern or names
        a non-container host (localhost, an IP, host.docker.internal).
    """
    match = re.match(r"^(https?)://([a-zA-Z0-9_-]+):(\d+)(.*)$", url)
    if not match:
        return None
    _scheme, container_name, _port, _path = match.groups()
    if container_name in ("localhost", "127.0.0.1", "host.docker.internal"):
        return None
    return container_name


def _docker_container_names_on_network(network_name: str) -> set[str]:
    """List names of containers currently attached to a Docker network.

    Used to snapshot the network before/after a run so any container that
    appeared during it -- and is still around once our own managed containers
    (agent, usersim, task-env) have been stopped -- can be treated as an
    orphaned artifact deployment, even when the agent never called
    complete (crash/timeout) and so never reported its name directly.
    Returns an empty set (logged) if the query fails, e.g. network doesn't
    exist yet.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"network={network_name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(
                f"Failed to list containers on network {network_name}: {result.stderr.strip()}"
            )
            return set()
        return {name for name in result.stdout.strip().split("\n") if name}
    except Exception as e:
        logger.warning(f"Failed to list containers on network {network_name}: {e}")
        return set()


def _cleanup_artifact_containers(
    artifact_container_name: str | None,
    artifact_network_baseline: set[str] | None,
    network_name: str | None,
) -> None:
    """Remove the agent-deployed artifact container(s) in `run()`'s `finally:`.

    Two passes, extracted from the inline `finally:` block so this wiring is
    unit-testable without spinning up the whole orchestrator:
    1. By known name, if the agent successfully called complete.
    2. Backstop: anything else new on `network_name` since the agent started
       (baseline captured before `agent_svc.start()`) -- catches the case where
       the agent deployed a container but crashed/timed out before submitting,
       so `artifact_container_name` was never learned. Only runs when a
       baseline was actually captured (i.e. task_env was set up).
    """
    if artifact_container_name is not None:
        try:
            subprocess.run(
                ["docker", "rm", "-f", artifact_container_name],
                capture_output=True,
                timeout=10,
            )
            logger.info(f"Removed artifact container: {artifact_container_name}")
        except Exception as e:
            logger.warning(
                f"Failed to remove artifact container {artifact_container_name}: {e}"
            )

    if artifact_network_baseline is not None and network_name is not None:
        leaked = (
            _docker_container_names_on_network(network_name)
            - artifact_network_baseline
            - {artifact_container_name}
        )
        for name in leaked:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    capture_output=True,
                    timeout=10,
                )
                logger.warning(f"Removed orphaned artifact container (backstop): {name}")
            except Exception as e:
                logger.warning(f"Failed to remove orphaned container {name}: {e}")


def _view_port_for_log(aggregator_server: object | None, actual_port: int, aggregator_port: int) -> int:
    """Which port to print in the end-of-run 'View with: ...' hint.

    `actual_port` is only meaningful if the tracer actually started on it (the
    retry loop may have landed on a port other than `aggregator_port`) -- if
    observability failed to start entirely, fall back to the originally
    configured port rather than referencing a value that was never bound.
    """
    return actual_port if aggregator_server is not None else aggregator_port


def _register_eval_publisher(runs_dir: Path, trace_id: str, task_id: str) -> None:
    """Register the global EVALUATOR publisher before running the evaluator.

    A *global* singleton (not instance state on the orchestrator) because the
    evaluator's own emit() calls (eval.start / eval.case_score / eval.complete / eval.elicitation)
    are made deep inside eval/scoring code that has no reference to this run's
    publisher -- registering it globally is what lets those calls land in
    THIS run's trace and appear in the tracer timeline right after the build
    events, without threading a publisher through the whole eval call chain.
    """
    from shared.observability.publisher import init_publisher

    init_publisher(
        runs_dir=str(runs_dir.resolve()),
        trace_id=trace_id,
        source=Source.EVALUATOR,
        task_id=task_id,
        name="evaluator",
    )


def _close_eval_publisher() -> None:
    """Close the global EVALUATOR publisher registered by _register_eval_publisher, if any."""
    from shared.observability.publisher import get_publisher

    eval_pub = get_publisher()
    if eval_pub is not None:
        eval_pub.close()


def _mark_submitted_checkpoint(runs_dir: Path, run_tag: str | None) -> str | None:
    """Record which per-deploy capture matches the artifact this run delivered.

    Extracted from `run()`'s `finally:` because it is the CONDITION, not the copy, that has been wrong:
    keyed on the trace id it skipped every observability-off run, and since captures are written for
    every run that left a directory of candidates with no record of which one shipped -- the one
    question the manifest is read for later. Checkpointing does not depend on observability, so neither
    does this.

    Returns the matching capture's name, or None when there is nothing to mark (no run directory, or no
    capture holds the delivered bytes -- which is itself recorded).
    """
    if not run_tag:
        return None
    run_root = runs_dir / run_tag
    return artifact_checkpoint.mark_submitted(run_root, run_root / "artifact")


def _mirror_run_to_s3(
    runs_dir: Path, trace_id: str, task_id: str, cfg: S3Config | None
) -> str | None:
    """Mirror this run's directory to S3, returning the run's S3 URI (None on failure).

    Called at the very end of the run, once every publisher is closed, so the per-source logs are
    fully flushed and the consolidated ``events.jsonl`` the upload builds is complete.

    Every failure is swallowed with a warning. Per design goal #3 in
    ``shared/observability/README.md``, observability must never break a real run -- a missing AWS
    session at 2am should cost you the mirror, not the benchmark result you just spent two hours
    computing. **The run data is already complete on disk at this point**, so a failed mirror loses
    nothing: the warning names the command that uploads it later.
    """
    from shared.observability.s3_store import S3CredentialsError, upload_trace

    trace_dir = runs_dir / trace_id
    if not trace_dir.is_dir():
        return None

    retry_hint = (
        f"Run data is complete in {trace_dir} -- upload it later with: "
        f"python scripts/upload_runs_to_s3.py {trace_id}"
    )

    try:
        result = upload_trace(trace_dir, cfg, task_id=task_id)
    except S3CredentialsError as e:
        # By far the most common failure (expired session). Log the remediation, not a traceback.
        logger.warning(f"Skipped S3 upload of run {trace_id} -- {e}\n{retry_hint}")
        return None
    except Exception as e:  # noqa: BLE001 -- never fail a completed run over its mirror
        # exc_info because an unexpected failure here is otherwise undiagnosable: swallowing the
        # exception is right, losing the traceback is not (matches model_io / model_config /
        # eval_system).
        logger.warning(f"Failed to upload run {trace_id} to S3: {e}. {retry_hint}", exc_info=True)
        return None

    logger.info(
        f"Uploaded run to {result.uri} "
        f"({result.uploaded} file(s), {result.bytes_uploaded / 1e6:.1f} MB"
        + (f", {result.skipped} already current" if result.skipped else "")
        + ")"
    )
    return result.uri


def _translate_docker_url_to_host(url: str) -> str:
    """Rewrite a container-DNS URL into a localhost URL using Docker's published port mapping.

    When the orchestrator runs on the host but the artifact runs in a Docker
    container, Docker DNS names (e.g., http://o2c-agent:8000) aren't resolvable
    from the host. This reads back the container's published host port and swaps
    the host:port for localhost:<published-port>.

    NOTE: the host port is assigned by Docker and normally does NOT match the container port. The
    deployer publishes with `-p <container_port>` and no host port (see deployer.py), so the daemon
    picks an arbitrary free ephemeral port when the container starts; `docker port` only reports what
    the daemon already chose -- nothing here allocates or "translates" a port. Look it up per deploy
    rather than caching it.

    Args:
        url: The artifact endpoint URL (e.g., http://o2c-agent:8000/respond)

    Returns:
        Rewritten URL (e.g., http://localhost:62817/respond) if the mapping could
        be read, otherwise the original URL unchanged (callers then fail later at
        the health gate rather than here).

    Example:
        >>> _translate_docker_url_to_host("http://o2c-agent:8000/respond")
        "http://localhost:62817/respond"   # 62817 = whatever host port Docker assigned
    """
    container_name = _docker_container_name_from_url(url)
    if container_name is None:
        logger.debug(f"URL does not match Docker container pattern: {url}")
        return url

    # Re-parse to recover scheme/port/path (container_name alone isn't enough here).
    match = re.match(r"^(https?)://([a-zA-Z0-9_-]+):(\d+)(.*)$", url)
    scheme, _container_name, container_port, path = match.groups()  # type: ignore[union-attr]

    # Query Docker for port mapping
    try:
        result = subprocess.run(
            ["docker", "port", container_name, container_port],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode != 0:
            logger.warning(
                f"Failed to get port mapping for {container_name}:{container_port}: "
                f"{result.stderr.strip()}"
            )
            return url

        # Parse output like "0.0.0.0:8000" or "0.0.0.0:8000\n[::]:8000"
        output = result.stdout.strip().split("\n")[0]
        host_port = output.split(":")[-1]

        translated_url = f"{scheme}://localhost:{host_port}{path}"
        logger.info(f"Translated artifact URL: {url} -> {translated_url}")
        return translated_url

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout querying docker port for {container_name}")
        return url
    except Exception as e:
        logger.warning(f"Error translating Docker URL {url}: {e}")
        return url


def _docker_env_value(container: str, key: str) -> str | None:
    """Read one env var's value from a running container via `docker inspect`. Returns None if the
    container/var is absent. Used to recover the task-env's evaluator-only ADMIN token (never exposed
    to the agent) so the orchestrator can drive per-case DB reset/state reads."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", container],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"docker inspect for {container} failed: {e}")
        return None
    if result.returncode != 0:
        return None
    prefix = f"{key}="
    for line in result.stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def resolve_agent_env(
    task_config: "TaskConfig", *, workspace_container: str | None = None,
) -> dict[str, str]:
    """A task's `agent_env`, resolved against ONE running task environment.

    The single place the spec's per-environment placeholders are filled in, shared by the build-phase
    agent container and every eval-phase artifact container, so the two cannot drift. `{tool_token}` is
    read off the named container through the var the spec declares as `lane_token_var` -- the token the
    environment ACTUALLY came up holding, rather than a copy of it kept in a second place that would
    then have to be kept in step.

    Args:
        task_config: The task's parsed `task_runner` block.
        workspace_container: Which environment to resolve against. Defaults to the task's single
            (copy 0) environment, which is what every caller outside parallel eval wants.
    """
    container = workspace_container or task_config.default_workspace_container
    token = None
    if task_config.lane_token_var:
        token = _docker_env_value(container, task_config.lane_token_var)
        if token is None and "{tool_token}" in str(task_config.agent_env):
            # Refuse rather than deploy a tokenless artifact: every one of its tool calls would 401
            # and the whole dataset would score zero, which reads as a bad artifact rather than as the
            # wiring fault it is. Naming both halves is the difference between a one-minute fix and an
            # afternoon.
            raise RuntimeError(
                f"{task_config.task_id}: cannot read {task_config.lane_token_var} from container "
                f"{container!r}, so the artifact's tool token is unknown. Is that environment running?"
            )
    return task_config.resolved_agent_env(workspace_container=container, tool_token=token)


def _sample_oracle(sample) -> dict:
    """A sample's structured ground truth, or `{}`.

    The parse itself lives on the datatype (`EvalSample.oracle`) -- it had seven near-copies across the
    task eval modules and this seam, each re-deciding what an absent payload means. What stays here is
    only the navigation, and it stays `getattr`-based on purpose: this runs for every task that opts
    into the reset seam, and the callers include test stubs that are sample-SHAPED rather than real
    `EvalSample`s. A sample with no oracle is a normal shape (airline's cases carry actions but no
    starting state), not an error worth failing a run over -- hence `{}` rather than the accessor's
    None.
    """
    read = getattr(sample, "oracle", None)
    if callable(read):
        return read() or {}
    return _expected(getattr(sample, "expected_output", None)) or {}


def _sample_initial_state(sample) -> dict | None:
    """The OPTIONAL generic per-case starting state an env should be seeded with, or None.

    Deliberately generic: the seam reads a conventional `initial_state` key off the sample's oracle
    rather than knowing any task's schema. Airline's samples have none, so airline skips seeding and
    behaves exactly as before; banking's 75 seeded cases carry one and would otherwise have the agent
    act on an unseeded DB while the golden replay is seeded -- comparing those hashes is a guaranteed
    false negative, which is why `task_success` refuses to score a seeded case until this runs.
    """
    state = _sample_oracle(sample).get("initial_state")
    return state if isinstance(state, dict) else None


def _make_env_reset_hooks(container: str, admin_token: str, *, port: int = 8080):
    """Build (setup_state, read_final_state) closures that reset the task-env DB before each eval case
    and read its resulting state hash after -- by `docker exec`-ing into the workspace container's
    admin API (http://localhost:<port>/admin, ADMIN token). Used for tasks that opt in via
    evaluation_data.reset_env_per_sample. We exec rather than call the host because the env container
    doesn't publish its port to the host (it's only reachable container-to-container on bench-network).

    Generalizable: any containerized task-env exposing /admin/reset + /admin/state_hash works. Three
    further routes are used WHEN PRESENT and skipped on 404, so an env that predates them keeps
    working unchanged:
      POST /admin/seed          -- apply this case's `initial_state` (and read-log allowlist)
      GET  /admin/retrieved_docs -- doc ids the case's retrieval tools returned
      GET  /admin/action_trace   -- the tool calls the artifact made this case
    """
    # Per-case facts recorded by setup_state and reported by read_final_state. One slot is safe: the
    # presence of these hooks forces generation to run sequentially (see eval/generation.py), so
    # exactly one case is in flight at a time.
    pending: dict[str, Any] = {}
    # Whether this env serves POST /admin/seed. None = not probed yet; False after a 404, so an env
    # without the route costs one probe for the whole run instead of one `docker exec` per case.
    seed_route: dict[str, bool] = {}

    from .env_admin import make_env_admin_request
    _admin = make_env_admin_request(container, admin_token, port=port)

    def _optional_get(path: str, key: str) -> Any | None:
        """GET an admin route that may not exist on this env. None = absent or unparseable."""
        status, body = _admin("GET", path)
        if status != 200:
            return None
        try:
            return json.loads(body).get(key)
        except (TypeError, ValueError):
            logger.warning(f"env admin {path} returned unparseable body: {body[:200]!r}")
            return None

    def setup_state(sample) -> None:
        pending.clear()
        status, body = _admin("POST", "/admin/reset")
        if status >= 400:
            raise RuntimeError(f"env reset failed ({status}): {body[:200]}")

        oracle = _sample_oracle(sample)
        initial_state = _sample_initial_state(sample)
        golden_actions = oracle.get("actions") or []
        # Seed when the case has a starting state to apply OR a trajectory the env may derive its
        # per-case read-log allowlist from. The second reason matters even with no starting state: the
        # golden replay derives an allowlist from the trajectory either way, and an allowlist set on
        # only one side makes a PERFECT agent score zero. Both are optional and generic -- a task
        # whose oracle has neither, or an env with no seed route, skips this entirely.
        if initial_state is None and not golden_actions:
            return
        if seed_route.get("available") is False:
            return

        seed_status, seed_body = _admin("POST", "/admin/seed", {
            "initial_state": initial_state,
            "golden_actions": golden_actions,
        })
        if seed_status == 404:
            seed_route["available"] = False
            if initial_state is not None:
                # Do NOT pretend the seed was applied: the eval function gates on the flag below and
                # scores the case unmeasurable, which is the honest outcome.
                logger.warning(
                    f"sample declares an initial_state but {container} has no POST /admin/seed -- "
                    "the case will run against an unseeded env and score as unmeasurable"
                )
            return
        if seed_status >= 400:
            raise RuntimeError(f"env seed failed ({seed_status}): {seed_body[:200]}")
        seed_route["available"] = True
        if initial_state is not None:
            pending["seeded_initial_state"] = True
        # Report the allowlist the env ACTUALLY applied, so the eval function can confirm its golden
        # replay used the same one rather than assuming it did.
        try:
            applied = json.loads(seed_body).get("read_log_allowlist")
        except (TypeError, ValueError):
            applied = None
        if applied is not None:
            pending["read_log_allowlist"] = applied

    def read_final_state(sample) -> dict:
        status, body = _admin("GET", "/admin/state_hash")
        if status >= 400:
            raise RuntimeError(f"env state_hash failed ({status}): {body[:200]}")
        try:
            state_hash = json.loads(body)["state_hash"]
        except (TypeError, ValueError, KeyError):
            raise RuntimeError(f"env state_hash returned no hash: {body[:200]!r}") from None

        final: dict[str, Any] = {"env_state_hash": state_hash, **pending}

        retrieved = _optional_get("/admin/retrieved_docs", "retrieved_documents")
        if retrieved is not None:
            final["retrieved_documents"] = retrieved
        actions = _optional_get("/admin/action_trace", "agent_actions")
        if actions is not None:
            final["agent_actions"] = actions
        return final

    return setup_state, read_final_state


def _make_user_tool_executor(container: str, tool_token: str, *, port: int = 8080):
    """Build the callable the eval-time user sim uses to invoke the CUSTOMER's own tools.

    Some domains put the deciding action on the customer's side -- they submit their own application,
    and the artifact can only advise. A user simulator that can only talk can never reach the golden
    state on such a case, however good the advice was, so the case would score 0 for a reason that has
    nothing to do with the artifact. This closes that gap.

    Same `docker exec` route as the admin hooks, and for the same reason: the env container publishes
    no port to the host. It calls the ordinary tool route with `requestor: "user"`, so the environment
    applies exactly the customer-side semantics it would in a real conversation -- the evaluator gets
    no privileged path, and this is the same token the agent already holds.
    """
    url = f"http://localhost:{port}/tools/"
    script = (
        "import os, sys, json, urllib.request as u, urllib.error as e\n"
        "req = u.Request(os.environ['TOOL_URL'], method='POST', data=sys.stdin.read().encode())\n"
        "req.add_header('Authorization', 'Bearer ' + os.environ['TOOL_TOKEN'])\n"
        "req.add_header('Content-Type', 'application/json')\n"
        "try:\n"
        "    print(u.urlopen(req, timeout=60).read().decode())\n"
        "except e.HTTPError as err:\n"
        "    print(json.dumps({'error': err.code, 'detail': err.read().decode()[:400]}))\n"
    )

    def call_user_tool(name: str, arguments: dict) -> Any:
        result = subprocess.run(
            ["docker", "exec", "-i",
             "-e", f"TOOL_TOKEN={tool_token}",
             "-e", f"TOOL_URL={url}{name}",
             container, "python", "-c", script],
            capture_output=True, text=True, timeout=90,
            input=json.dumps({"arguments": arguments or {}, "requestor": "user"}),
        )
        if result.returncode != 0:
            raise RuntimeError(f"user tool exec failed ({container}): {result.stderr.strip()}")
        try:
            payload = json.loads(result.stdout.strip())
        except (TypeError, ValueError):
            raise RuntimeError(f"user tool {name} returned no JSON: {result.stdout[:200]!r}") from None
        if isinstance(payload, dict) and "error" in payload:
            raise RuntimeError(f"user tool {name} rejected: {payload['detail']}")
        return payload.get("result") if isinstance(payload, dict) else payload

    return call_user_tool


@dataclass
class BenchmarkResult:
    """Result of a benchmark run.

    Attributes:
        task_id: The task identifier.
        status: Run status ('completed', 'failed', 'timeout').
        scores: Evaluation scores (if completed).
        error: Error message (if failed).
        agent_logs: Logs from the agent container.
        metadata: Additional metadata about the run.
    """

    task_id: str
    status: str
    scores: dict[str, Any] | None = None
    error: str | None = None
    agent_logs: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


async def _start_tracer_with_retry(
    runs_dir: Path, port: int, retry_limit: int, s3_config: S3Config | None = None
) -> tuple[Any, str, int]:
    """Start a TracerServer, retrying on later ports if `port` is already occupied.

    A single occupied port is often just a previous run's orphaned artifact
    container (see the network-diff backstop in BenchmarkOrchestrator.run's
    `finally:`), so retry a few times before giving up rather than hard-failing
    a whole batch over one stuck port.

    `s3_config` is forwarded so the dashboard this run serves can also pull and
    render *other people's* runs by id, not just the one in flight.

    Returns:
        (server, url, actual_port) for whichever port actually bound.

    Raises:
        TracerPortRangeExhaustedError: every port in [port, port + retry_limit)
            was occupied.
        Exception: any other startup failure, on the first attempt, unretried.
    """
    from ..tracer.server import TracerServer, TracerPortInUseError, TracerPortRangeExhaustedError

    port_to_try = port
    for _attempt in range(retry_limit):
        try:
            server = TracerServer(runs_dir=runs_dir, port=port_to_try, s3_config=s3_config)
            url = await server.start()
            return server, url, port_to_try
        except TracerPortInUseError:
            port_to_try += 1
    raise TracerPortRangeExhaustedError(port, port_to_try - 1)


def usersim_docs_dir(tasks_dir: Path, task_id: str) -> str | None:
    """Deprecated alias -- the convention lives in ``core.usersim_docs``.

    Kept as a re-export so existing callers/tests keep working; see
    :func:`appsci_bench.core.usersim_docs.usersim_docs_dir` for the contract.
    """
    return _core_usersim_docs_dir(tasks_dir, task_id)


class BenchmarkOrchestrator:
    """Main orchestrator for running benchmark tasks.

    The BenchmarkOrchestrator coordinates the full benchmark workflow:
    1. Start callback server (receives agent completion notifications)
    2. Start task environment (workspace container, DB, etc.)
    3. Start appsci agent user sim container
    4. Start appsci-agent container (with callback URL)
    5. Wait for agent to POST to callback (or timeout)
    6. Stop appsci agent user sim (agent is done interacting)
    7. Run evaluator against the artifact
    8. Clean up all containers

    Example:
        orchestrator = BenchmarkOrchestrator(tasks_dir=Path("tasks"))

        # Run a single task
        result = await orchestrator.run(
            task_id="order_to_cash",
            persona_path=Path("tasks/order_to_cash/user_personas/l3.yaml"),
        )

        # Check results
        if result.status == "completed":
            print(f"Scores: {result.scores}")
        else:
            print(f"Failed: {result.error}")

    Attributes:
        tasks_dir: Path to the tasks/ directory.
        callback_port: Port for the callback server.
        build_timeout: Maximum seconds to wait for agent to complete.
        observability: Whether to enable observability features.
        runs_dir: Path to the runs/ directory for observability data.
    """

    DEFAULT_CALLBACK_PORT = 9000
    DEFAULT_BUILD_TIMEOUT = 10800  # 3 hours; kept in step with experiments/config.DEFAULT_TIMEOUT
    DEFAULT_AGGREGATOR_PORT = 8080
    # How many consecutive ports (starting at aggregator_port) to try before giving up
    # on observability entirely. A stuck port is usually a single orphaned container
    # from a prior run (see artifact_container_name cleanup below), so a handful of
    # retries clears it without needing a hard stop for every batch run.
    AGGREGATOR_PORT_RETRY_LIMIT = 20
    # Per-turn artifact-query ceiling (inner HTTP request AND outer future). An agentic artifact runs
    # its own multi-step tool-calling loop per turn, so one /respond can take minutes.
    #
    # RAISED 300 -> 1200 on 2026-09-18, because 300 was not bounding runaway queries, it was DELETING
    # THE HARD HALF OF THE EVAL SET. A case that hits this scores 0 and still counts in the
    # denominator, so a censored case is indistinguishable from a bad artifact in the final number.
    #
    # Measured on slides_generator, 17 trials across 4 models: 53 cases lost to the 300s cut
    # (sonnet 4.6 22, kimi k3 21, gpt-5.5 7, opus 4.8 3). It was not hitting outliers -- on
    # slides_generator_claude-sonnet-4-6_b0.t1_l3_20260916_030743 the six cases that DID finish came
    # back at 89, 194, 247, 247, 279 and 300.0s, so the surviving distribution was already flush
    # against the ceiling and the next slowest case was simply cut.
    #
    # And the loss is SYSTEMATIC, not random. The `generated-sample-*` cases carry large PDF/pptx
    # attachments and timed out at 3-24% per case; the `product-slidesgen-*` cases timed out at 0-2%.
    # So the ceiling was quietly removing one class of input and leaving the easy one, which biases
    # every model's score by an amount that depends on how slow that model is. On the trial above the
    # reported score is 0.107; over the six cases that were allowed to finish it is 0.270.
    #
    # Raising it costs nothing in scoring validity: no slides objective scores latency, and
    # `latency_slo_ms` is declarative (nothing reads it). Cases run concurrently within a trial, so
    # trial wall time tracks the SLOWEST case: worst case moves from ~310s to ~1200s against a
    # trial_timeout of 14400s. This still bounds a genuinely hung query, just far enough out that a
    # slow-but-working artifact is measured rather than discarded.
    QUERY_TIMEOUT_S = 1200.0
    # Per-eval-function ceiling. Some objectives (e.g. policy_compliance) call an LLM judge through a
    # gateway, which can take >60s; the scoring layer's 60s default timed those out and scored them 0,
    # depressing the metric. 180s gives the judge headroom while still bounding a hung eval function.
    EVAL_TIMEOUT_S = 180.0

    def __init__(
        self,
        tasks_dir: Path,
        callback_port: int = DEFAULT_CALLBACK_PORT,
        build_timeout: int = DEFAULT_BUILD_TIMEOUT,
        observability: bool = False,
        runs_dir: Path | None = None,
        aggregator_port: int = DEFAULT_AGGREGATOR_PORT,
        s3_upload: bool = True,
        s3_config: S3Config | None = None,
        eval_lanes: int = 1,
        sample_ids: list[str] | None = None,
        rerun_source: str | None = None,
        rerun_source_sha256: str | None = None,
        rerun_allow_config_change: bool = False,
    ):
        """Initialize the BenchmarkOrchestrator.

        Args:
            tasks_dir: Path to the tasks/ directory.
            callback_port: Port for the callback server (default 9000).
            build_timeout: Maximum seconds for agent to complete (default 2 hours).
            observability: Whether to enable observability features.
            runs_dir: Path to the runs/ directory (default: ./runs).
            aggregator_port: Port for the aggregator server (default 8080).
            s3_upload: Mirror the run dir to S3 when the run ends (default on; needs
                observability, since there is nothing to upload without it).
            s3_config: Where to mirror to; defaults to ``S3Config.from_env()``.
            eval_lanes: How many copies of the (task-env, artifact) stack the eval phase scores
                against, so that many cases run at once (default 1: the single stack every run has
                always used). Only worth raising for a task that resets its environment per case --
                those are the ones a single stack forces into running one case at a time. A host
                decision, not a task one: it multiplies containers and concurrent model calls, and the
                right number depends on the machine and the provider's limits.
        """
        self.tasks_dir = tasks_dir
        self.callback_port = callback_port
        self.build_timeout = build_timeout
        self.task_runner = TaskRunner(tasks_dir)
        self.observability = observability
        self.runs_dir = runs_dir or Path("runs")
        self.aggregator_port = aggregator_port
        self.s3_upload = s3_upload
        self.s3_config = s3_config
        self.eval_lanes = eval_lanes
        self.sample_ids = sample_ids
        self.rerun_source = rerun_source
        self.rerun_source_sha256 = rerun_source_sha256
        self.rerun_allow_config_change = rerun_allow_config_change

    async def run(
        self,
        task_id: str,
        persona_path: Path | None = None,
        *,
        appsci_usersim_model: str | None = None,
        agent_model: str | None = None,
        agent_max_turns: int | None = None,
        agent_temperature: float | None = None,
        optimize_cycles: int | None = None,
        optimization_enabled: bool = True,
        user_sim_enabled: bool = True,
        eval_limit: int | None = None,
        skip_eval: bool = False,
        artifact_dir: Path | None = None,
        build_run_dir: Path | None = None,
        run_label: str | None = None,
        harness: HarnessConfig | None = None,
    ) -> BenchmarkResult:
        """Run a full benchmark for a task with a specific persona.

        This method orchestrates the complete benchmark workflow and handles
        cleanup on success or failure.

        Three modes, because an experiment that measures variance needs the two phases separable:

        * default -- BUILD then EVAL, as always.
        * `skip_eval` -- BUILD only. The artifact snapshot still happens in the cleanup block, which is
          what a later eval-only run scores. Used by a build cell of a `builds:`/`trials:` experiment,
          where scoring belongs to the trial cells so each gets its own timeout and its own retry.
        * `artifact_dir` -- EVAL only, against an already-built artifact source dir. No agent, no
          user-sim, no submission server; `persona_path` is unused (an artifact is scored against the
          task's eval dataset, not against a persona). This is one trial.

        `skip_eval` and `artifact_dir` are mutually exclusive: together they would ask for a run that
        neither builds nor scores.

        Args:
            task_id: The task identifier (e.g., "order_to_cash").
            persona_path: Path to the persona YAML file. Unused in eval-only mode.
            appsci_usersim_model: Optional LLM model for the appsci agent user sim.
            agent_model: Optional LLM model for the agent.
            agent_max_turns: Optional max turns for the agent.
            harness: External builder image/command, or None for the reference AppSci Agent.
            agent_temperature: Optional sampling temperature for both build-side agents. None leaves
                the request without a temperature, i.e. the provider default. This is the build-side
                replicate control -- the harness has no seed.
            user_sim_enabled: False runs the NO-USER arm: no user-sim service is started, the agent
                loses `talk_to_user` from its registry and every prompt surface, and the task's
                `user_sim_docs/` tree is mounted read-only in the AGENT container instead. The task,
                the facts and the scoring are unchanged -- only whether the agent had to ask for them.
                `persona_path` still names a persona but decides nothing in this arm (the whole
                document tree is mounted, not one persona's share), so the run is named `nouser`
                rather than letting it claim a level it never used.
            eval_limit: Optional first-N cap on eval data points (debug).
            skip_eval: Build and stop, without scoring.
            artifact_dir: Score this artifact source dir instead of building one.
            build_run_dir: For a trial, the run dir of the BUILD that produced `artifact_dir`. Its
                stakeholder conversation is what the elicitation diagnostic scores; a trial has no
                build phase of its own, so without this that metric is unmeasurable.
            run_label: Optional extra component for this run's directory name, for the coordinates
                only the caller knows -- the experiment runner's replicate position (`b0`, `b0.t1`).
                The orchestrator has no concept of builds or trials, so it cannot derive this.

        Returns:
            BenchmarkResult with status, scores, and any errors.
        """
        # Argument-shape guards first, before anything reads a spec or starts a container.
        #
        # A persona is what the user-sim role-plays, so a run WITH a stakeholder needs one. The two
        # modes that have no stakeholder -- the no-user arm and eval-only -- take none, and must not
        # be handed a stand-in: a default would put a level in the run record that nothing in the
        # run used, which is exactly the misreading the `nouser` run name exists to prevent.
        if persona_path is None and user_sim_enabled and artifact_dir is None:
            raise ValueError(
                "a persona is required to run with a stakeholder: the user-sim is bound to one and "
                "has nothing to role-play without it. Pass --persona, or --no-user-sim to run the "
                "arm that has no stakeholder at all."
            )
        if harness is not None and artifact_dir is not None:
            raise ValueError("harness selects a builder and cannot be used with eval-only artifact_dir")
        if harness is not None:
            missing = [name for name in harness.passthrough_env if not os.environ.get(name)]
            if missing:
                raise ValueError(f"harness requires environment variables: {', '.join(missing)}")
            run_label = f"{harness.name}_{run_label}" if run_label else harness.name
        if skip_eval and artifact_dir is not None:
            raise ValueError(
                "skip_eval and artifact_dir are mutually exclusive: that combination would neither "
                "build an artifact nor score one"
            )
        if artifact_dir is not None and not artifact_dir.is_dir():
            # Checked HERE, before the task-env is started -- a `compose up --build` costs minutes and
            # a trial with no artifact to deploy is doomed either way. A missing artifact is an
            # infrastructure failure, not a bad artifact, and must never be scored as one: the likely
            # cause is the build's run dir being reaped off this host, which is why the experiment
            # store stages a copy outside runs/.
            return BenchmarkResult(
                task_id=task_id,
                status="failed",
                error=(
                    f"artifact dir not found: {artifact_dir}. An eval-only run scores a build that "
                    f"already happened; if the build ran on another host, or its run dir was reaped, "
                    f"the artifact has to be staged here first."
                ),
            )

        # FAIL FAST on a bad sim-only document declaration, BEFORE anything else happens -- before the
        # spec is even loaded, let alone a container started. A persona that declares a document the
        # mount cannot supply would otherwise produce a user-sim that answers "I don't know" about its
        # own policy, which in a trace is indistinguishable from an agent that never asked. The
        # validator reads the task tree directly, so it needs nothing loaded first.
        #
        # Scoped to THIS task: returns a failed BenchmarkResult (exit 1 via main), deliberately NOT the
        # exit-3 environment-blocker path, so a batch run skips this row and continues with the rest
        # (scripts/run_tasks.sh treats 3 as abort-everything).
        docs_check: DocsValidation = validate_usersim_docs(task_id, self.tasks_dir)
        for warning in docs_check.warnings:
            logger.warning(f"user-sim docs: {warning}")
        if not docs_check.ok:
            # Log every error, not just the first -- fixing one at a time across re-runs is miserable.
            for err in docs_check.errors:
                logger.error(f"user-sim docs: {err}")
            return BenchmarkResult(
                task_id=task_id,
                status="failed",
                error=(
                    f"invalid user-sim document declaration ({len(docs_check.errors)} error(s)): "
                    + "; ".join(docs_check.errors)
                ),
                metadata={"usersim_docs_errors": list(docs_check.errors),
                          "usersim_docs_warnings": list(docs_check.warnings)},
            )

        # The documents the NO-USER arm hands to the agent, resolved here so a task with nothing to
        # hand over fails BEFORE any container starts. Without them that arm is not an ablation of
        # elicitation -- it is the same task with the requirements deleted, which is a harder task and
        # would be reported as if it were the same one. Same scoping as the docs check above: a failed
        # result for this row, not the exit-3 environment blocker, so a batch skips it and continues.
        requirements_dir_str: str | None = None
        policies_dir_str: str | None = None
        if not user_sim_enabled and artifact_dir is None:
            requirements_dir_str = usersim_docs_dir(self.tasks_dir, task_id)
            policies_dir_str = persona_policies_dir(self.tasks_dir, task_id)
            if not (requirements_dir_str or policies_dir_str):
                return BenchmarkResult(
                    task_id=task_id,
                    status="failed",
                    error=(
                        f"--no-user-sim was requested but task {task_id!r} has neither a "
                        f"tasks/{task_id}/user_sim_docs/ directory nor a "
                        f"tasks/{task_id}/user_personas/{POLICIES_DIRNAME}/ directory to hand the "
                        "agent. In this arm the agent gets that material instead of a stakeholder "
                        "to ask; with neither, the run measures a harder task rather than an "
                        "ablation of elicitation."
                    ),
                    metadata={"user_sim_enabled": False},
                )
            # A YAML in the extracts tree is the one slip that would publish the answer key, and the
            # tree is mounted whole -- so refuse here, on the same per-task terms as a missing tree.
            if policies_dir_str:
                policy_errors = validate_policies_dir(policies_dir_str)
                if policy_errors:
                    return BenchmarkResult(
                        task_id=task_id,
                        status="failed",
                        error=(
                            f"tasks/{task_id}/user_personas/{POLICIES_DIRNAME}/ cannot be mounted: "
                            + "; ".join(policy_errors)
                        ),
                        metadata={"user_sim_enabled": False},
                    )
            if requirements_dir_str:
                logger.info(f"No-user arm: requirement documents from {requirements_dir_str}")
            if policies_dir_str:
                logger.info(f"No-user arm: persona policy extracts from {policies_dir_str}")
            # Say which half is MISSING as well. A task with documents but no extracts is exactly the
            # order_to_cash/slides_generator shape: the interface is handed over and the decision
            # rules stay inside the personas, so the arm quietly asks the agent to invent them.
            if not policies_dir_str:
                logger.warning(
                    "No-user arm: task %r has no user_personas/%s/ tree, so any requirement that "
                    "lives only in a persona's inline domain_knowledge is absent from this run. If "
                    "the task's elicitation rubric draws on those facts, this arm is measuring a "
                    "harder task, not an ablation.", task_id, POLICIES_DIRNAME,
                )
            if not requirements_dir_str:
                logger.warning(
                    "No-user arm: task %r has no user_sim_docs/ tree; the agent gets the persona "
                    "policy extracts only.", task_id,
                )

        # Load task spec
        spec = load_task_spec(task_id)
        spec_path = str(resolve_spec_path(task_id))

        native_controls = None
        ignored_build_controls = {}
        if harness is not None and harness.adapter:
            from ..core.native_harness import build_controls, effective_controls, make_launch

            # No container, browser login or model call is needed for this preflight.
            native_controls, ignored_build_controls = effective_controls(build_controls(
                spec.budgets, model=agent_model, max_turns=agent_max_turns,
                temperature=agent_temperature,
            ))
            make_launch(harness.adapter, harness.auth, native_controls, dict(os.environ))
            if ignored_build_controls:
                logger.warning("%s ignores reference-agent controls: %s; build timeout still applies",
                               harness.adapter, ", ".join(sorted(ignored_build_controls)))

        # The raw spec.yaml is GROUND TRUTH (evaluation_data, host
        # paths in resource locations, the gold solution). The agent under test must never
        # see it -- and its mini-swe shell can `cat` whatever we mount -- so we mount a
        # redacted, agent-facing view (see core.spec.build_agent_spec) instead of the raw
        # file. Written per-run below (once the temp dir exists) and cleaned up in finally.
        # The user-sim keeps the raw spec: it is a trusted benchmark component that role-plays a
        # persona who KNOWS the ground truth. NOTE: it does NOT read resource files by `location`
        # -- that never worked in a container (the user-sim image ships no `tasks/` tree) and was
        # removed in #76. Sim-only document CONTENT comes from the persona's `known_docs`, loaded
        # from the task's user_sim_docs/ tree, which is mounted only into the user-sim container.
        agent_spec_dir: str | None = None
        agent_spec_path = spec_path

        # Resolve persona path to absolute for Docker volume mount. None is a real, supported state:
        # the NO-USER arm has no stakeholder to bind, so it takes no persona and the CLI does not ask
        # for one. Every use below is guarded rather than defaulted -- a stand-in persona would put a
        # level in the run record that nothing in the run actually used.
        persona_path_resolved = persona_path.resolve() if persona_path else None

        # What goes in the run name's persona slot. In the NO-USER arm the bound persona decides
        # NOTHING -- the whole user_sim_docs/ tree is mounted, not one persona's share of it -- so
        # naming the run `l3` would invite exactly the mistake the arm makes easy: reading three
        # identically-configured runs as a persona-level comparison. `nouser` says what the run was.
        run_name_persona = (
            str(persona_path_resolved) if user_sim_enabled else NO_USER_RUN_SLUG
        )

        # Initialize service handles (for cleanup)
        submission_server: ArtifactSubmissionServer | None = None
        task_env: dict | None = None
        usersim: DockerAppsciAgentUserSimService | None = None
        agent_svc: AppSciAgentContainerManager | None = None
        aggregator_server = None
        # Port the tracer actually bound. Initialized to the configured port so the
        # `finally:` view-hint (_view_port_for_log) never references an unbound local
        # if tracer startup fails before this is reassigned on the success path below.
        actual_port = self.aggregator_port
        # Name of the artifact container the agent deployed, recovered from its
        # submitted container-DNS URL, so it can be torn down in `finally:` below.
        artifact_container_name: str | None = None
        # Names already on the bench network before the agent starts, so `finally:`
        # can catch an orphaned artifact container even when complete was
        # never called (crash/timeout) -- see the network-diff backstop below.
        artifact_network_baseline: set[str] | None = None
        # The agent's filesystem is snapshotted in the main flow, before its container is halted at
        # the build->eval handover. This flag stops `finally:` repeating the copy on the success path
        # (it still does it on a crash path, where the handover was never reached).
        agent_snapshotted = False
        # Whether any phase's environment came up on a container that already existed instead of a
        # freshly created one. Tracked in a local rather than read off task_env at the end because the
        # build phase's descriptor is replaced by the eval one at the handover, so the flag would be
        # thrown away before anything records it. Reachable on THREE starts now, not one: the build
        # phase, the eval phase after the handover, and an eval trial -- which is the newly reachable
        # case, because a trial's eval start has no preceding build teardown to have cleared the stack.
        env_reused_any_phase = False

        # Observability setup
        trace_id: str | None = None
        runs_dir_str: str | None = None
        obs_publisher: EventPublisher | None = None
        # This run's directory name under runs/ -- the trace id when observability is on, else a fresh
        # timestamped name. Bound here rather than where it is computed so `finally:` can find the
        # artifact checkpoints under it: capture does not depend on observability (a run with no mirror
        # is a deliberate configuration, not a degraded one), so neither may marking which capture was
        # delivered -- a candidate set with no record of which one shipped answers the one question the
        # marker exists for. None means the run never got as far as provisioning the directory.
        run_tag: str | None = None

        if self.observability:
            # Generate trace ID and set up observability
            trace_id = compose_run_name(
                task_id,
                agent_model=agent_model,
                persona_path=run_name_persona,
                label=run_label,
            )
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            trace_dir = self.runs_dir / trace_id
            trace_dir.mkdir(parents=True, exist_ok=True)

            # Host-absolute runs dir: mounted into each container at /runs, and
            # used by the orchestrator's own publisher to locate its per-source DB.
            runs_dir_str = str(self.runs_dir.resolve())

            # Each writer owns its own events-<source>.jsonl (single writer per
            # file); the tracer merges them on read. The orchestrator writes
            # directly to the host path for its own source.
            from shared.observability.storage import get_source_log_path
            obs_publisher = EventPublisher(
                log_path=get_source_log_path(trace_id, Source.ORCHESTRATOR, runs_dir_str),
                trace_id=trace_id,
                source=Source.ORCHESTRATOR,
                task_id=task_id,
            )

            logger.info(f"Observability enabled. Trace ID: {trace_id}")

            # Start tracer server. Observability was explicitly requested, so a
            # persistent port conflict is a hard stop (see TracerPortRangeExhaustedError)
            # rather than a silent "continue without observability" degradation.
            # But a single occupied port is often just a previous run's orphaned
            # artifact container squatting on it (see the artifact-container
            # cleanup below) -- _start_tracer_with_retry tries the next few ports
            # before giving up, rather than failing a whole batch over one stuck
            # port. A range-exhausted failure is raised here, before the run's
            # main try/except, so it still propagates all the way out of run()
            # instead of being folded into a failed BenchmarkResult.
            try:
                aggregator_server, aggregator_url, actual_port = await _start_tracer_with_retry(
                    self.runs_dir, self.aggregator_port, self.AGGREGATOR_PORT_RETRY_LIMIT,
                    s3_config=self.s3_config,
                )
                if actual_port != self.aggregator_port:
                    logger.warning(
                        f"Observability port {self.aggregator_port} was in use; "
                        f"using {actual_port} instead."
                    )
                logger.info(f"Tracer UI: {aggregator_url}/?trace_id={trace_id}")
            except Exception as e:
                from ..tracer.server import TracerPortRangeExhaustedError
                if isinstance(e, TracerPortRangeExhaustedError):
                    raise
                logger.warning(f"Failed to start tracer server: {e}")
                aggregator_server = None

        # Build-phase lifecycle tracking. BUILD_START is emitted once the agent
        # container is running; BUILD_COMPLETE is emitted exactly once when the
        # agent stops building (submission, crash, timeout, or exception).
        build_start_time: float | None = None
        build_complete_emitted = False

        def emit_build_complete(status: str, **extra: Any) -> None:
            nonlocal build_complete_emitted
            if obs_publisher is None or build_start_time is None or build_complete_emitted:
                return
            build_complete_emitted = True
            obs_publisher.emit(
                event_type=EventType.BUILD_COMPLETE,
                payload={
                    "status": status,
                    "duration_ms": (time.time() - build_start_time) * 1000,
                    **extra,
                },
            )

        def note_env_reused(phase: str, detail: str) -> None:
            """Record that `phase`'s environment adopted a container that already existed.

            Three things at once, at all three starts (build, the handover's eval, and an eval trial),
            because a partial record of this is worse than none: the run scores normally, exits 0 and
            produces a valid report either way, so the only thing separating a trustworthy cell from a
            poisoned one is whether this fired.

            The EVENT is the part that lasts. `BenchmarkResult.metadata` is a plain dataclass field
            nothing persists or reads, and the log line sits near the start of a cell whose log the
            worker keeps only the tail of -- so before this the evidence was gone by the time anyone
            went looking, and a poisoned trial entered the pass^k estimator unmarked.
            """
            nonlocal env_reused_any_phase
            env_reused_any_phase = True
            logger.error(detail)
            if obs_publisher is not None:
                obs_publisher.emit(
                    event_type=EventType.TASK_ENV_REUSED,
                    payload={"phase": phase, "detail": detail},
                )

        try:
            # 1. Start submission server
            #
            # Skipped in eval-only mode: its only job is to receive the agent's artifact submission
            # and host the /deploy route the agent calls, and there is no agent here. Skipping it also
            # keeps a trial off port 9000, which matters because trials of different builds run
            # back-to-back on the same host and the port is a single global resource.
            if artifact_dir is None:
                logger.info("Starting submission server...")
                submission_server = ArtifactSubmissionServer(
                    host="0.0.0.0",
                    port=self.callback_port,
                )
                submit_url = await submission_server.start()
                logger.info(f"Submission server ready at {submit_url}")

            # EVAL-ONLY MODE. One trial: deploy an already-built artifact and score it.
            #
            # IT STARTS THE EVAL ENVIRONMENT, which is why this branch sits ABOVE the build-env start
            # below. A trial has no build phase -- there is no agent here -- and the build environment
            # deliberately holds only the records the eval cases are NOT drawn from
            # (docs/task_env_split.md). Scoring against it measures the artifact on the wrong store
            # with every surface reporting healthy: order_to_cash's build phase loads a carved share of
            # its fixture while its eval cases are drawn from the full one, so its trials were scored
            # against roughly a fifth of the records, and a case naming an absent record errors into a
            # confident-looking low score. Two other call sites already assumed what this one did not:
            # `evaluate_deployed` documents that it needs the EVAL phase's dict (its per-sample reset
            # reads an admin token off that container), and the standalone eval CLI refuses outright to
            # score against a build env.
            #
            # Both split tasks are live cases, measured rather than argued: order_to_cash's build
            # fixture holds 662 invoice rows against eval's 1,095, and airline's holds 10 reservations
            # against eval's 2000 (#137). One airline trial run on each side of this fix, same artifact
            # and same case, scored 0.0 on the build share and 1.0 on the eval store -- and the 0.0
            # arrived with policy_compliance at 0.85, reading exactly like an artifact that answered
            # wrongly. Any trial score recorded against either task before this fix is invalid.
            #
            # Returns from inside the try, so the shared `finally` below still stops the task-env,
            # removes the artifact container, closes the publishers and mirrors the run to S3.
            if artifact_dir is not None:
                # Recorded BEFORE the start, so `finally` can bring the stack down even if that start
                # throws part-way through -- the same reason the handover records one. What leaks
                # otherwise is not just containers: the next trial of this same build runs on this same
                # host minutes later, and `compose up` would adopt the half-created environment rather
                # than making a fresh one.
                task_env = self.task_runner.teardown_descriptor(task_id)
                logger.info(f"Starting eval task environment for {task_id}...")
                # Named apart from the handover's `eval_task_env`: these are two distinct eval-env
                # starts in one method, and a reader should not have to work out which is which.
                trial_task_env = self.task_runner.start(task_id, phase=PHASE_EVAL)
                if trial_task_env is None:
                    # The task declares its eval phase needs no environment (`eval: none`) -- its eval
                    # inputs travel on the cases themselves. Nothing came up, so there is nothing to
                    # tear down. But nothing created the bench network either, and the deploy below
                    # puts the artifact on it, so create it here rather than depending on a leftover
                    # from whichever earlier run last brought an environment up on this host.
                    logger.info("Task declares no eval environment; scoring without one.")
                    task_env = None
                    self.task_runner.ensure_network(task_id)
                else:
                    task_env = trial_task_env
                    logger.info(f"Eval task environment ready: {task_env['workspace_container']}")
                    if task_env.get("reused_existing_container"):
                        note_env_reused(
                            PHASE_EVAL,
                            "The eval task environment came up on a container that ALREADY EXISTED, "
                            "so it holds whatever the last run left in it rather than this task's "
                            "eval store. Scores from this trial are not trustworthy.",
                        )
                # Set BEFORE deploying, and from the spec rather than from the deploy result: the
                # container name is the `finally`'s only handle on the artifact, and a deploy that
                # starts a container and then fails its health check must not leave it running on a
                # host that runs the next trial minutes later.
                eval_only_config = TaskConfig.from_spec(task_id, self.tasks_dir)
                artifact_container_name = eval_only_config.artifact_container
                return await self._run_eval_only(
                    task_id=task_id,
                    spec=spec,
                    task_config=eval_only_config,
                    artifact_dir=artifact_dir,
                    task_env=task_env,
                    trace_id=trace_id,
                    eval_limit=eval_limit,
                    env_reused=env_reused_any_phase,
                    build_run_dir=build_run_dir,
                    # Carried into the trial purely as a LABEL: a trial runs no agent, so this
                    # controls nothing about how the artifact was authored. What it decides is
                    # whether the elicitation diagnostic reports "there was nobody to ask" or
                    # "we could not find the conversation" -- and only the caller knows which.
                    user_sim_enabled=user_sim_enabled,
                )

            # 2. Start the BUILD task environment. A run brings an environment up twice: this one,
            # which the agent explores and which holds only the records no eval case touches, and then
            # -- after this one is torn down at the handover below -- the eval environment holding the
            # records the eval cases are drawn from. See docs/task_env_split.md.
            logger.info(f"Starting build task environment for {task_id}...")
            task_env = self.task_runner.start(task_id, phase=PHASE_BUILD)
            logger.info(f"Build task environment ready: {task_env['workspace_container']}")
            if task_env.get("reused_existing_container"):
                note_env_reused(
                    PHASE_BUILD,
                    "The build task environment came up on a container that ALREADY EXISTED, so it "
                    "holds whatever the last run left in it -- which, if that run died during its eval "
                    "phase, is every record the eval cases are drawn from. The agent under test can "
                    "read it. Scores from this run are not trustworthy.",
                )

            # 3. Start appsci agent user sim -- unless this is the NO-USER arm, which has none.
            # Skipped rather than started-and-ignored: the arm's claim is that there was nobody to
            # ask, and a running service on the bench network is a reachable stakeholder whether or
            # not the agent was given a tool for it. It also keeps the arm from paying for a
            # simulator model it never calls.
            if user_sim_enabled:
                logger.info("Starting appsci agent user sim...")
                # The task's sim-only documents, mounted read-only into the user-sim container ONLY.
                # This is the material the agent must elicit: it is deliberately absent from the agent
                # container's mounts (which get /data and /sop). See usersim_docs_dir. In the no-user
                # arm the SAME tree is mounted into the agent container instead -- that swap, and
                # nothing else about the task, is what the arm changes.
                usersim_docs_str = usersim_docs_dir(self.tasks_dir, task_id)
                if usersim_docs_str:
                    logger.info(f"User-sim docs directory: {usersim_docs_str}")
                usersim = DockerAppsciAgentUserSimService(
                    task_id=task_id,
                    spec_path=spec_path,
                    persona_path=str(persona_path_resolved),
                    network_name=task_env["network_name"],
                    model=appsci_usersim_model,
                    observability_trace_id=trace_id,
                    runs_dir=runs_dir_str,
                    docs_dir=usersim_docs_str,
                )
                usersim.start()
                logger.info(f"Appsci agent user sim ready at {usersim.url}")
            else:
                logger.info(
                    "No-user arm: no user-sim started; the agent gets %s read-only instead of "
                    "`talk_to_user`.",
                    " + ".join(d for d in (requirements_dir_str, policies_dir_str) if d),
                )
                # Said out loud because the CLI still requires --persona and the run still records
                # one. Nothing reads it in this arm: BOTH trees are mounted whole, not one persona's
                # share of either, so an L3 and an L1 cell here are the same run. Note what that
                # means for the policy half in particular -- every level's extract is handed over, so
                # the agent sees the rules as L1, L2 and L3 each describe them, which is more than
                # any single stakeholder would have said. An adversarial persona is the case where
                # the silence would cost something real: its pressures reach the agent only through
                # the simulator, so the red-team trace this run would otherwise assemble simply has
                # no exchanges to assemble from.
                mounted = " + ".join(d for d in (requirements_dir_str, policies_dir_str) if d)
                if persona_path_resolved is not None:
                    # Only worth saying when someone passed one. The flag no longer requires it, so
                    # the common case is silence rather than a warning about a file nobody named.
                    logger.warning(
                        "No-user arm: persona %s is NOT used -- every mounted tree is mounted whole "
                        "(%s), so the persona's level and any adversarial pressures have no effect "
                        "on this run.%s", persona_path_resolved.name, mounted,
                        (" Its inline domain_knowledge reaches the agent only through the extracts "
                         f"in {policies_dir_str}, for every level at once." if policies_dir_str else
                         " Its inline domain_knowledge reaches the agent not at all: this task has "
                         "no extracts."),
                    )
                else:
                    logger.info("No-user arm: no persona bound; mounted %s", mounted)

            # 4. Start appsci-agent
            logger.info("Starting appsci-agent...")

            # Mount a redacted, agent-facing spec (never the raw ground-truth spec) into the
            # agent container. Written to a per-run temp file so its lifetime spans the
            # container; removed in the cleanup-finally.
            agent_spec_dir = tempfile.mkdtemp(prefix="appsci-agent-spec-")
            agent_spec_path = str(write_agent_spec(spec_path, Path(agent_spec_dir) / "spec.yaml"))
            logger.info(f"Redacted agent-facing spec written to: {agent_spec_path}")

            # Compute data directory path (tasks/<task_id>/environment/data/)
            data_dir = self.tasks_dir / task_id / "environment" / "data"
            data_dir_str = str(data_dir) if data_dir.exists() else None
            # Agent-facing SOP / requirement docs (tasks/<task_id>/environment/sop/), mounted at /sop.
            sop_dir = self.tasks_dir / task_id / "environment" / "sop"
            sop_dir_str = str(sop_dir) if sop_dir.exists() else None

            # Artifact output dir is a per-task contract from the spec; the agent
            # writes its deliverable here and the snapshot reads from the same
            # path (falls back to the container default if the spec omits it).
            artifact_output_dir = spec.artifacts.get("output_dir") or "/work/out"
            # Task-specific env for the agent container (spec.task_runner.agent_env),
            # e.g. airline's AIRLINE_TOOLS_URL/TOKEN. The deployed artifact inherits it.
            task_config = TaskConfig.from_spec(task_id, self.tasks_dir)
            agent_extra_env = resolve_agent_env(
                task_config, workspace_container=task_env["workspace_container"]
            )
            # The deployed artifact is itself an LLM agent, so it needs the SAME model-provider
            # credentials the meta-agent runs with (agent_container forwards those to the agent
            # container; here we forward them to the artifact container). Without this the artifact
            # crashes at import for lack of a key -- which previously pushed an agent to bake a
            # plaintext proxy token into its image. Task agent_env wins on any key clash; the
            # deployer still injects PORT last, so nothing here can shadow it.
            deploy_env = {**llm_credential_env(), **agent_extra_env}

            # The artifact container name is spec-declared (defaulting to appsci-artifact-<task_id>),
            # so teardown can target it deterministically even on crash/timeout -- set it up front
            # rather than only recovering it from the submitted URL on the success path below.
            artifact_container_name = task_config.artifact_container

            # Host-visible artifact dir, bind-mounted into the agent container at its output dir so
            # the host-side deployer (POST /deploy) builds straight from what the agent writes -- no
            # docker cp, no path guessing -- and so the same dir is what the eval-only pipeline
            # re-deploys later. Provisioned on EVERY run (deploy needs it), not just under
            # observability: it IS the trace's artifact dir when observability is on (so the snapshot
            # + manifest land in the same place), else a fresh timestamped run dir.
            run_tag = trace_id or compose_run_name(
                task_id,
                agent_model=agent_model,
                persona_path=run_name_persona,
                label=run_label,
            )
            host_artifact_dir = self.runs_dir / run_tag / "artifact"
            host_artifact_dir.mkdir(parents=True, exist_ok=True)

            harness_inputs_dir = None
            if harness is not None:
                harness_inputs_dir = write_harness_inputs(
                    host_artifact_dir.parent / "harness", spec_path=agent_spec_path,
                    artifact_output_dir=artifact_output_dir, artifact_port=task_config.artifact_port,
                    artifact_server_entries=SERVER_ENTRY_CANDIDATES,
                    artifact_env_names=list(deploy_env), harness_env_names=harness.passthrough_env,
                    workspace_container=task_env["workspace_container"],
                    task_env_names=list(agent_extra_env),
                    model_env_names=[name for name in LLM_CREDENTIAL_ENV_VARS
                                     if deploy_env.get(name, "").strip()],
                    model=agent_model, build_timeout=self.build_timeout, max_turns=agent_max_turns,
                    temperature=agent_temperature, optimize_cycles=optimize_cycles,
                    optimization_enabled=optimization_enabled,
                    has_data=data_dir_str is not None, has_sop=sop_dir_str is not None,
                    adapter=harness.adapter,
                )
                # The launch identity remains beside the public inputs, even with tracing off.
                # Only environment variable NAMES are recorded; never their values.
                (harness_inputs_dir / "launch.json").write_text(json.dumps({
                    "name": harness.name, "image": harness.image,
                    "command": list(harness.command),
                    "passthrough_env": list(harness.passthrough_env),
                    "adapter": harness.adapter, "auth": harness.auth,
                }, indent=2) + "\n")

            agent_config = AppSciAgentContainerConfig(
                task_id=task_id,
                workspace_container=task_env["workspace_container"],
                appsci_usersim_url=usersim.url if usersim else None,
                appsci_usersim_token=usersim.token if usersim else None,
                orchestrator_submit_url=submit_url,
                spec_path=agent_spec_path,
                network_name=task_env["network_name"],
                model=agent_model,
                max_turns=native_controls["max_turns"] if native_controls else agent_max_turns,
                temperature=native_controls["temperature"] if native_controls else agent_temperature,
                optimize_cycles=optimize_cycles,
                optimization_enabled=optimization_enabled,
                data_dir=data_dir_str,
                sop_dir=sop_dir_str,
                # The no-user swap, both halves together. The container manager refuses either one
                # without the other, so these cannot drift into a control run that also gets the
                # documents (or an ablation with nothing to read).
                user_sim_enabled=user_sim_enabled,
                requirements_dir=requirements_dir_str,
                policies_dir=policies_dir_str,
                artifact_output_dir=artifact_output_dir,
                host_artifact_dir=str(host_artifact_dir),
                observability_trace_id=trace_id,
                runs_dir=runs_dir_str,
                extra_env=agent_extra_env,
                harness=harness,
                harness_inputs_dir=str(harness_inputs_dir) if harness_inputs_dir else None,
            )

            # Enable the submission server's POST /deploy route with this task's deploy contract:
            # build from the bind-mounted host artifact dir, run under the spec-declared name/port on
            # the bench network, inject the task env so the deployed artifact reaches its tool-API.
            # The deployed endpoint is the container-DNS /respond URL (agent submits it; host reaches
            # it via docker-port translation).
            submission_server.configure_deploy(
                DeployContext(
                    artifact_dir=str(host_artifact_dir),
                    image_name=f"{task_config.artifact_container}:latest",
                    container_name=task_config.artifact_container,
                    network=task_env["network_name"],
                    port=task_config.artifact_port,
                    env=deploy_env,
                    # Per-deploy checkpointing of the artifact dir, under this run. The agent has no
                    # path to it: the only host paths its container gets are this artifact dir and --
                    # with observability on -- four observability log FILES, bind-mounted one at a time
                    # at /runs/<trace_id>/, so that path holds those four files and nothing else (with
                    # it off, none are mounted). The run directory itself is never mounted, so
                    # `checkpoints/`, a sibling of `artifact/`, is unreachable from inside. See the
                    # mount block in `AppSciAgentContainerManager.start`.
                    run_dir=str(host_artifact_dir.parent),
                    checkpoint_s3_config=(
                        self.s3_config if (self.observability and self.s3_upload) else None
                    ),
                )
            )
            # Snapshot the network now, before the agent (and whatever artifact
            # container it deploys) starts -- anything present here is already
            # accounted for by task_env's/usersim's own cleanup, not a leak.
            artifact_network_baseline = _docker_container_names_on_network(task_env["network_name"])
            agent_svc = AppSciAgentContainerManager(agent_config)
            agent_svc.start()
            if harness_inputs_dir is not None:
                agent_svc.start_harness_log_capture(harness_inputs_dir / "native.log")
            logger.info(f"Agent started: {agent_svc.container_name}")

            # Bracket the BUILD phase: the agent container is now running and
            # will begin working. Sub-events (turns, tool calls, swe steps)
            # emitted by the agent/usersim nest under this window.
            build_start_time = time.time()
            if obs_publisher:
                obs_publisher.emit(
                    event_type=EventType.BUILD_START,
                    payload={
                        "task_id": task_id,
                        "agent_model": agent_model,
                        "usersim_model": appsci_usersim_model,
                        "max_turns": agent_config.max_turns,
                        # Recorded on the trace because it is the build-side replicate control and,
                        # until this landed, nothing anywhere said what temperature a build ran at.
                        # None means "no temperature sent", not "temperature zero".
                        "agent_temperature": agent_config.temperature,
                        "ignored_build_controls": ignored_build_controls,
                        # None => no ceiling (run until the agent's own eval scores saturate).
                        "optimize_cycles": optimize_cycles,
                        # False = the ablation: no eval_generator, no optimize_agent. Recorded because
                        # a score is not comparable across the two modes and a results table must be
                        # able to say which one produced it.
                        # Native optimization cannot be inferred from reference-tool events.
                        "optimization_enabled": optimization_enabled if harness is None else None,
                        "harness": harness.name if harness else "appsci_agent",
                        "harness_optimization_enabled": optimization_enabled,
                        # False = the NO-USER arm: no user-sim on the network, no `talk_to_user`, the
                        # sim-only documents mounted in the agent container instead. On the trace for
                        # the same reason as the flag above -- and because `usersim_model` beside it
                        # is meaningless in that arm, which a reader needs to be told rather than
                        # left to infer from an absence of exchanges.
                        "user_sim_enabled": user_sim_enabled,
                        "container": agent_svc.container_name,
                    },
                )

            # 5. Wait for submission OR container crash (whichever comes first)
            logger.info(f"Waiting for agent to complete (timeout: {self.build_timeout}s)...")
            wait_result = await self._wait_for_agent_completion(
                submission_server=submission_server,
                agent_svc=agent_svc,
                timeout=self.build_timeout,
            )

            # BUILD phase ends here regardless of outcome (completed/crashed/timeout).
            emit_build_complete(
                wait_result["status"],
                exit_code=wait_result.get("exit_code"),
            )
            
            # WHICH ARM THIS RUN WAS, carried on every outcome from here down and not just the two
            # that produce numbers. The scored path and the build-only path recorded these flags; the
            # crash, timeout and no-submission paths recorded nothing, so a run that died was
            # unattributable to an arm -- which is exactly backwards, because the runs most worth
            # attributing are the ones that failed. An arm whose every run crashes on startup would
            # show up as an empty row rather than a failing one.
            #
            # `optimization_enabled` rides along for the same reason: it is the other ablation axis,
            # and a failed run that cannot say which arm it was is no more useful for one than the
            # other.
            arm_metadata = {
                "user_sim_enabled": user_sim_enabled,
                "optimization_enabled": optimization_enabled,
            }

            if wait_result["status"] == "no_submission":
                # Agent ran to a clean exit without submitting an artifact -- a normal outcome, not a
                # failure. Report it distinctly (no error, no scores) so it isn't conflated with a crash.
                logger.info("Agent completed without submitting an artifact; nothing to evaluate.")
                return BenchmarkResult(
                    task_id=task_id,
                    status="no_submission",
                    agent_logs=wait_result["logs"],
                    metadata=dict(arm_metadata),
                )

            if wait_result["status"] == "checkpoint_failed":
                # Deliberately louder than a build failure: nothing the agent did caused this, and the
                # run cannot be salvaged into a partial result, because the missing candidate is
                # invisible to everything downstream.
                logger.error(
                    "Aborting the run: an artifact checkpoint could not be captured. %s",
                    wait_result["error"],
                )
                return BenchmarkResult(
                    task_id=task_id,
                    status="failed",
                    error=wait_result["error"],
                    agent_logs=wait_result["logs"],
                    metadata=dict(arm_metadata),
                )

            if wait_result["status"] == "crashed":
                # Agent container exited before sending callback - likely auth or startup error
                return BenchmarkResult(
                    task_id=task_id,
                    status="failed",
                    error=wait_result["error"],
                    agent_logs=wait_result["logs"],
                    metadata=dict(arm_metadata),
                )

            if wait_result["status"] == "timeout":
                return BenchmarkResult(
                    task_id=task_id,
                    status="timeout",
                    error=f"Agent did not complete within {self.build_timeout} seconds",
                    agent_logs=wait_result["logs"],
                    metadata=dict(arm_metadata),
                )

            # Success - got callback
            artifact_endpoint = wait_result["artifact_endpoint"]
            logger.info(f"Agent completed. Artifact submitted at: {artifact_endpoint}")

            # Recover the artifact container's name from its raw container-DNS URL
            # (before translation below rewrites it to localhost) so `finally:` can
            # remove it -- otherwise it's left running indefinitely after this run.
            # Keep the spec-declared name (set at step 4) if the URL names no container
            # (e.g. a localhost submission), so teardown still has a deterministic target.
            recovered_name = _docker_container_name_from_url(artifact_endpoint)
            if recovered_name:
                artifact_container_name = recovered_name

            # ================================================================================
            # 6. BUILD -> EVAL HANDOVER
            #
            # The build environment is torn down and the eval environment brought up in its place.
            # Every step below is ordered, and the order is the whole point (docs/task_env_split.md):
            #   a. snapshot the agent's work, then HALT its container -- it must be inert BEFORE the
            #      eval environment exists, because that environment comes up at the same container
            #      name and network alias the agent spent the build phase talking to;
            #   b. stop the user-sim (the agent is done talking to it either way);
            #   c. build env down, eval env up;
            #   d. redeploy the artifact -- AFTER the eval env is up, so it reconnects to something.
            # ================================================================================

            # 6a. Snapshot the agent's deliverable and filesystem while its container still exists,
            # then halt it. `docker cp` works on a stopped container but not a removed one, so
            # removal is deferred to `finally:`; halting is what makes the agent inert.
            if self.observability and trace_id:
                try:
                    agent_svc.snapshot_artifact(str(self.runs_dir / trace_id / "artifact"))
                    agent_svc.snapshot_workspace(
                        str(self.runs_dir / trace_id / "agent_fs" / "appsci_agent")
                    )
                    agent_snapshotted = True
                except Exception as e:
                    logger.warning(f"Failed to snapshot agent filesystem before handover: {e}")
            logger.info("Halting agent container before the eval environment starts...")
            try:
                agent_svc.halt()
            except Exception as e:
                # Not fatal, but say so loudly: a still-running agent container can reach the eval
                # environment at the address it used all through the build phase.
                logger.error(f"Failed to halt the agent container before the handover: {e}")

            # 6b. Stop appsci agent user sim (agent is done talking to user)
            #
            # GUARDED, BECAUSE THE NO-USER ARM NEVER STARTED ONE. `usersim` is bound `None` above and
            # only assigned under `if user_sim_enabled`, so in that arm it is still None here. The
            # unguarded call raised `AttributeError`, the outer handler turned it into
            # `status="failed"` with no scores, and it landed at the handover -- after the entire
            # build had been paid for. The snapshot below sat in a try/except, so it survived the
            # same None and only logged a misleading "failed to snapshot" warning.
            if usersim is not None:
                logger.info("Stopping appsci agent user sim...")
                # Snapshot the user-sim's files (its mounted /spec: spec.yaml + persona.yaml) to the
                # host BEFORE stop() removes the container, so the tracer can show what it had
                # access to. The cleanup-finally snapshot is only a crash-before-here fallback.
                if self.observability and trace_id:
                    try:
                        usersim.snapshot_files(
                            str(self.runs_dir / trace_id / "agent_fs" / "usersim")
                        )
                    except Exception as e:
                        logger.warning(f"Failed to snapshot user-sim files: {e}")
                usersim.stop()
                usersim = None  # Mark as stopped for cleanup
            else:
                logger.info("No-user arm: no stakeholder process to stop at the handover.")

            # BUILD-ONLY MODE. Stop here with the artifact built but unscored. The cleanup block below
            # still snapshots it into `runs/<trace>/artifact`, which is what the eval-only trials of
            # this build will deploy -- so this run's product is the artifact, not a score.
            #
            # `status="completed"` with no scores, deliberately: the build DID succeed, and calling it
            # anything else would make a build cell of a `builds:`/`trials:` experiment look like a
            # failed cell to the worker, which retries failures and would pay for the build twice.
            if skip_eval:
                logger.info("--skip-eval: artifact built, stopping before EVAL")
                return BenchmarkResult(
                    task_id=task_id,
                    status="completed",
                    agent_logs=agent_svc.get_logs() if agent_svc else None,
                    metadata={
                        "persona_path": str(persona_path_resolved) if persona_path_resolved else None,
                        "artifact_endpoint": artifact_endpoint,
                        "phase": "build_only",
                        "harness": harness.name if harness else "appsci_agent",
                        # Which arm this run was. Recorded because nothing else in a build-only
                        # result says so -- it carries no scores and no diagnostic -- and the trials
                        # scored off this build need to know which arm they are reporting on.
                        "user_sim_enabled": user_sim_enabled,
                        # Carried here too: a build-only cell whose agent explored a stale
                        # environment produced its artifact against the wrong records, and that
                        # has to stay identifiable in the trials scored off this build.
                        "task_env_reused_existing_container": env_reused_any_phase,
                    },
                )

            # 6c. Swap the task environment. The build env must be fully DOWN before the eval env goes
            # up: `docker compose up` under an existing project name attaches to whatever is already
            # running instead of recreating it, so a build env still standing would be silently reused
            # and the eval data would load on top of the build data with every surface reporting
            # healthy. TaskRunner compares container ids across the call to catch exactly that.
            logger.info("Tearing down the build task environment...")
            try:
                self.task_runner.stop(task_env)
            except Exception as e:
                # Log and carry on, which is what docs/task_env_split.md commits to for a handover
                # failure -- and the same treatment this call already gets in `finally:`. Aborting here
                # would throw away a whole completed build phase over a teardown hiccup, with no score
                # to show for it. Continuing is safe to reason about because the consequence is already
                # covered: if the build env is still standing, `compose up` adopts it instead of making
                # a fresh one, and start() reports that, which lands in the run record.
                logger.error(
                    "Failed to tear down the build task environment before the handover: %s. "
                    "Continuing -- if it is still up, the eval environment will come up on the same "
                    "container and the run record will say so.", e,
                )
            # Record a teardown descriptor before starting the eval env, so `finally:` can bring the
            # stack down even if that start throws part-way through.
            task_env = self.task_runner.teardown_descriptor(task_id)
            logger.info("Starting eval task environment...")
            eval_task_env = self.task_runner.start(task_id, phase=PHASE_EVAL)
            if eval_task_env is None:
                # The task declares its eval phase needs no environment (its eval inputs travel on
                # the cases themselves). Nothing is up, so nothing to tear down.
                logger.info("Task declares no eval environment; scoring without one.")
                task_env = None
            else:
                task_env = eval_task_env
                logger.info(
                    "Eval task environment ready: %s", task_env["workspace_container"]
                )
                if task_env.get("reused_existing_container"):
                    note_env_reused(
                        PHASE_EVAL,
                        "The eval task environment REUSED the build environment's container -- its "
                        "data is loaded on top of the build data. Scores from this run are not "
                        "trustworthy.",
                    )

            # 6d. Redeploy the artifact, now that the eval environment is up. This restarts it so
            # every connection it holds is re-established against the eval env (a pooled client would
            # otherwise be holding a dead socket), and it rebuilds from the artifact's final directory
            # so eval never scores code the agent replaced and abandoned.
            redeployed_endpoint, redeploy_detail = submission_server.redeploy_before_eval()
            logger.info("Pre-eval artifact redeploy: %s", redeploy_detail)
            if redeployed_endpoint:
                artifact_endpoint = redeployed_endpoint
                recovered_name = _docker_container_name_from_url(artifact_endpoint)
                if recovered_name:
                    artifact_container_name = recovered_name

            # The artifact runs in its OWN container on the bench network, and deploy_artifact
            # records a container-DNS endpoint (e.g. http://<artifact-container>:8080/respond) that
            # complete then reports to the orchestrator. The orchestrator runs on the host, which
            # can't resolve Docker DNS names, so translate the URL to the container's host-published
            # port. (A localhost URL is left unchanged -- but the deployed endpoint is a container-DNS
            # URL.) Done AFTER the redeploy, whose ephemeral host port is freshly assigned.
            artifact_endpoint = _translate_docker_url_to_host(artifact_endpoint)

            # 7. Run evaluator. The eval sequence (health-gate, history-aware client, user-sim
            # selection, per-sample reset hooks, timeouts, scoring) is factored into the module-level
            # evaluate_deployed() so the eval-only rerun pipeline runs the SAME code and scores
            # identically by construction (see docs/artifact_design.md).
            logger.info("Running evaluator...")

            # Under observability, register a global EVALUATOR publisher first so the eval path's
            # emit() calls (eval.start / eval.case_score / eval.complete / eval.elicitation) land in
            # THIS run's trace and appear in the tracer timeline right after the build events.
            if self.observability and trace_id:
                _register_eval_publisher(self.runs_dir, trace_id, task_id)

            report = evaluate_deployed(
                spec,
                artifact_endpoint,
                task_env,
                limit=eval_limit,
                query_timeout_s=self.QUERY_TIMEOUT_S,
                eval_timeout_s=self.EVAL_TIMEOUT_S,
                # Write the eval store STRAIGHT INTO the run dir rather than a temp dir copied out at
                # the end. The evaluator persists and re-reports per scored case, so this is what makes
                # that progress survivable: everything lands where the tracer reads and the S3 mirror
                # ships, as it happens. A run killed mid-eval used to leave its transcripts in
                # /tmp/appsci_eval_* on the worker host with the copy step never reached -- 80 scored
                # cases discarded because the copy was at the end of a path that never finished.
                workdir=(str(self.runs_dir / trace_id / "eval")
                         if self.observability and trace_id else None),
                # The build conversation the elicitation diagnostic scores. Only exists under
                # observability -- without it there is no trace directory and no event log, so the
                # diagnostic reports itself unmeasurable instead of inventing a source.
                #
                # In the NO-USER arm there is no conversation to find, and the difference from "we
                # could not read one" is the whole point: passing None there would report the run
                # unmeasurable, and passing an empty run dir would report a 100% MISSED rate against
                # an agent that was handed the same facts as files and had nothing to miss. Neither
                # is true, and the second would drag the arm's row in a results table to zero.
                build_transcript=(
                    no_stakeholder_transcript() if not user_sim_enabled
                    else (self.runs_dir / trace_id if self.observability and trace_id else None)
                ),
                # This run's own directory: its build phase is the one whose optimization loop the
                # diagnostic counts. Same availability condition -- without observability there is no
                # event log to count it from, and it reports itself unmeasurable.
                build_run_dir=(self.runs_dir / trace_id
                               if self.observability and trace_id else None),
                # How many copies of the (task-env, artifact) stack to spread the cases over. 1 is the
                # single stack this path has always used; above 1, `evaluate_deployed` stands the
                # extras up from the same artifact source the agent submitted and tears them down.
                eval_lanes=self.eval_lanes,
                task_config=task_config,
                artifact_dir=submission_server.artifact_dir_or_none(),
                tasks_dir=self.tasks_dir,
            )

            # Persist the eval report under the run dir so the tracer can serve/link it next to the
            # unified build+eval timeline (mirrors the artifact snapshot below).
            if self.observability and trace_id:
                self._persist_eval_report(report, trace_id)

            # Extract scores from report
            scores = self._extract_scores(report)


            # Persist AFTER the verdict is written into the store, so the copy carries it.
            if self.observability and trace_id:
                self._persist_eval_report(report, trace_id)
                # Diagnostic only -- see _maybe_build_redteam_trace's docstring for why its
                # artifact-axis scores are non-authoritative and independent of `scores` above.
                self._maybe_build_redteam_trace(trace_id, task_id, persona_path_resolved,
                                                user_sim_enabled=user_sim_enabled)

            logger.info(f"Evaluation complete. Scores: {scores}")

            return BenchmarkResult(
                task_id=task_id,
                status="completed",
                scores=scores,
                agent_logs=agent_svc.get_logs() if agent_svc else None,
                error=None,
                metadata={
                    "persona_path": str(persona_path_resolved) if persona_path_resolved else None,
                    "artifact_endpoint": artifact_endpoint,
                    # Which arm this run was. The elicitation diagnostic distinguishes the two
                    # (`no_stakeholder` vs a real rate), but nothing else in the result does, and a
                    # results table keyed on the scores alone would have no column for it.
                    "user_sim_enabled": user_sim_enabled,
                    # True when a phase's environment came up on a pre-existing container, so its data
                    # is not only what that phase loaded. Recorded rather than only logged because the
                    # run still returns a complete-looking score: without this, a contaminated cell is
                    # indistinguishable from a clean one once the logs are gone.
                    "task_env_reused_existing_container": env_reused_any_phase,
                    # How many copies of the task environment the cases were spread over. A condition
                    # of the measurement rather than a performance note: above 1, a case's neighbours
                    # ran in other artifact processes against other stores, so two scores are strictly
                    # comparable only at equal width.
                    "eval_lanes": self.eval_lanes,
                    "harness": harness.name if harness else "appsci_agent",
                },
            )

        except Exception as e:
            logger.exception(f"Benchmark run failed: {e}")
            # If the build was in flight when we failed, close out its span.
            emit_build_complete("failed", error=str(e))
            agent_logs = agent_svc.get_logs() if agent_svc else None
            return BenchmarkResult(
                task_id=task_id,
                status="failed",
                error=str(e),
                agent_logs=agent_logs,
                # Same reason as the arm labels on the early returns above, and this handler needs
                # them most: it is where an unexpected failure anywhere in the run lands, so it is
                # the one most likely to be the only record an arm-specific bug leaves behind.
                metadata={
                    "user_sim_enabled": user_sim_enabled,
                    "optimization_enabled": optimization_enabled,
                },
            )

        finally:
            # 8. Cleanup
            logger.info("Cleaning up...")

            # Snapshot the built artifact to the host BEFORE removing the
            # container, so it stays inspectable after the run (observability).
            # docker cp works on exited containers too, so this covers crash /
            # timeout outcomes as well as success.
            # Skipped when the handover already did this (success path); still needed when the run
            # crashed or timed out before reaching it.
            if self.observability and trace_id and agent_svc is not None and not agent_snapshotted:
                try:
                    artifact_dest = str(self.runs_dir / trace_id / "artifact")
                    agent_svc.snapshot_artifact(artifact_dest)
                except Exception as e:
                    logger.warning(f"Failed to snapshot artifact: {e}")
                # Also snapshot the agent container's filesystem (mounted inputs /data + /spec and
                # what it generated in /workspace + /work) so the tracer can browse everything the
                # agent had access to, not just the deliverable.
                try:
                    agent_fs_dest = str(self.runs_dir / trace_id / "agent_fs" / "appsci_agent")
                    agent_svc.snapshot_workspace(agent_fs_dest)
                except Exception as e:
                    logger.warning(f"Failed to snapshot agent workspace: {e}")

            # Record which per-deploy checkpoint (if any) matches the artifact this run submitted.
            # Done here rather than at the handover so a crashed or timed-out run is covered too, and
            # unconditionally because "none of them" is a real answer worth writing down: an agent that
            # edits after its last deploy submits something no checkpoint holds.
            _mark_submitted_checkpoint(self.runs_dir, run_tag)

            # Fallback user-sim snapshot: normally done in the main flow just before usersim.stop()
            # (where it's set to None), so this only fires if the run crashed before that point while
            # the user-sim container is still alive.
            if self.observability and trace_id and usersim is not None:
                try:
                    usersim_fs_dest = str(self.runs_dir / trace_id / "agent_fs" / "usersim")
                    usersim.snapshot_files(usersim_fs_dest)
                except Exception as e:
                    logger.warning(f"Failed to snapshot user-sim files: {e}")

            if agent_svc is not None:
                if harness is not None and run_tag is not None:
                    try:
                        # Stop the writer before finalizing the file; keep timestamped live output.
                        agent_svc.halt()
                        agent_svc.stop_harness_log_capture()
                        native_log = self.runs_dir / run_tag / "harness" / "native.log"
                        if not native_log.exists() or native_log.stat().st_size == 0:
                            from shared.observability.redact import scrub, secret_values

                            native_log.write_text(scrub(agent_svc.get_logs(), secret_values({
                                **os.environ, **(agent_extra_env or {}),
                                "APPSCI_USERSIM_TOKEN": agent_config.appsci_usersim_token,
                            })))
                    except Exception as e:
                        logger.warning(f"Failed to preserve native harness logs: {e}")
                try:
                    agent_svc.stop()
                except Exception as e:
                    logger.warning(f"Failed to stop agent: {e}")

            if usersim is not None:
                try:
                    usersim.stop()
                except Exception as e:
                    logger.warning(f"Failed to stop appsci agent user sim: {e}")

            if task_env is not None:
                try:
                    self.task_runner.stop(task_env)
                except Exception as e:
                    logger.warning(f"Failed to stop task environment: {e}")

            if submission_server is not None:
                try:
                    await submission_server.stop()
                except Exception as e:
                    logger.warning(f"Failed to stop submission server: {e}")

            # The artifact container is deployed by the agent itself (via its own tool calls, not by
            # anything the orchestrator started), so it isn't covered by agent_svc.stop()/usersim.stop()
            # above. Remove it by known name (complete succeeded) and/or via the network-diff
            # backstop (crash/timeout before completing) -- see _cleanup_artifact_containers.
            _cleanup_artifact_containers(
                artifact_container_name,
                artifact_network_baseline,
                task_env["network_name"] if task_env is not None else None,
            )

            if aggregator_server is not None:
                try:
                    await aggregator_server.stop()
                except Exception as e:
                    logger.warning(f"Failed to stop tracer server: {e}")

            if obs_publisher is not None:
                try:
                    obs_publisher.close()
                except Exception as e:
                    logger.warning(f"Failed to close observability publisher: {e}")

            # Close the global EVALUATOR publisher registered for the eval phase, if any.
            try:
                _close_eval_publisher()
            except Exception as e:
                logger.warning(f"Failed to close evaluator publisher: {e}")

            # Remove the per-run redacted agent-spec temp dir (agent container is stopped by now).
            if agent_spec_dir is not None:
                shutil.rmtree(agent_spec_dir, ignore_errors=True)

            logger.info("Cleanup complete")
            if trace_id:
                logger.info(f"Run data saved to: {self.runs_dir / trace_id}")

                # The build's optimization loop, counted out of the event logs -- which are complete
                # by now, since every publisher above is closed. Written here rather than only beside
                # the eval report because the runs where it is most worth reading are the ones that
                # never reach an eval: a `--skip-eval` build cell, where the build IS the phase the
                # loop ran in, and a run that died inside the loop. Before the S3 mirror below, so the
                # copy travels with the run.
                # `had_build_phase` is the eval-only test, and it is the same condition the
                # branch far above returns on: a trial deploys an artifact someone else built, so
                # there is no agent and no agent event log in THIS run directory.
                if self.observability:
                    self._persist_optimization_diagnostic(
                        trace_id, had_build_phase=artifact_dir is None)

                # Mirror to S3 last: every publisher above is now closed, so the per-source logs
                # are flushed and the events.jsonl the upload consolidates is complete. Runs in a
                # thread so a slow upload doesn't block this coroutine's event loop.
                if self.observability and self.s3_upload:
                    s3_uri = await asyncio.to_thread(
                        _mirror_run_to_s3, self.runs_dir, trace_id, task_id, self.s3_config
                    )
                    if s3_uri:
                        logger.info(f"Share it: view run id {trace_id} in any tracer with S3 access")

                view_port = _view_port_for_log(aggregator_server, actual_port, self.aggregator_port)
                logger.info(f"View with: python -m appsci_bench.tracer --port {view_port}")

    async def _wait_for_agent_completion(
        self,
        submission_server: ArtifactSubmissionServer,
        agent_svc: AppSciAgentContainerManager,
        timeout: int,
    ) -> dict[str, Any]:
        """Wait for agent submission OR detect early container crash.

        Races between:
        1. An artifact checkpoint that could not be captured (fatal -- outranks the rest)
        2. Submission received (success)
        3. Container exits without submission (crash/error)
        4. Timeout reached

        Args:
            submission_server: The submission server waiting for agent POST.
            agent_svc: The agent container manager.
            timeout: Maximum seconds to wait.

        Returns:
            Dict with status ('completed', 'crashed', 'timeout') and relevant data.
        """
        poll_interval = 2  # Check container status every 2 seconds
        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            remaining = timeout - elapsed

            if remaining <= 0:
                return {
                    "status": "timeout",
                    "logs": agent_svc.get_logs(),
                }

            # A checkpoint that could not be captured ends the run, and is checked BEFORE the
            # submission: the candidate is already unrecoverable, so continuing to eval would produce a
            # score alongside a candidate set that is silently short one entry.
            if submission_server.checkpoint_error is not None:
                return {
                    "status": "checkpoint_failed",
                    "error": submission_server.checkpoint_error,
                    "logs": agent_svc.get_logs(),
                }

            # Check if submission was already received
            if submission_server._artifact_endpoint is not None:
                return {
                    "status": "completed",
                    "artifact_endpoint": submission_server._artifact_endpoint,
                }

            # Container is no longer running and no submission arrived. Distinguish a clean exit
            # (code 0 -- the agent legitimately decided not to submit an artifact) from a genuine
            # crash (non-zero). appsci_agent/run.py returns 0 on the no-submission path, so treating
            # "not running" as "crashed" would misreport a normal outcome as a failure.
            if not agent_svc.is_running():
                exit_code = agent_svc.get_exit_code()
                logs = agent_svc.get_logs()

                if exit_code == 0:
                    logger.info("Agent exited cleanly (code 0) without submitting an artifact.")
                    return {"status": "no_submission", "exit_code": 0, "logs": logs}

                # Try to extract a meaningful error from logs
                error_msg = self._extract_error_from_logs(logs, exit_code)

                logger.error(f"Agent container exited unexpectedly (code={exit_code})")
                return {
                    "status": "crashed",
                    "exit_code": exit_code,
                    "error": error_msg,
                    "logs": logs,
                }

            # Wait a bit before checking again
            await asyncio.sleep(min(poll_interval, remaining))

    def _extract_error_from_logs(self, logs: str, exit_code: int | None) -> str:
        """Extract a meaningful error message from container logs.

        Looks for common error patterns (auth errors, import errors, etc.)
        and returns a user-friendly message.

        Args:
            logs: The container stdout/stderr logs.
            exit_code: The container's exit code.

        Returns:
            A descriptive error message.
        """
        if not logs:
            return f"Agent container crashed (exit code {exit_code}) with no logs"

        logs_lower = logs.lower()

        # Check for common error patterns
        if "authentication" in logs_lower or "401" in logs or "unauthorized" in logs_lower:
            return f"Agent container crashed: authentication error. Check your API key configuration. (exit code {exit_code})"

        if "authenticationerror" in logs_lower or "invalid api key" in logs_lower:
            return f"Agent container crashed: invalid API key. (exit code {exit_code})"

        if "connection refused" in logs_lower or "connectionerror" in logs_lower:
            return f"Agent container crashed: connection error. Check network and endpoint configuration. (exit code {exit_code})"

        if "modulenotfounderror" in logs_lower or "importerror" in logs_lower:
            return f"Agent container crashed: import error. Container image may be corrupted or incomplete. (exit code {exit_code})"

        # Try to find the last ERROR or Exception line
        for line in reversed(logs.split("\n")):
            if "ERROR" in line or "Exception" in line or "Error:" in line:
                # Truncate long lines
                truncated = line[:200] + "..." if len(line) > 200 else line
                return f"Agent container crashed: {truncated.strip()} (exit code {exit_code})"

        return f"Agent container crashed unexpectedly (exit code {exit_code}). Check agent_logs for details."

    async def _run_eval_only(
        self,
        *,
        task_id: str,
        spec: "TaskSpec",
        task_config: "TaskConfig",
        artifact_dir: Path,
        task_env: dict | None,
        trace_id: str | None,
        eval_limit: int | None,
        env_reused: bool,
        build_run_dir: Path | None = None,
        user_sim_enabled: bool = True,
    ) -> BenchmarkResult:
        """One eval trial: deploy an already-built artifact source dir and score it.

        Called from inside `run()`'s try block so the shared cleanup applies -- this method starts
        nothing it has to tear down itself except the artifact container, whose name `run()` recorded
        before calling.

        This is the unit a `trials: k` experiment queues k times against one build. Each trial deploys
        from source into a freshly started EVAL task-env, which is what makes the k trials
        exchangeable -- the property pass^k's estimator assumes. Scoring a build's own live container
        instead would make trial 0 different from trials 1..k-1: it would run against an environment
        the agent had been using for two hours.

        `task_env` is the EVAL phase's environment, or None for a task that declares it needs none.
        `env_reused` says that environment came up on a container that already existed, so it holds
        whatever the last run left in it. Required rather than defaulted, because the safe-looking
        default is the wrong one: a caller that forgot it would record a clean environment for a trial
        that may have run on a poisoned one, and nothing would fail. It is copied into the result
        metadata for a reader who has the object in hand; what makes it survive the run is the
        `task_env.reused` event `note_env_reused` emits, since this metadata dict is not persisted.

        The artifact dir's existence is checked by the caller, before any container starts.
        """
        logger.info(f"Eval-only: deploying artifact from {artifact_dir} ...")
        endpoint, _container, error = deploy_artifact_dir(task_config, artifact_dir)
        if error or not endpoint:
            # Deploy is not the thing under test: a failed deploy is reported as a failed cell (which
            # the worker will retry) rather than as an artifact that scored zero.
            return BenchmarkResult(
                task_id=task_id,
                status="failed",
                error=f"artifact deploy failed: {error}",
            )
        logger.info(f"Eval-only: artifact deployed, scoring at {endpoint} ...")

        # Same publisher registration as the build+eval path, so a trial's eval events land in its own
        # trace and the tracer renders it like any other run.
        if self.observability and trace_id:
            _register_eval_publisher(self.runs_dir, trace_id, task_id)

        report = evaluate_deployed(
            spec,
            endpoint,
            task_env,
            limit=eval_limit,
            query_timeout_s=self.QUERY_TIMEOUT_S,
            eval_timeout_s=self.EVAL_TIMEOUT_S,
            # As on the build+eval path: score straight into the run dir so per-case progress is
            # durable and mirrored as it happens. An eval trial is the LONGEST-running kind of cell
            # (a whole dataset with no build in front of it), so it is the one that most needs this.
            workdir=(str(self.runs_dir / trace_id / "eval")
                     if self.observability and trace_id else None),
            # The BUILD's stakeholder conversation, from the run that produced this artifact -- a
            # trial has no build phase of its own. Without it the elicitation diagnostic reports
            # `unmeasurable`, and since a build cell never scores at all (`--skip-eval`), a
            # `builds`/`trials` sweep computed elicitation NOWHERE while a plain build+eval run still
            # did. None is still honest rather than fatal: the diagnostic says so and no other metric
            # depends on it.
            #
            # A trial of a NO-USER build is the case that has to be named rather than left to the
            # reader: that build had no conversation to record, so its run dir yields `unreadable`
            # and the trial would report `unmeasurable` -- "we could not find the conversation",
            # which is wrong and looks like a plumbing fault. This is the only place a sweep computes
            # elicitation at all, so getting it wrong here is getting the whole arm's column wrong.
            build_transcript=(
                no_stakeholder_transcript() if not user_sim_enabled else build_run_dir
            ),
            # A trial has no optimization loop of its own: the loop ran in the build that produced
            # this artifact, and that build's run dir is what records it. Passing it means every
            # trial of one build reports the same optimization figures, which is correct -- they are
            # facts about the artifact's provenance, not about the trial.
            build_run_dir=build_run_dir,
            # An eval trial is the longest-running cell there is -- a whole dataset and no build in
            # front of it -- so it is where extra stacks buy the most. The artifact source is already
            # in hand here, which is what makes replicating it possible.
            eval_lanes=self.eval_lanes,
            sample_ids=self.sample_ids,
            rerun_source=self.rerun_source,
            rerun_source_sha256=self.rerun_source_sha256,
            allow_config_change=self.rerun_allow_config_change,
            task_config=task_config,
            artifact_dir=artifact_dir,
            tasks_dir=self.tasks_dir,
        )

        if self.observability and trace_id:
            self._persist_eval_report(report, trace_id)

        scores = self._extract_scores(report)
        logger.info(f"Eval-only complete. Scores: {scores}")
        return BenchmarkResult(
            task_id=task_id,
            status="completed",
            scores=scores,
            metadata={
                "artifact_dir": str(artifact_dir),
                "artifact_endpoint": endpoint,
                "phase": "eval_only",
                # The same key the build-only path records. NOT the durable record of this -- nothing
                # persists or reads this dict; the `task_env.reused` event is what a sweep queries.
                "task_env_reused_existing_container": env_reused,
                # How many copies of the task environment the cases were spread over; the durable copy
                # of this is `eval_lanes` on the eval report.
                "eval_lanes": self.eval_lanes,
            },
        )

    def _persist_eval_report(self, report, trace_id: str) -> None:
        """Copy the eval report + per-case transcripts from the eval store into this run's dir
        (runs/<trace_id>/eval/) so the tracer can natively re-render the Eval tab beside the
        build+eval timeline. report.json supplies metrics + judge metadata; the conversations/ dir
        supplies the per-case turns + LLM traces + final_state (report.json has no transcripts).
        Best-effort: a copy failure is logged, never fatal to the run."""
        dest = self.runs_dir / trace_id / "eval"
        try:
            copied = persist_eval_report(report, dest)
        except Exception as e:
            logger.warning(f"Failed to persist eval report: {e}")
            return
        if copied:
            logger.info(f"Eval data persisted to {dest} ({', '.join(copied)})")

    def _persist_optimization_diagnostic(self, trace_id: str, *, had_build_phase: bool) -> None:
        """Write `runs/<trace>/optimization.json`: how this build's optimization loop went.

        The same diagnostic the eval phase attaches to its report, written for every run that BUILT
        something so it also exists for one that never gets an eval. Nothing is judged and no model is
        called -- it counts records this run already wrote (see
        `appsci_bench/eval/build_optimization.py`) -- so this costs nothing and cannot depend on
        anything still running.

        `had_build_phase` is False for an eval-only trial, and then nothing is written at all. A trial
        deploys an artifact some earlier run built: its own run directory holds no agent event log, so
        counting over it produces `unmeasurable` -- "we could not see the loop" -- about a run that
        had no loop to see. That file would land beside `<trial>/eval/optimization.json`, which
        carries the real figures read from the BUILD's run directory, leaving one run holding two
        files of the same name with opposite answers and the wrong one on top. The trial's copy of the
        diagnostic is the eval one; this path has nothing to add to it.

        Best-effort, same posture as `_persist_eval_report`: a diagnostic must never be the thing that
        fails a run.
        """
        if not had_build_phase:
            logger.info(
                "Eval-only run %s: no build phase here, so the optimization diagnostic is the one "
                "the eval report carries for the build that produced this artifact.", trace_id)
            return
        run_root = self.runs_dir / trace_id
        try:
            result = read_optimization(run_root)
            (run_root / "optimization.json").write_text(
                json.dumps(result.to_dict(), indent=2))
        except Exception as e:
            logger.warning(f"Failed to write the optimization diagnostic for {trace_id}: {e}")
            return
        logger.info(
            "Optimization diagnostic (%s): %d optimize_agent call(s), %s cycle(s), stop reason %s, "
            "internal eval %s -> %s, %d change(s) kept and %d rolled back",
            result.status, result.calls, result.cycles_used, result.stop_reason,
            result.internal_eval_first, result.internal_eval_final,
            result.changes_kept, result.changes_rolled_back,
        )

    def _maybe_build_redteam_trace(self, trace_id: str, task_id: str, persona_path: Path | None,
                                   *, user_sim_enabled: bool = True) -> None:
        """If `persona_path` declares any `adversarial_pressures`, assemble a red-team trace from
        this run's own logs (appsci_bench/eval/redteam_trace.py) and score it with the existing,
        untouched scripts/score_redteam_trace.py -- so a red-team run's trace is produced the same
        way every time, rather than by someone hand-reformatting logs after the fact.

        Called once, after the SECOND `_persist_eval_report` (the one after the verdict is written
        into the store), so `eval/conversations/` reflects the run's final state before this reads it.
        Best-effort and diagnostic, same posture as `_persist_eval_report`: never fails the run, and
        a normal cooperative-persona run is a silent no-op (assemble_trace_dir returns None). So is
        a no-user run, whatever persona it is bound to (including none at all, which is the usual
        case now that the arm does not ask for one) -- `user_sim_enabled` is forwarded because
        this arm can legitimately carry an adversarial persona, and scoring a conversation that
        never happened produced a verdict saying the agent resisted.

        This is a SEPARATE, non-authoritative diagnostic pass, not a second source of truth: the two
        artifact-safety axes the scorer computes here are already scored per eval case at weight 0.0
        via `spec.yaml` (#155). That per-case pass and this one (over a concatenation of every case)
        are independent judge calls and can disagree -- neither overrides the other, and this run's
        recorded score for those axes still comes from the weighted per-case path, never from
        `redteam/verdict.json`.

        The two artifact-axis judge calls below happen inside the scorer's own subprocess, not in this
        process, so they never reach this run's model-call publisher singleton (`_register_eval_
        publisher`/`init_publisher` is process-local) and are excluded from the run's usage/model-call
        accounting -- unlike the in-process pressure-tagging calls `assemble_trace_dir` already made
        above, which do get recorded there.
        """
        try:
            trace_dir = assemble_trace_dir(
                self.runs_dir / trace_id, task_id, trace_id, persona_path,
                dest=self.runs_dir / trace_id / "redteam",
                user_sim_enabled=user_sim_enabled,
            )
        except Exception as e:
            logger.warning(f"Failed to assemble red-team trace: {e}")
            return
        if trace_dir is None:
            return  # cooperative persona, or no stakeholder at all -- nothing to score
        try:
            result = subprocess.run(
                [sys.executable, "scripts/score_redteam_trace.py", str(trace_dir)],
                cwd=REPO_ROOT, check=False, timeout=600, capture_output=True, text=True,
            )
        except Exception as e:
            logger.warning(f"Failed to score red-team trace at {trace_dir}: {e}")
            return
        # check=False means a non-zero exit does not raise -- it must be inspected explicitly, or a
        # scorer failure (a malformed trace, sys.exit before verdict.json is ever written) logs as a
        # success with nothing on disk to show for it. Checking verdict.json too, not just the exit
        # code, is belt-and-suspenders against a future scorer change that exits 0 without writing it.
        verdict_path = trace_dir / "verdict.json"
        if result.returncode != 0 or not verdict_path.exists():
            logger.warning(
                f"Red-team scoring failed for {trace_dir} (exit {result.returncode}, "
                f"verdict.json {'present' if verdict_path.exists() else 'missing'}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            return
        logger.info(f"Red-team trace assembled and scored at {trace_dir}")

    def _extract_scores(self, report) -> dict[str, Any]:
        """Extract a flat {objective: score} dict from an EvalReport.

        The evaluator aggregates one score per objective into
        ``report.views["per_metric"]`` (each row: objective/score/weight/positive)
        plus a weighted ``report.final_score``. NOTE: EvalReport has no
        ``metric_results`` attribute -- reading that (as this did) silently
        yielded empty scores even when the eval ran fine.

        Reads the aggregate `views["per_metric"]` (one entry per objective, keys `objective`/`score`)
        and the task-wide `final_score`. A metric whose score is None (never applied to any data
        point -- design: Task-Specific Evaluation Function) is skipped rather than reported as 0.

        Args:
            report: The evaluation report from the evaluator.

        Returns:
            Dict mapping objective names to scores, plus "final_score".
        """
        scores: dict[str, Any] = {}
        views = getattr(report, "views", None) or {}
        for row in views.get("per_metric", []):
            name, score = row.get("objective"), row.get("score")
            if name is not None and score is not None:
                scores[name] = score
        if getattr(report, "final_score", None) is not None:
            scores["final_score"] = report.final_score
        return scores

    async def run_all_personas(
        self,
        task_id: str,
        *,
        appsci_usersim_model: str | None = None,
        agent_model: str | None = None,
    ) -> list[BenchmarkResult]:
        """Run the benchmark for all personas defined in the task spec.

        Args:
            task_id: The task identifier.
            appsci_usersim_model: Optional LLM model for the appsci agent user sim.
            agent_model: Optional LLM model for the agent.

        Returns:
            List of BenchmarkResults, one per persona.
        """
        spec = load_task_spec(task_id)
        results = []

        for persona_path in spec.user_personas:
            logger.info(f"Running with persona: {persona_path}")
            result = await self.run(
                task_id=task_id,
                persona_path=Path(persona_path),
                appsci_usersim_model=appsci_usersim_model,
                agent_model=agent_model,
            )
            results.append(result)

        return results



def deploy_artifact_dir(
    task_config: "TaskConfig", artifact_dir: Path, *,
    workspace_container: str | None = None,
    container_name: str | None = None,
    image_name: str | None = None,
    build: bool = True,
) -> tuple[str | None, str | None, str | None]:
    """Stand an artifact SOURCE dir up as a container and return `(host_endpoint, container, error)`.

    A third caller of the shared deploy capability (see `evaluate_deployed` for callers A and B): the
    agent's `/deploy` route, the eval-only CLI, and now an eval-only *cell* of an experiment, which
    scores one build's artifact k times to produce a pass^k curve. All three use the same
    `DockerArtifactDeployer` with the same spec-derived container name / port / network and the same
    injected env, so a trial stands the artifact up the way the build did rather than the way this
    function happened to be written.

    Parallel eval is a fourth caller and the only one that overrides the defaults: it stands one
    artifact up once per copy of the task environment, so it names each container itself, points each
    at its own `workspace_container`, and builds the image only for the first.

    Args:
        workspace_container: Which task environment this artifact should talk to. Defaults to the
            task's single (copy 0) environment.
        container_name: Name for the artifact container. Defaults to the spec-declared one.
        image_name: Tag to build/run. Defaults to `<container_name>:latest`, so two containers under
            different names never race on one tag. Parallel eval passes ONE tag for all its copies
            together with `build=False`, because they are the same artifact directory and rebuilding
            it per copy would be several cache-warm rebuilds for an identical image.
        build: Whether to build the image. False reuses a tag a previous call already built.

    Returns rather than raises, and returns the container name even on failure, because every caller
    has to clean up a container that may exist despite a failed health check -- the deployer starts
    the container before the artifact is necessarily answering.
    """
    agent_env = resolve_agent_env(task_config, workspace_container=workspace_container)
    name = container_name or task_config.artifact_container
    # The network comes from the SPEC, not from a running environment dict. A task whose eval phase
    # needs no environment (`eval: none`) has no dict to read, and indexing one crashed every caller
    # here with a TypeError. Both phases share one network anyway, so the spec is the authoritative
    # source in every case -- which is also why this function no longer takes the environment at all.
    deployer = DockerArtifactDeployer(
        artifact_dir=artifact_dir,
        image_name=image_name or f"{name}:latest",
        container_name=name,
        network=task_config.network_name,
        port=task_config.artifact_port,
        # The artifact is itself an LLM agent, so it needs the same model credentials the harness runs
        # with, plus the task's own env (tool-API URLs/tokens). Task env wins on a key clash; the
        # deployer injects PORT last, so nothing here can shadow it.
        env={**llm_credential_env(), **agent_env},
        build=build,
    )
    result = deployer.deploy()
    if not result.success:
        return None, name, result.error
    # The deployer returns a container-DNS URL and publishes the port to a random host port; the
    # evaluator runs on the host and cannot resolve Docker DNS, so translate before scoring.
    return (
        _translate_docker_url_to_host(result.endpoint_url),
        result.container_name or name,
        None,
    )

def persist_eval_report(report, dest_dir: Path) -> list[str]:
    """Copy an eval report + per-case transcripts out of the eval store into a durable dir.

    Both callers want them somewhere stable and discoverable: the orchestrator copies to
    ``runs/<trace>/eval/``, and the eval-only CLI to ``<artifact_dir>/../eval/`` -- which, for a run's
    ``artifact/``, is that same ``runs/<trace>/eval/``, exactly where the tracer looks for a native
    Eval tab. Returns the list of copied entries (empty when the report has no ``store_root``).
    Raises on a copy failure -- callers wrap it best-effort.

    NOTHING TO COPY IS THE NORMAL CASE NOW. Under observability the evaluator is pointed at
    ``runs/<trace>/eval/`` as its store root, so it writes the report and each case's transcript there
    as the run proceeds and this returns early. The copy path remains for the store roots that are
    still temp dirs -- a run without observability, and any caller that passes no ``workdir`` -- so a
    finished run of either shape ends up with the same layout."""
    import shutil

    store_root = getattr(report, "store_root", None)
    if not store_root:
        return []
    # Same directory: the evaluator wrote straight here, so there is nothing to move and a copy would
    # be `shutil.copy2(x, x)` -- a SameFileError, i.e. a warning about a run that did the right thing.
    if Path(store_root).resolve() == dest_dir.resolve():
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    # elicitation.json and optimization.json ride along with the report. They exist so a consumer that
    # wants only one diagnostic -- a sweep comparing elicitation, or one asking how often the
    # optimization loop saturated -- need not parse a whole report to reach it, and leaving them in the
    # temp store would defeat that on the one path that matters.
    for name in ("report.html", "report.json", "elicitation.json", "optimization.json",
                 TranscriptStore.PROGRESS):
        src = Path(store_root) / name
        if src.exists():
            shutil.copy2(src, dest_dir / name)
            copied.append(name)
    # Per-case transcripts (turns + response.trace + final_state) live in conversations/, alongside
    # report.json -- the layout the tracer's native Eval tab (_load_conversations) expects. results/
    # carries the same cases' scores, which is what makes a killed run's progress readable.
    for sub in ("conversations", "results", "infrastructure_attempts"):
        src = Path(store_root) / sub
        if src.is_dir():
            shutil.copytree(src, dest_dir / sub, dirs_exist_ok=True)
            copied.append(f"{sub}/ ({len(list(src.glob('*.json')))})")
    return copied


def evaluate_deployed(
    spec: "TaskSpec",
    endpoint_url: str,
    task_env: dict | None,
    *,
    limit: int | None = None,
    sample_seed: int | None = None,
    sample_ids: list[str] | None = None,
    rerun_source: str | None = None,
    rerun_source_sha256: str | None = None,
    allow_config_change: bool = False,
    query_timeout_s: float = BenchmarkOrchestrator.QUERY_TIMEOUT_S,
    eval_timeout_s: float = BenchmarkOrchestrator.EVAL_TIMEOUT_S,
    workdir: str | None = None,
    # BuildTranscript widens this beyond a path: the no-user arm passes an already-decided
    # transcript (`no_stakeholder_transcript()`) rather than a directory to read a conversation out
    # of, because in that arm there was no conversation and "unreadable" would be the wrong verdict.
    build_transcript: "str | Path | BuildTranscript | None" = None,
    build_run_dir: str | Path | None = None,
    eval_lanes: int = 1,
    task_config: "TaskConfig | None" = None,
    artifact_dir: Path | None = None,
    tasks_dir: Path | None = None,
):
    """Score a deployed artifact endpoint against the task's single evaluation_data block.

    This is the ONE eval sequence shared by both callers -- the orchestrator's completed-submission
    branch and the standalone eval-only pipeline -- so a rerun reproduces the orchestrator's score by
    construction rather than by hope (see docs/artifact_design.md). From `spec` + `task_env` it derives
    identically on both paths:
      - a health-gated, history-aware `HttpArtifactClient` over `/respond` (NOT the lossy single-shot
        bridge that collapses multi-turn tasks);
      - `ArtifactAgentUserSim` iff `evaluation_data.type == "multi_turn"`, else no user sim;
      - per-sample env reset hooks iff `evaluation_data.reset_env_per_sample` is set and the task-env
        admin token is recoverable, else none;
      - the same query/eval timeouts and conformance probe the orchestrator uses.

    Observability publisher registration and report persistence are intentionally NOT here -- the
    orchestrator wraps this call with them; the eval-only pipeline doesn't need them.

    Args:
        spec: The loaded task spec (carries the evaluation_data block).
        endpoint_url: Host-reachable artifact URL (already translated via `docker port` for the
            orchestrator path; the eval-only pipeline passes its own translated URL).
        task_env: The running EVAL task environment dict (workspace_container +
            env_admin_token_var), used only for per-sample reset. Must be the eval phase's
            environment: the admin token is read off that container, so the build phase's dict would
            point reset at a container that no longer exists by the time scoring runs. Pass None when
            the task doesn't reset per sample, or declares no eval environment.
        limit: Optional debug cap on how many data points to score (the orchestrator's eval_limit).
            None scores the full block. Without `sample_seed` the cap keeps the first N in dataset
            order.
        sample_seed: With `limit`, choose WHICH data points by seeded random draw instead of taking the
            first N -- see `eval_system.subsample` for why dataset order is not a neutral way to take
            fewer. Alone it does nothing (the cap is what drops cases). A seeded score is comparable
            only with other scores carrying the same limit AND seed, not with an orchestrator score at
            the same limit.
        query_timeout_s: Per-turn ceiling; an agentic artifact runs its own tool loop per /respond.
        eval_timeout_s: Per-objective scoring ceiling.
        workdir: Durable directory to write transcripts + report into (e.g. runs/<trace>/eval). When
            None the evaluator mints a throwaway tempfile store (the orchestrator copies out of it
            afterward); pass it to write straight to the durable location and skip the tmp round-trip.
        build_transcript: The run directory holding this run's build-time user-sim event log (e.g.
            runs/<trace>), scored by the standalone elicitation diagnostic. Safe to pass here because
            the user-sim container is stopped before eval begins and the log is a host bind-mount, so
            it is complete on disk by now. None -- which is what an observability-off run has, since
            there is no trace directory -- reports the diagnostic as unmeasurable, correctly.
        build_run_dir: The run directory of the BUILD that produced this artifact, read by the
            optimization diagnostic (the agent's event log + the run's checkpoint manifest). The same
            directory as `build_transcript` on a benchmark run; a separate argument because that one
            also accepts a bare transcript file, which carries no event log. Both the agent's log and
            the manifest are complete on disk before eval starts -- the agent container is halted at
            the handover -- so reading them here scores the loop that actually ran.
        eval_lanes: How many COPIES of the (task-env, artifact) stack to score against, so that many
            cases run at once. 1 (the default) is the single stack every run has always used, and
            starts nothing extra. Above 1 the extra copies are started and torn down here; the caller
            must also pass `task_config`, `artifact_dir` and `tasks_dir`, since there is nothing to
            replicate without the artifact source and the task's spec.
        task_config: The task's parsed `task_runner` block. Required above `eval_lanes=1`.
        artifact_dir: The artifact SOURCE directory. Required above `eval_lanes=1`.
        tasks_dir: Where task specs live. Required above `eval_lanes=1`.

    Returns:
        The evaluator's report (same object the orchestrator feeds to _extract_scores).
    """
    # Health-gate the endpoint before scoring (same probe the orchestrator used inline).
    artifact = HttpArtifact(endpoint_url=endpoint_url)
    artifact.wait_for_health()

    eval_type = (spec.evaluation_data or {}).get("type")
    resets_env = bool((spec.evaluation_data or {}).get("reset_env_per_sample"))

    def make_lane(client, lane_env: dict | None, label: str) -> EvalLane:
        """Bind one lane's artifact client to one lane's environment.

        Every per-environment capability is wired here and nowhere else, so lane 0 and the extra
        copies are constructed by the same code and cannot drift into scoring differently. Passed to
        the lane pool as a callback for exactly that reason.
        """
        setup_state, read_final_state = _env_reset_hooks_for(lane_env, resets_env)
        step_budget_factory = cancel_query = None
        if (spec.evaluation_data or {}).get("max_steps") is not None:
            from .env_admin import make_env_admin_request
            from .eval_step_budget import make_step_budget_hooks
            if not lane_env or not resets_env or task_config is None:
                raise RuntimeError("max_steps requires an isolated task environment and managed artifact")
            container = lane_env["workspace_container"]
            admin_var = lane_env.get("env_admin_token_var")
            admin_token = _docker_env_value(container, admin_var) if admin_var else None
            if not admin_token:
                raise RuntimeError("max_steps requires the environment admin token")
            index = int(label.removeprefix("lane-"))
            artifact_name = task_config.artifact_container + (f"-l{index}" if index else "")
            step_budget_factory, cancel_query, check_ready = make_step_budget_hooks(
                make_env_admin_request(container, admin_token), artifact_name, client,
                task_config.artifact_port)
            reset_state = setup_state

            def setup_state(sample):
                check_ready()  # never reset/open a DB while an earlier artifact is still running
                reset_state(sample)
        return EvalLane(
            client=client,
            setup_state=setup_state,
            read_final_state=read_final_state,
            user_sim=_user_sim_for(eval_type, lane_env),
            label=label,
            step_budget_factory=step_budget_factory,
            cancel_query=cancel_query,
        )

    with eval_lane_pool(
        task_config=task_config,
        tasks_dir=tasks_dir,
        width=eval_lanes,
        endpoint_url=endpoint_url,
        task_env=task_env,
        artifact_dir=artifact_dir,
        make_lane=make_lane,
        query_timeout_s=query_timeout_s,
    ) as lanes:
        lane0 = lanes.lanes[0]
        evaluator = Evaluator()
        # Lane 0's client and simulator are passed positionally as well as inside the pool: the
        # evaluator uses them for the things that are about the RUN rather than about a case -- the
        # user-sim parity note on the report, and the legacy artifact adapter.
        return evaluator.evaluate(
            spec, lane0.client, user_sim=lane0.user_sim, sample_seed=sample_seed,
            **({"sample_ids": sample_ids, "rerun_source": rerun_source,
                "rerun_source_sha256": rerun_source_sha256,
                "allow_config_change": allow_config_change} if rerun_source else {}),
            lanes=lanes,
            query_timeout_s=query_timeout_s, eval_timeout_s=eval_timeout_s, limit=limit,
            workdir=workdir, build_transcript=build_transcript, build_run_dir=build_run_dir,
        )


def _user_sim_for(eval_type: str | None, lane_env: dict | None):
    """The eval-time customer simulator for one lane, or None for a single-turn task.

    A task whose environment can execute CUSTOMER-held tools declares the tool-token var, and the sim
    gets an executor for them. Without it the sim can only talk, and any case whose golden outcome
    depends on the customer acting is unreachable regardless of the artifact's advice. Absent (every
    task but banking today) -> None -> the sim behaves exactly as it always has.

    Built per lane because the executor reaches ONE container: a sim shared across lanes would have
    the customer acting on lane 0's store while the artifact worked on lane 2's.

    NOTE the signatures of those same customer-held tools are NOT wired here. The executor is a live
    capability of the running environment, so only the run can supply it; a signature is static
    per-case data and rides in the sample itself (`multi_turn_input.user_tools`, see
    `datatypes.UserTool`). That keeps every per-case fact about this simulator in one place and keeps
    the build phase -- which starts the same task-env and has no customer simulator at all -- from
    carrying a field it would never read.
    """
    if eval_type != "multi_turn":
        return None
    user_tool_executor = None
    if lane_env is not None:
        tool_var = lane_env.get("env_tool_token_var")
        if tool_var:
            container = lane_env["workspace_container"]
            tool_token = _docker_env_value(container, tool_var)
            if tool_token:
                user_tool_executor = _make_user_tool_executor(container, tool_token)
                logger.info(f"User-sim tool execution enabled via docker exec into {container}")
            else:
                logger.warning(
                    f"task declares env_tool_token_var={tool_var!r} but {container} does not expose "
                    "it -- the user sim cannot call customer-held tools, so cases needing one will "
                    "not reach their golden state"
                )
    return ArtifactAgentUserSim(tool_executor=user_tool_executor)


def _env_reset_hooks_for(lane_env: dict | None, resets_env: bool):
    """`(setup_state, read_final_state)` for one lane, or `(None, None)`.

    Tasks that opt in via `evaluation_data.reset_env_per_sample` get their task-env DB reset to a
    fresh fixture before each case and its state hash read after, so an eval function can score the
    resulting DB state. We reach the containerized env's admin API by `docker exec` (its port isn't
    host-published).

    Bound to ONE container, which is precisely why a lane has its own pair: the reset and the hash are
    whole-store operations, so a second case sharing them would wipe this one's work mid-conversation.
    """
    if not resets_env or lane_env is None:
        return None, None
    container = lane_env["workspace_container"]
    admin_var = lane_env.get("env_admin_token_var")
    admin_token = _docker_env_value(container, admin_var) if admin_var else None
    if admin_token:
        logger.info(f"Per-sample env reset enabled via docker exec into {container}")
        return _make_env_reset_hooks(container, admin_token)
    if not admin_var:
        logger.warning(
            "reset_env_per_sample is set but task_runner.env_admin_token_var is not "
            "configured; DB component will not be measured"
        )
    else:
        logger.warning(
            f"reset_env_per_sample set but env var {admin_var!r} not found on "
            f"{container}; DB component will not be measured"
        )
    return None, None


def _install_shutdown_handler() -> None:
    """Make SIGTERM cancel this task, so `run()`'s `finally:` teardown actually runs.

    `run()`'s `finally:` is the only thing that stops this run's containers -- the agent, the user
    sim, the task environment, and the artifact container the agent deployed itself. Default SIGTERM
    terminates the process outright, so none of it executed: measured against a coroutine of exactly
    this shape (`try` / `except Exception` / `finally` with `await`s in it), an unhandled SIGTERM ran
    *zero* lines of the `finally:`. Every container the run had started was left up on the host, and
    on the shared jobs box that means still holding a gateway credential and still spending.

    Cancelling the main task instead raises `CancelledError` at the current `await`. That is a
    BaseException, so it passes straight through `run()`'s `except Exception` and executes the
    `finally:` -- including the `await`s inside it, which still work after cancellation because the
    cancellation is only delivered once.

    SIGINT is deliberately left alone. `asyncio.run`'s runner already installs exactly this
    behaviour for it (first SIGINT cancels the main task, a second raises `KeyboardInterrupt`), and
    that second one is the operator's escape hatch from a teardown that has itself hung. Taking
    SIGINT over here would remove it. The queue worker signals this process with SIGINT for the same
    reason -- see `CHILD_GRACEFUL_SIGNAL` in appsci_bench/experiments/worker.py.

    Caller must be inside a running loop, on the main thread (i.e. `main()`).
    """
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    state = {"cancelled": False}

    def _cancel_for_shutdown() -> None:
        # One-shot: a second SIGTERM would land inside the teardown's own `await`s and abandon it
        # part-way, which is the container leak this handler exists to prevent. The hard bound is
        # the SIGKILL that follows from systemd's TimeoutStopSec (or from the queue worker's own
        # escalation), not a repeated signal from the operator.
        if state["cancelled"]:
            logger.warning("Already shutting down; ignoring SIGTERM so cleanup can finish.")
            return
        state["cancelled"] = True
        logger.warning("SIGTERM received -- cancelling the run so its containers are torn down.")
        if main_task is not None:
            main_task.cancel()

    loop.add_signal_handler(signal.SIGTERM, _cancel_for_shutdown)


async def main() -> None:
    """CLI entry point for running the benchmark orchestrator."""
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(description="Run appsci benchmark")
    parser.add_argument("--task", required=True, help="Task ID (e.g., order_to_cash)")
    # NOT required, because two modes have no stakeholder to bind one to: --no-user-sim (there is no
    # simulator at all) and --artifact-dir (an artifact is scored against the task's eval dataset, not
    # a persona). Requiring it there made the caller name a file the run then ignored, which reads as
    # though the level mattered. Checked below instead, so the message can say which mode is missing
    # what rather than argparse's bare "the following arguments are required".
    parser.add_argument("--persona", help="Path to persona YAML. Required unless --no-user-sim or "
                                          "--artifact-dir is given, neither of which binds one.")
    parser.add_argument("--tasks-dir", default="tasks", help="Path to tasks directory")
    parser.add_argument("--harness-config", help="YAML selecting an external harness image and command; "
                        "omitted uses the reference AppSci Agent")
    parser.add_argument("--appsci-usersim-model", help="LLM model for the appsci agent user sim")
    parser.add_argument(
        "--agent-model",
        default=(os.getenv("AGENT_MODEL") or "").strip() or None,
        help="LLM model for agent (default: $AGENT_MODEL, else the agent's own built-in default). "
             "Whatever this resolves to is what the run id and the run record name, and it is what "
             "the credential preflight checks -- so `export AGENT_MODEL=...` selects the model for "
             "every run in that shell rather than being silently ignored.",
    )
    parser.add_argument(
        "--agent-temperature",
        type=float,
        default=None,
        help="Sampling temperature for BOTH build-side agents (the interviewing agent and its "
             "mini-swe sub-agent). Omitted => no temperature is sent, i.e. the provider default, "
             "which is how every run before this flag existed behaved. This is the build-side "
             "replicate control: the harness has no seed, so temperature is what makes N builds of "
             "one configuration samples of a distribution rather than N attempts at one intention.",
    )
    parser.add_argument(
        "--optimize-cycles",
        type=int,
        default=(int(os.environ["AGENT_OPTIMIZE_CYCLES"])
                 if (os.environ.get("AGENT_OPTIMIZE_CYCLES") or "").strip().lstrip("-").isdigit()
                 else None),
        help="Cap how many optimization cycles the agent's `optimize_agent` tool may spend improving "
             "its candidate. A cycle is one attempt to CHANGE the candidate -- a mini-swe run plus a "
             "rebuild and two eval runs -- so this bounds the most expensive part of a build; a call "
             "that only measures costs nothing. Omitted => no ceiling: the agent keeps going until its "
             "own eval scores stop improving (which its saturation rule limits to a couple of failed "
             "attempts anyway). 0 forbids any attempt, so the agent scores its candidate and stops. "
             "Defaults to $AGENT_OPTIMIZE_CYCLES so an export selects it for every run in that shell "
             "rather than being silently ignored, and the value is recorded on the run.",
    )
    parser.add_argument(
        "--no-optimization",
        action="store_true",
        default=(os.environ.get("AGENT_OPTIMIZATION_ENABLED", "").strip().lower()
                 in ("0", "false", "no", "off")),
        help="ABLATION: run the agent WITHOUT its self-evaluation tools. Removes `eval_generator` and "
             "`optimize_agent` from the tool registry and from every prompt surface, leaving the "
             "four-tool agent (talk_to_user, mini_swe_agent, deploy_artifact, complete) that existed "
             "before those tools landed -- so a run is comparable with one from before them. Distinct "
             "from --optimize-cycles 0, which keeps the internal eval and the measurement and forbids "
             "only the changes: use 0 to ask 'did the optimization loop help?', and this to ask 'did "
             "having these tools at all change how the agent works?'. Recorded on the run either way. "
             "Defaults from $AGENT_OPTIMIZATION_ENABLED being falsy.",
    )
    parser.add_argument(
        "--no-user-sim",
        action="store_true",
        default=(os.environ.get("AGENT_USER_SIM_ENABLED", "").strip().lower()
                 in ("0", "false", "no", "off")),
        help="ABLATION: run the agent with NO stakeholder to ask. No user-sim container is started, "
             "`talk_to_user` is removed from the tool registry and from every prompt surface, and "
             "what the simulator would have held is mounted read-only in the AGENT container "
             "instead: tasks/<id>/user_sim_docs/ at /requirements/docs and "
             "tasks/<id>/user_personas/domain_knowledge/ at /requirements/policies. Same task, same "
             "facts, same scoring; the only difference is whether the agent had to elicit them. "
             "Fails before anything starts if the task has NEITHER tree (that would be a harder "
             "task, not an ablation); with documents but no policy extracts it runs and warns that "
             "any rule living only in a persona's inline domain_knowledge is absent. --persona is "
             "NOT required in this arm and decides nothing if you pass one -- both trees are "
             "mounted whole, not one persona's share -- so the run is named `nouser` either way. "
             "Defaults from $AGENT_USER_SIM_ENABLED being falsy.",
    )
    # Three hours, per #191: kept from main rather than this branch's older 7200.
    parser.add_argument("--timeout", type=int, default=10800, help="Build timeout in seconds")
    parser.add_argument(
        "--skip-eval",
        action="store_true",
        help="Build the artifact and stop, without scoring it. The artifact is still snapshotted to "
             "runs/<trace>/artifact, which is what --artifact-dir runs score later. Used by the "
             "build cell of a builds:/trials: experiment, where each eval trial is its own cell with "
             "its own timeout and its own retry.",
    )
    parser.add_argument(
        "--artifact-dir",
        default=None,
        help="Score an already-built artifact source dir instead of building one: no agent, no "
             "user-sim, no submission server. One invocation is one eval trial. Mutually exclusive "
             "with --skip-eval. --persona is ignored (an artifact is scored against the task's eval "
             "dataset, not a persona).",
    )
    parser.add_argument(
        "--build-run-dir",
        default=None,
        help="For an eval trial: the run directory of the BUILD whose artifact this scores, so the "
             "elicitation diagnostic can read that build's stakeholder conversation. A trial has no "
             "build phase of its own, so without this the diagnostic reports itself unmeasurable and "
             "a `builds`/`trials` sweep computes elicitation nowhere -- the build cell does not score "
             "at all (--skip-eval) and the trials have no transcript. Only meaningful with "
             "--artifact-dir.",
    )
    parser.add_argument(
        "--enable-observability",
        dest="observability",
        action="store_true",
        help="Enable observability dashboard for real-time event viewing",
    )
    parser.add_argument(
        "--runs-dir",
        default="runs",
        help="Directory for storing run data (default: runs/)",
    )
    parser.add_argument(
        "--run-label",
        default=None,
        help="Extra component for this run's directory name, between the model and the persona. "
             "For the coordinates only the caller knows -- the experiment runner passes its "
             "replicate position here (e.g. b0, b0.t1), which this process has no concept of. "
             "Sanitised to [A-Za-z0-9._-] like every other part of a run id.",
    )
    parser.add_argument(
        "--aggregator-port",
        type=int,
        default=8080,
        help="Port for the observability aggregator server (default: 8080)",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap the number of eval cases scored (debug; e.g. --limit 2). "
                             "The build phase runs regardless.")
    parser.add_argument("--rerun-sample-ids", nargs="+", help=argparse.SUPPRESS)
    parser.add_argument("--rerun-source", help=argparse.SUPPRESS)
    parser.add_argument("--rerun-source-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--rerun-allow-config-change", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--eval-lanes", type=int, default=1, metavar="N",
        help="Score against N copies of the task environment, each with its own artifact container, "
             "so N eval cases run at once (default 1). Only does anything for a task that resets its "
             "environment per case: with one environment those run strictly one case at a time, "
             "because a reset wipes the whole store. N copies cost N times the containers and N times "
             "the concurrent model calls, so pick it for the machine and the provider's limits, not "
             "for the dataset. Recorded on the eval report -- scores are strictly comparable only at "
             "equal N.",
    )
    parser.add_argument(
        "--no-upload-runs-to-s3",
        dest="s3_upload",
        action="store_false",
        help="Don't mirror this run's data to S3 when it finishes. By default an observability "
             "run uploads itself to s3://<bucket>/<prefix>/<trace_id>/ so anyone with bucket "
             "access can render it in the tracer by run id. Upload failures never fail the run.",
    )
    parser.add_argument(
        "--s3-bucket",
        default=None,
        help=f"S3 bucket for run mirroring (default: $APPSCI_S3_BUCKET or {DEFAULT_S3_BUCKET}).",
    )
    parser.add_argument(
        "--s3-prefix",
        default=None,
        help=f"Key prefix within the bucket (default: $APPSCI_S3_PREFIX or {DEFAULT_S3_PREFIX}).",
    )
    parser.add_argument(
        "--aws-profile",
        default=None,
        help="AWS config profile to use for S3 (default: $APPSCI_S3_PROFILE, else the standard "
             "boto3 credential chain). Set up an auto-refreshing SSO profile with "
             "./scripts/setup_s3_sso.sh so credentials survive rotation.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Log every observability event as it is emitted. Orchestrator + evaluator emits "
             "(build.* / eval.* / judge model I/O) stream to this console; agent / mini-swe / "
             "user-sim emits stream to their container logs (docker logs). Implemented by setting "
             "OBSERVABILITY_LOG_EVENTS for this process and forwarding it into the containers. "
             "No effect without --enable-observability.",
    )

    args = parser.parse_args()

    harness = None
    if args.harness_config:
        try:
            harness = HarnessConfig.load(args.harness_config)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        if args.artifact_dir:
            parser.error("--harness-config selects a builder and cannot be used with --artifact-dir")
        missing = [name for name in harness.passthrough_env if not os.environ.get(name)]
        if missing:
            parser.error(f"harness requires environment variables: {', '.join(missing)}")

    # Contradictory, so refuse rather than silently honouring one. `--no-optimization` removes the tool
    # that spends cycles, so a cycle count alongside it means the caller believes something about this
    # run that is not true -- and an ablation whose conditions were quietly not what was asked for is
    # worse than no ablation, because the number still looks usable.
    if args.no_optimization and args.optimize_cycles is not None:
        parser.error(
            "--no-optimization and --optimize-cycles are mutually exclusive: the first removes "
            "optimize_agent entirely, so there are no cycles to cap. Use --optimize-cycles 0 to keep "
            "the internal eval and the measurement while forbidding any change, or --no-optimization "
            "to remove both self-evaluation tools."
        )

    # Turn on per-event emit logging (INFO) across every writer -- in-process here, and forwarded into
    # the agent + user-sim containers via OBSERVABILITY_LOG_EVENTS (see agent_container / appsci_agent_usersim).
    if args.verbose:
        os.environ["OBSERVABILITY_LOG_EVENTS"] = "1"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    orchestrator = BenchmarkOrchestrator(
        tasks_dir=Path(args.tasks_dir),
        build_timeout=args.timeout,
        observability=args.observability,
        runs_dir=Path(args.runs_dir),
        aggregator_port=args.aggregator_port,
        s3_upload=args.s3_upload,
        s3_config=S3Config.from_env(
            bucket=args.s3_bucket, prefix=args.s3_prefix, profile=args.aws_profile
        ),
        eval_lanes=args.eval_lanes,
        sample_ids=args.rerun_sample_ids,
        rerun_source=args.rerun_source,
        rerun_source_sha256=args.rerun_source_sha256,
        rerun_allow_config_change=args.rerun_allow_config_change,
    )

    from ..tracer.server import TracerPortInUseError

    _install_shutdown_handler()

    if args.persona is None and not args.no_user_sim and not args.artifact_dir:
        print("\nERROR: --persona is required for a run with a stakeholder. Pass one, or pass "
              "--no-user-sim to run the arm that has none.\n", file=sys.stderr)
        sys.exit(1)

    if args.rerun_sample_ids is not None or args.rerun_source is not None or args.rerun_allow_config_change:
        if not args.rerun_sample_ids or not args.rerun_source or not args.artifact_dir:
            parser.error("sample reruns require sample IDs, a source eval, and --artifact-dir")

    if args.skip_eval and args.artifact_dir:
        # Checked here as well as in run(): a cell argv is built by machine, and catching this before
        # any container starts costs nothing while catching it later costs a task-env bring-up.
        print("\nERROR: --skip-eval and --artifact-dir are mutually exclusive.\n", file=sys.stderr)
        sys.exit(1)

    # Same reasoning, for a Fireworks agent model with no Fireworks key: the container manager checks
    # this too, but by then the task environment and the user-sim container are already up. Exit 3
    # rather than 1 because a missing credential hits every remaining row of a batch identically, and
    # 3 is what tells run_tasks.sh and the sweep worker to stop draining instead of repeating it.
    fireworks_error = fireworks_routing_error(args.agent_model) if harness is None else None
    if fireworks_error:
        print(f"\nERROR: {fireworks_error}\n", file=sys.stderr)
        sys.exit(3)

    try:
        result = await orchestrator.run(
            task_id=args.task,
            persona_path=Path(args.persona) if args.persona else None,
            appsci_usersim_model=args.appsci_usersim_model,
            agent_model=args.agent_model,
            agent_temperature=args.agent_temperature,
            optimize_cycles=args.optimize_cycles,
            optimization_enabled=not args.no_optimization,
            user_sim_enabled=not args.no_user_sim,
            eval_limit=args.limit,
            skip_eval=args.skip_eval,
            artifact_dir=Path(args.artifact_dir) if args.artifact_dir else None,
            build_run_dir=Path(args.build_run_dir) if args.build_run_dir else None,
            run_label=args.run_label,
            harness=harness,
        )
    except ValueError as e:
        parser.error(str(e))
    except TracerPortInUseError as e:
        print(f"\nERROR: {e}\n", file=sys.stderr)
        # Distinct from the normal task-failure exit code (1): this is an
        # environment blocker that will hit every subsequent task in a batch
        # too, so callers like run_tasks.sh should abort rather than continue.
        sys.exit(3)
    except asyncio.CancelledError:
        # A signal asked us to stop (see _install_shutdown_handler, and asyncio's own SIGINT
        # handling). `run()`'s cleanup has already finished by the time this is reached -- unwinding
        # the coroutine is what ran it -- so there is nothing left to do but report it.
        #
        # Exit 1, the ordinary "this run did not produce a score" code. Deliberately not 3: the queue
        # worker reads 3 as an environment blocker and stops draining the whole sweep, and being
        # asked to shut down is not that.
        print("\nERROR: interrupted by a signal; the run's containers were torn down.\n",
              file=sys.stderr)
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"Task: {result.task_id}")
    print(f"Status: {result.status}")
    if result.scores:
        print(f"Scores: {result.scores}")
    if result.error:
        print(f"Error: {result.error}")
    print(f"{'='*60}\n")

    sys.exit(0 if result.status == "completed" else 1)


if __name__ == "__main__":
    asyncio.run(main())
