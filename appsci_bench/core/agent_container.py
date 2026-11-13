"""Container manager for the appsci-agent Docker container.

This module manages the appsci-agent container lifecycle. The agent container
builds artifacts based on user requirements. It receives environment variables
for connecting to:
- Appsci agent user sim service (APPSCI_USERSIM_URL, APPSCI_USERSIM_TOKEN)
- Task workspace container (TASK_WORKSPACE)
- Orchestrator submit URL (ORCHESTRATOR_SUBMIT_URL)

The agent runs autonomously and calls complete when done, which POSTs to the callback URL.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .harness import HarnessConfig

logger = logging.getLogger(__name__)

# Docker's magic hostname for "the host the daemon runs on". The orchestrator's submission server
# (POST /submit, POST /deploy -- see orchestrator/submission_server.py) listens on the HOST, and the
# agent container calls back to it under this name via ORCHESTRATOR_SUBMIT_URL.
HOST_ALIAS = "host.docker.internal"

# DO NOT DELETE AS REDUNDANT -- this flag is what makes HOST_ALIAS resolve on **Linux**.
# Docker Desktop (macOS/Windows) injects `host.docker.internal` into every container implicitly, so
# the name works there with no flag. Docker Engine on **Linux** -- what our EC2 runner boxes use --
# does NOT: without `--add-host`, the agent's submit/deploy callbacks die with "Could not resolve
# host: host.docker.internal" and the BUILD phase can never complete on Linux at all.
# Passed UNCONDITIONALLY rather than gated on platform: verified on Docker Desktop for macOS that the
# explicit mapping resolves to exactly the host gateway Desktop would have injected anyway (a
# container reached a host-side HTTP server identically with and without the flag, on both the
# default bridge and bench-network), so one code path is correct on every platform.
# `host-gateway` requires Docker >= 20.10.
HOST_GATEWAY_RUN_ARGS: tuple[str, str] = ("--add-host", f"{HOST_ALIAS}:host-gateway")

# Container-filesystem snapshot (snapshot_container_dirs): which absolute dirs to copy out per
# container. These are chosen to capture EVERY application file the agent was given or produced,
# and nothing else (no system/dep dirs). For the agent container (per its Dockerfile: WORKDIR
# /work, mkdir -p /work/out /workspace /spec) that is:
#   MOUNTED IN  -- /spec (redacted spec.yaml), /data (task sample data), /sop (requirement docs)
#   CREATED     -- /work (the agent's WORKDIR, incl. /work/out the artifact output dir), /workspace
# Deliberately OMITTED:
#   /app                    - the agent framework's OWN installed code (not app files it authored)
#   /runs                   - the observability bind-mount (copying it would recurse into this snap)
#   /var/run/docker.sock    - a host socket, not a file the agent created
#   OS/system dirs (/usr,/bin,/etc,/tmp,...) - not application files; heavy dep/build dirs under the
#                             copied roots are pruned below (SNAPSHOT_EXCLUDE_DIRS).
# The agent is instructed to work under /work (its WORKDIR); a stray write elsewhere on the rootfs
# is out of scope by design (we snapshot the agent's designated surface, not the whole rootfs).
# NOTE the asymmetry with the user-sim's own set (_USERSIM_SNAPSHOT_DIRS, in appsci_agent_usersim.py):
# /user_sim_docs is deliberately ABSENT here. The agent container is never given that mount, and that
# is what makes the material elicitable rather than readable (issue #76).
#: Where the NO-USER arm hands the agent what the stakeholder would have held, read-only. Empty on
#: every ordinary run -- the asymmetry described above is the benchmark as designed, and this is the
#: one arm that deliberately removes it, in exchange for removing `talk_to_user` too.
#: Passed to the container as APPSCI_REQUIREMENTS_DIR as well as used as the bind target, so the
#: prompt's "your requirements are in X" and the mount are one value and cannot drift.
#:
#: This is a directory OWNED BY THE IMAGE with the two halves bound underneath it, not a bind mount
#: itself. A stakeholder's knowledge has two sources and they live in two places on disk -- the
#: task's `user_sim_docs/` tree and the per-level extracts under `user_personas/domain_knowledge/`
#: -- and binding one of them at this path with the other nested inside it would make Docker create
#: the nested mount point INSIDE the host source directory, writing into the repo.
REQUIREMENTS_MOUNT = "/requirements"
#: The documents half: the task's `user_sim_docs/` tree, which the simulator would have been handed
#: verbatim.
REQUIREMENTS_DOCS_MOUNT = f"{REQUIREMENTS_MOUNT}/docs"
#: The policy half: `user_personas/domain_knowledge/`, the per-level extracts of the knowledge each
#: persona otherwise carries inline in its YAML and answers questions out of. Every level's extract is
#: mounted, matching the documents half, which is also mounted whole rather than per-persona.
REQUIREMENTS_POLICIES_MOUNT = f"{REQUIREMENTS_MOUNT}/policies"
#: Included in the snapshot: in that arm these documents are an INPUT the agent was given, and a trace
#: that does not show what it was handed cannot be read against one where it had to ask. On a control
#: run the path does not exist and the copy is a no-op.
_AGENT_SNAPSHOT_DIRS = ("/workspace", "/work", "/data", "/sop", "/spec", REQUIREMENTS_MOUNT)
#: Dependency/build directories never worth copying out of a container -- they are reproducible from
#: the source beside them, and they are where an unbounded copy actually gets big. Public because the
#: per-deploy artifact checkpointer (appsci_bench/orchestrator/checkpoint.py) prunes the same set, and
#: two hand-maintained copies of this list would drift.
SNAPSHOT_EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".cache", ".next", "dist", "build", ".egg-info",
}
_WORKSPACE_SNAPSHOT_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB

# Budget for the agent-writable observability log mounts (see `start()`), summed across them.
#
# WHY A BUDGET AND NOT A LIMIT. These four files are bind-mounted rw into the container so the agent's
# own event/model-call streams land on the host as they are written, and the writer is the benchmark's
# SUBJECT -- code an LLM wrote, under no obligation to be reasonable. Docker cannot bound a bind mount:
# `--storage-opt size=` applies to the container's writable layer (and only on xfs+pquota / devmapper),
# never to a mount, and there is no per-file quota to attach. `--ulimit fsize=` would be a real kernel
# bound but applies to EVERY file the container writes, so a legitimate large write (a pip wheel) would
# die with SIGXFSZ far from the cause. So this is detection, not prevention: the cell is killed loudly
# instead of the host disk filling silently, which is the difference between one failed-and-retried
# cell and a box where every subsequent cell fails and the S3 mirror cannot write either.
#
# WHY 2 GiB. model_calls-*.jsonl carries whole prompts and responses, so a long build phase legitimately
# reaches the tens-to-low-hundreds of MB; 2 GiB is an order of magnitude above anything observed and
# still ~1% of the jobs box's disk. It is a runaway detector, not a tight quota -- a tight one would
# kill real cells, which is a worse failure than the one it prevents.
AGENT_LOG_BUDGET_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

# The agent container's own observability sources (agent loop + its in-process mini-swe sub-agent) and
# the two per-source files each of them writes. Keep in sync with Source.APPSCI_AGENT /
# Source.MINI_SWE_AGENT -- `start()` mounts exactly this set and the budget check measures it, so one
# list is what keeps "what is mounted" and "what is watched" from drifting apart.
_AGENT_LOG_SOURCES = ("appsci_agent", "mini_swe_agent")
_AGENT_LOG_PREFIXES = ("events", "model_calls")

#: LiteLLM's model-id prefix for Fireworks-served models, and the two env vars it reads the key from
#: (`FIREWORKS_API_KEY` first, `FIREWORKS_AI_API_KEY` as the fallback -- see litellm's
#: llms/fireworks_ai/common_utils.py). Fireworks is reached as a FIRST-PARTY provider: LiteLLM
#: resolves its endpoint from the model id, so a run on it needs a key and nothing else. That is why
#: there is no base-url var to forward beside it, and why the gateway gateway pair has to be out of
#: the way (see `fireworks_routing_error`).
FIREWORKS_MODEL_PREFIX = "fireworks_ai/"
FIREWORKS_KEY_VARS = ("FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY")

# Per-request LLM timeout, in seconds. Read INSIDE the agent container by both the agent's LLM client
# and the mini-swe sub-agent, so it has to be forwarded explicitly to have any effect; see `start`.
LLM_TIMEOUT_VAR = "APPSCI_LLM_TIMEOUT_S"



def fireworks_routing_error(model: str | None) -> str | None:
    """Why a Fireworks-served agent model must not be started, or None when it can.

    Returns a message rather than raising, because the two callers react differently: the container
    manager turns it into an exception, and `orchestrator/run.py`'s `main()` prints it and exits 3 --
    an environment blocker, so `run_tasks.sh` and the sweep worker abort the queue instead of
    repeating the same failure once per row.

    Only a `fireworks_ai/` model id is examined; every other run is untouched. The check keys off the
    RESOLVED model (the orchestrator's `--agent-model`, which defaults to `AGENT_MODEL`), the only
    spelling that decides what the container actually calls.

    Two refusals, both before anything starts:

    NO KEY. LiteLLM has nothing to authenticate with, and the run would die at its first model call
    minutes later with an auth error buried in the agent's own log.

    A GATEWAY URL WITH NO TOKEN OF ITS OWN -- and this one is a credential leak, not an inconvenience.
    `APPSCI_AGENT_BASE_URL` becomes LiteLLM's endpoint while the key resolves from the environment, so
    the agent sends `Authorization: Bearer <the Fireworks key>` to whatever that URL names. gateway
    would receive a live Fireworks credential it has no business holding. This state is reachable
    without contrivance: `setup_local_with_gateway.sh` exports the URL and the token under two
    separate guards, so a stale or half-loaded gateway session yields URL-without-token. Refusing
    closes nothing legitimate off -- a real proxy in front of Fireworks carries its own token, which
    is the case that only warns (see `warn_on_fireworks_gateway_conflict`).
    """
    model = (model or "").strip()
    if not model.startswith(FIREWORKS_MODEL_PREFIX):
        return None
    if not any(os.environ.get(name, "").strip() for name in FIREWORKS_KEY_VARS):
        return (
            f"agent model {model!r} is served by Fireworks, but neither "
            f"{' nor '.join(FIREWORKS_KEY_VARS)} is set. Export FIREWORKS_API_KEY (or run "
            "`source scripts/setup_local_with_fireworks.sh`) before starting the run."
        )
    gateway_url = os.environ.get("APPSCI_AGENT_BASE_URL", "").strip()
    gateway_key = os.environ.get("APPSCI_AGENT_API_KEY", "").strip()
    if gateway_url and not gateway_key:
        return (
            f"agent model {model!r} is served by Fireworks, but APPSCI_AGENT_BASE_URL is set with no "
            "APPSCI_AGENT_API_KEY beside it. That combination sends the FIREWORKS KEY as the bearer "
            "token to that endpoint, handing a live credential to a party that should not hold it. "
            "Unset APPSCI_AGENT_BASE_URL (`source scripts/setup_local_with_fireworks.sh` does this), "
            "or set the token that endpoint expects if it really is a proxy in front of Fireworks."
        )
    return None


def warn_on_fireworks_gateway_conflict(model: str | None) -> None:
    """Warn when a leftover agent-side gateway var will misroute a Fireworks run.

    Two states reach here, both broken runs rather than leaks, which is why they warn where the
    URL-without-token case above refuses:

    * URL AND TOKEN both set: LiteLLM sends the Fireworks model id to that endpoint with that token.
      gateway does not serve the id, so every call fails -- but the same shape is legitimate when the
      URL is a proxy in FRONT of Fireworks, and telling the two apart would mean guessing from a
      hostname, which is worth less than an honest warning.
    * TOKEN alone: `llm_client` and mini-swe pass `api_key` and `base_url` to LiteLLM independently, so
      the gateway token is handed to Fireworks as the Fireworks credential, `FIREWORKS_API_KEY` is
      never consulted, and every call 401s. Nothing fails until the first model call, so without this
      warning the cause is invisible.

    Either way the fix is the same, and `scripts/setup_local_with_fireworks.sh` performs it, so the
    scripted path never reaches this branch.
    """
    model = (model or "").strip()
    if not model.startswith(FIREWORKS_MODEL_PREFIX):
        return
    set_vars = [name for name in ("APPSCI_AGENT_BASE_URL", "APPSCI_AGENT_API_KEY")
                if os.environ.get(name, "").strip()]
    if not set_vars:
        return
    logger.warning(
        "agent model %r is a Fireworks id, but %s %s set, so LiteLLM will not reach Fireworks with "
        "the Fireworks key: an endpoint and a token given for the agent's own calls take precedence "
        "over the provider the model id names. Unset APPSCI_AGENT_BASE_URL, APPSCI_AGENT_API_KEY and "
        "APPSCI_AGENT_CUSTOM_HEADERS (scripts/setup_local_with_fireworks.sh does this) unless that "
        "endpoint is a proxy in front of Fireworks.",
        model, " and ".join(set_vars), "is" if len(set_vars) == 1 else "are",
    )


def snapshot_container_dirs(container_name: str | None, dirs, dest_dir: str) -> dict | None:
    """Copy the given absolute dirs out of a (possibly exited) container to the host, so the tracer
    can browse what the container was given + produced after it's gone.

    Uses ``docker cp`` (works on an exited-but-not-removed container, so call BEFORE the container's
    ``stop()``; ``docker exec`` would not, once its CMD has finished). Each source dir lands at
    ``dest_dir/<basename>`` (e.g. ``/workspace`` -> ``dest_dir/workspace``). Heavy dep/build dirs are
    pruned and any file over the size cap is dropped, on the host afterwards, to keep it small.
    Best-effort: a missing dir is skipped; returns a manifest dict, or None if nothing was copied.
    """
    if not container_name:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    copied_any = False
    for d in dirs:
        result = subprocess.run(
            ["docker", "cp", f"{container_name}:{d}", dest_dir],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            copied_any = True
        else:
            logger.debug("Container snapshot: %s:%s not copied (%s)", container_name, d,
                         result.stderr.strip())
    if not copied_any:
        logger.warning("Container snapshot skipped for %s (no dirs copied)", container_name)
        return None

    # Prune heavy dep/build dirs that got copied.
    for dirpath, dirnames, _filenames in os.walk(dest_dir, topdown=True):
        for dn in list(dirnames):
            if dn in SNAPSHOT_EXCLUDE_DIRS:
                shutil.rmtree(os.path.join(dirpath, dn), ignore_errors=True)
                dirnames.remove(dn)

    # Drop oversized files + build a manifest.
    files: list[dict] = []
    total = 0
    dropped = 0
    for dirpath, _dirnames, filenames in os.walk(dest_dir):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size > _WORKSPACE_SNAPSHOT_MAX_FILE_BYTES:
                try:
                    os.remove(full)
                    dropped += 1
                except OSError:
                    pass
                continue
            files.append({"path": os.path.relpath(full, dest_dir), "size": size})
            total += size

    files.sort(key=lambda f: f["path"])
    logger.info(
        "Container snapshot for %s: %d files (%d bytes, %d large dropped) -> %s",
        container_name, len(files), total, dropped, dest_dir,
    )
    return {"files": files, "file_count": len(files), "total_bytes": total, "dropped_large": dropped}


@dataclass
class AppSciAgentContainerConfig:
    """Configuration for the AppSciAgentContainerManager.

    Attributes:
        task_id: The task identifier for container naming.
        workspace_container: Name of the task workspace container on the shared network.
        appsci_usersim_url: URL of the appsci agent user sim service, or None in the no-user arm.
        appsci_usersim_token: Bearer token for the appsci agent user sim, or None in the no-user arm.
        orchestrator_submit_url: URL for the agent to POST when artifact is ready.
        network_name: Docker network for container communication.
        spec_path: Path to the task spec.yaml file.
        image: Docker image name for the agent.
        harness: External builder image/command, or None for the reference agent.
        harness_inputs_dir: Public input bundle to mount for an external builder.
        data_dir: Path to the task data directory (mounted at /data in container).
        observability_trace_id: Trace ID for observability (if observability enabled).
        runs_dir: Path to runs directory to mount (if observability enabled).
    """

    task_id: str
    workspace_container: str
    # None in the NO-USER arm, where no user-sim service is started at all. Not "" -- an empty URL
    # would still be injected and the agent's client would fail at call time with a connection error
    # instead of the tool simply not existing, which is a different experiment.
    appsci_usersim_url: str | None
    appsci_usersim_token: str | None
    orchestrator_submit_url: str
    spec_path: str
    network_name: str = "bench-network"
    image: str = "appsci-bench-agent:latest"
    # None selects the reference agent. Other harnesses use the same mounts,
    # task services, lifecycle, and artifact handover with their own image/CMD.
    harness: HarnessConfig | None = None
    harness_inputs_dir: str | None = None

    # Optional configuration
    model: str | None = None
    max_turns: int | None = None
    # Sampling temperature for both build-side agents (the interviewing agent and its mini-swe
    # sub-agent). None => inject nothing, so the build runs at the provider default exactly as it did
    # before this knob existed. This is the build-side replicate control: with no seed in the harness,
    # temperature is what makes n builds of one cell samples rather than repeats.
    temperature: float | None = None
    # How many optimization cycles `optimize_agent` may spend improving the candidate, or None for
    # "no ceiling -- keep going until its own eval scores stop moving". A cycle is one attempt to
    # CHANGE the candidate, and each one costs a mini-swe run plus a rebuild and two eval runs, so
    # this is the knob that bounds the most expensive part of a build. 0 is meaningful and distinct
    # from None: score the candidate, record it, never try to improve it.
    optimize_cycles: int | None = None
    # False removes the agent's self-evaluation tools entirely -- `eval_generator` and
    # `optimize_agent`, from the registry and from every prompt surface -- leaving the four-tool agent
    # that existed before them. The ablation control: a run to compare against one from before those
    # tools landed. Distinct from `optimize_cycles=0`, which keeps the measurement and forbids only the
    # changes.
    optimization_enabled: bool = True
    # False is the NO-USER ablation arm: no user-sim service is started, `talk_to_user` is removed
    # from the agent's tool registry and every prompt surface, and `requirements_dir` below is mounted
    # in its place. The two move together by construction -- the orchestrator sets both or neither,
    # and `AgentConfig` inside the container refuses the half-state (see its `__post_init__`).
    user_sim_enabled: bool = True
    # HOST path of the task's `user_sim_docs/` tree, bind-mounted read-only at
    # REQUIREMENTS_DOCS_MOUNT. Only ever set when `user_sim_enabled` is False; `start()` rejects the
    # combination otherwise, because mounting the stakeholder's own documents while the stakeholder
    # is still reachable removes the elicitation the control arm exists to measure.
    requirements_dir: str | None = None
    # HOST path of the task's `user_personas/domain_knowledge/` tree, bind-mounted read-only at
    # REQUIREMENTS_POLICIES_MOUNT. The other half of what the stakeholder knew: the rules a persona
    # holds inline in its YAML and would only ever have released by answering a question. Same
    # arm-only rule as `requirements_dir`, and a task may have either, both, or neither -- the two
    # halves are independent on disk, and `start()` requires at least one of them in this arm.
    policies_dir: str | None = None
    data_dir: str | None = None
    # Agent-facing SOP / requirement docs (policy, tool reference, KPIs), mounted read-only at /sop.
    # Distinct from data_dir (/data = sample data): /sop is "what a compliant artifact must do".
    # Sourced from tasks/<id>/environment/sop by the orchestrator when that dir exists.
    sop_dir: str | None = None
    # Extra env vars to inject into the agent container (and thus the artifact it
    # deploys, which inherits the container env). Sourced from spec.task_runner.agent_env
    # -- e.g. airline's AIRLINE_TOOLS_URL/TOKEN so the deployed artifact can reach the
    # containerized task-env tool-API.
    extra_env: dict[str, str] = field(default_factory=dict)

    # Where the agent writes its deliverable file artifacts inside the container.
    # Sourced from the task spec's `artifacts.output_dir` by the orchestrator so
    # the snapshot reads exactly where the agent was told to write. Falls back to
    # the container's default output dir.
    artifact_output_dir: str = "/work/out"

    # Host directory bind-mounted at `artifact_output_dir` inside the container, so everything the
    # agent writes there (its Dockerfile + server entry) is LIVE-visible on the host the instant it's
    # written. This is what lets the host-side deployer (the orchestrator's POST /deploy route) build
    # straight from the agent's output with no `docker cp`, and it doubles as the durable run snapshot
    # the eval-only pipeline re-deploys later. The orchestrator sets it to the run's artifact dir
    # (runs/<tag>/artifact). None => no mount (legacy / local flows that don't host-deploy).
    host_artifact_dir: str | None = None

    # Observability configuration
    observability_trace_id: str | None = None
    runs_dir: str | None = None


class AppSciAgentContainerManager:
    """Manages the appsci-agent Docker container lifecycle.

    This is a client/controller that runs on the orchestrator host and manages
    the agent container via Docker CLI commands. The actual agent service runs
    inside the container (appsci_agent/run.py).

    The manager:
    1. Starts a container that runs the appsci-agent
    2. Waits for the agent to complete (via container exit)
    3. Retrieves container logs for debugging
    4. Cleans up the container when done

    The agent inside the container:
    1. Reads task context from the spec and workspace
    2. Interacts with the appsci agent user sim to gather requirements
    3. Uses mini-swe-agent to write code and build the artifact
    4. Calls complete when done, which POSTs to the submit URL

    Example usage:
        config = AppSciAgentContainerConfig(
            task_id="order_to_cash",
            workspace_container="o2c-workspace-1",
            appsci_usersim_url="http://usersim-order_to_cash:8000",
            appsci_usersim_token="<token>",
            orchestrator_submit_url="http://host.docker.internal:9000/submit",
            spec_path="/path/to/spec.yaml",
        )
        manager = AppSciAgentContainerManager(config)
        manager.start()
        # ... wait for submission ...
        manager.stop()

    Attributes:
        config: AppSciAgentContainerConfig with all necessary parameters.
    """

    DEFAULT_IMAGE = "appsci-bench-agent:latest"
    CONTAINER_PORT = 8080  # Port the artifact will be served on

    def __init__(self, config: AppSciAgentContainerConfig):
        """Initialize the AppSciAgentContainerManager.

        Args:
            config: Configuration for the agent container.
        """
        self.config = config
        self._container_name: str | None = None
        self._harness_log_capture = None
        # Set when the agent-writable log mounts blow their budget and this manager kills the
        # container: the reason, for a caller that wants to report it as something other than a crash.
        self.log_budget_error: str | None = None

    def _mounted_log_paths(self) -> list[str]:
        """Host paths of the agent-writable observability log files, or [] when none are mounted.

        One definition, used by ``start()`` to build the mounts and by the budget check to measure
        them, so the set that is watched cannot drift from the set that is writable.
        """
        if not (self.config.runs_dir and self.config.observability_trace_id):
            return []
        trace_subdir = os.path.join(
            os.path.abspath(self.config.runs_dir), self.config.observability_trace_id
        )
        return [
            os.path.join(trace_subdir, f"{prefix}-{source}.jsonl")
            for source in _AGENT_LOG_SOURCES
            for prefix in _AGENT_LOG_PREFIXES
        ]

    def log_budget_exceeded(self) -> str | None:
        """Have the agent-writable log mounts exceeded ``AGENT_LOG_BUDGET_BYTES``?

        Returns a message naming the total and the largest file, or None. Cheap enough to call on
        every poll: it is one ``stat`` per mounted file (four), and a missing file counts as zero
        rather than raising -- the point is a runaway writer, and half a measurement is enough to
        detect one.
        """
        total = 0
        biggest = ("", 0)
        for path in self._mounted_log_paths():
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            total += size
            if size > biggest[1]:
                biggest = (os.path.basename(path), size)
        if total <= AGENT_LOG_BUDGET_BYTES:
            return None
        return (
            f"agent-writable log mounts total {total} bytes, over the "
            f"{AGENT_LOG_BUDGET_BYTES}-byte budget (largest: {biggest[0]} at {biggest[1]} bytes)"
        )

    def _kill_if_over_log_budget(self) -> str | None:
        """Kill the container if its log mounts are over budget; return the reason, or None.

        `docker kill`, not `stop`+`rm`: the container must exit NON-ZERO and stay around, so the
        caller's normal crash path reports it (exit 137) and ``get_logs`` still works. Removing it
        here would turn a diagnosable failure into "container disappeared".
        """
        reason = self.log_budget_exceeded()
        if reason is None or not self._container_name:
            return None
        self.log_budget_error = reason
        logger.error(
            "Killing agent container %s: %s. The agent under test is filling the host disk through "
            "its rw log mount; failing this cell is the cheap outcome -- a full disk fails every "
            "later cell on this host and blocks the S3 mirror too.",
            self._container_name, reason,
        )
        subprocess.run(["docker", "kill", self._container_name], capture_output=True)
        return reason

    @property
    def container_name(self) -> str:
        """Docker container name for the agent."""
        return f"appsci-agent-{self.config.task_id}"

    def _check_fireworks_routing(self) -> None:
        """Refuse to start, or warn, when the agent's Fireworks model cannot actually be reached.

        Split across two module-level helpers because the two callers need different failure
        behaviour: here a missing key is an exception, while `orchestrator/run.py`'s `main()` turns
        the same message into exit 3 (an environment blocker, so a batch aborts instead of repeating
        the failure per row). One implementation, two reactions.
        """
        error = fireworks_routing_error(self.config.model)
        if error:
            raise RuntimeError(error)
        warn_on_fireworks_gateway_conflict(self.config.model)

    def start(self) -> "AppSciAgentContainerManager":
        """Start the reference agent or selected external harness Docker container.

        The container is started with:
        - NO Docker socket (deployment is host-side via deploy_artifact -> POST /deploy)
        - Environment variables for usersim, workspace, and callback URLs
        - The spec bind-mounted read-only, and the host artifact dir bind-mounted rw
        - Connection to the bench-network

        Returns:
            self for chaining.

        Raises:
            ValueError: If the user-sim and no-user settings contradict each other.
            subprocess.CalledProcessError: If docker run fails.
        """
        # Fail closed on either half-state, before anything starts. Both are silent corruptions of an
        # arm rather than crashes: a control run that also mounts the documents scores elicitation it
        # never had to do, and a no-user run with no documents and no stakeholder is a harder task
        # than either arm, not an ablation of one. Neither would leave a mark in the run record.
        if self.config.user_sim_enabled and (self.config.requirements_dir or self.config.policies_dir):
            raise ValueError(
                "requirements_dir/policies_dir is set while user_sim_enabled is True. Mounting the "
                "user-sim's own documents or the personas' policy extracts into the agent container "
                "while the user-sim is still reachable is neither arm of the no-user experiment."
            )
        if not self.config.user_sim_enabled and not (
            self.config.requirements_dir or self.config.policies_dir
        ):
            raise ValueError(
                "user_sim_enabled is False but neither requirements_dir nor policies_dir was given. "
                "The no-user arm hands the agent what the user-sim would have held; with neither "
                "half it is a harder task, not an ablation. Check the task has a user_sim_docs/ "
                "directory, a user_personas/domain_knowledge/ directory, or both."
            )
        if self.config.user_sim_enabled and not (
            self.config.appsci_usersim_url and self.config.appsci_usersim_token
        ):
            raise ValueError(
                "user_sim_enabled is True but the user-sim URL/token are missing. The agent would "
                "get a `talk_to_user` tool with nothing behind it and spend its turns on connection "
                "errors."
            )

        self._container_name = self.container_name

        # Ensure spec_path is absolute (Docker volume mounts require absolute paths)
        spec_path_abs = os.path.abspath(self.config.spec_path)

        # Build environment variables
        env_vars = []

        # The user-sim coordinates FIRST, which is where they sat before the no-user arm existed.
        # Order carries no meaning to Docker -- `-e` is a set and nothing here is repeated -- but
        # keeping it means a control run's whole command is byte-identical to the pre-arm one, so
        # "this arm changed nothing for the control" is a diff anyone can run rather than a claim
        # they have to take on trust. Omitted entirely in the NO-USER arm rather than injected empty:
        # the agent has no `talk_to_user` tool there, and an empty URL left in the environment would
        # be a live-looking handle to a service that was never started.
        if self.config.user_sim_enabled:
            env_vars.extend([
                "-e", f"APPSCI_USERSIM_URL={self.config.appsci_usersim_url}",
                "-e", f"APPSCI_USERSIM_TOKEN={self.config.appsci_usersim_token}",
            ])

        env_vars += [
            "-e", f"TASK_WORKSPACE={self.config.workspace_container}",
            "-e", f"ORCHESTRATOR_SUBMIT_URL={self.config.orchestrator_submit_url}",
            "-e", f"TASK_SPEC_PATH=/spec/spec.yaml",
            # The artifact runs as a sibling container on this same Docker network, so the agent can
            # reach the deployed endpoint by container-DNS (the complete tool's reachability check).
            # The orchestrator attaches it -- the agent never does -- but it still needs the network
            # name to resolve that endpoint. (This used to exist so the agent could attach the
            # container itself with `docker run --network`; that is now the host's job, and without
            # the Docker socket it isn't even possible.)
            "-e", f"APPSCI_AGENT_ARTIFACT_NETWORK={self.config.network_name}",
        ]

        if not self.config.user_sim_enabled:
            # Both, together. `AGENT_USER_SIM_ENABLED` drops the tool from the registry and the
            # prompt; `APPSCI_REQUIREMENTS_DIR` tells the agent where what it would have had to
            # elicit is instead mounted. Every path here is the same string used as a bind target
            # below, so the prompt cannot name a directory that was not mounted -- and the two halves
            # are advertised SEPARATELY and only when bound, because "read everything under
            # /requirements" is true either way but tells the agent nothing about which kind of
            # material it is about to read.
            env_vars.extend([
                "-e", "AGENT_USER_SIM_ENABLED=0",
                "-e", f"APPSCI_REQUIREMENTS_DIR={REQUIREMENTS_MOUNT}",
            ])
            if self.config.requirements_dir:
                env_vars.extend(
                    ["-e", f"APPSCI_REQUIREMENTS_DOCS_DIR={REQUIREMENTS_DOCS_MOUNT}"]
                )
            if self.config.policies_dir:
                env_vars.extend(
                    ["-e", f"APPSCI_REQUIREMENTS_POLICIES_DIR={REQUIREMENTS_POLICIES_MOUNT}"]
                )

        # Add optional configuration
        if self.config.model:
            env_vars.extend(["-e", f"AGENT_MODEL={self.config.model}"])
        if self.config.max_turns:
            env_vars.extend(["-e", f"AGENT_MAX_TURNS={self.config.max_turns}"])
        # `is not None`, not truthiness: `temperature: 0.0` is a legitimate and meaningful request
        # (pin the sampler as hard as the provider allows), and a plain `if` would silently drop it
        # and run the build at the provider default while the cell record claimed 0.0.
        if self.config.temperature is not None:
            env_vars.extend(["-e", f"AGENT_TEMPERATURE={self.config.temperature}"])
        # `is not None` for the same reason as temperature, and it matters more here: 0 means "forbid
        # any optimization attempt", which truthiness would read as unset and silently replace with
        # "run until the scores saturate" -- the opposite of what was asked, and invisible in the run
        # record. Unset (None) injects nothing, so the agent keeps its own default.
        if self.config.optimize_cycles is not None:
            env_vars.extend(["-e", f"AGENT_OPTIMIZE_CYCLES={self.config.optimize_cycles}"])
        # Injected only when ablating, so a normal run's container env is byte-identical to before this
        # knob existed -- and an ablation is visible in the argv rather than implied by an absence.
        if not self.config.optimization_enabled:
            env_vars.extend(["-e", "AGENT_OPTIMIZATION_ENABLED=0"])

        # Add observability configuration. The runs dir is mounted at /runs
        # (see below); the publisher derives its per-source DB path from it.
        if self.config.observability_trace_id and self.config.runs_dir:
            env_vars.extend([
                "-e", "OBSERVABILITY_RUNS_DIR=/runs",
                "-e", f"OBSERVABILITY_TRACE_ID={self.config.observability_trace_id}",
                "-e", f"OBSERVABILITY_TASK={self.config.task_id}",
            ])
            # Forward per-event emit logging (orchestrator --verbose) so this container's publisher
            # streams each emit to its docker logs too.
            if os.environ.get("OBSERVABILITY_LOG_EVENTS"):
                env_vars.extend(["-e", f"OBSERVABILITY_LOG_EVENTS={os.environ['OBSERVABILITY_LOG_EVENTS']}"])
        # Inject task-specific env (spec.task_runner.agent_env), e.g. airline's
        # AIRLINE_TOOLS_URL/TOKEN. The deployed artifact subprocess inherits these.
        for name, value in self.config.extra_env.items():
            env_vars.extend(["-e", f"{name}={value}"])

        # Validate that at least one LLM API key is available. NOTE: only *credentials* belong in
        # this list -- APPSCI_AGENT_CUSTOM_HEADERS is an identity header, not a key, and a run
        # without it must not hard-fail here.
        #
        # NON-EMPTY, not merely present. `KEY=` in a systemd EnvironmentFile assigns the empty string,
        # and an empty string is not a credential: read as presence, one blank line in
        # /etc/appsci-bench/experiments.env would satisfy this check for EVERY cell on the host and
        # retire the fail-fast that catches a failed gateway refresh before a container starts. The
        # Fireworks guard below already tested non-emptiness, so this also makes the two agree.
        auth_keys = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "APPSCI_AGENT_API_KEY",
                     *FIREWORKS_KEY_VARS]
        if self.config.harness is None and not any(os.environ.get(k, "").strip() for k in auth_keys):
            raise RuntimeError(
                "No LLM API key found. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
                "APPSCI_AGENT_API_KEY, or FIREWORKS_API_KEY in your environment."
            )

        # A Fireworks model needs a Fireworks key, and a Fireworks id must not be aimed at a gateway.
        # Checked before any docker invocation, so neither costs a container. See the helpers.
        if self.config.harness is None:
            self._check_fireworks_routing()

        # Pass through API keys and auth config from host environment.
        # Docker containers don't inherit host env vars by default, so we must
        # explicitly pass them. Using just "-e KEY" (without =value) tells Docker
        # to copy the value from the host environment.
        for key in [
            "ANTHROPIC_API_KEY",      # LiteLLM's native fallback for first-party Anthropic
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "OPENAI_API_KEY",         # LiteLLM's native fallback for OpenAI (may be a gateway token)
            "OPENAI_BASE_URL",        # its matching endpoint -- forwarded as a pair, never alone
            "APPSCI_AGENT_API_KEY",   # Explicit gateway/proxy token (read by AgentConfig)
            "APPSCI_AGENT_BASE_URL",  # Custom LLM endpoint (read by AgentConfig)
            # Gateway identity headers, e.g. "X-Gateway-User: <email>" (read by AgentConfig and sent
            # as litellm extra_headers). Optional: absent => no extra headers, never a hard failure.
            "APPSCI_AGENT_CUSTOM_HEADERS",
            # Fireworks, for an agent under test served from there (AGENT_MODEL=fireworks_ai/...).
            # A provider-standard key LiteLLM resolves the endpoint from by itself, exactly like
            # ANTHROPIC_API_KEY above -- so there is no base-url var to forward beside it, and the
            # APPSCI_AGENT_* pair must be UNSET for such a run (see _check_fireworks_routing).
            # Both spellings LiteLLM accepts are forwarded, since either one is enough for it.
            *FIREWORKS_KEY_VARS,
        ]:
            # Match the deployer's precedence: explicit task settings override host defaults.
            # Otherwise the harness would test with different model credentials than its artifact.
            if key in os.environ and key not in self.config.extra_env:
                env_vars.extend(["-e", key])

        # The per-request LLM timeout, forwarded because BOTH readers of it live inside this container
        # (the agent's own LLM client and the mini-swe sub-agent), so a value exported only on the host
        # had no effect whatsoever: they always saw the 120s default. That silence was expensive to
        # diagnose. A long single generation -- `eval_generator` writing a multi_turn eval file in one
        # shot -- would blow the 120s ceiling, and litellm's `num_retries` would then re-issue it with
        # exponential backoff, so a build appeared to hang for many minutes and a call that eventually
        # succeeded reported a duration covering the whole retry sequence rather than one attempt.
        # It also made the manifest lie: MANIFEST_ENV_NAMES records this var, so a cell's provenance
        # claimed whatever the host set while the run had really used 120.
        #
        # Validated here, not just copied, for two reasons. `-e NAME` copies the host value verbatim,
        # and `NAME=` in an EnvironmentFile assigns the empty string -- which is NOT absent, so the
        # in-container `float(os.getenv(..., "120"))` would not fall back to its default but raise
        # ValueError at the first model call, deep into a build that has already cost containers. And a
        # nonsensical value (0, negative, unparseable) is always a mistake worth surfacing before any
        # docker invocation, like the Fireworks routing check above.
        timeout_raw = os.environ.get(LLM_TIMEOUT_VAR)
        if timeout_raw is not None and timeout_raw.strip():
            try:
                timeout_s = float(timeout_raw)
            except ValueError:
                raise RuntimeError(
                    f"{LLM_TIMEOUT_VAR}={timeout_raw!r} is not a number. It is a per-request timeout "
                    "in seconds (e.g. 300). Unset it to use the default."
                ) from None
            if timeout_s <= 0:
                raise RuntimeError(
                    f"{LLM_TIMEOUT_VAR}={timeout_raw!r} must be greater than zero: it is a "
                    "per-request timeout in seconds. Unset it to use the default."
                )
            env_vars.extend(["-e", f"{LLM_TIMEOUT_VAR}={timeout_s}"])

        # Build the docker run command
        cmd = [
            "docker", "run", "-d",
            "--name", self._container_name,
            "--network", self.config.network_name,
            # Resource limits for reproducibility and to prevent runaway containers
            "--memory", "16g",
            "--cpus", "4.0",
            # Map host.docker.internal -> the host gateway so the agent can reach the orchestrator's
            # host-side submit/deploy server. REQUIRED on Linux; see HOST_GATEWAY_RUN_ARGS.
            *HOST_GATEWAY_RUN_ARGS,
            # NOTE: the Docker socket is deliberately NOT mounted. Deployment is a host-side service
            # now (the agent's deploy_artifact tool -> the orchestrator's POST /deploy -> the shared
            # DockerArtifactDeployer), so the agent has no reason to talk to the daemon: it authors a
            # Dockerfile + server entry and the host builds and runs them. Nothing in the agent image
            # shells out to docker, and the task environment is reached over this Docker network (DB
            # DSNs, tool-API URLs) plus the read-only /data mount -- never via `docker exec`. Mounting
            # the socket would hand the agent-under-test root-equivalent control of the host daemon
            # (including every other container in the run) for no functional gain.
            # Mount spec file (using absolute path)
            "-v", f"{spec_path_abs}:/spec/spec.yaml:ro",
            # Expose port for artifact serving
            "-p", f"{self.CONTAINER_PORT}",
        ]

        # Mount the task's sample-data directory at /data, when it has one. Conditional on
        # purpose: a task that hands the agent labeled examples is testing file access rather
        # than requirements gathering, so several tasks deliberately provide none (order_to_cash
        # dropped its examples.jsonl for exactly that reason in #89). Name no file here.
        if self.config.data_dir:
            data_dir_abs = os.path.abspath(self.config.data_dir)
            cmd.extend(["-v", f"{data_dir_abs}:/data:ro"])
            logger.info(f"Mounting data directory: {data_dir_abs} -> /data")

        # Mount the agent-facing SOP / requirement docs (policy, tool reference, KPIs) at /sop.
        if self.config.sop_dir:
            sop_dir_abs = os.path.abspath(self.config.sop_dir)
            cmd.extend(["-v", f"{sop_dir_abs}:/sop:ro"])
            logger.info(f"Mounting SOP directory: {sop_dir_abs} -> /sop")

        # NO-USER arm only: the two halves of what the stakeholder knew, read-only, as siblings under
        # REQUIREMENTS_MOUNT. These are the mounts the comment on _AGENT_SNAPSHOT_DIRS calls
        # deliberately absent -- they ARE absent on every ordinary run, and the guard in `start()`
        # above is what keeps it that way. Each half is independent: a task may have documents, policy
        # extracts, or both, and the prompt names exactly the ones that were bound.
        if self.config.requirements_dir:
            requirements_abs = os.path.abspath(self.config.requirements_dir)
            cmd.extend(["-v", f"{requirements_abs}:{REQUIREMENTS_DOCS_MOUNT}:ro"])
            logger.info(
                f"Mounting requirement documents: {requirements_abs} -> {REQUIREMENTS_DOCS_MOUNT}"
            )
        if self.config.policies_dir:
            policies_abs = os.path.abspath(self.config.policies_dir)
            cmd.extend(["-v", f"{policies_abs}:{REQUIREMENTS_POLICIES_MOUNT}:ro"])
            logger.info(
                f"Mounting persona policy extracts: {policies_abs} -> {REQUIREMENTS_POLICIES_MOUNT}"
            )

        # Bind-mount the host artifact dir at the container's output dir (rw), so the Dockerfile +
        # server entry the agent authors are live-visible on the host. The host-side deployer (POST
        # /deploy) then builds straight from this directory -- no `docker cp`, no path guessing -- and
        # it's the same dir the eval-only pipeline re-deploys later.
        if self.config.host_artifact_dir:
            host_artifact_abs = os.path.abspath(self.config.host_artifact_dir)
            os.makedirs(host_artifact_abs, exist_ok=True)
            # World-writable because the writer inside the container is the mini-swe sub-agent, which
            # runs as uid 1001 (`swe`, created in appsci_agent/Dockerfile) rather than root -- that
            # uid separation is what keeps the agent-under-test out of the harness's own source. This
            # directory is created by whatever user runs the orchestrator, and Docker cannot set the
            # ownership of a bind mount (there is no `--mount ...,uid=` for a host path), so the mode
            # is the only lever: without it every artifact write dies with EACCES on a Linux host.
            # Files then land owned by 1001 on the host instead of 0 -- not a regression, since a
            # root sub-agent already made them root-owned, and the parent dir stays orchestrator-owned
            # so cleanup still works.
            os.chmod(host_artifact_abs, 0o777)
            cmd.extend(["-v", f"{host_artifact_abs}:{self.config.artifact_output_dir}:rw"])
            logger.info(
                f"Mounting artifact output dir: {host_artifact_abs} -> {self.config.artifact_output_dir}"
            )

        # Mount ONLY the agent's own per-source observability logs -- never the whole run dir -- so the
        # agent-under-test can't see other actors' files. In particular it must not see
        # events-usersim.jsonl (its filename would reveal it's talking to a user simulator). Each log is
        # a single-writer append file, so bind-mounting the individual files is enough; the publisher's
        # path (OBSERVABILITY_RUNS_DIR=/runs + trace_id) is unchanged. The agent container emits exactly
        # these two sources (agent loop + in-process mini-swe sub-agent) -- keep in sync with
        # Source.APPSCI_AGENT / Source.MINI_SWE_AGENT. For each source we mount BOTH the event stream
        # (events-*) AND the model-I/O store (model_calls-*, a separate per-source file added in #47),
        # so the tracer's mini-swe conversation view is populated.
        if self.config.runs_dir and self.config.observability_trace_id:
            trace_id = self.config.observability_trace_id
            trace_subdir = os.path.join(os.path.abspath(self.config.runs_dir), trace_id)
            os.makedirs(trace_subdir, exist_ok=True)
            fnames = [os.path.basename(p) for p in self._mounted_log_paths()]
            for host_file in self._mounted_log_paths():
                # Pre-create so Docker bind-mounts a FILE (an absent target is created as a directory).
                #
                # NOTE: `events-mini_swe_agent.jsonl` stays EMPTY in every run, and that is expected.
                # The container runs one publisher, which appends every event to its OWN log path --
                # `events-appsci_agent.jsonl` -- whatever `source` an individual event carries. So the
                # sub-agent's `swe.task_start` / `swe.bash_command` / `swe.task_complete` events are in
                # the agent's stream, not this file. The model-I/O store DOES split per source
                # (`model_calls-mini_swe_agent.jsonl` fills up), which makes the empty event file look
                # like missing instrumentation. It is not: the tracer merges all `events-*` on read.
                open(host_file, "a").close()
                cmd.extend(["-v", f"{host_file}:/runs/{trace_id}/{os.path.basename(host_file)}:rw"])
            logger.info(f"Mounting agent-only observability logs {fnames} under /runs/{trace_id}")

        if self.config.harness is not None:
            from .harness_inputs import HTTP_CONTRACT_PATH, INPUT_MOUNT
            from .native_harness import NATIVE_ENV

            if not self.config.harness_inputs_dir:
                raise ValueError("external harness requires an input bundle")
            input_dir = Path(self.config.harness_inputs_dir).resolve()
            if not all((input_dir / name).is_file() for name in (
                "context.json", "prompt.md", "http_contract.md",
            )):
                raise ValueError("external harness input bundle is missing context.json, prompt.md or http_contract.md")
            deploy_url = self.config.orchestrator_submit_url.removesuffix("/submit").rstrip("/") + "/deploy"
            cmd.extend([
                "--workdir", "/work",
                "-v", f"{input_dir}:{INPUT_MOUNT}:ro",
                # Native JSON event streams are Docker stdout, not tracer-mounted JSONL.
                "--log-driver", "json-file", "--log-opt", "max-size=50m",
                "--log-opt", "max-file=2",
            ])
            env_vars.extend([
                "-e", f"APPSCI_HARNESS_NAME={self.config.harness.name}",
                "-e", f"APPSCI_HARNESS_PROMPT={INPUT_MOUNT}/prompt.md",
                "-e", f"APPSCI_HARNESS_CONTEXT={INPUT_MOUNT}/context.json",
                "-e", f"APPSCI_HARNESS_HTTP_CONTRACT={HTTP_CONTRACT_PATH}",
                "-e", f"APPSCI_HARNESS_ARTIFACT_DIR={self.config.artifact_output_dir}",
                "-e", f"APPSCI_USERSIM_ASK_URL={self.config.appsci_usersim_url.rstrip('/')}/ask",
                "-e", f"ORCHESTRATOR_DEPLOY_URL={deploy_url}",
                "-e", f"ORCHESTRATOR_DEPLOY_STATUS_URL={deploy_url}/status",
            ])
            if self.config.harness.adapter:
                env_vars.extend([
                    "-e", f"APPSCI_HARNESS_ADAPTER={self.config.harness.adapter}",
                    "-e", f"APPSCI_HARNESS_AUTH={self.config.harness.auth}",
                ])
                for name in NATIVE_ENV:
                    if os.environ.get(name):
                        if name in self.config.extra_env:
                            raise ValueError(f"native harness environment variable {name} conflicts with the task")
                        env_vars.extend(["-e", name])
            for name in self.config.harness.passthrough_env:
                if not os.environ.get(name):
                    raise ValueError(f"harness requires environment variable {name}")
                if name in self.config.extra_env:
                    raise ValueError(f"harness environment variable {name} conflicts with the task")
                env_vars.extend(["-e", name])
        cmd.extend(env_vars)
        cmd.append(self.config.harness.image if self.config.harness else self.config.image)
        if self.config.harness:
            cmd.extend(self.config.harness.command)

        logger.info(f"Starting agent container: {self._container_name}")

        # Remove any pre-existing container with the same name before starting.
        # The name is fixed per task (appsci-agent-<task_id>), so a prior run that
        # crashed or didn't finish cleanup leaves a stale container behind, and
        # `docker run --name` would then fail with exit 125 ("name already in
        # use"). This makes start() idempotent. Safe if nothing is there.
        subprocess.run(
            ["docker", "rm", "-f", self._container_name],
            capture_output=True,
        )

        subprocess.run(cmd, check=True, capture_output=True)

        logger.info(f"Agent container started: {self._container_name}")
        return self

    def get_artifact_url(self) -> str:
        """Get the URL where the artifact will be served.

        Returns:
            The artifact endpoint URL on the Docker network.
        """
        return f"http://{self.container_name}:{self.CONTAINER_PORT}"

    def wait_for_exit(self, timeout: int = 7200) -> int:
        """Wait for the agent container to exit.

        Args:
            timeout: Maximum seconds to wait (default 2 hours).

        Returns:
            The container exit code.

        Raises:
            TimeoutError: If container doesn't exit within timeout.
        """
        if not self._container_name:
            raise RuntimeError("Agent not started")

        start_time = time.time()

        while time.time() - start_time < timeout:
            # Same budget enforcement as is_running(), for a caller that waits here instead: this loop
            # is then the only clock, and an unbounded writer would otherwise have the full timeout
            # (2 hours by default) to fill the disk.
            self._kill_if_over_log_budget()

            result = subprocess.run(
                [
                    "docker", "inspect",
                    "--format", "{{.State.Running}}",
                    self._container_name,
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                # Container doesn't exist anymore
                return -1

            if result.stdout.strip() == "false":
                # Container has stopped, get exit code
                exit_result = subprocess.run(
                    [
                        "docker", "inspect",
                        "--format", "{{.State.ExitCode}}",
                        self._container_name,
                    ],
                    capture_output=True,
                    text=True,
                )
                return int(exit_result.stdout.strip())

            time.sleep(5)

        raise TimeoutError(
            f"Agent container {self._container_name} did not exit within {timeout} seconds"
        )

    def start_harness_log_capture(self, path: Path) -> None:
        """Make native activity visible on the host while the build is still running."""
        if self.config.harness is None or not self._container_name or self._harness_log_capture:
            return
        from .harness_logging import HarnessLogCapture

        capture = HarnessLogCapture(self._container_name, path, {
            **os.environ, **(self.config.extra_env or {}),
            "APPSCI_USERSIM_TOKEN": self.config.appsci_usersim_token,
        })
        try:
            capture.start()
            self._harness_log_capture = capture
        except OSError:
            logger.exception("Live native log capture failed; cleanup will attempt a log snapshot")

    def stop_harness_log_capture(self) -> None:
        if self._harness_log_capture is not None:
            try:
                self._harness_log_capture.stop()
            except (OSError, subprocess.TimeoutExpired, RuntimeError):
                logger.exception("Could not stop native log observer cleanly")
            finally:
                self._harness_log_capture = None

    def get_logs(self) -> str:
        """Get the container logs.

        Returns:
            The container stdout/stderr logs.
        """
        if not self._container_name:
            return ""

        result = subprocess.run(
            ["docker", "logs", self._container_name],
            capture_output=True,
            text=True,
        )
        return result.stdout + result.stderr

    def is_running(self) -> bool:
        """Check if the container is still running.

        Returns:
            True if container is running, False if stopped or doesn't exist.

        Also enforces the agent-writable log budget. THE CHECK LIVES HERE BECAUSE THIS IS THE CLOCK:
        the orchestrator's wait loop polls this every 2 seconds for the whole build phase
        (orchestrator/run.py, _wait_for_agent_completion), and it is the only periodic call this
        manager gets. A container killed for blowing the budget then reports not-running here and a
        non-zero exit code from ``get_exit_code``, so the caller's existing crash path handles it with
        the reason in ``log_budget_error`` and in the log.
        """
        if not self._container_name:
            return False

        if self._kill_if_over_log_budget():
            # Just killed it, so answer for the kill rather than asking docker. `docker kill` returns
            # once the signal is delivered, and the daemon can still report State.Running=true for a
            # moment after that -- which would hand the caller a container it should stop waiting on.
            return False

        result = subprocess.run(
            [
                "docker", "inspect",
                "--format", "{{.State.Running}}",
                self._container_name,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return False

        return result.stdout.strip() == "true"

    def get_exit_code(self) -> int | None:
        """Get the container's exit code if it has stopped.

        Returns:
            Exit code if container has stopped, None if still running or doesn't exist.
        """
        if not self._container_name:
            return None

        result = subprocess.run(
            [
                "docker", "inspect",
                "--format", "{{.State.Running}} {{.State.ExitCode}}",
                self._container_name,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return None

        parts = result.stdout.strip().split()
        if len(parts) != 2:
            return None

        is_running, exit_code = parts
        if is_running == "true":
            return None  # Still running

        return int(exit_code)

    def snapshot_artifact(self, dest_dir: str) -> dict | None:
        """Copy the built artifact out of the container to the host.

        This gives the run a durable, inspectable copy of what the agent
        produced (the "output" of the run), independent of the container's
        lifetime -- so observability tooling can read it after the container
        is removed. Uses ``docker cp``, which works even on an exited (but not
        yet removed) container, so call this BEFORE ``stop()``.

        Args:
            dest_dir: Host directory to copy the artifact contents into
                (created if needed).

        Returns:
            A manifest dict ``{"files": [{"path", "size"}], "file_count",
            "total_bytes"}`` written alongside ``dest_dir`` as
            ``artifact_manifest.json``, or None if nothing was copied (e.g. the
            agent crashed before creating the artifact directory).
        """
        if not self._container_name:
            return None

        os.makedirs(dest_dir, exist_ok=True)

        # `<src>/.` copies the *contents* of the artifact dir into dest_dir
        # (rather than nesting a further `artifact/` level).
        src = f"{self._container_name}:{self.config.artifact_output_dir}/."
        result = subprocess.run(
            ["docker", "cp", src, dest_dir],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "Artifact snapshot skipped for %s: %s",
                self._container_name,
                result.stderr.strip() or "artifact directory not found",
            )
            return None

        # Build a manifest so consumers can render a file tree without walking.
        files: list[dict] = []
        total = 0
        for dirpath, _dirnames, filenames in os.walk(dest_dir):
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                files.append({"path": os.path.relpath(full, dest_dir), "size": size})
                total += size

        files.sort(key=lambda f: f["path"])
        manifest = {"files": files, "file_count": len(files), "total_bytes": total}
        manifest_path = os.path.join(os.path.dirname(dest_dir.rstrip("/")), "artifact_manifest.json")
        try:
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)
        except OSError as e:
            logger.warning("Failed to write artifact manifest: %s", e)

        logger.info(
            "Artifact snapshot for %s: %d files (%d bytes) -> %s",
            self._container_name, len(files), total, dest_dir,
        )
        return manifest

    def snapshot_workspace(self, dest_dir: str) -> dict | None:
        """Copy the agent container's filesystem (everything it was given + produced) to the host --
        the counterpart to ``snapshot_artifact`` but broad: the mounted inputs (``/data`` task data,
        ``/spec``) plus what it generated (``/workspace``, ``/work``), so the tracer can browse the
        whole agent-docker filesystem after the container is gone. Deliberately excludes ``/runs``
        (the observability bind-mount -- copying it would recurse into this snapshot). Delegates to
        :func:`snapshot_container_dirs`."""
        return snapshot_container_dirs(self._container_name, _AGENT_SNAPSHOT_DIRS, dest_dir)

    def halt(self) -> None:
        """Stop the agent container but KEEP it, so it can still be read from.

        Called at the build->eval handover. The agent must be inert before the eval environment
        exists: its own loop has ended by then (the entrypoint parks on a signal wait after
        submitting), but the eval env comes up at the same container name and network alias the agent
        spent the whole build phase talking to, so a background process the coding subagent left
        behind would reach it. Halting the container removes that possibility structurally rather than
        relying on the agent being well-behaved.

        Deliberately does NOT remove the container or clear the recorded name: ``get_logs`` and the
        ``snapshot_*`` methods still need something to read, and ``docker logs`` / ``docker cp`` both
        work on a stopped container. Removal stays in ``remove()``, called during cleanup once the
        snapshots are taken.

        Safe to call multiple times or if the container doesn't exist.
        """
        if not self._container_name:
            return

        logger.info(f"Halting agent container (keeping it readable): {self._container_name}")
        subprocess.run(
            ["docker", "stop", self._container_name],
            capture_output=True,
        )
        self.stop_harness_log_capture()

    def remove(self) -> None:
        """Remove the agent container. Call only after every snapshot / log read is done.

        Safe to call multiple times or if the container doesn't exist.
        """
        self.stop_harness_log_capture()
        if not self._container_name:
            return

        logger.info(f"Removing agent container: {self._container_name}")
        subprocess.run(
            ["docker", "rm", "-f", self._container_name],
            capture_output=True,
        )

        self._container_name = None

    def stop(self) -> None:
        """Stop and remove the agent container -- ``halt()`` then ``remove()``.

        Retained as the single call for callers that are finished with the container (cleanup, the
        context-manager exit). Code that still needs to read from the container must call ``halt()``
        and defer ``remove()``.

        Safe to call multiple times or if container doesn't exist.
        """
        self.halt()
        self.remove()

    def __enter__(self) -> "AppSciAgentContainerManager":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
