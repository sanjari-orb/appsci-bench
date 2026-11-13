"""File-based (JSONL) observability storage + publisher tests."""
from __future__ import annotations

import json

import pytest

from shared.observability import storage
from shared.observability.events import EventType, ObservabilityEvent, Source
from shared.observability.publisher import EventPublisher


def _event(source, event_type, ts, task_id="order_to_cash", **payload):
    return ObservabilityEvent(
        source=str(source),
        event_type=str(event_type),
        trace_id="t1",
        task_id=task_id,
        payload=payload,
        timestamp=ts,
    )


def test_append_and_read_round_trip(tmp_path):
    log = tmp_path / "events.jsonl"
    storage.append_event(log, _event(Source.ORCHESTRATOR, EventType.BUILD_START, "2026-07-16T00:00:00"))
    events = storage.read_events(log)
    assert len(events) == 1
    e = events[0]
    assert e["id"] == 1
    assert e["source"] == "orchestrator"
    assert e["event_type"] == "build.start"
    assert e["task_id"] == "order_to_cash"
    assert e["payload"] == {}


def test_source_log_and_consolidated_paths(tmp_path):
    src = storage.get_source_log_path("t1", Source.APPSCI_AGENT, tmp_path)
    assert src.name == "events-appsci_agent.jsonl"
    cons = storage.get_consolidated_log_path("t1", tmp_path)
    assert cons.name == "events.jsonl"


def test_consolidate_merges_sources_and_dedups(tmp_path):
    trace = tmp_path / "t1"
    agent_log = trace / "events-appsci_agent.jsonl"
    orch_log = trace / "events-orchestrator.jsonl"
    storage.append_event(orch_log, _event(Source.ORCHESTRATOR, EventType.BUILD_START, "2026-07-16T00:00:00"))
    storage.append_event(agent_log, _event(Source.APPSCI_AGENT, EventType.AGENT_TURN_START, "2026-07-16T00:00:01"))

    cons = storage.consolidate_trace(trace)
    events = storage.read_events(cons)
    assert len(events) == 2
    # merged view is ordered by the shared host clock
    assert [e["event_type"] for e in events] == ["build.start", "agent.turn_start"]

    # Idempotent: re-consolidating with no new source events changes nothing.
    line_count_before = cons.read_text().count("\n")
    storage.consolidate_trace(trace)
    storage.consolidate_trace(trace)
    assert cons.read_text().count("\n") == line_count_before
    assert len(storage.read_events(cons)) == 2


def test_since_id_cursor_returns_only_new(tmp_path):
    trace = tmp_path / "t1"
    agent_log = trace / "events-appsci_agent.jsonl"
    storage.append_event(agent_log, _event(Source.APPSCI_AGENT, EventType.AGENT_TURN_START, "2026-07-16T00:00:00"))
    cons = storage.consolidate_trace(trace)
    first = storage.read_events(cons)
    assert len(first) == 1
    last_id = first[-1]["id"]

    # A new event appended after the cursor.
    storage.append_event(agent_log, _event(Source.APPSCI_AGENT, EventType.AGENT_LLM_REQUEST, "2026-07-16T00:00:01"))
    storage.consolidate_trace(trace)

    new = storage.read_events(cons, since_id=last_id)
    assert len(new) == 1
    assert new[0]["event_type"] == "agent.llm_request"
    assert new[0]["id"] == last_id + 1


def test_event_type_filter(tmp_path):
    log = tmp_path / "events.jsonl"
    storage.append_event(log, _event(Source.MINI_SWE_AGENT, EventType.SWE_BASH_COMMAND, "2026-07-16T00:00:00"))
    storage.append_event(log, _event(Source.MINI_SWE_AGENT, EventType.SWE_FILE_WRITE, "2026-07-16T00:00:01"))
    storage.append_event(log, _event(Source.MINI_SWE_AGENT, EventType.SWE_BASH_COMMAND, "2026-07-16T00:00:02"))
    only_bash = storage.read_events(log, event_type="swe.bash_command")
    assert len(only_bash) == 2
    assert all(e["event_type"] == "swe.bash_command" for e in only_bash)


def test_trace_summary_counts(tmp_path):
    trace = tmp_path / "t1"
    storage.append_event(trace / "events-orchestrator.jsonl", _event(Source.ORCHESTRATOR, EventType.BUILD_START, "2026-07-16T00:00:00"))
    storage.append_event(trace / "events-appsci_agent.jsonl", _event(Source.APPSCI_AGENT, EventType.AGENT_TURN_START, "2026-07-16T00:00:01"))
    storage.append_event(trace / "events-appsci_agent.jsonl", _event(Source.APPSCI_AGENT, EventType.AGENT_TURN_START, "2026-07-16T00:00:02"))
    cons = storage.consolidate_trace(trace)
    summary = storage.trace_summary(cons, "t1")
    assert summary["event_count"] == 3
    assert summary["sources"] == {"orchestrator": 1, "appsci_agent": 2}
    assert summary["event_types"] == {"build.start": 1, "agent.turn_start": 2}
    assert summary["start_time"] == "2026-07-16T00:00:00"
    assert summary["end_time"] == "2026-07-16T00:00:02"


def test_read_tolerates_truncated_final_line(tmp_path):
    log = tmp_path / "events.jsonl"
    storage.append_event(log, _event(Source.ORCHESTRATOR, EventType.BUILD_START, "2026-07-16T00:00:00"))
    # Simulate a partial line from a write still in flight.
    with open(log, "a", encoding="utf-8") as f:
        f.write('{"event_id": "partial", "event_ty')
    events = storage.read_events(log)
    assert len(events) == 1  # the good line survives; the partial is skipped
    assert events[0]["event_type"] == "build.start"


def test_list_traces_most_recent_first(tmp_path):
    for tid, ts in [("run_a", "2026-07-16T00:00:00"), ("run_b", "2026-07-16T05:00:00")]:
        storage.append_event(tmp_path / tid / "events-orchestrator.jsonl",
                             _event(Source.ORCHESTRATOR, EventType.BUILD_START, ts))
    traces = storage.list_traces(tmp_path)
    assert [t["trace_id"] for t in traces] == ["run_b", "run_a"]
    assert traces[0]["event_count"] == 1


def test_publisher_appends_jsonl_with_task_id(tmp_path):
    log = tmp_path / "events-appsci_agent.jsonl"
    pub = EventPublisher(log_path=log, trace_id="t1", source=Source.APPSCI_AGENT, task_id="order_to_cash")
    span = pub.emit(EventType.AGENT_TURN_START, payload={"turn": 1})
    pub.close()
    assert span is not None

    lines = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    row = lines[0]
    assert row["source"] == "appsci_agent"       # enum -> value, not repr
    assert row["event_type"] == "agent.turn_start"
    assert row["task_id"] == "order_to_cash"
    assert row["trace_id"] == "t1"
    assert row["payload"] == {"turn": 1}


def test_publisher_truncates_large_payloads(tmp_path):
    log = tmp_path / "events-orchestrator.jsonl"
    pub = EventPublisher(log_path=log, trace_id="t1", source=Source.ORCHESTRATOR,
                         max_payload_chars=10)
    pub.emit(EventType.BUILD_START, payload={"blob": "x" * 100})
    pub.close()
    row = json.loads(log.read_text().splitlines()[0])
    assert row["payload"]["blob"].endswith("...")
    assert len(row["payload"]["blob"]) <= 13  # 10 chars + "..."
