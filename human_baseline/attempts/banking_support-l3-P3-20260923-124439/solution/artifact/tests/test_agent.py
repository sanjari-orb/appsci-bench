"""Tests for agent.run_agent: the /respond entry point that wires the confirmed core together.

Driven by fakes swapped into the ``agent`` module namespace, so no real LLM, banking server, or env is
touched. They assert run_agent: builds the loop and returns the model's answer, advertises the full
confirmed tool set (retrieval + BASE + discovery), closes both httpx-backed clients after the turn, is
import-safe with no environment set, and raises an honest error (never a fabricated answer) when the
required runtime config is missing.
"""
from __future__ import annotations

import pytest

import agent
from llm_client import LLMResponse, ToolCall
from tools import ToolResult, KIND_OK


class FakeLLM:
    """Scripted LLM with context-manager + close tracking, matching AnthropicMessagesClient usage."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.closed = False

    def create_message(self, *, system, messages, tools=None, max_tokens=None):
        self.calls.append({"system": system, "tools": tools})
        return self._responses.pop(0)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeToolClient:
    def __init__(self, handler):
        self._handler = handler
        self.calls = []
        self.closed = False

    def call(self, name, arguments=None):
        self.calls.append((name, arguments))
        return self._handler(name, arguments)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _text(text):
    return LLMResponse(text=text, tool_calls=[], stop_reason="end_turn",
                       raw_content=[{"type": "text", "text": text}])


def _install_fakes(monkeypatch, fake_llm, fake_tc):
    monkeypatch.setattr(agent.config, "require_banking_tools", lambda: ("https://tools.test", "tok"))

    class LLMClientStub:
        @classmethod
        def from_config(cls, client=None):
            return fake_llm

    monkeypatch.setattr(agent, "AnthropicMessagesClient", LLMClientStub)
    monkeypatch.setattr(agent, "ToolClient", lambda url, token: fake_tc)


def test_run_agent_returns_answer_and_advertises_confirmed_tools(monkeypatch):
    fake_llm = FakeLLM([_text("Here is your KB-grounded answer.")])
    fake_tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="unused"))
    _install_fakes(monkeypatch, fake_llm, fake_tc)

    out = agent.run_agent([{"role": "user", "content": "What is the overdraft fee?"}])

    assert out == "Here is your KB-grounded answer."
    advertised = {t["name"] for t in fake_llm.calls[0]["tools"]}
    # The full confirmed set is advertised: retrieval + BASE read + BASE action/discovery tools.
    assert {"KB_search_bm25", "shell"}.issubset(advertised)
    assert {"get_user_information_by_id", "get_current_time", "list_discoverable_agent_tools"}.issubset(advertised)
    assert {"log_verification", "change_user_email", "transfer_to_human_agents"}.issubset(advertised)
    assert {"unlock_discoverable_agent_tool", "call_discoverable_agent_tool", "give_discoverable_user_tool"}.issubset(advertised)
    assert "KB_search_dense" not in advertised


def test_run_agent_closes_both_clients(monkeypatch):
    fake_llm = FakeLLM([_text("done")])
    fake_tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="x"))
    _install_fakes(monkeypatch, fake_llm, fake_tc)

    agent.run_agent([{"role": "user", "content": "hi"}])

    assert fake_llm.closed is True
    assert fake_tc.closed is True


def test_run_agent_dispatches_a_base_tool_call(monkeypatch):
    # A tool_use for a confirmed BASE tool is dispatched through the tool client, then the final
    # answer is returned.
    responses = [
        LLMResponse(
            text="",
            tool_calls=[ToolCall(id="tu_1", name="get_user_information_by_id", input={"user_id": "u1"})],
            stop_reason="tool_use",
            raw_content=[{"type": "tool_use", "id": "tu_1", "name": "get_user_information_by_id",
                          "input": {"user_id": "u1"}}],
        ),
        _text("Your record shows ..."),
    ]
    fake_llm = FakeLLM(responses)
    fake_tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="Name: Jo; DOB: ..."))
    _install_fakes(monkeypatch, fake_llm, fake_tc)

    out = agent.run_agent([{"role": "user", "content": "look me up, id u1"}])

    assert out == "Your record shows ..."
    assert fake_tc.calls == [("get_user_information_by_id", {"user_id": "u1"})]


def test_import_agent_is_side_effect_free(monkeypatch):
    # Importing the module must not touch the environment; construction is lazy inside run_agent.
    for var in ("BANKING_TOOLS_URL", "BANKING_TOOLS_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    import importlib
    importlib.reload(agent)  # should not raise
    assert callable(agent.run_agent)


def test_run_agent_raises_honest_error_without_banking_env(monkeypatch):
    # No fakes installed and no env: run_agent must raise (server.py turns this into an HTTP 500
    # error envelope), never silently fabricate an answer.
    for var in ("BANKING_TOOLS_URL", "BANKING_TOOLS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError):
        agent.run_agent([{"role": "user", "content": "hi"}])
