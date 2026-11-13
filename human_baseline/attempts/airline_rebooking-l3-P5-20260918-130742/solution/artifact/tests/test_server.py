"""Tests for the HTTP contract the deployer depends on.

The shape matters as much as the behaviour: a correct agent behind a malformed response is worth nothing
to the harness.
"""
from __future__ import annotations

import importlib
import os

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture()
def client(monkeypatch):
    """A TestClient over a freshly imported server with no LLM credential configured.

    No credential is deliberate: it exercises the degraded path, which must still honour the contract.
    """
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOG_LEVEL", "CRITICAL")
    import server

    importlib.reload(server)
    return fastapi_testclient.TestClient(server.app)


def test_health_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_respond_returns_the_openai_shape(client):
    response = client.post(
        "/respond", json={"messages": [{"role": "user", "content": "hello"}]}
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["choices"], list) and body["choices"]
    message = body["choices"][0]["message"]
    assert message["role"] == "assistant"
    assert isinstance(message["content"], str) and message["content"]


@pytest.mark.parametrize(
    "payload",
    [
        {},                                   # no messages key
        {"messages": []},                     # empty conversation
        {"messages": [{"role": "user"}]},    # message with no content
        {"messages": "not-a-list"},          # wrong type
    ],
)
def test_malformed_bodies_still_return_the_contract(client, payload):
    """A bad request must never produce a 500 or a broken response shape."""
    response = client.post("/respond", json=payload)
    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert isinstance(content, str) and content


def test_health_works_without_any_configuration(client):
    """Health must not depend on the environment being reachable or the LLM being configured."""
    assert client.get("/health").json() == {"status": "ok"}
