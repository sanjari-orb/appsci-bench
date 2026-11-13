"""Mock deployer for the Applied AI Scientist agent.

Provides a mock deployment mechanism for testing artifacts locally.
Real Docker deployment is deferred to a later phase.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DeploymentResult:
    """Result of deploying an artifact."""
    success: bool
    endpoint_url: str | None = None
    error: str | None = None
    process: subprocess.Popen | None = field(default=None, repr=False)
    logs: list[str] = field(default_factory=list)


class MockDeployer:
    """Mock deployer that runs artifacts as local subprocesses.

    This is a simplified deployer for testing that:
    - Validates artifact files exist
    - Starts the artifact as a local Python subprocess
    - Returns a mock endpoint URL
    - Provides health check functionality

    Real Docker deployment is deferred.
    """

    def __init__(
        self,
        artifact_dir: str | Path,
        host: str = "localhost",
        port: int = 8000,
    ):
        self.artifact_dir = Path(artifact_dir)
        self.host = host
        self.port = port
        self._process: subprocess.Popen | None = None
        self._log_thread: threading.Thread | None = None
        self._logs: list[str] = []

    @property
    def endpoint_url(self) -> str:
        """Get the endpoint URL for the deployed artifact."""
        return f"http://{self.host}:{self.port}/respond"

    @property
    def health_url(self) -> str:
        """Get the health check URL for the deployed artifact."""
        return f"http://{self.host}:{self.port}/health"

    def deploy(self, timeout: float = 30.0) -> DeploymentResult:
        """Deploy the artifact as a local subprocess.

        Args:
            timeout: Timeout in seconds to wait for the server to start

        Returns:
            DeploymentResult with deployment status and endpoint URL
        """
        # Validate artifact directory
        if not self.artifact_dir.exists():
            return DeploymentResult(
                success=False,
                error=f"Artifact directory does not exist: {self.artifact_dir}",
            )

        # Find the main entry point
        entry_point = self._find_entry_point()
        if not entry_point:
            return DeploymentResult(
                success=False,
                error="No entry point found. Expected server.py, app.py, or main.py",
            )

        # Check if we need to install dependencies
        requirements_file = self.artifact_dir / "requirements.txt"
        if requirements_file.exists():
            try:
                self._install_dependencies(requirements_file)
            except Exception as e:
                return DeploymentResult(
                    success=False,
                    error=f"Failed to install dependencies: {e}",
                )

        # Start the server process
        try:
            self._process = subprocess.Popen(
                [sys.executable, str(entry_point)],
                cwd=str(self.artifact_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            # Start log capture thread
            self._start_log_capture()

            # Wait for server to be ready
            if not self._wait_for_server(timeout):
                self.stop()
                return DeploymentResult(
                    success=False,
                    error=f"Server did not start within {timeout}s. Logs: {self._logs[-10:]}",
                    logs=self._logs,
                )

            return DeploymentResult(
                success=True,
                endpoint_url=self.endpoint_url,
                process=self._process,
                logs=self._logs,
            )

        except Exception as e:
            return DeploymentResult(
                success=False,
                error=f"Failed to start server: {e}",
            )

    def _find_entry_point(self) -> Path | None:
        """Find the main entry point file."""
        candidates = ["server.py", "app.py", "main.py"]
        for candidate in candidates:
            path = self.artifact_dir / candidate
            if path.exists():
                return path
        return None

    def _install_dependencies(self, requirements_file: Path) -> None:
        """Install dependencies from requirements.txt."""
        logger.info(f"Installing dependencies from {requirements_file}")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements_file)],
            check=True,
            capture_output=True,
        )

    def _start_log_capture(self) -> None:
        """Start a thread to capture process logs."""
        def capture_logs():
            if self._process and self._process.stdout:
                for line in self._process.stdout:
                    self._logs.append(line.strip())
                    logger.debug(f"[artifact] {line.strip()}")

        self._log_thread = threading.Thread(target=capture_logs, daemon=True)
        self._log_thread.start()

    def _wait_for_server(self, timeout: float) -> bool:
        """Wait for the server to be ready.

        Args:
            timeout: Timeout in seconds

        Returns:
            True if server is ready, False otherwise
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            # Check if process is still running
            if self._process and self._process.poll() is not None:
                # Process has exited
                return False

            if self._check_health():
                return True

            time.sleep(0.5)

        return False

    def _check_health(self) -> bool:
        """Check if the server is healthy."""
        try:
            req = urllib.request.Request(self.health_url)
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError):
            return False

    def query(
        self,
        messages: list[dict[str, str]],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Query the deployed artifact.

        Args:
            messages: List of message dicts with 'role' and 'content'
            timeout: Request timeout in seconds

        Returns:
            Response dict from the artifact

        Raises:
            RuntimeError: If the artifact is not deployed
            urllib.error.URLError: If the request fails
        """
        if not self._process or self._process.poll() is not None:
            raise RuntimeError("Artifact is not deployed")

        request_data = json.dumps({"messages": messages}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint_url,
            data=request_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def stop(self) -> None:
        """Stop the deployed artifact."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    def is_running(self) -> bool:
        """Check if the artifact is running."""
        return self._process is not None and self._process.poll() is None

    def __enter__(self) -> "MockDeployer":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
