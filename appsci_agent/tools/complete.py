"""Tool signalling that the agent has finished building and deploying its artifact.

Calling this tool ends the BUILD phase and starts evaluation. It takes no arguments: the endpoint
evaluated is whatever `deploy_artifact` deployed and recorded on the context, so the only endpoint
that can be evaluated is the one that was actually deployed (a hand-typed, stale, or loopback URL
can't be introduced here). It re-verifies that endpoint is reachable, then notifies the orchestrator
callback URL that the build is complete and evaluation may begin.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any

from .base import BaseTool, ToolContext, ToolResult, load_tool_config

logger = logging.getLogger(__name__)


class CompleteTool(BaseTool):
    """Signal that the artifact is built, deployed, and ready for evaluation."""

    def __init__(self):
        self._config = load_tool_config("complete")

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
        """Signal completion of the BUILD phase.

        Reads the deployed endpoint from the context (deploy_artifact records it there), verifies it
        is reachable, notifies the orchestrator callback that the build is complete, and marks the
        build complete. The agent passes no URL.
        """
        endpoint_url = context.artifact_endpoint

        if not endpoint_url:
            return ToolResult(
                success=False,
                output={},
                error=(
                    "No deployed artifact endpoint found. Call deploy_artifact first to build and "
                    "deploy your artifact; complete then ends the BUILD phase and evaluation runs "
                    "against that deployed endpoint."
                ),
            )

        # Guard against calling complete twice.
        if context.build_complete:
            return ToolResult(
                success=False,
                output={"endpoint_url": endpoint_url},
                error="This build has already been completed",
            )

        # Re-verify the deployed endpoint is (still) reachable before ending BUILD.
        is_reachable, error_msg = self._check_endpoint(endpoint_url)
        if not is_reachable:
            return ToolResult(
                success=False,
                output={"endpoint_url": endpoint_url},
                error=f"Endpoint is not reachable: {error_msg}",
            )

        # POST to orchestrator submit URL if configured
        submit_url = os.environ.get("ORCHESTRATOR_SUBMIT_URL")
        if submit_url:
            notified, notify_error = self._notify_orchestrator(
                submit_url, endpoint_url
            )
            if not notified:
                return ToolResult(
                    success=False,
                    output={"endpoint_url": endpoint_url},
                    error=f"Failed to notify orchestrator: {notify_error}",
                )

        # Mark the build complete; the endpoint was already recorded on the context by deploy_artifact.
        context.build_complete = True

        return ToolResult(
            success=True,
            output={
                "status": "complete",
                "endpoint_url": endpoint_url,
                "message": "Build complete. Evaluation will begin against the deployed endpoint.",
            },
        )

    def _check_endpoint(self, url: str) -> tuple[bool, str | None]:
        """Check if the endpoint is reachable.

        Args:
            url: The endpoint URL to check

        Returns:
            Tuple of (is_reachable, error_message)
        """
        try:
            # Try a simple GET request to check if server is up
            # The actual completions endpoint expects POST, but we just want to verify connectivity
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", "appsci-agent-healthcheck")

            with urllib.request.urlopen(req, timeout=10) as response:
                # Any response (even 4xx/5xx) means the server is running
                logger.debug(f"Endpoint check succeeded: {url} returned {response.status}")
                return True, None
        except urllib.error.HTTPError as e:
            # Any HTTP error means the server is running and responded
            # (e.g., 405 Method Not Allowed if it only accepts POST)
            logger.debug(f"Endpoint check succeeded: {url} returned HTTP {e.code} (server is up)")
            return True, None
        except urllib.error.URLError as e:
            logger.error(f"Endpoint check failed: {url} - connection failed: {e.reason}")
            return False, f"Connection failed: {e.reason}"
        except TimeoutError:
            logger.error(f"Endpoint check failed: {url} - connection timed out")
            return False, "Connection timed out"
        except Exception as e:
            logger.error(f"Endpoint check failed: {url} - {type(e).__name__}: {e}")
            return False, str(e)

    def _notify_orchestrator(
        self, submit_url: str, artifact_endpoint: str
    ) -> tuple[bool, str | None]:
        """Tell the orchestrator the build is complete, via its POST /submit route.

        The route keeps the name `/submit` -- that is the orchestrator's HTTP contract, unaffected
        by this tool's rename; what we send it is the deployed endpoint to evaluate.

        Args:
            submit_url: URL of the orchestrator's /submit endpoint
            artifact_endpoint: URL of the deployed artifact endpoint to report

        Returns:
            Tuple of (notified, error_message)
        """
        try:
            payload = json.dumps({"artifact_endpoint": artifact_endpoint}).encode(
                "utf-8"
            )
            req = urllib.request.Request(
                submit_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            logger.debug(f"Notifying orchestrator at {submit_url} with artifact endpoint {artifact_endpoint}")
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status in (200, 201, 202):
                    logger.info(f"Orchestrator notified: {submit_url} returned {response.status}")
                    return True, None
                logger.warning(f"Orchestrator notify returned unexpected status: {response.status}")
                return False, f"Unexpected status: {response.status}"

        except urllib.error.HTTPError as e:
            # The response explains how to recover (for example, redeploy edited source).
            # Preserve it for both the agent and the trace instead of reporting only "409 Conflict".
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace").strip()
            except Exception:
                pass  # A broken response stream must not hide the original HTTP failure.
            try:
                body = json.loads(detail)
            except json.JSONDecodeError:
                pass  # Keep plain-text or malformed responses as diagnostic context.
            else:
                if isinstance(body, dict) and isinstance(body.get("detail"), str):
                    detail = body["detail"].strip()
            error = f"HTTP error: {e.code} {e.reason}"
            if detail:
                error += f" -- {detail}"
            logger.error("Orchestrator notify failed: %s returned %s", submit_url, error)
            return False, error
        except urllib.error.URLError as e:
            logger.error(f"Orchestrator notify failed: {submit_url} - connection failed: {e.reason}")
            return False, f"Connection failed: {e.reason}"
        except TimeoutError:
            logger.error(f"Orchestrator notify failed: {submit_url} - connection timed out")
            return False, "Connection timed out"
        except Exception as e:
            logger.error(f"Orchestrator notify failed: {submit_url} - {type(e).__name__}: {e}")
            return False, str(e)
