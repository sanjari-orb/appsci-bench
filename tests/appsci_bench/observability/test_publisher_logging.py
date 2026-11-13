"""Per-event emit logging: OBSERVABILITY_LOG_EVENTS (set by the orchestrator's --verbose flag) makes
the publisher log one INFO line per emitted event so the stream is watchable; off by default it stays
at DEBUG and a normal INFO-level run is quiet."""
from __future__ import annotations

import logging

import shared.observability.publisher as pub
from shared.observability.publisher import init_publisher
from shared.observability.events import Source, EventType

LOGGER = "shared.observability.publisher"


def _reset():
    p = pub.get_publisher()
    if p is not None:
        p.close()
    pub._publisher = None


def _emit_lines(records):
    return [r for r in records if r.getMessage().startswith("emit ")]


def test_log_events_on_emits_one_info_line_per_event(tmp_path, caplog):
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=str(tmp_path), trace_id="tr", source=Source.ORCHESTRATOR, task_id="t")
    pub.get_publisher().log_events = True                      # what --verbose / the env toggle sets
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            pub.emit(EventType.BUILD_START, payload={"task_id": "t"})
            pub.emit(EventType.BUILD_COMPLETE, payload={"status": "completed"})
    finally:
        _reset()

    lines = _emit_lines(caplog.records)
    assert len(lines) == 2
    assert all(r.levelno == logging.INFO for r in lines)
    msg = lines[0].getMessage()
    assert "orchestrator" in msg and "build.start" in msg and "span=" in msg


def test_default_is_quiet_at_info(tmp_path, caplog):
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=str(tmp_path), trace_id="tr", source=Source.ORCHESTRATOR, task_id="t")
    # log_events defaults off (env unset) -> emits log at DEBUG, invisible at INFO.
    assert pub.get_publisher().log_events is False
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            pub.emit(EventType.BUILD_START, payload={"task_id": "t"})
    finally:
        _reset()
    assert _emit_lines(caplog.records) == []


def test_env_var_enables_logging(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_LOG_EVENTS", "1")
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=str(tmp_path), trace_id="tr", source=Source.EVALUATOR, task_id="t")
    assert pub.get_publisher().log_events is True             # picked up from the env at construction
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            pub.emit(EventType.EVAL_COMPLETE, payload={"final_score": 0.5})
    finally:
        _reset()
    assert len(_emit_lines(caplog.records)) == 1
