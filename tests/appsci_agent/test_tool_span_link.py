"""The turn -> tool -> mini-swe span chain.

A dispatched mini-swe run must be parentable to the exact tool call that spawned it, so the
tracer can nest it under the right agent turn even when a turn makes multiple tool calls (rather
than relying on event ordering). The agent loop threads the AGENT_TOOL_INVOCATION span_id onto the
ToolContext before each execute(); mini-swe reads it as the parent of its swe.task_start. Here we
assert that seam: whatever span the invocation event carries is exactly what the tool sees on the
context.
"""
from __future__ import annotations

import json

import shared.observability.publisher as pub
from shared.observability.publisher import init_publisher
from shared.observability.events import Source, EventType
from shared.observability.storage import get_source_log_path

from appsci_agent.core.agent import AppSciAgent
from appsci_agent.core.llm_client import ToolCall
from appsci_agent.tools.base import BaseTool, ToolContext, ToolResult


class _ProbeTool(BaseTool):
    """Records the parent_span_id the agent put on the context before calling execute()."""

    def __init__(self):
        self.seen_parent_span_id = "<<unset>>"

    @property
    def name(self) -> str:
        return "probe_tool"

    @property
    def description(self) -> str:
        return "records context.parent_span_id"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, params: dict, context: ToolContext) -> ToolResult:
        self.seen_parent_span_id = context.parent_span_id
        return ToolResult(success=True, output={"ok": True})


def _reset():
    p = pub.get_publisher()
    if p is not None:
        p.close()
    pub._publisher = None


def _agent_with_publisher(tool: BaseTool) -> AppSciAgent:
    # Bypass the heavy constructor (LLM client / config); _execute_tool only needs these.
    agent = object.__new__(AppSciAgent)
    agent._obs_publisher = pub.get_publisher()
    agent.tools = {tool.name: tool}
    return agent


def test_tool_invocation_span_is_threaded_to_the_tool_context(tmp_path):
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=str(tmp_path), trace_id="tr", source=Source.APPSCI_AGENT, task_id="t")
    probe = _ProbeTool()
    try:
        agent = _agent_with_publisher(probe)
        agent._execute_tool(ToolCall(id="c1", name="probe_tool", arguments={}), ToolContext())
    finally:
        _reset()

    evs = [json.loads(l) for l in
           get_source_log_path("tr", Source.APPSCI_AGENT, str(tmp_path)).read_text().splitlines()
           if l.strip()]
    inv = next(e for e in evs if e["event_type"] == str(EventType.AGENT_TOOL_INVOCATION))

    # The span the invocation event carries is exactly what the tool saw on the context --
    # so a sub-span opened during execute() (mini-swe's swe.task_start) parents under this call.
    assert inv["span_id"], "tool invocation should have a span_id under an active publisher"
    assert probe.seen_parent_span_id == inv["span_id"]


def test_tool_invocation_parents_under_the_turn_span(tmp_path):
    # Completing the span tree: the invocation nests under the turn span the agent loop passes in.
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=str(tmp_path), trace_id="tr", source=Source.APPSCI_AGENT,
                   task_id="t", name="AppSci agent")
    probe = _ProbeTool()
    try:
        agent = _agent_with_publisher(probe)
        agent._execute_tool(ToolCall(id="c1", name="probe_tool", arguments={}), ToolContext(),
                            parent_span_id="turn-span-1")
    finally:
        _reset()
    evs = [json.loads(l) for l in
           get_source_log_path("tr", Source.APPSCI_AGENT, str(tmp_path)).read_text().splitlines() if l.strip()]
    inv = next(e for e in evs if e["event_type"] == str(EventType.AGENT_TOOL_INVOCATION))
    assert inv["parent_span_id"] == "turn-span-1"     # invocation nests under the turn
    assert inv["name"] == "AppSci agent"              # publisher-default role label
    assert probe.seen_parent_span_id == inv["span_id"]  # sub-agents still nest under the invocation


def test_parent_span_id_is_none_when_observability_off():
    _reset()  # no publisher
    probe = _ProbeTool()
    agent = _agent_with_publisher(probe)  # _obs_publisher is None
    agent._execute_tool(ToolCall(id="c1", name="probe_tool", arguments={}), ToolContext())
    # No invocation span exists, so the tool sees None -- mini-swe then emits with parent None (no-op anyway).
    assert probe.seen_parent_span_id is None
