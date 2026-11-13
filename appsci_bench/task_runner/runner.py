"""TaskRunner - main orchestrator for task environment lifecycle.

This module provides the TaskRunner class which coordinates all the steps
needed to spin up a task environment, prepare it with data, and tear it down.

A run brings up an environment TWICE, once per phase: the build env the AppSci agent explores, and
then -- after that one is torn down -- the eval env the artifact is scored against. Both come up from
the same compose file under the same project name, so they share a container name and network alias.
Which phase's data is loaded is the only difference, and it comes from the task's per-phase
declaration. See docs/task_env_split.md and docs/task_runner.md.
"""

import logging
import tempfile
from pathlib import Path

from .bundle import BundleManager
from .config import PHASE_BUILD, PHASES, TaskConfig, TaskEnvPhase
from .containers import ContainerManager

logger = logging.getLogger(__name__)


class TaskRunner:
    """Manages task environment lifecycle.

    The TaskRunner orchestrates the complete process of setting up a task
    environment for the appsci-agent to work in. It handles:

    1. Parsing task configuration from spec.yaml
    2. Creating the Docker network (if needed)
    3. Starting container via docker-compose
    4. Waiting for health checks to pass
    5. Bundling task data files
    6. Transferring the bundle into the workspace container
    7. Extracting the bundle
    8. Running the setup script to initialize DB/KB/API
    9. Returning an environment dict for lifecycle management
    10. Tearing down containers when done

    The TaskRunner delegates specific operations to helper classes:
    - TaskConfig: Parses spec.yaml
    - BundleManager: Creates tar.gz bundles
    - ContainerManager: Handles Docker operations

    Note: The agent receives its task context (resources, db_url, etc.) via
    TaskContext built from spec.yaml, not from TaskRunner. The environment dict
    returned by start() is only for container lifecycle management (stop()).

    Example usage:
        from pathlib import Path
        from appsci_bench.task_runner import TaskRunner

        # Initialize runner
        runner = TaskRunner(tasks_dir=Path("tasks"))

        # Start task environment
        env = runner.start("order_to_cash")
        # env = {
        #     "workspace_container": "o2c-workspace-1",
        #     "compose_project": "o2c",
        #     "compose_file": "tasks/order_to_cash/environment/docker-compose.yml",
        #     "network_name": "bench-network",
        # }

        # ... run agent with env ...

        # Tear down (can be hours later)
        runner.stop(env)

    Attributes:
        tasks_dir: Path to the tasks/ directory containing all task folders.
    """

    def __init__(self, tasks_dir: Path):
        """Initialize the TaskRunner.

        Args:
            tasks_dir: Path to the tasks/ directory containing all task folders.
                Each task should have a spec.yaml file defining its configuration.

        Example:
            runner = TaskRunner(tasks_dir=Path("tasks"))
        """
        self.tasks_dir = tasks_dir
        self._bundle_manager = BundleManager()
        self._container_manager = ContainerManager()

    def start(
        self, task_id: str, phase: str = PHASE_BUILD, *,
        lane: int = 0, env_overlay: dict[str, str] | None = None,
    ) -> dict | None:
        """Spin up one phase's task environment and return the environment dict.

        This method orchestrates the full setup sequence by calling private
        helper methods for each step.

        Args:
            task_id: The task identifier (e.g., "order_to_cash").
                Must correspond to a directory under tasks_dir with a spec.yaml.
            phase: Which phase's environment to bring up -- "build" (default) or "eval". The phase
                selects the data bundled in and the script that loads it; everything else (image,
                compose file, container name, network alias) is shared, so the artifact reaches the
                same address in both phases.
            lane: Which of several parallel copies of this environment to bring up. 0 (the default)
                is THE environment -- the historic names, unsuffixed -- so every existing caller is
                unaffected. A non-zero lane gets its own compose project (`<project>-l<n>`) and
                therefore its own containers, volumes and workspace name, which is what lets the eval
                phase run several cases at once without them sharing a store. The caller must hand
                the resulting `workspace_container` to anything that addresses the environment: the
                name is no longer derivable from the task id alone.
            env_overlay: Extra environment for the `compose up`, so a lane's stack can differ from
                lane 0's where the compose file reads `${VAR}` (a per-lane tool token).

        Returns:
            None when the task declares this phase needs no environment (``eval: none``), so a
            caller can start unconditionally and pass the result straight to stop().
            A dict containing information needed for container lifecycle management.
            This dict should be passed to stop() to tear down the environment.

            {
                "workspace_container": "o2c-workspace-1",
                "compose_project": "o2c",
                "compose_file": "tasks/order_to_cash/environment/docker-compose.yml",
                "network_name": "bench-network",
            }

            Note: The agent receives its task context (resources, db_url, etc.)
            via TaskContext built from spec.yaml, not from this return value.

        Raises:
            FileNotFoundError: If task spec.yaml doesn't exist.
            KeyError: If required configuration is missing from spec.yaml.
            subprocess.CalledProcessError: If any Docker operation fails.
            TimeoutError: If health checks don't pass within timeout.

        Example:
            env = runner.start("order_to_cash", phase="build")
            print(env["workspace_container"])  # o2c-workspace-1
            # ... later ...
            runner.stop(env)
        """
        if phase not in PHASES:
            raise ValueError(f"unknown run phase {phase!r}; expected one of {PHASES}")

        config = TaskConfig.from_spec(task_id, self.tasks_dir)
        env_phase = config.phase(phase)
        if env_phase is None:
            logger.info("Task %s declares no %s environment; nothing to start.", task_id, phase)
            return None

        project_name = self._derive_project_name(config.workspace_name, lane=lane)
        workspace_container = f"{project_name}-workspace-1"

        reused = self._start_containers(config, project_name, env_overlay)
        self._transfer_and_extract_bundle(config, env_phase, workspace_container)
        self._run_setup_script(env_phase, workspace_container)

        return self._build_environment_dict(
            config, project_name, workspace_container, phase=phase, reused=reused, lane=lane,
        )

    def ensure_network(self, task_id: str) -> str:
        """Create this task's Docker network if it does not exist, and return its name.

        Normally a side effect of ``start()``: the network is what the workspace container, the agent
        and the deployed artifact find each other on. A task that declares ``eval: none`` starts no
        environment at all, so nothing creates it -- and an eval trial of such a task still deploys the
        artifact with ``--network <this>``, which fails outright if it is missing. Calling this makes
        that path stand on its own rather than on a network some earlier run happened to leave behind.

        Idempotent: an existing network is not an error.

        Args:
            task_id: The task identifier.

        Returns:
            The network name from the task's spec.
        """
        config = TaskConfig.from_spec(task_id, self.tasks_dir)
        self._create_network(config.network_name)
        return config.network_name

    def teardown_descriptor(self, task_id: str, *, lane: int = 0) -> dict:
        """The minimal environment dict ``stop()`` needs, derivable without starting anything.

        Used to make the build->eval handover safe to fail. Both phases come up from the same compose
        file under the same project name, so teardown needs nothing a phase's own start() produced --
        which means a caller can record this BEFORE starting a phase and still bring the stack down if
        that start throws part-way through, instead of leaking half-created containers.

        Args:
            task_id: The task identifier.
            lane: Which parallel copy of the environment to describe (see `start`). Must match the
                lane that was started, or this brings down a different stack than the caller means.

        Returns:
            A dict carrying compose_file, compose_project and network_name.
        """
        config = TaskConfig.from_spec(task_id, self.tasks_dir)
        project_name = self._derive_project_name(config.workspace_name, lane=lane)
        return {
            "compose_file": str(config.docker_compose),
            "compose_project": project_name,
            "network_name": config.network_name,
            "workspace_container": f"{project_name}-workspace-1",
            "lane": lane,
        }

    def stop(self, environment: dict | None) -> None:
        """Tear down a task environment. A None environment (a phase that declares it needs none) is
        a no-op, so a caller can pass whatever start() returned straight through.

        Stops and removes all containers associated with the task environment.
        This method uses the information in the environment dict (returned by
        start()) to know which containers to stop.

        This method can be called hours after start() - it only needs the
        environment dict which can be persisted (e.g., to JSON) between calls.

        Args:
            environment: The dict returned by start(). Must contain:
                - compose_file: Path to docker-compose.yml
                - compose_project: Project name used when starting

        Raises:
            subprocess.CalledProcessError: If docker-compose down fails.
            KeyError: If required keys are missing from environment dict.

        Example:
            # Start environment
            env = runner.start("order_to_cash")

            # ... hours later ...

            # Stop environment
            runner.stop(env)
        """
        if environment is None:
            return
        compose_file = Path(environment["compose_file"])
        project_name = environment["compose_project"]
        self._container_manager.stop(compose_file, project_name)

    # -------------------------------------------------------------------------
    # Private helper methods
    # -------------------------------------------------------------------------

    def _derive_project_name(self, workspace_name: str, *, lane: int = 0) -> str:
        """Derive docker-compose project name from workspace name.

        The project name is used as a prefix for container names and allows
        running multiple tasks simultaneously.

        Lane 0 derives the historic, unsuffixed name, so nothing that already addresses an environment
        by its container name changes meaning. Lanes above 0 get `-l<n>`, giving each its own compose
        project -- and compose keys containers, volumes and networks off the project, so two lanes
        share nothing but the external bench network.

        Args:
            workspace_name: The workspace container name suffix (e.g., "o2c-workspace").
            lane: Parallel-copy index; 0 is the unsuffixed original.

        Returns:
            Project name derived from workspace_name (e.g., "o2c", or "o2c-l1" for lane 1).

        Example:
            project = self._derive_project_name("o2c-workspace")  # returns "o2c"
        """
        # Extract prefix from workspace_name (e.g., "o2c-workspace" -> "o2c")
        base = workspace_name.rsplit("-", 1)[0] if "-" in workspace_name else workspace_name
        return base if lane == 0 else f"{base}-l{lane}"

    def _create_network(self, network_name: str) -> None:
        """Create a Docker network for container communication.

        Creates the network if it doesn't already exist. Safe to call
        multiple times.

        Args:
            network_name: Name of the Docker network (e.g., "bench-network").

        Raises:
            subprocess.CalledProcessError: If docker command fails for reasons
                other than the network already existing.
        """
        import subprocess

        try:
            subprocess.run(
                ["docker", "network", "create", network_name],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            # Ignore "network already exists" error
            if b"already exists" not in e.stderr:
                raise

    def _start_containers(
        self, config: TaskConfig, project_name: str,
        env_overlay: dict[str, str] | None = None,
    ) -> bool:
        """Start the task containers using docker-compose.

        Creates the Docker network, starts containers, and waits for health
        checks to pass.

        Args:
            config: Task configuration containing network_name, docker_compose path.
            project_name: Docker-compose project name for container naming.

        Returns:
            True if `compose up` adopted a workspace container that already existed rather than
            creating a fresh one -- which means an earlier environment never came down and its data is
            still loaded. Reported rather than raised (see docs/task_env_split.md: handover failures
            are logged and the run continues), but it invalidates the phase, so the orchestrator
            records it in the run record as well as logging it.

        Raises:
            subprocess.CalledProcessError: If docker-compose fails.
            TimeoutError: If health checks don't pass within timeout.
        """
        self._create_network(config.network_name)
        # Read the id BEFORE bringing the stack up: `compose up` under an existing project name
        # attaches to whatever is running instead of recreating it, and an attach is indistinguishable
        # from a clean start on every other surface (health passes, setup runs, nothing errors).
        workspace_before = f"{project_name}-workspace-1"
        id_before = self._container_manager.container_id(workspace_before)
        self._container_manager.start(config.docker_compose, project_name, env_overlay)
        id_after = self._container_manager.container_id(workspace_before)
        reused = id_before is not None and id_before == id_after
        if reused:
            logger.error(
                "Task environment container %s was NOT RECREATED -- its id (%s) is unchanged, so "
                "compose adopted a container that already existed rather than making a fresh one. It "
                "still holds whatever was loaded into it before, and this phase's data is loading on "
                "top of that. Scores from this run are not trustworthy.",
                workspace_before, (id_before or "")[:12],
            )

        # Wait for workspace container to be ready
        workspace_container = f"{project_name}-workspace-1"
        if not self._container_manager.wait_until_ready(workspace_container):
            raise TimeoutError(f"Container {workspace_container} did not become ready in time")

        # Also wait for db container if it has a healthcheck. Skip when the task
        # defines no db container (e.g. a single-service task-env like airline),
        # otherwise wait_until_ready would block for its full timeout on a name
        # that never appears.
        db_container = f"{project_name}-db-1"
        if self._container_manager.exists(db_container):
            self._container_manager.wait_until_ready(db_container)

        return reused

    def _transfer_and_extract_bundle(
        self, config: TaskConfig, env_phase: TaskEnvPhase, workspace_container: str,
    ) -> None:
        """Create, transfer, and extract this phase's data bundle into the container.

        Args:
            config: Task configuration (for the task directory the bundle sources are relative to).
            env_phase: The phase being started; supplies the bundle mappings, so only that phase's
                data ever enters the container.
            workspace_container: Name of the workspace container.

        Raises:
            FileNotFoundError: If source directories don't exist.
            subprocess.CalledProcessError: If docker cp or tar extraction fails.
        """
        task_dir = self.tasks_dir / config.task_id
        bundle_path = self._bundle_manager.create_bundle(
            task_dir, env_phase.bundle, Path(tempfile.gettempdir())
        )

        self._container_manager.copy_to(workspace_container, bundle_path, "/tmp/")
        self._container_manager.execute_command(
            workspace_container,
            ["tar", "-xzf", "/tmp/bundle.tar.gz", "-C", "/"]
        )

        # Cleanup bundle from host
        bundle_path.unlink(missing_ok=True)

    def _run_setup_script(self, env_phase: TaskEnvPhase, workspace_container: str) -> None:
        """Run this phase's setup script inside the container to initialize DB/KB/API.

        A task's setup script is also where it asserts its data actually landed (record counts above
        zero). The container's own health gate only proves the service answers -- a service can be
        healthy while holding nothing, which airline's will be by design once it starts empty -- so a
        failed load would otherwise show up only as a run of zeros. A failing setup script raises here
        and aborts the run, which is the right outcome: there is nothing worth measuring against
        missing data.

        Args:
            env_phase: The phase being started; supplies the setup script path.
            workspace_container: Name of the workspace container.

        Raises:
            subprocess.CalledProcessError: If the setup script fails.
        """
        # The setup_script path is already the container path (e.g., /scripts/setup.sh)
        setup_script_path = str(env_phase.setup_script)
        self._container_manager.execute_command(
            workspace_container,
            ["bash", setup_script_path]
        )

    def _build_environment_dict(
        self,
        config: TaskConfig,
        project_name: str,
        workspace_container: str,
        *,
        phase: str,
        reused: bool = False,
        lane: int = 0,
    ) -> dict:
        """Build the environment dict returned by start().

        Args:
            config: Task configuration.
            project_name: Docker-compose project name.
            workspace_container: Name of the workspace container.
            phase: Which phase this environment serves ("build" or "eval").
            reused: True if compose adopted a pre-existing container instead of creating one.
            lane: Which parallel copy of the environment this is (0 = the unsuffixed original).

        Returns:
            Environment dict with container lifecycle info for stop().
        """
        return {
            "workspace_container": workspace_container,
            "compose_project": project_name,
            # Which parallel copy this is. Carried so a caller holding only the dict can stop the
            # right stack and can tell lane 0 (the historic single environment) from the rest.
            "lane": lane,
            "compose_file": str(config.docker_compose),
            "network_name": config.network_name,
            # Which run phase this environment serves. The orchestrator passes the EVAL env's dict to
            # the scorer: the per-case reset token below is read off that container, so handing over
            # the build env's dict would point reset at a container that no longer exists.
            "phase": phase,
            # Handover diagnostic, carried on the dict rather than only logged so a run record can
            # show why its scores are not trustworthy: a prior phase's environment never came down.
            "reused_existing_container": reused,
            # Name of the env var holding the evaluator-only admin token (for per-sample DB reset).
            # None for tasks that don't opt into reset_env_per_sample.
            "env_admin_token_var": config.env_admin_token_var,
            # Name of the env var holding the TOOL token, for tasks whose eval-time user simulator
            # must call customer-held tools against the environment. None for every task whose
            # simulated user only talks.
            "env_tool_token_var": config.env_tool_token_var,
        }
