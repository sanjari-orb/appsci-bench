"""Restarted artifacts can move ports; failed recovery must prevent lane reuse."""
import subprocess
from unittest.mock import Mock

import pytest

from appsci_bench.orchestrator.eval_step_budget import make_step_budget_hooks
from appsci_bench.eval.step_budget import LaneCleanupError
from appsci_bench.eval.artifact_client import HttpArtifactClient
from appsci_bench.eval.datatypes import ArtifactAgentQuery


def test_old_environment_image_is_rejected_before_evaluation():
    with pytest.raises(RuntimeError, match="updated banking environment image"):
        make_step_budget_hooks(lambda *args: (404, "missing"), "artifact",
                               HttpArtifactClient("http://localhost:123/respond"), 8080)


def test_each_restart_refreshes_health_and_real_request_endpoint(monkeypatch):
    ports = iter(["0.0.0.0:456\n[::]:456\n", "[::]:789\n"])
    commands, probes, requests = [], [], []

    def docker(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, next(ports) if cmd[1] == "port" else "", "")

    def request(req, **kwargs):
        requests.append((req.full_url, kwargs["timeout"]))
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"response": "ok"}'
        return response

    monkeypatch.setattr("appsci_bench.orchestrator.eval_step_budget.subprocess.run", docker)
    monkeypatch.setattr("appsci_bench.orchestrator.eval_step_budget.HttpArtifact.wait_for_health",
                        lambda self, **kw: probes.append(self.endpoint_url))
    monkeypatch.setattr("appsci_bench.eval.artifact.urllib.request.urlopen", request)
    client = HttpArtifactClient("http://localhost:123/respond?version=1", timeout=17)
    _, cancel, ready = make_step_budget_hooks(lambda *args: (200, "{}"),
                                               "banking-artifact-l2", client, 8080)
    for port in (456, 789):
        cancel()
        ready()
        assert client.endpoint_url == f"http://localhost:{port}/respond?version=1"
        sid = client.create_session()
        assert client.query_session(sid, ArtifactAgentQuery(text="hi")).text == "ok"
        client.delete_session(sid)
    assert probes == [url for url, _ in requests]
    assert requests == [(f"http://localhost:{p}/respond?version=1", 17) for p in (456, 789)]
    assert commands == [["docker", command, *args] for _ in range(2) for command, args in (
        ("restart", ["--time", "0", "banking-artifact-l2"]),
        ("port", ["banking-artifact-l2", "8080"]),
    )]


@pytest.mark.parametrize("failure", ["restart", "mapping", "empty", "invalid", "health"])
def test_failed_recovery_permanently_refuses_lane_reuse(monkeypatch, failure):
    def docker(cmd, **kwargs):
        failed = (cmd[1] == "restart" and failure == "restart"
                  or cmd[1] == "port" and failure == "mapping")
        output = {"empty": "", "invalid": "0.0.0.0:bad"}.get(failure, "0.0.0.0:456")
        return subprocess.CompletedProcess(cmd, int(failed), output, "failed" if failed else "")

    monkeypatch.setattr("appsci_bench.orchestrator.eval_step_budget.subprocess.run", docker)
    monkeypatch.setattr("appsci_bench.orchestrator.eval_step_budget.HttpArtifact.wait_for_health",
                        Mock(side_effect=TimeoutError("unhealthy") if failure == "health" else None))
    client = HttpArtifactClient("http://localhost:123/respond")
    factory, cancel, ready = make_step_budget_hooks(lambda *args: (200, "{}"),
                                                    "artifact", client, 8080)
    with pytest.raises(LaneCleanupError):
        cancel()
    with pytest.raises(LaneCleanupError):
        ready()
    with pytest.raises(LaneCleanupError):
        factory(200)
    assert client.endpoint_url == "http://localhost:123/respond"
