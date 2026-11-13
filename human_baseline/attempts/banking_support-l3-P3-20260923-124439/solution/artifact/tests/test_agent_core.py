"""Tests for agent_core.AgentLoop: the generic tool-use loop over confirmed interfaces.

Driven entirely by fakes (a scripted LLM and a scripted tool client), so no network and no real
model are needed. They assert the loop:
  * returns the model's text when there are no tool calls,
  * replays the assistant tool_use turn and feeds back tool_result blocks, then returns the final text,
  * surfaces an unknown tool and a failed tool call as is_error results (never masked as success),
  * ends with an honest, non-fabricated message when it runs out of internal steps,
  * appends incoming system messages to the base system prompt and coerces list content to text,
  * never calls the model on an empty conversation.
"""
from __future__ import annotations

import json

import pytest

from agent_core import AgentLoop, ToolSpec, _EXHAUSTED_MESSAGE
from llm_client import LLMResponse, ToolCall
from tools import ToolResult, KIND_OK, KIND_AUTH_ERROR
import tool_specs

SYSTEM = "BASE SYSTEM PROMPT"

BM25 = ToolSpec(
    name="KB_search_bm25",
    description="search",
    input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
)


class FakeLLM:
    """Returns scripted LLMResponses in order and records each request it received."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create_message(self, *, system, messages, tools=None, max_tokens=None):
        # Deep-ish copy of messages so later mutation of the loop's list doesn't rewrite history.
        self.calls.append({
            "system": system,
            "messages": json.loads(json.dumps(messages)),
            "tools": tools,
            "max_tokens": max_tokens,
        })
        if not self._responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        return self._responses.pop(0)


class FakeToolClient:
    """Returns a scripted ToolResult per call and records (name, arguments)."""

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def call(self, name, arguments=None):
        self.calls.append((name, arguments))
        return self._handler(name, arguments)


def _text(text):
    return LLMResponse(text=text, tool_calls=[], stop_reason="end_turn",
                       raw_content=[{"type": "text", "text": text}])


def _tool(name, tid, inp):
    block = {"type": "tool_use", "id": tid, "name": name, "input": inp}
    return LLMResponse(text="", tool_calls=[ToolCall(id=tid, name=name, input=inp)],
                       stop_reason="tool_use", raw_content=[block])


def _loop(llm, tool_client, tools=(BM25,), **kwargs):
    return AgentLoop(llm, tool_client, list(tools), SYSTEM, **kwargs)


def test_returns_text_when_no_tool_calls():
    llm = FakeLLM([_text("Your balance question: here is the KB answer.")])
    tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="unused"))
    out = _loop(llm, tc).run([{"role": "user", "content": "hi"}])

    assert out == "Your balance question: here is the KB answer."
    assert tc.calls == []                       # no tool call was requested
    assert llm.calls[0]["system"] == SYSTEM
    assert llm.calls[0]["messages"] == [{"role": "user", "content": "hi"}]
    # The confirmed tool was advertised to the model.
    assert llm.calls[0]["tools"][0]["name"] == "KB_search_bm25"


def test_executes_tool_then_returns_final_answer():
    llm = FakeLLM([
        _tool("KB_search_bm25", "tu_1", {"query": "overdraft fee"}),
        _text("The overdraft fee is documented as $X."),
    ])
    tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="KB: overdraft fee is ..."))
    out = _loop(llm, tc).run([{"role": "user", "content": "what's the overdraft fee?"}])

    assert out == "The overdraft fee is documented as $X."
    assert tc.calls == [("KB_search_bm25", {"query": "overdraft fee"})]

    # Second LLM call must include the assistant tool_use turn and the user tool_result turn.
    second = llm.calls[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-2]["content"][0]["type"] == "tool_use"
    tool_result_turn = second[-1]
    assert tool_result_turn["role"] == "user"
    block = tool_result_turn["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tu_1"
    assert "overdraft fee" in block["content"]
    assert "is_error" not in block


def test_unknown_tool_is_reported_not_invoked():
    llm = FakeLLM([
        _tool("mystery_action", "tu_1", {"x": 1}),
        _text("I could not do that."),
    ])
    tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="nope"))
    out = _loop(llm, tc).run([{"role": "user", "content": "do a thing"}])

    assert out == "I could not do that."
    assert tc.calls == []            # the unadvertised tool was NOT dispatched to the tool server
    block = llm.calls[1]["messages"][-1]["content"][0]
    assert block["is_error"] is True
    assert "not available" in block["content"]


def test_tool_failure_is_surfaced_as_error_result():
    llm = FakeLLM([
        _tool("KB_search_bm25", "tu_1", {"query": "x"}),
        _text("I hit an error and did not complete this."),
    ])
    tc = FakeToolClient(
        lambda n, a: ToolResult(False, n, KIND_AUTH_ERROR, status_code=401, detail="denied")
    )
    out = _loop(llm, tc).run([{"role": "user", "content": "x"}])

    assert out == "I hit an error and did not complete this."
    block = llm.calls[1]["messages"][-1]["content"][0]
    assert block["is_error"] is True
    assert "auth_error" in block["content"]
    assert "401" in block["content"]


def test_multiple_tool_calls_in_one_turn_execute_in_order():
    resp = LLMResponse(
        text="",
        tool_calls=[
            ToolCall(id="a", name="KB_search_bm25", input={"query": "one"}),
            ToolCall(id="b", name="KB_search_bm25", input={"query": "two"}),
        ],
        stop_reason="tool_use",
        raw_content=[
            {"type": "tool_use", "id": "a", "name": "KB_search_bm25", "input": {"query": "one"}},
            {"type": "tool_use", "id": "b", "name": "KB_search_bm25", "input": {"query": "two"}},
        ],
    )
    llm = FakeLLM([resp, _text("done")])
    tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result=f"res:{a['query']}"))
    out = _loop(llm, tc).run([{"role": "user", "content": "x"}])

    assert out == "done"
    assert tc.calls == [("KB_search_bm25", {"query": "one"}), ("KB_search_bm25", {"query": "two"})]
    results_turn = llm.calls[1]["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in results_turn] == ["a", "b"]


def test_dict_result_is_json_encoded_for_tool_result():
    llm = FakeLLM([_tool("KB_search_bm25", "tu_1", {"query": "x"}), _text("ok")])
    tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result={"hits": ["p1", "p2"]}))
    _loop(llm, tc).run([{"role": "user", "content": "x"}])
    content = llm.calls[1]["messages"][-1]["content"][0]["content"]
    assert json.loads(content) == {"hits": ["p1", "p2"]}


def test_error_prefixed_ok_result_is_flagged_as_error():
    # The banking tools may report a failure as an "Error:"-prefixed string inside an HTTP 200
    # response. The loop must surface it to the model as is_error, NOT as a successful result.
    llm = FakeLLM([_tool("KB_search_bm25", "tu_1", {"query": "x"}), _text("I could not do that.")])
    tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="Error: customer not found"))
    _loop(llm, tc).run([{"role": "user", "content": "x"}])
    block = llm.calls[1]["messages"][-1]["content"][0]
    assert block["is_error"] is True
    assert "Error: customer not found" in block["content"]


def test_ordinary_ok_result_is_not_flagged_as_error():
    # Regression guard: a normal result that merely contains the word "error" mid-string is NOT a
    # failure -- only a leading "Error:" is.
    llm = FakeLLM([_tool("KB_search_bm25", "tu_1", {"query": "x"}), _text("ok")])
    tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="No error occurred; balance is $5."))
    _loop(llm, tc).run([{"role": "user", "content": "x"}])
    block = llm.calls[1]["messages"][-1]["content"][0]
    assert "is_error" not in block


def test_exhaustion_returns_honest_message():
    # The model keeps calling a tool and never gives a final answer; the loop must stop honestly.
    responses = [_tool("KB_search_bm25", f"tu_{i}", {"query": "x"}) for i in range(5)]
    llm = FakeLLM(responses)
    tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="more"))
    out = _loop(llm, tc, max_iterations=3).run([{"role": "user", "content": "x"}])

    assert out == _EXHAUSTED_MESSAGE
    assert len(tc.calls) == 3            # exactly max_iterations tool rounds, then a clean stop


def test_empty_conversation_does_not_call_llm():
    llm = FakeLLM([_text("unused")])
    tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="x"))
    out = _loop(llm, tc).run([])
    assert "help" in out.lower()
    assert llm.calls == []


def test_system_messages_are_appended_to_base_prompt():
    llm = FakeLLM([_text("ok")])
    tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="x"))
    _loop(llm, tc).run([
        {"role": "system", "content": "EXTRA POLICY"},
        {"role": "user", "content": "hi"},
    ])
    system = llm.calls[0]["system"]
    assert system.startswith(SYSTEM)
    assert system.endswith("EXTRA POLICY")
    # The system message is NOT left in the message list.
    assert llm.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_list_content_is_coerced_to_text():
    llm = FakeLLM([_text("ok")])
    tc = FakeToolClient(lambda n, a: ToolResult(True, n, KIND_OK, result="x"))
    _loop(llm, tc).run([
        {"role": "user", "content": [{"type": "text", "text": "part one "}, {"type": "text", "text": "part two"}]},
    ])
    assert llm.calls[0]["messages"] == [{"role": "user", "content": "part one part two"}]


def test_default_retrieval_tools_are_confirmed_only():
    # Guardrail: the default advertised set is exactly the confirmed retrieval tools -- no guessed
    # BASE/unlock/specialist tools have crept in.
    names = {t.name for t in tool_specs.RETRIEVAL_TOOLS}
    assert names == {"KB_search_bm25", "shell"}
