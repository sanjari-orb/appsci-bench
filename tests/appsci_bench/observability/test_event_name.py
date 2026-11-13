"""The role-label `name` field: recorded on events + ModelCall, per-writer default, per-emit override,
OBSERVABILITY_AGENT_NAME env fallback, and back-compat with old rows that have no name."""
from __future__ import annotations

import json

import shared.observability.publisher as pub
from shared.observability.publisher import init_publisher
from shared.observability.events import ObservabilityEvent, Source, EventType
from shared.observability.storage import get_source_log_path
from shared.observability import model_io


def _reset():
    p = pub.get_publisher()
    if p is not None:
        p.close()
    pub._publisher = None


def _events(tmp_path, source=Source.APPSCI_AGENT):
    return [json.loads(l) for l in
            get_source_log_path("tr", source, str(tmp_path)).read_text().splitlines() if l.strip()]


# --- events.py: round-trip + truncate + back-compat --------------------------------------------

def test_name_round_trips_through_dict():
    e = ObservabilityEvent(source="appsci_agent", event_type="agent.turn_start", trace_id="t", name="AppSci agent")
    assert e.to_dict()["name"] == "AppSci agent"
    assert ObservabilityEvent.from_dict(e.to_dict()).name == "AppSci agent"


def test_name_survives_truncate_payload():
    # Regression: truncate_payload reconstructs the event and must not drop `name`.
    e = ObservabilityEvent(source="usersim", event_type="usersim.response", trace_id="t",
                           name="Product Manager", payload={"answer": "x" * 5000})
    t = e.truncate_payload(500)
    assert t.name == "Product Manager"
    assert t.payload["answer"].endswith("...")


def test_from_dict_without_name_is_backcompat():
    # Old rows lack `name` entirely.
    old = {"source": "appsci_agent", "event_type": "agent.turn_start", "trace_id": "t",
           "payload": {}, "event_id": "e1", "timestamp": "2026-01-01T00:00:00+00:00",
           "span_id": "s1", "parent_span_id": None, "task_id": "tk"}
    assert ObservabilityEvent.from_dict(old).name is None


# --- publisher: default / per-emit override / env fallback -------------------------------------

def test_publisher_default_name_stamped_on_events(tmp_path):
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=str(tmp_path), trace_id="tr", source=Source.APPSCI_AGENT,
                   task_id="t", name="AppSci agent")
    try:
        pub.emit(EventType.AGENT_TURN_START, payload={"turn": 1})
    finally:
        _reset()
    assert _events(tmp_path)[0]["name"] == "AppSci agent"


def test_per_emit_name_overrides_publisher_default(tmp_path):
    # mini-swe runs in the agent process (default "AppSci agent") but tags its own events "mini-swe".
    # The event's source/name reflect the override, but it still lands in the agent process's file.
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=str(tmp_path), trace_id="tr", source=Source.APPSCI_AGENT,
                   task_id="t", name="AppSci agent")
    try:
        pub.emit(EventType.SWE_BASH_COMMAND, payload={"command": "ls"},
                 source=Source.MINI_SWE_AGENT, name="mini-swe")
    finally:
        _reset()
    ev = _events(tmp_path)[0]  # publisher's own file (events-appsci_agent.jsonl)
    assert ev["name"] == "mini-swe" and ev["source"] == "mini_swe_agent"


def test_env_agent_name_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_AGENT_NAME", "from-env")
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=str(tmp_path), trace_id="tr", source=Source.APPSCI_AGENT, task_id="t")
    try:
        assert pub.get_publisher().agent_name == "from-env"
        pub.emit(EventType.AGENT_TURN_START, payload={"turn": 1})
    finally:
        _reset()
    assert _events(tmp_path)[0]["name"] == "from-env"


def test_no_name_is_none(tmp_path):
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=str(tmp_path), trace_id="tr", source=Source.APPSCI_AGENT, task_id="t")
    try:
        pub.emit(EventType.AGENT_TURN_START, payload={"turn": 1})
    finally:
        _reset()
    assert _events(tmp_path)[0]["name"] is None


# --- model_io: name flows onto MODEL_REQUEST/RESPONSE events + the ModelCall record -------------

def test_model_call_name_on_events_and_record(tmp_path):
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=str(tmp_path), trace_id="tr", source=Source.APPSCI_AGENT, task_id="t")
    try:
        h = model_io.model_call_start([{"role": "user", "content": "q"}], model="m",
                                      caller="mini_swe", source=Source.MINI_SWE_AGENT, name="mini-swe")
        model_io.model_call_end(h, content="a")
    finally:
        _reset()
    evs = [e for e in _events(tmp_path) if e["event_type"].startswith("model.")]  # publisher's file
    assert evs and all(e["name"] == "mini-swe" for e in evs)              # both request + response
    call = model_io.load_model_calls(tmp_path / "tr")[evs[0]["payload"]["ref"]]
    assert call["name"] == "mini-swe"