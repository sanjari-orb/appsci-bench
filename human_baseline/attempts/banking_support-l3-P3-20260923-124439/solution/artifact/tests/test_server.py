"""Server-contract tests: health, and that /respond never fabricates an answer on failure."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture
def client():
    return TestClient(server.app)


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_respond_with_core_wired_but_unconfigured_is_honest_error(client, monkeypatch):
    # Phase B: agent.py now exists, so _load_agent() returns the real run_agent (no longer None).
    # With no runtime config in this local env, run_agent raises and /respond returns an HONEST
    # error -- never a fabricated assistant turn. (In Phase A this path returned 503 core-absent;
    # now it is a 500 core-error from the missing runtime config. Both are honest, neither fakes.)
    assert server._load_agent() is not None  # the core is wired
    for var in ("BANKING_TOOLS_URL", "BANKING_TOOLS_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    r = client.post("/respond", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "agent_core_error"
    assert "choices" not in body


def test_respond_wires_agent_core_when_present(client, monkeypatch):
    monkeypatch.setattr(server, "_load_agent", lambda: (lambda messages: "grounded answer"))
    r = client.post("/respond", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json() == {
        "choices": [{"message": {"role": "assistant", "content": "grounded answer"}}]
    }


def test_respond_agent_failure_is_honest_error_not_success(client, monkeypatch):
    def boom(messages):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(server, "_load_agent", lambda: boom)
    r = client.post("/respond", json={"messages": []})
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "agent_core_error"
    assert "choices" not in body
    # The generic message must not carry the exception text.
    assert "kaboom" not in r.text


def test_respond_error_does_not_leak_any_credential_in_exception(client, monkeypatch):
    # The exception message contains a credential-like string that is NOT the banking token, so a
    # token-only redactor would not catch it. The generic customer-facing error must not include it.
    fake_llm_key = "sk-fake-llm-key-abc123"
    fake_endpoint = "https://secret-llm.internal.example/v1"

    def leaky(messages):
        raise RuntimeError(f"call to {fake_endpoint} failed with key {fake_llm_key}")

    monkeypatch.setattr(server, "_load_agent", lambda: leaky)
    r = client.post("/respond", json={"messages": []})
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "agent_core_error"
    assert fake_llm_key not in r.text
    assert fake_endpoint not in r.text


def test_respond_rejects_malformed_body(client):
    # Missing 'messages' is tolerated (defaults to []), but a wrong type is a 422 validation error.
    r = client.post("/respond", json={"messages": "not-a-list"})
    assert r.status_code == 422
