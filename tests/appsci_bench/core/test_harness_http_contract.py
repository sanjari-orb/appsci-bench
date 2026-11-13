"""External harnesses use plain HTTP; the existing servers own the contract."""
from __future__ import annotations

import json
import re
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from appsci_bench.appsci_agent_user_sim.service.app import create_app
from appsci_bench.core.harness_inputs import HTTP_CONTRACT_SOURCE
from appsci_bench.orchestrator.deployer import DeploymentResult
from appsci_bench.orchestrator.submission_server import ArtifactSubmissionServer, DeployContext

ENDPOINT = "http://artifact:8080/respond"


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    source = tmp_path / "artifact"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")
    server = ArtifactSubmissionServer()
    server.configure_deploy(DeployContext(
        artifact_dir=str(source), image_name="artifact", container_name="artifact",
        network="test", port=8080,
    ))
    # Observe the real submission signal without starting a network listener.
    server._loop = Mock()
    server._loop.call_soon_threadsafe.side_effect = lambda callback: callback()
    deploy = Mock(return_value=DeploymentResult(
        success=True, endpoint_url=ENDPOINT, container_name="artifact",
        healthy=True, health_detail="HTTP 200",
    ))
    monkeypatch.setattr("appsci_bench.orchestrator.submission_server.DockerArtifactDeployer.deploy", deploy)
    with TestClient(server._create_app()) as client:
        yield server, client, source, deploy


def test_documented_http_contract_works_with_existing_services(deployment, monkeypatch):
    """Exercise the documented methods/payloads, without prescribing a client implementation."""
    server, orchestrator, _, _ = deployment
    monkeypatch.setenv("APPSCI_USERSIM_TOKEN", "test-token")
    session = Mock()
    session.ask.return_value = ("Return a structured decision.", 1)
    env = {
        "APPSCI_USERSIM_TOKEN": "test-token",
        "APPSCI_USERSIM_ASK_URL": "http://usersim/ask",
        "ORCHESTRATOR_DEPLOY_URL": "http://orchestrator/deploy",
        "ORCHESTRATOR_DEPLOY_STATUS_URL": "http://orchestrator/deploy/status",
        "ORCHESTRATOR_SUBMIT_URL": "http://orchestrator/submit",
    }
    contract = HTTP_CONTRACT_SOURCE.read_text()
    assert "curl" not in contract and "```sh" not in contract
    # Index by URL variable, not method (three operations use POST).
    methods = {variable: method for method, variable in re.findall(
        r"^\| .*? \| (POST|GET) \| `([^`]+)` \|", contract, re.MULTILINE,
    )}
    assert len(methods) == 4
    assert "Bearer token from `APPSCI_USERSIM_TOKEN`" in contract
    assert "Content-Type: application/json" in contract
    bodies = [json.loads(body) for body in re.findall(
        r"Request \(JSON\):\n\n```json\n(.*?)\n```", contract, re.DOTALL,
    )]
    assert len(bodies) == 3
    assert bodies[1] == {}
    assert bodies[2] == {"artifact_endpoint": "<endpoint_url returned by deploy_artifact>"}
    payloads = {
        "APPSCI_USERSIM_ASK_URL": bodies[0], "ORCHESTRATOR_DEPLOY_URL": bodies[1],
        "ORCHESTRATOR_DEPLOY_STATUS_URL": None, "ORCHESTRATOR_SUBMIT_URL": bodies[2],
    }
    responses = {}
    with TestClient(create_app(session)) as usersim:
        for variable, body in payloads.items():
            url = urlsplit(env[variable])
            client = usersim if url.hostname == "usersim" else orchestrator
            headers = {"Content-Type": "application/json"}
            if client is usersim:
                headers["Authorization"] = "Bearer " + env["APPSCI_USERSIM_TOKEN"]
            if variable == "ORCHESTRATOR_SUBMIT_URL":
                body = {"artifact_endpoint": responses["ORCHESTRATOR_DEPLOY_URL"]["endpoint_url"]}
            response = client.request(methods[variable], url.path,
                                      headers=headers, json=body)
            assert response.status_code == 200, response.text
            responses[variable] = response.json()
    assert responses["APPSCI_USERSIM_ASK_URL"] == {"message": "Return a structured decision.", "turn": 1}
    session.ask.assert_called_once_with("What output does the customer need?", parent_span_id=None)
    assert responses["ORCHESTRATOR_DEPLOY_URL"]["success"] is True
    assert responses["ORCHESTRATOR_DEPLOY_URL"]["endpoint_url"] == ENDPOINT
    assert responses["ORCHESTRATOR_DEPLOY_STATUS_URL"]["deployed"] is True
    assert responses["ORCHESTRATOR_DEPLOY_STATUS_URL"]["stale"] is False
    assert responses["ORCHESTRATOR_SUBMIT_URL"]["status"] == "accepted"
    assert server._completion_event.is_set()


