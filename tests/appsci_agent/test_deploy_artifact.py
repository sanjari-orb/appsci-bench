"""Unit tests for the deploy_artifact agent tool.

Under the corrected architecture (see docs/artifact_design.md) this tool is a thin HTTP RPC client:
it POSTs (empty body) to the orchestrator's POST /deploy route and surfaces endpoint-or-reason. The
docker build/run/health work happens host-side in the orchestrator, NOT here -- so these tests mock
the /deploy call and never touch docker or the deployer. (The deployer mechanism itself is tested in
tests/appsci_bench/orchestrator/test_deployer.py.)
"""
from __future__ import annotations

from unittest import mock

from appsci_agent.tools.base import ToolContext
from appsci_agent.tools.deploy_artifact import DeployArtifactTool, _resolve_deploy_url


# --- _resolve_deploy_url -----------------------------------------------------

def test_resolve_deploy_url_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DEPLOY_URL", "http://host:9000/deploy")
    monkeypatch.setenv("ORCHESTRATOR_SUBMIT_URL", "http://host:9000/submit")
    assert _resolve_deploy_url() == "http://host:9000/deploy"


def test_resolve_deploy_url_derives_from_submit_url(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_DEPLOY_URL", raising=False)
    monkeypatch.setenv("ORCHESTRATOR_SUBMIT_URL", "http://host.docker.internal:9000/submit")
    assert _resolve_deploy_url() == "http://host.docker.internal:9000/deploy"


def test_resolve_deploy_url_none_without_orchestrator(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_DEPLOY_URL", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_SUBMIT_URL", raising=False)
    assert _resolve_deploy_url() is None


# --- DeployArtifactTool: RPC-result mapping ----------------------------------

def _patch_deploy_url(url: str | None):
    return mock.patch("appsci_agent.tools.deploy_artifact._resolve_deploy_url", return_value=url)


def test_tool_unavailable_without_orchestrator():
    tool = DeployArtifactTool()
    context = ToolContext()
    with _patch_deploy_url(None):
        out = tool.execute({}, context)
    assert out.success is False
    assert "only available under the orchestrator" in out.error
    assert context.artifact_endpoint is None


def test_tool_success_sets_endpoint_on_context():
    tool = DeployArtifactTool()
    context = ToolContext()
    body = {"success": True, "endpoint_url": "http://artifact-x:8080/respond", "healthy": True}
    with _patch_deploy_url("http://host:9000/deploy"), \
         mock.patch.object(DeployArtifactTool, "_post_deploy", return_value=(body, None)):
        out = tool.execute({}, context)
    assert out.success is True
    assert out.output["endpoint_url"] == "http://artifact-x:8080/respond"
    assert out.output["healthy"] is True
    assert context.artifact_endpoint == "http://artifact-x:8080/respond"
    assert context.artifact_idx == 1
    # Deploying is not completing: the agent still calls complete.
    assert context.build_complete is False


def test_artifact_idx_increments_on_each_successful_deploy():
    """Tags a persisted eval report with which version of the artifact it measured."""
    tool = DeployArtifactTool()
    context = ToolContext()
    body = {"success": True, "endpoint_url": "http://artifact-x:8080/respond", "healthy": True}
    with _patch_deploy_url("http://host:9000/deploy"), \
         mock.patch.object(DeployArtifactTool, "_post_deploy", return_value=(body, None)):
        tool.execute({}, context)
        tool.execute({}, context)
    assert context.artifact_idx == 2


def test_tool_failure_passes_error_through_and_leaves_endpoint_unset():
    tool = DeployArtifactTool()
    context = ToolContext()
    body = {"success": False, "error": "no Dockerfile in /work/out -- the artifact must ship one."}
    with _patch_deploy_url("http://host:9000/deploy"), \
         mock.patch.object(DeployArtifactTool, "_post_deploy", return_value=(body, None)):
        out = tool.execute({}, context)
    assert out.success is False
    assert out.error == "no Dockerfile in /work/out -- the artifact must ship one."
    # The failure is a signal the agent can act on: to_dict() exposes the error.
    assert "error" in out.to_dict()
    assert context.artifact_endpoint is None


def test_tool_transport_error_is_surfaced():
    tool = DeployArtifactTool()
    context = ToolContext()
    with _patch_deploy_url("http://host:9000/deploy"), \
         mock.patch.object(DeployArtifactTool, "_post_deploy",
                           return_value=({}, "connection failed: refused")):
        out = tool.execute({}, context)
    assert out.success is False
    assert "deploy request failed" in out.error
    assert "connection failed: refused" in out.error
    assert context.artifact_endpoint is None


def test_tool_success_without_endpoint_is_failure():
    tool = DeployArtifactTool()
    context = ToolContext()
    body = {"success": True}  # malformed: success but no endpoint_url
    with _patch_deploy_url("http://host:9000/deploy"), \
         mock.patch.object(DeployArtifactTool, "_post_deploy", return_value=(body, None)):
        out = tool.execute({}, context)
    assert out.success is False
    assert "no endpoint_url" in out.error
    assert context.artifact_endpoint is None


def test_tool_posts_empty_body_to_resolved_url():
    """execute() takes no params and POSTs to the resolved /deploy URL (path + task are ambient)."""
    tool = DeployArtifactTool()
    context = ToolContext()
    body = {"success": True, "endpoint_url": "http://a:8080/respond", "healthy": True}
    with _patch_deploy_url("http://host:9000/deploy"), \
         mock.patch.object(DeployArtifactTool, "_post_deploy", return_value=(body, None)) as post:
        tool.execute({}, context)
    post.assert_called_once_with("http://host:9000/deploy")


# --- health reporting: "reachable" must not be dressed up as "healthy" -------
#
# The orchestrator succeeds a deploy when the container answers, and separately reports whether the
# health route returned 200. The tool must pass that distinction through: if it hardcoded healthy=True
# the agent would be told a broken health route is fine, and only discover otherwise at eval -- after
# BUILD has ended and it can no longer redeploy.

def test_tool_reports_unhealthy_deploy_truthfully():
    tool = DeployArtifactTool()
    context = ToolContext()
    detail = "server is up but GET /health returned HTTP 404, not 200"
    body = {
        "success": True,
        "endpoint_url": "http://artifact-x:8080/respond",
        "healthy": False,
        "health_detail": detail,
    }
    with _patch_deploy_url("http://host:9000/deploy"), \
         mock.patch.object(DeployArtifactTool, "_post_deploy", return_value=(body, None)):
        out = tool.execute({}, context)
    # The deploy still succeeded and the endpoint is recorded -- the artifact serves.
    assert out.success is True
    assert context.artifact_endpoint == "http://artifact-x:8080/respond"
    # But the tool reports the health state as observed, and tells the agent what to fix.
    assert out.output["healthy"] is False
    assert out.output["health_check"] == detail
    assert "did NOT pass the health check" in out.output["message"]
    assert "deploy_artifact again" in out.output["message"]


def test_tool_omitted_healthy_field_is_not_assumed_healthy():
    """A response with no `healthy` key must default to not-healthy, never to True."""
    tool = DeployArtifactTool()
    context = ToolContext()
    body = {"success": True, "endpoint_url": "http://artifact-x:8080/respond"}
    with _patch_deploy_url("http://host:9000/deploy"), \
         mock.patch.object(DeployArtifactTool, "_post_deploy", return_value=(body, None)):
        out = tool.execute({}, context)
    assert out.success is True
    assert out.output["healthy"] is False
    assert out.output["health_check"] == "the health check did not pass"


def test_tool_healthy_deploy_still_reads_as_healthy():
    tool = DeployArtifactTool()
    context = ToolContext()
    body = {
        "success": True,
        "endpoint_url": "http://artifact-x:8080/respond",
        "healthy": True,
        "health_detail": "healthy",
    }
    with _patch_deploy_url("http://host:9000/deploy"), \
         mock.patch.object(DeployArtifactTool, "_post_deploy", return_value=(body, None)):
        out = tool.execute({}, context)
    assert out.success is True
    assert out.output["healthy"] is True
    assert "deployed and healthy" in out.output["message"]
    assert "did NOT" not in out.output["message"]
