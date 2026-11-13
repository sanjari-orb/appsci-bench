"""Build-phase orchestration: the benchmark's appsci-agent-user-sim app serves the business
appsci-agent-user-sim. Uses a fake session so no model creds / LLM are needed."""
from __future__ import annotations

from fastapi.testclient import TestClient

from appsci_bench.appsci_agent_user_sim.service.app import create_app


def test_user_sim_app_serves_business_persona(monkeypatch):
    """The business appsci-agent-user-sim app (what AppsciAgentUserSimService launches) answers /ask behind the token."""
    class FakeSession:
        def ask(self, q, parent_span_id=None):
            return ("you should build an airline agent", 1)

    monkeypatch.setenv("APPSCI_USERSIM_TOKEN", "t")
    client = TestClient(create_app(session=FakeSession()))
    auth = {"Authorization": "Bearer t"}
    assert client.get("/health").status_code == 401                  # token-gated
    assert client.get("/health", headers=auth).status_code == 200
    r = client.post("/ask", json={"question": "what should I build?"}, headers=auth)
    assert r.status_code == 200 and "airline" in r.json()["message"]
