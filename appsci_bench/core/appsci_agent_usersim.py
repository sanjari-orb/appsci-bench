"""Benchmark-side appsci-agent-user-sim lifecycle manager.

The appsci agent user sim is a SEPARATE service: its core logic (persona rendering, the LLM call, the
`/health` + `/ask` HTTP API) lives in the `appsci_bench/appsci_agent_user_sim/` subpackage -- decoupled from
the rest of the benchmark, so it runs in its own container. This module is the BENCHMARK's handle on
that service -- it owns the service's lifecycle, not its behavior:

  - starts the appsci-agent-user-sim (either as a local uvicorn thread or as a Docker container) with the run's
    startup params (task, persona level L1/L2/L3, model) -- the agent under test never sees these;
  - polls `/health` until the service is ready;
  - hands the agent the connection info (URL + per-run token) out-of-band;
  - tears down the service when done.

There is NO simulator logic here. See docs/user_simulator_api.md for the HTTP contract the service
exposes.

The service has a single lifecycle implementation:
  - DockerAppsciAgentUserSimService: container-based, for `python -m appsci_bench.orchestrator.run`

The `AppsciAgentUserSimulator` Protocol below is the in-process answer() contract -- the local-call
analogue of the HTTP `/ask` endpoint.
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import time

from typing import Protocol, runtime_checkable

from .agent_container import snapshot_container_dirs

logger = logging.getLogger(__name__)

__all__ = ["AppsciAgentUserSimulator", "DockerAppsciAgentUserSimService"]

#: Container-filesystem snapshot set for the user-sim (see `snapshot_container_dirs`): the dirs to copy
#: out so the tracer can show what the sim was given. It is mounted /spec (spec.yaml + persona.yaml) and
#: /user_sim_docs (the sim-only documents its persona internalized, issue #76), and authors nothing, so
#: its set is just those two. The counterpart for the agent is _AGENT_SNAPSHOT_DIRS in agent_container.py,
#: which deliberately omits /user_sim_docs -- that asymmetry is what forces elicitation.
_USERSIM_SNAPSHOT_DIRS = ("/spec", "/user_sim_docs")


@runtime_checkable
class AppsciAgentUserSimulator(Protocol):
    """In-process answer contract (local analogue of the service's HTTP `/ask`).

    The agent asks; the simulator answers. The simulator never leads, never ends the conversation,
    and answers only from the persona's known facts (no fabrication).
    """
    def answer(self, question: str, history: list[dict]) -> str: ...


class DockerAppsciAgentUserSimService:
    """Handle on a running appsci-agent-user-sim Docker container (lifecycle only -- no simulator logic).

    Container-based implementation for orchestrated runs
    (`python -m appsci_bench.orchestrator.run`).

    The AppsciAgentUserSimService manages the appsci agent user sim container which exposes /health and /ask endpoints.
    The container is started with environment variables for configuration and mounts for spec/persona
    files.

    Example usage:
        usersim = DockerAppsciAgentUserSimService(
            task_id="order_to_cash",
            spec_path="/path/to/spec.yaml",
            persona_path="/path/to/persona.yaml",
            network_name="bench-network",
        )
        usersim.start()
        # usersim.url = "http://usersim-order_to_cash:8000"
        # usersim.token = "<generated token>"
        # ... agent uses usersim.url with usersim.token ...
        usersim.stop()

    Attributes:
        task_id: The task identifier used for container naming.
        spec_path: Path to the task spec.yaml file.
        persona_path: Path to the persona YAML file.
        model: Optional LLM model name for the appsci agent user sim.
        network_name: Docker network to attach the container to.
        image: Docker image name for the appsci agent user sim.
        url: URL of the running service (set after start()).
        token: Bearer token for authenticating with the service.
    """

    DEFAULT_IMAGE = "appsci-bench-usersim:latest"
    CONTAINER_PORT = 8000
    HEALTH_TIMEOUT = 60  # seconds to wait for container to be healthy
    #: Where the task's user_sim_docs/ tree is mounted. Mounted into THIS container only -- the agent
    #: container never gets it, which is what forces elicitation (issue #76). Keep in sync with
    #: appsci_agent_user_sim.service.app.DEFAULT_DOCS_DIR and _USERSIM_SNAPSHOT_DIRS (above).
    DOCS_MOUNT = "/user_sim_docs"

    def __init__(
        self,
        *,
        task_id: str,
        spec_path: str,
        persona_path: str,
        network_name: str = "bench-network",
        model: str | None = None,
        image: str | None = None,
        observability_trace_id: str | None = None,
        runs_dir: str | None = None,
        docs_dir: str | None = None,
    ) -> None:
        """Initialize the DockerAppsciAgentUserSimService.

        Args:
            task_id: The task identifier (e.g., "order_to_cash").
            spec_path: Absolute path to the task spec.yaml file.
            persona_path: Absolute path to the persona YAML file.
            network_name: Docker network name for container communication.
            model: Optional LLM model name for this sim (env APPSCI_USERSIM_MODEL, else MODEL_NAME).
            image: Docker image name (defaults to DEFAULT_IMAGE).
            observability_trace_id: Trace ID for observability (if enabled).
            runs_dir: Path to runs directory to mount (if observability enabled).
            docs_dir: Path to the task's user_sim_docs/ directory, mounted read-only at DOCS_MOUNT so
                the bound persona's `known_docs` can be loaded. None when the task has no such
                directory (its personas must then declare no documents, or startup fails loud).
        """
        self.task_id = task_id
        self.spec_path = spec_path
        self.persona_path = persona_path
        self.network_name = network_name
        self.model = model
        self.image = image or self.DEFAULT_IMAGE
        self.docs_dir = docs_dir

        # Observability configuration
        self.observability_trace_id = observability_trace_id
        self.runs_dir = runs_dir

        self.url: str | None = None
        self.token: str | None = None
        self._container_name: str | None = None

    @property
    def container_name(self) -> str:
        """Docker container name for the appsci agent user sim."""
        return f"appsci-usersim-{self.task_id}"

    def snapshot_files(self, dest_dir: str) -> dict | None:
        """Copy the files this container was given (its mounted /spec: spec.yaml + persona.yaml, and
        /user_sim_docs: the documents its persona internalized) to the host so the tracer can show what
        the user-sim had access to. Call BEFORE ``stop()`` (which removes the container). Delegates to
        the shared container-snapshot helper."""
        return snapshot_container_dirs(self.container_name, _USERSIM_SNAPSHOT_DIRS, dest_dir)

    def start(self) -> "DockerAppsciAgentUserSimService":
        """Start the appsci-agent-user-sim Docker container and wait for it to be healthy.

        Creates a Docker container with:
        - Mounted spec.yaml and persona.yaml files
        - Environment variables for configuration
        - Connection to the bench-network

        Returns:
            self for chaining.

        Raises:
            subprocess.CalledProcessError: If docker run fails.
            TimeoutError: If the container doesn't become healthy in time.
        """
        # Generate a secure token for this run
        self.token = secrets.token_urlsafe(32)
        self._container_name = self.container_name

        # Build environment variables
        env_vars = [
            "-e", f"APPSCI_USERSIM_TOKEN={self.token}",
            "-e", "APPSCI_USERSIM_SPEC_PATH=/spec/spec.yaml",
            "-e", "APPSCI_USERSIM_PERSONA_PATH=/spec/persona.yaml",
            "-e", f"APPSCI_USERSIM_DOCS_DIR={self.DOCS_MOUNT}",
        ]

        # Add model if specified. APPSCI_USERSIM_MODEL is this sim's own model var; MODEL_NAME (passed
        # through below) remains its fallback, so an unset one still means "the shared Claude id".
        if self.model:
            env_vars.extend(["-e", f"APPSCI_USERSIM_MODEL={self.model}"])

        # Pass through model config + API credentials from host environment
        # This sim's model var (when not passed explicitly above) + the shared fallback: MODEL_NAME
        # For a model gateway: MODEL_BASE_URL + MODEL_API_KEY (+ MODEL_CUSTOM_HEADERS for the
        # gateway's identity headers, e.g. X-Gateway-User -- optional, absent => none sent)
        # For direct API: ANTHROPIC_API_KEY / OPENAI_API_KEY
        # OPENAI_BASE_URL travels WITH OPENAI_API_KEY: that key may hold a gateway token (the
        # gateway setup maps one onto it), which aimed at api.openai.com would fail confusingly.
        passthrough = ["MODEL_NAME", "MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_CUSTOM_HEADERS",
                       "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                       "OPENAI_API_KEY", "OPENAI_BASE_URL",
                       # Per-request LiteLLM timeout. The user-sim calls its model through
                       # `appsci_bench.common.model_config`, which reads this var -- and that module
                       # runs INSIDE this container, so without forwarding it the user-sim silently
                       # keeps the 120s default while the host and the manifest both say otherwise.
                       # Same gap as the agent container had; a slow-serving model turns it into
                       # repeated 120s timeouts plus litellm's retries rather than one honest wait.
                       "APPSCI_LLM_TIMEOUT_S"]
        if not self.model:
            passthrough.insert(0, "APPSCI_USERSIM_MODEL")
        for key in passthrough:
            if key in os.environ:
                env_vars.extend(["-e", f"{key}={os.environ[key]}"])

        # Add observability configuration. The runs dir is mounted at /runs
        # (see below); the publisher derives its per-source DB path from it.
        if self.observability_trace_id and self.runs_dir:
            env_vars.extend([
                "-e", "OBSERVABILITY_RUNS_DIR=/runs",
                "-e", f"OBSERVABILITY_TRACE_ID={self.observability_trace_id}",
                "-e", f"OBSERVABILITY_TASK={self.task_id}",
            ])
            # Forward per-event emit logging (orchestrator --verbose) into this container's logs.
            if os.environ.get("OBSERVABILITY_LOG_EVENTS"):
                env_vars.extend(["-e", f"OBSERVABILITY_LOG_EVENTS={os.environ['OBSERVABILITY_LOG_EVENTS']}"])

        # Build the docker run command
        cmd = [
            "docker", "run", "-d",
            "--name", self._container_name,
            "--network", self.network_name,
            # Mount spec and persona files
            "-v", f"{self.spec_path}:/spec/spec.yaml:ro",
            "-v", f"{self.persona_path}:/spec/persona.yaml:ro",
        ]

        # Mount the task's user_sim_docs/ tree (read-only) so the bound persona's `known_docs` can be
        # loaded. This is the ASYMMETRY that makes elicitation real: the agent container is never given
        # this directory (see agent_container.py -- it mounts /data and /sop only), so the material here
        # is reachable only by asking the sim. Absent for tasks that have no such directory.
        if self.docs_dir:
            docs_abs = os.path.abspath(self.docs_dir)
            cmd.extend(["-v", f"{docs_abs}:{self.DOCS_MOUNT}:ro"])
            logger.info(f"Mounting user-sim docs directory: {docs_abs} -> {self.DOCS_MOUNT}")

        # Mount ONLY the user-sim's own per-source logs -- never the whole run dir -- so it can't read
        # the agent's or orchestrator's files either (symmetric least-privilege with the agent
        # container). Both the event stream (events-usersim) and the model-I/O store
        # (model_calls-usersim, a separate per-source file added in #47); single-writer append files,
        # so file bind-mounts suffice.
        if self.runs_dir and self.observability_trace_id:
            trace_id = self.observability_trace_id
            trace_subdir = os.path.join(os.path.abspath(self.runs_dir), trace_id)
            os.makedirs(trace_subdir, exist_ok=True)
            for fname in ("events-usersim.jsonl", "model_calls-usersim.jsonl"):
                host_file = os.path.join(trace_subdir, fname)
                # Pre-create so Docker bind-mounts a FILE (an absent target is created as a directory).
                open(host_file, "a").close()
                cmd.extend(["-v", f"{host_file}:/runs/{trace_id}/{fname}:rw"])

        cmd.extend(env_vars)
        cmd.append(self.image)

        logger.info(f"Starting appsci-agent-user-sim container: {self._container_name}")

        # Remove any stale container with the same (fixed, per-task) name so a
        # prior run's leftover doesn't make `docker run --name` fail with exit
        # 125. Idempotent; safe if nothing is there.
        subprocess.run(
            ["docker", "rm", "-f", self._container_name],
            capture_output=True,
        )

        subprocess.run(cmd, check=True, capture_output=True)

        # Set the URL (container name is DNS-resolvable on Docker network)
        self.url = f"http://{self._container_name}:{self.CONTAINER_PORT}"

        # Wait for the service to be healthy
        self._wait_for_health()

        logger.info(f"Appsci-agent-user-sim service ready at {self.url}")
        return self

    def _wait_for_health(self) -> None:
        """Poll /health until the service responds 200.

        Uses docker exec to check health from inside the container, since the container's
        internal URL (http://container-name:8000) is not reachable from the host.

        Raises:
            TimeoutError: If health check doesn't pass within HEALTH_TIMEOUT.
        """
        if not self._container_name or not self.token:
            raise RuntimeError("Service not started")

        start_time = time.time()

        while time.time() - start_time < self.HEALTH_TIMEOUT:
            try:
                # Check health from inside the container using curl/wget/python
                result = subprocess.run(
                    [
                        "docker", "exec", self._container_name,
                        "python", "-c",
                        f"import urllib.request; "
                        f"req = urllib.request.Request('http://localhost:8000/health'); "
                        f"req.add_header('Authorization', 'Bearer {self.token}'); "
                        f"urllib.request.urlopen(req, timeout=5)"
                    ],
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    return
            except (subprocess.TimeoutExpired, subprocess.SubprocessError):
                pass  # Service not ready yet

            time.sleep(1)

        raise TimeoutError(
            f"Appsci-agent-user-sim container {self._container_name} did not become healthy "
            f"within {self.HEALTH_TIMEOUT} seconds"
        )

    def stop(self) -> None:
        """Stop and remove the appsci-agent-user-sim container.

        Safe to call multiple times or if container doesn't exist.
        """
        if not self._container_name:
            return

        logger.info(f"Stopping appsci-agent-user-sim container: {self._container_name}")

        # Stop the container (ignore errors if already stopped)
        subprocess.run(
            ["docker", "stop", self._container_name],
            capture_output=True,
        )

        # Remove the container (ignore errors if already removed)
        subprocess.run(
            ["docker", "rm", "-f", self._container_name],
            capture_output=True,
        )

        self.url = None
        self._container_name = None

    def __enter__(self) -> "DockerAppsciAgentUserSimService":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
