"""Combined observability: the evaluator emits eval.* events into the shared trace, and the tracer
serves the persisted eval report. Verifies the eval-viewer <-> build-observability integration."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

import shared.observability.publisher as pub
from shared.observability.events import EventType, Source
from shared.observability.publisher import init_publisher
from shared.observability.storage import get_source_log_path
from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.eval_system import run_evaluation
from appsci_bench.eval.datatypes import ArtifactAgentOutput
from appsci_bench.tracer.server import TracerServer

_PKG = "tests.fixtures"  # eval_task.eval_fns


def _echo(q):
    return ArtifactAgentOutput(text=(q.text or "").upper())


def _spec():
    return {"task_id": "t_single", "evaluation_data": {
        "type": "single_turn",
        "data": [{"data_point_id": "d1", "single_turn_input": {"text": "go"},
                  "expected_output": {"text": "GO"}}],
        "objectives": [{"name": "match", "eval_function_path": "eval_task.eval_fns.text_exact_match",
                        "positive": True, "weight": 1.0}],
    }}


def _reset_publisher():
    p = pub.get_publisher()
    if p is not None:
        p.close()
    pub._publisher = None


def test_eval_emits_events_into_active_trace(tmp_path):
    runs = str(tmp_path)
    trace_id = "trace_test"
    (tmp_path / trace_id).mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=runs, trace_id=trace_id, source=Source.EVALUATOR, task_id="t_single")
    try:
        run_evaluation(_spec(), InProcessArtifactClient(_echo), package=_PKG)
    finally:
        _reset_publisher()

    log = get_source_log_path(trace_id, Source.EVALUATOR, runs)
    events = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    types = [e["event_type"] for e in events]
    assert str(EventType.EVAL_START) in types
    assert str(EventType.EVAL_CASE_SCORE) in types
    assert str(EventType.EVAL_COMPLETE) in types
    complete = next(e for e in events if e["event_type"] == str(EventType.EVAL_COMPLETE))
    assert "final_score" in complete["payload"]
    case = next(e for e in events if e["event_type"] == str(EventType.EVAL_CASE_SCORE))
    assert case["payload"]["data_point_id"] == "d1"
    # every eval event carries the shared trace_id (so it groups with the build events)
    assert all(e["trace_id"] == trace_id for e in events)


def test_eval_emit_is_noop_without_publisher():
    # No active publisher -> emit() no-ops; run_evaluation must still complete normally.
    _reset_publisher()
    rep = run_evaluation(_spec(), InProcessArtifactClient(_echo), package=_PKG)
    assert rep.final_score is not None


def test_tracer_serves_persisted_eval_report(tmp_path):
    trace_id = "trace_rep"
    eval_dir = tmp_path / trace_id / "eval"
    eval_dir.mkdir(parents=True)
    (eval_dir / "report.html").write_text("<html><body>EVAL REPORT OK</body></html>")
    client = TestClient(TracerServer(runs_dir=tmp_path, port=0)._create_app())

    assert client.get(f"/api/eval/{trace_id}").json()["has_report"] is True
    r = client.get(f"/eval/{trace_id}/report")
    assert r.status_code == 200 and "EVAL REPORT OK" in r.text
    # missing trace -> 404 + status false
    assert client.get("/eval/nope/report").status_code == 404
    assert client.get("/api/eval/nope").json()["has_report"] is False
