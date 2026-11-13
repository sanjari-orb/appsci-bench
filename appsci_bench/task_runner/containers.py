"""ContainerManager for Docker container operations.

This module handles all Docker-related operations including:
- Creating Docker networks
- Starting/stopping containers via docker-compose
- Copying files into containers
- Executing commands inside containers
"""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class ContainerCommandError(subprocess.CalledProcessError):
    """A `docker exec` that failed, carrying the command's OWN output in its message.

    CalledProcessError's message is the argv and the exit status, nothing else. For a task's setup
    script that discards the entire diagnostic: a script ending
    `echo "expected 2000 reservations, found 0" >&2; exit 1` otherwise reaches the operator as
    `Command '[...]' returned non-zero exit status 1`, and asserting your data landed is the only thing
    standing between a partial load and a run of zeros with nothing to explain it. The text has to be in
    the message rather than only logged, because the orchestrator puts `str(e)` into the run record.

    Subclasses rather than replaces CalledProcessError so existing handlers still catch it.
    """

    def __str__(self) -> str:
        detail = "\n".join(
            f"  {stream}: {text.strip()}"
            for stream, text in (("stderr", self.stderr or ""), ("stdout", self.output or ""))
            if text and text.strip()
        )
        return f"{super().__str__()}\n{detail}" if detail else super().__str__()


