"""Tool for deploying the agent's artifact to a reachable endpoint.

This is a thin HTTP RPC client (see docs/artifact_design.md). It does NOT run docker itself:
the agent writes its Dockerfile + server entry into the artifact output dir, then calls this tool,
which POSTs (empty body) to the orchestrator's POST /deploy route. The orchestrator holds the
host-visible artifact dir (bind-mounted from this container) and the task's deploy contract
(container name / port / network / env from the spec), runs the shared DockerArtifactDeployer
host-side, and returns endpoint-or-reason.

On success the returned endpoint_url is recorded on the context; the complete tool (which takes no
arguments) later ends the BUILD phase, and evaluation runs against that recorded endpoint. Success
means the deployed container *answers*; whether it passed its health check is reported separately
(`healthy` / `health_check` in the output) so a reachable-but-unhealthy artifact isn't described to
the agent as healthy. On any failure the descriptive reason is surfaced as a tool failure so the
agent can fix its code and call deploy_artifact again (the deploy is idempotent host-side).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from .base import BaseTool, ToolContext, ToolResult, load_tool_config

logger = logging.getLogger(__name__)

# Deploy can take minutes (docker build) plus up to the health-poll timeout, so give the RPC a
# generous ceiling -- well above the host-side HEALTH_TIMEOUT_S -- and let the orchestrator be the
# authority on when a deploy has failed.
DEPLOY_RPC_TIMEOUT_S = 600


def _resolve_deploy_url() -> str | None:
    """Resolve the orchestrator's /deploy URL.

    Single source of truth is ORCHESTRATOR_SUBMIT_URL (the sibling route on the same server); we
    swap its trailing /submit for /deploy. ORCHESTRATOR_DEPLOY_URL overrides explicitly if set.
    Returns None when running without an orchestrator (local/in-process mode), so the tool can
    report that deploy_artifact isn't available there.
    """
    explicit = os.environ.get("ORCHESTRATOR_DEPLOY_URL")
    if explicit:
        return explicit
    submit = os.environ.get("ORCHESTRATOR_SUBMIT_URL")
    if not submit:
        return None
    if submit.endswith("/submit"):
        return submit[: -len("/submit")] + "/deploy"
    return submit.rstrip("/") + "/deploy"


class DeployArtifactTool(BaseTool):
    """Deploy the artifact the agent authored into a healthy, reachable endpoint."""

    def __init__(self):
        self._config = load_tool_config("deploy_artifact")

    @property
    def name(self) -> str:
        return self._config["name"]

    @property
    def description(self) -> str:
        return self._config["description"]

    @property
    def parameters(self) -> dict[str, Any]:
        return self._config["parameters"]

    def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """POST to the orchestrator's /deploy route and surface endpoint-or-reason.

        Takes no parameters: the artifact dir and deploy contract are ambient on the orchestrator.
        """
        deploy_url = _resolve_deploy_url()
        if not deploy_url:
            return ToolResult(
                success=False,
                output={},
                error=(
                    "deploy_artifact is only available under the orchestrator "
                    "(ORCHESTRATOR_SUBMIT_URL / ORCHESTRATOR_DEPLOY_URL not set)."
                ),
            )

        body, error = self._post_deploy(deploy_url)
        if error is not None:
            return ToolResult(success=False, output={}, error=f"deploy request failed: {error}")

        if not body.get("success"):
            # The orchestrator ran the deployer and it failed at a specific step -- pass the
            # descriptive reason straight through so the agent can fix its code and redeploy.
            return ToolResult(
                success=False,
                output={},
                error=body.get("error") or "deploy failed for an unknown reason",
            )

        endpoint_url = body.get("endpoint_url")
        if not endpoint_url:
            return ToolResult(
                success=False,
                output={},
                error="deploy reported success but returned no endpoint_url",
            )

        # Record the reachable endpoint for the complete tool to use. We do NOT set
        # build_complete -- deploying is not completing; the agent still calls complete.
        context.artifact_endpoint = endpoint_url
        context.artifact_idx += 1

        # Report the health check truthfully. The orchestrator distinguishes "the container answers"
        # (which is what makes a deploy succeed) from "GET /health returned 200" (the contract), so
        # pass that distinction through instead of asserting healthy=True for every live container.
        # A reachable-but-unhealthy artifact is still a successful deploy -- the endpoint serves, and
        # the agent can proceed -- but it gets told what's wrong while it can still fix it.
        healthy = bool(body.get("healthy"))
        health_detail = body.get("health_detail") or (
            "healthy" if healthy else "the health check did not pass"
        )
        if healthy:
            message = (
                "Artifact deployed and healthy. When you are ready to be evaluated, call "
                "complete (no arguments) -- evaluation runs against this deployed endpoint."
            )
        else:
            message = (
                f"Artifact deployed and REACHABLE, but it did NOT pass the health check: "
                f"{health_detail}. The endpoint is serving, so you may proceed -- but a 200 from "
                "the health route is part of the artifact contract, so prefer fixing it and calling "
                "deploy_artifact again before you call complete."
            )

        return ToolResult(
            success=True,
            output={
                "status": "deployed",
                "endpoint_url": endpoint_url,
                "healthy": healthy,
                "health_check": health_detail,
                "message": message,
            },
        )

    def _post_deploy(self, deploy_url: str) -> tuple[dict[str, Any], str | None]:
        """POST an empty body to the /deploy route and parse the JSON result.

        Returns (body, None) on a well-formed response (whether the deploy succeeded or failed),
        or ({}, error_message) if the request itself couldn't be completed / parsed.
        """
        try:
            req = urllib.request.Request(
                deploy_url,
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            logger.debug("Requesting deploy at %s", deploy_url)
            with urllib.request.urlopen(req, timeout=DEPLOY_RPC_TIMEOUT_S) as response:
                raw = response.read().decode("utf-8")
            return json.loads(raw), None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            logger.error("Deploy request failed: HTTP %s %s", e.code, e.reason)
            return {}, f"HTTP {e.code}: {e.reason}{f' -- {detail}' if detail else ''}"
        except urllib.error.URLError as e:
            logger.error("Deploy request connection failed: %s", e.reason)
            return {}, f"connection failed: {e.reason}"
        except TimeoutError:
            logger.error("Deploy request timed out after %ss", DEPLOY_RPC_TIMEOUT_S)
            return {}, f"timed out after {DEPLOY_RPC_TIMEOUT_S}s"
        except json.JSONDecodeError as e:
            logger.error("Deploy response was not valid JSON: %s", e)
            return {}, f"invalid JSON response: {e}"
        except Exception as e:  # noqa: BLE001 - surface any unexpected transport failure to the agent
            logger.error("Deploy request failed: %s: %s", type(e).__name__, e)
            return {}, str(e)
