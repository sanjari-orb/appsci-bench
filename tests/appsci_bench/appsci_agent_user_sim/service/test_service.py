"""Tests for the user-sim FastAPI service (Phase service-layer). No network -- the sim is faked.

Focus: the service is ONE history-aware session. /ask must accumulate the transcript across calls and
return {message, turn}; auth + error codes mirror docs/user_simulator_api.md.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from appsci_bench.appsci_agent_user_sim.service.session import Session
from appsci_bench.appsci_agent_user_sim.service.app import create_app

TOKEN = "run_test_token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _FakeSim:
    """Stand-in for AppsciAgentLLMUserSim: records the history it was given and echoes its length so tests can
    prove the Session accumulates the transcript across calls."""
    def __init__(self):
        self.seen_histories: list[list[dict]] = []

    def answer(self, question: str, history: list[dict], parent_span_id: str | None = None) -> str:
        # history already contains the just-appended agent turn when answer() is called.
        self.seen_histories.append([dict(t) for t in history])
        return f"reply to {question!r} (history_len={len(history)})"


@pytest.fixture
def client_and_sim(monkeypatch):
    monkeypatch.setenv("APPSCI_USERSIM_TOKEN", TOKEN)
    sim = _FakeSim()
    app = create_app(session=Session(sim))  # inject session -> no env/model needed
    return TestClient(app), sim


class TestHealth:
    def test_health_ok_with_token(self, client_and_sim):
        client, _ = client_and_sim
        r = client.get("/health", headers=AUTH)
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_health_requires_token(self, client_and_sim):
        client, _ = client_and_sim
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401


class TestAsk:
    def test_ask_returns_message_and_turn(self, client_and_sim):
        client, _ = client_and_sim
        r = client.post("/ask", json={"question": "what is success?"}, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["turn"] == 1
        assert "reply to" in body["message"]

    def test_ask_requires_token(self, client_and_sim):
        client, _ = client_and_sim
        assert client.post("/ask", json={"question": "q"}).status_code == 401

    def test_empty_question_400(self, client_and_sim):
        client, _ = client_and_sim
        assert client.post("/ask", json={"question": "   "}, headers=AUTH).status_code == 400

    def test_session_accumulates_history_and_turn_increments(self, client_and_sim):
        """The core requirement: each /ask is aware of the prior conversation."""
        client, sim = client_and_sim

        r1 = client.post("/ask", json={"question": "q1"}, headers=AUTH)
        r2 = client.post("/ask", json={"question": "q2"}, headers=AUTH)
        r3 = client.post("/ask", json={"question": "q3"}, headers=AUTH)

        # turn counter increments across calls
        assert [r1.json()["turn"], r2.json()["turn"], r3.json()["turn"]] == [1, 2, 3]

        # the sim saw a GROWING PRIOR history each call (not the current question, which answer()
        # appends itself): q1 -> []; q2 -> [q1,a1]; q3 -> [q1,a1,q2,a2]
        lens = [len(h) for h in sim.seen_histories]
        assert lens == [0, 2, 4]
        # and the accumulated history carries the real prior turns, in order
        third = sim.seen_histories[2]
        assert [t["content"] for t in third] == ["q1", third[1]["content"], "q2", third[3]["content"]]
        assert third[0]["role"] == "agent" and third[1]["role"] == "user_sim"

    def test_no_active_session_409(self, monkeypatch):
        monkeypatch.setenv("APPSCI_USERSIM_TOKEN", TOKEN)
        app = create_app(session=Session(_FakeSim()))
        app.state.session = None  # simulate the orchestrator not having opened/closed it
        client = TestClient(app)
        assert client.post("/ask", json={"question": "q"}, headers=AUTH).status_code == 409