def _run_or_raise(
    argv: list[str], what: str, env_overlay: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run a docker command, raising ContainerCommandError (with its output) if it fails.

    Every call here used `check=True, capture_output=True`, which captures the command's own
    explanation and then throws it away: CalledProcessError's message is the argv and the exit status
    only. For `compose up` that discards the build log, and for `compose down` it discards the reason a
    teardown failed -- both of which the operator needs and neither of which was reaching them.

    `env_overlay` is merged over this process's environment for this one call. Compose files resolve
    `${VAR}` against the environment of the process that invoked them, so this is how two stacks from
    one compose file get different values -- a per-lane tool token, say -- without a second file.
    Values are not logged: they are credentials.
    """
    env = {**os.environ, **env_overlay} if env_overlay else None
    result = subprocess.run(argv, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        error = ContainerCommandError(
            result.returncode, argv, output=result.stdout, stderr=result.stderr
        )
        logger.error("%s failed: %s", what, error)
        raise error
    return result


class ContainerManager:
    """Manages Docker containers via docker-compose and docker CLI.

    The ContainerManager provides a Python interface for common Docker
    operations needed by the TaskRunner. All operations are performed
    by shelling out to docker and docker-compose CLI commands.

    This class does NOT manage container state - it simply executes
    commands. The TaskRunner is responsible for orchestrating the
    sequence of operations.

    Example usage:
        manager = ContainerManager()

        # Ensure network exists
        manager.ensure_network("bench-network")

        # Start containers
        manager.start(
            compose_file=Path("tasks/order_to_cash/environment/docker-compose.yml"),
            project_name="o2c"
        )

        # Copy bundle into container
        manager.copy_to("o2c-workspace", Path("/tmp/bundle.tar.gz"), "/tmp/")

        # Execute command
        output = manager.exec("o2c-workspace", ["tar", "-xzf", "/tmp/bundle.tar.gz", "-C", "/"])

        # Stop containers
        manager.stop(
            compose_file=Path("tasks/order_to_cash/environment/docker-compose.yml"),
            project_name="o2c"
        )
    """

    def start(
        self, compose_file: Path, project_name: str,
        env_overlay: dict[str, str] | None = None,
    ) -> None:
        """Start containers defined in a docker-compose file.

        Runs docker-compose up in detached mode with the specified
        project name. The project name is used as a prefix for container
        names and allows running multiple instances of the same compose
        file simultaneously.

        Args:
            compose_file: Path to the docker-compose.yml file.
            project_name: Project name for docker-compose (e.g., "o2c").
            env_overlay: Extra environment for this `compose up` only, merged over the current
                process's. Compose expands `${VAR}` from it, which is what lets two stacks started
                from one compose file differ (e.g. a per-lane tool token).
                This becomes the prefix for container names.

        Raises:
            subprocess.CalledProcessError: If docker-compose fails to start.
            FileNotFoundError: If compose_file doesn't exist.

        Example:
            manager.start(
                compose_file=Path("tasks/order_to_cash/environment/docker-compose.yml"),
                project_name="o2c"
            )
        """
        if not compose_file.exists():
            raise FileNotFoundError(f"Compose file not found: {compose_file}")

        _run_or_raise(
            [
                "docker", "compose",
                "-f", str(compose_file),
                "-p", project_name,
                "up", "-d", "--build"
            ],
            f"docker compose up for project {project_name}",
            env_overlay,
        )

    def exists(self, container: str) -> bool:
        """Return True if a container with this name exists (any state).

        Used to skip waiting on containers a task doesn't define (e.g. a
        single-service task-env has no db container), avoiding a full
        wait_until_ready timeout on a name that will never appear.

        Args:
            container: Name of the container to check.

        Returns:
            True if `docker inspect` finds the container, False otherwise.
        """
        result = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
        )
        return result.returncode == 0

    def container_id(self, container: str) -> str | None:
        """The container's full Docker id, or None if no such container exists.

        Used to prove the environment actually changed across the build->eval handover. `docker
        compose up` under an existing project name ATTACHES to whatever is already running instead of
        recreating it, so a build env that failed to come down would be silently reused for the eval
        phase -- the eval data would load into the build container on top of the build data, and every
        surface would still report healthy. Comparing ids catches that.

        Args:
            container: Name of the container to inspect.

        Returns:
            The container id, or None when the container does not exist.
        """
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.Id}}", container],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def wait_until_ready(self, container: str, timeout: int = 120) -> bool:
        """Wait for a container to be ready.

        Polls the container's health status until it becomes healthy
        or a timeout is reached. If the container has no healthcheck,
        it checks if the container is running.

        Args:
            container: Name of the container to check (e.g., "o2c-workspace").
            timeout: Maximum seconds to wait (default 120).

        Returns:
            True if the container is ready, False if timeout reached.

        Raises:
            subprocess.CalledProcessError: If docker inspect command fails.

        Example:
            if manager.wait_until_ready("o2c-workspace"):
                print("Container is ready")
            else:
                print("Timed out waiting for container")
        """
        import time

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                # First check if container is running
                result = subprocess.run(
                    [
                        "docker", "inspect",
                        "--format", "{{.State.Running}}",
                        container
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                if result.stdout.strip() != "true":
                    # Container not running yet, keep waiting
                    time.sleep(2)
                    continue

                # Container is running, check if it has a healthcheck
                result = subprocess.run(
                    [
                        "docker", "inspect",
                        "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}",
                        container
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                status = result.stdout.strip()

                if status == "healthy" or status == "no-healthcheck":
                    # Either healthy or no healthcheck defined (running is enough)
                    return True
                # status is "starting" or "unhealthy", keep waiting

            except subprocess.CalledProcessError:
                # Container might not exist yet, keep waiting
                pass

            time.sleep(2)

        return False

    def stop(self, compose_file: Path, project_name: str) -> None:
        """Stop and remove containers defined in a docker-compose file.

        Runs docker-compose down to stop all containers and remove them,
        along with networks created by docker-compose up.

        Args:
            compose_file: Path to the docker-compose.yml file.
            project_name: Project name used when starting (e.g., "o2c").

        Raises:
            subprocess.CalledProcessError: If docker-compose fails.
            FileNotFoundError: If compose_file doesn't exist.

        Example:
            manager.stop(
                compose_file=Path("tasks/order_to_cash/environment/docker-compose.yml"),
                project_name="o2c"
            )
        """
        if not compose_file.exists():
            raise FileNotFoundError(f"Compose file not found: {compose_file}")

        _run_or_raise(
            [
                "docker", "compose",
                "-f", str(compose_file),
                "-p", project_name,
                "down", "--volumes", "--remove-orphans"
            ],
            f"docker compose down for project {project_name}",
        )

    def copy_to(self, container: str, src: Path, dest: str) -> None:
        """Copy a file from the host into a container.

        Uses docker cp to transfer files. This is typically used to
        copy the data bundle into the workspace container.

        Args:
            container: Name of the target container (e.g., "o2c-workspace").
            src: Path to the source file on the host.
            dest: Destination path inside the container (e.g., "/tmp/").

        Raises:
            subprocess.CalledProcessError: If docker cp fails.
            FileNotFoundError: If src file doesn't exist.

        Example:
            manager.copy_to("o2c-workspace", Path("/tmp/bundle.tar.gz"), "/tmp/")
        """
        if not src.exists():
            raise FileNotFoundError(f"Source file not found: {src}")

        _run_or_raise(
            ["docker", "cp", str(src), f"{container}:{dest}"],
            f"docker cp {src} into {container}:{dest}",
        )

    def execute_command(self, container: str, command: list[str]) -> str:
        """Execute a command inside a running container.

        Runs docker exec to execute a command and captures the output.
        This is used for extracting bundles and running setup scripts.

        Args:
            container: Name of the container (e.g., "o2c-workspace").
            command: Command to execute as a list of strings.
                Example: ["tar", "-xzf", "/tmp/bundle.tar.gz", "-C", "/"]

        Returns:
            The stdout from the command as a string.

        Raises:
            ContainerCommandError: If the command fails (non-zero exit). A subclass of
                subprocess.CalledProcessError whose message carries the command's own stderr/stdout,
                so a setup script's assertion text reaches the log and the run record instead of being
                captured and dropped.

        Example:
            # Extract bundle
            manager.execute_command("o2c-workspace", ["tar", "-xzf", "/tmp/bundle.tar.gz", "-C", "/"])

            # Run setup script
            manager.execute_command("o2c-workspace", ["bash", "/scripts/setup.sh"])

            # Query database
            output = manager.execute_command("o2c-db", ["psql", "-U", "sandbox", "-c", "SELECT 1"])
        """
        argv = ["docker", "exec", container] + command
        # Checked by hand rather than with check=True so the captured streams can be put into the
        # exception's message. subprocess.run's own error populates .stdout/.stderr but its __str__
        # ignores them, which is how a setup script's "found 0 records" became "exit status 1".
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            error = ContainerCommandError(
                result.returncode, argv, output=result.stdout, stderr=result.stderr
            )
            logger.error("Command failed in container %s: %s", container, error)
            raise error
        return result.stdout
