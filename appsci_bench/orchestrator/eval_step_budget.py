"""Bind a conversation's shared step budget to its isolated banking environment."""
import json
import logging
import subprocess
from urllib.parse import urlsplit

from appsci_bench.eval.artifact import HttpArtifact
from appsci_bench.eval.step_budget import LaneCleanupError, MaxStepsReached, StepBudgetUnavailable

logger = logging.getLogger(__name__)


class EnvironmentStepBudget:
    tools_counted_externally = True

    def __init__(self, admin, max_steps):
        self.admin = admin
        self._request("POST", "/admin/simulation_steps", {"max_steps": max_steps})

    def _request(self, method, path, payload=None):
        try:
            status, body = self.admin(method, path, payload)
        except Exception as exc:
            # Do not let an accounting outage become an artifact error/zero score. POSTs cannot
            # safely be retried on this environment if completion is uncertain.
            from .env_admin import EnvAdminError
            detail = str(exc) if isinstance(exc, EnvAdminError) else type(exc).__name__
            raise StepBudgetUnavailable(f"simulation step accounting {method} {path} failed: {detail}",
                                       unsafe_to_reuse_lane=method != "GET") from None
        if status == 409:
            raise MaxStepsReached
        if status != 200:
            raise StepBudgetUnavailable(f"simulation step accounting failed (HTTP {status})")
        try:
            state = json.loads(body)
            if (not isinstance(state, dict) or type(state.get("steps")) is not int
                    or type(state.get("max_steps")) is not int):
                raise ValueError("invalid counter state")
        except (ValueError, TypeError):
            raise StepBudgetUnavailable("environment returned invalid simulation step accounting") from None
        return state

    def consume(self, steps=1):
        self._request("POST", "/admin/simulation_steps/advance", {"steps": steps})

    def snapshot(self):
        return self._request("GET", "/admin/simulation_steps")


def make_step_budget_hooks(admin, artifact_container, client, artifact_port):
    # Required capability, not an optional scoring route: an old image must fail before scoring.
    status, _ = admin("GET", "/admin/simulation_steps")
    if status != 200:
        raise RuntimeError("max_steps requires the updated banking environment image (simulation_steps API)")
    failed = False

    def check_ready():
        if failed:
            raise LaneCleanupError("artifact cleanup failed; refusing to reuse its environment")

    def factory(max_steps):
        check_ready()
        return EnvironmentStepBudget(admin, max_steps)

    def cancel_query():
        nonlocal failed
        # Closing the evaluator's HTTP socket would leave the artifact's tool loop alive. Kill
        # its process before resetting the DB for the next case, then health-gate the replacement.
        try:
            result = subprocess.run(["docker", "restart", "--time", "0", artifact_container],
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
            # Docker may allocate a NEW host port on restart when deployed with -p <port>.
            # Refresh both the health probe and the actual client before releasing this lane.
            mapping = subprocess.run(["docker", "port", artifact_container, str(artifact_port)],
                                     capture_output=True, text=True, timeout=5)
            if mapping.returncode or not mapping.stdout.strip():
                raise RuntimeError(f"cannot resolve restarted artifact port: {mapping.stderr.strip()}")
            host_port = int(mapping.stdout.splitlines()[0].rsplit(":", 1)[1])
            if not 0 < host_port < 65536:
                raise ValueError(f"invalid published artifact port: {host_port}")
            endpoint = urlsplit(client.endpoint_url)._replace(netloc=f"localhost:{host_port}").geturl()
            HttpArtifact(endpoint_url=endpoint).wait_for_health(timeout=60)
            logger.info("Recovered artifact %s: %s -> %s", artifact_container,
                        client.endpoint_url, endpoint)
            client.update_endpoint(endpoint)
        except Exception as exc:
            failed = True
            raise LaneCleanupError(f"cannot stop/restart artifact {artifact_container}: {exc}") from exc

    return factory, cancel_query, check_ready
