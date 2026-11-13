"""Applied-scientist shim: deploy the locally-hosted airline artifact and hand back its endpoint.

This stands in for the AppSci agent's build phase: instead of having mini_swe author + deploy an
artifact, it deploys the prebuilt local FastAPI artifact (local_server/) via MockDeployer and returns
an HTTP artifact pointing at it, exercising the full build->eval pipeline against a genuinely deployed
artifact that calls the airline tool-API. It is a `build(context)`-factory reference kept for
documentation; the supported entrypoint is the Docker orchestrator (`python -m appsci_bench.orchestrator.run`),
which builds via the containerized AppSci agent rather than loading this file directly.

This is the localhost stand-in for the deployed HTTP artifact container.
Creds-gated: the deployed server calls Claude when queried.
"""
from __future__ import annotations

import atexit
import contextlib
import logging
import os
import socket

from appsci_bench.eval.artifact import ArtifactQuery, ArtifactResponse, InProcessArtifact
from appsci_bench.core.spec import REPO_ROOT

_log = logging.getLogger("airline_artifact")
_SERVER_DIR = REPO_ROOT / "gold" / "airline_rebooking" / "local_server"


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_artifact(endpoint_url: str) -> InProcessArtifact:
    """An artifact that POSTs {messages} to the deployed /respond endpoint and reads {choices}."""
    def respond(query: ArtifactQuery) -> ArtifactResponse:
        import httpx
        messages = [*list(query.history), {"role": "user", "content": query.intended_task}]
        try:
            r = httpx.post(endpoint_url, json={"messages": messages}, timeout=180.0)
            choices = (r.json().get("choices") or []) if r.status_code < 400 else []
            content = (choices[0].get("message") or {}).get("content", "") if choices else ""
            if not content and r.status_code >= 400:
                content = f"ERROR {r.status_code}: {r.text[:200]}"
        except Exception as e:  # noqa: BLE001 - unreachable artifact -> empty response (scored as a miss)
            content = f"ERROR: {e!r}"
        return ArtifactResponse(output={"response": content},
                                messages=[{"role": "assistant", "content": content}])
    return InProcessArtifact(respond)


class LocalArtifactAppliedScientist:
    def build(self, context):
        from appsci_agent.artifact.deployer import MockDeployer
        port = _free_port()
        os.environ["APPSCI_AGENT_ARTIFACT_PORT"] = str(port)     # server.py listens here
        deployer = MockDeployer(artifact_dir=str(_SERVER_DIR), host="127.0.0.1", port=port)
        _log.info("deploying locally-hosted airline artifact from %s on port %d ...", _SERVER_DIR, port)
        result = deployer.deploy(timeout=90.0)
        if not result.success:
            _log.warning("artifact deploy failed: %s", result.error)
            return InProcessArtifact(lambda q: ArtifactResponse(
                output={"error": f"artifact deploy failed: {result.error}"}, messages=[]))
        atexit.register(deployer.stop)                     # tear down the subprocess when the run exits
        _log.info("artifact deployed at %s", result.endpoint_url)
        return _http_artifact(result.endpoint_url)


def build() -> LocalArtifactAppliedScientist:
    return LocalArtifactAppliedScientist()