@pytest.mark.parametrize("case,detail", [
    ("not_deployed", "Deploy the artifact"),
    ("wrong_endpoint", "latest successful deployment"),
    ("edited", "source changed"),
    ("deleted_source", "source changed"),
    ("failed_redeploy", "source changed"),
])
def test_raw_submit_cannot_bypass_deployment_checks(deployment, case, detail):
    server, client, source, deploy = deployment
    if case != "not_deployed":
        assert client.post("/deploy", json={}).json()["success"] is True
    if case in ("edited", "failed_redeploy"):
        (source / "server.py").write_text("# edited after deployment\n")
    if case == "deleted_source":
        (source / "Dockerfile").unlink()
    if case == "failed_redeploy":
        deploy.return_value = DeploymentResult(success=False, error="build failed")
        assert client.post("/deploy", json={}).json() == {"success": False, "error": "build failed"}
    endpoint = "http://other/respond" if case == "wrong_endpoint" else ENDPOINT
    reply = client.post("/submit", json={"artifact_endpoint": endpoint})
    assert reply.status_code == 409
    assert detail in reply.json()["detail"]
    assert server._artifact_endpoint is None
    assert not server._completion_event.is_set()


def test_redeploy_after_rejection_allows_completion_from_another_client(deployment):
    server, client, source, _ = deployment
    client.post("/deploy", json={})
    (source / "server.py").write_text("# new source\n")
    assert client.post("/submit", json={"artifact_endpoint": ENDPOINT}).status_code == 409
    assert client.post("/deploy", json={}).json()["success"] is True
    with TestClient(server._create_app()) as fresh:
        status = fresh.get("/deploy/status").json()
        assert status["stale"] is False
        reply = fresh.post("/submit", json={"artifact_endpoint": status["endpoint_url"]})
        assert reply.status_code == 200
        assert reply.json()["status"] == "accepted"
        duplicate = fresh.post("/submit", json={"artifact_endpoint": status["endpoint_url"]})
        assert duplicate.status_code == 409
        assert "already submitted" in duplicate.json()["detail"]


@pytest.mark.parametrize("endpoint", ["", "   "])
def test_empty_submission_is_rejected(deployment, endpoint):
    _, client, _, _ = deployment
    assert client.post("/submit", json={"artifact_endpoint": endpoint}).status_code == 400


def test_standalone_endpoint_only_submission_still_works():
    with TestClient(ArtifactSubmissionServer()._create_app()) as client:
        assert client.post("/submit", json={"artifact_endpoint": ENDPOINT}).status_code == 200


def test_documented_stakeholder_errors(monkeypatch):
    monkeypatch.setenv("APPSCI_USERSIM_TOKEN", "test-token")
    session = Mock()
    with TestClient(create_app(session)) as client:
        assert client.post("/ask", json={"question": "q"}).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        assert client.post("/ask", headers=headers, json={"question": " "}).status_code == 400
        assert client.post("/ask", headers=headers, json={}).status_code == 422
    session.ask.assert_not_called()
