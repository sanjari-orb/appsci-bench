"""Event publisher for the observability system.

Provides the EventPublisher class that appends events to a per-source JSONL log,
and a singleton getter for use across the codebase.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Generator, TextIO

from .storage import get_source_log_path
from .events import EventType, ObservabilityEvent, Source, create_span_id

logger = logging.getLogger(__name__)

# Module-level singleton
_publisher: EventPublisher | None = None
_publisher_lock = threading.Lock()


def _enum_value(v: Any) -> str:
    """Return the clean string for an enum member, else ``str(v)``.

    ``Source``/``EventType`` subclass ``(str, Enum)``, but ``str(member)`` yields
    the repr (``"Source.APPSCI_AGENT"``), not the value. We persist ``.value``
    (``"appsci_agent"``) so stored rows carry clean, filterable identifiers.
    """
    return v.value if isinstance(v, Enum) else str(v)


class EventPublisher:
    """Publishes observability events to SQLite.

    This class is thread-safe and handles graceful degradation if the
    database is unavailable.

    Example usage:
        publisher = EventPublisher(log_path="runs/abc-123/events-orchestrator.jsonl", trace_id="abc-123")
        publisher.emit(
            source=Source.APPSCI_AGENT,
            event_type=EventType.AGENT_TURN_START,
            payload={"turn": 1},
        )
    """

    def __init__(
        self,
        log_path: str | Path,
        trace_id: str,
        source: str | Source | None = None,
        task_id: str | None = None,
        truncate_payloads: bool = True,
        max_payload_chars: int = 500,
        log_events: bool | None = None,
        name: str | None = None,
    ):
        """Initialize the event publisher.

        Args:
            log_path: Path to this writer's per-source JSONL log file.
            trace_id: The trace ID for this run.
            source: Default source for events (can be overridden per-emit).
            task_id: The task this run is for (e.g. "order_to_cash"); stamped
                onto every emitted event for shared-store filtering/aggregation.
            truncate_payloads: Whether to truncate large payloads.
            max_payload_chars: Maximum characters for payload values.
            name: Default human-readable role label for this writer's agent
                (e.g. "AppSci agent"), stamped on every event unless a per-emit
                name overrides it. Falls back to the OBSERVABILITY_AGENT_NAME env
                var, else None.
        """
        self.log_path = Path(log_path)
        self.trace_id = trace_id
        self.task_id = task_id
        self.default_source = _enum_value(source) if source is not None else None
        self.agent_name = name if name is not None else (os.environ.get("OBSERVABILITY_AGENT_NAME") or None)
        self.truncate_payloads = truncate_payloads
        self.max_payload_chars = max_payload_chars
        # When on, log a one-line record per emitted event at INFO so the stream is watchable in the
        # console / docker logs without turning DEBUG on globally. Defaults from OBSERVABILITY_LOG_EVENTS
        # (set + forwarded to containers by the orchestrator's --verbose flag); off -> emits log at DEBUG.
        self.log_events = (
            log_events if log_events is not None
            else str(os.environ.get("OBSERVABILITY_LOG_EVENTS", "")).strip().lower()
            not in ("", "0", "false", "no")
        )

        self._fh: TextIO | None = None
        self._lock = threading.Lock()
        self._enabled = True

        # Open the append handle. One writer per file, so no cross-process
        # coordination is needed; the lock only guards multi-threaded writers
        # in one process (e.g. the user-sim's threadpool /ask handlers).
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.log_path, "a", encoding="utf-8")
            logger.info(f"Observability publisher initialized: {self.log_path}")
        except Exception as e:
            logger.warning(f"Failed to open observability log: {e}. Events will be dropped.")
            self._enabled = False

    def emit(
        self,
        event_type: str | EventType,
        payload: dict[str, Any] | None = None,
        source: str | Source | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        truncate: bool = True,
        name: str | None = None,
    ) -> str | None:
        """Emit an observability event.

        Args:
            event_type: The type of event.
            payload: Event-specific data.
            source: Source component (uses default if not provided).
            span_id: Span ID for this operation (generated if not provided).
            parent_span_id: Parent span ID for nested operations.
            truncate: Truncate payload values to ``max_payload_chars`` (default). Pass False for a
                payload that must survive whole, and bound it yourself unless something downstream
                already does. The one caller today is the user-sim exchange, which is deliberately
                UNBOUNDED: those events are the scored input to the elicitation diagnostic, where a
                clipped answer reads as a fact the persona never gave, so no cap may be reintroduced
                here (a test in the user-sim service enforces that). What keeps it safe is downstream:
                the tracer clamps the rendered text to a scrolling box rather than relying on a cap.
            name: Per-emit role-label override (e.g. mini-swe emitting from the agent process). Falls
                back to the publisher's ``agent_name`` when not provided.

        Returns:
            The span_id of the emitted event, or None if emission failed.
        """
        if not self._enabled or self._fh is None:
            return None

        # Resolve source
        resolved_source = _enum_value(source) if source is not None else self.default_source
        if not resolved_source:
            logger.warning("No source provided for event, using 'unknown'")
            resolved_source = "unknown"

        # Generate span_id if not provided
        resolved_span_id = span_id or create_span_id()

        # Create event
        event = ObservabilityEvent(
            source=resolved_source,
            event_type=_enum_value(event_type),
            trace_id=self.trace_id,
            task_id=self.task_id,
            span_id=resolved_span_id,
            parent_span_id=parent_span_id,
            name=name if name is not None else self.agent_name,
            payload=payload or {},
        )

        # Truncate payload if configured
        if truncate and self.truncate_payloads:
            event = event.truncate_payload(self.max_payload_chars)

        # Append one JSON line, flushed so a host reader sees it immediately.
        try:
            line = json.dumps(event.to_dict(), separators=(",", ":"))
            with self._lock:
                self._fh.write(line + "\n")
                self._fh.flush()
            # Stream visibility: log one compact line per emitted event. INFO when log_events is on so
            # the stream is watchable without global DEBUG; otherwise DEBUG (silent on a normal run).
            # Lazy %-args + the isEnabledFor guard keep this free when neither level is active.
            if self.log_events or logger.isEnabledFor(logging.DEBUG):
                logger.log(
                    logging.INFO if self.log_events else logging.DEBUG,
                    "emit %s %s span=%s parent=%s",
                    resolved_source, event.event_type,
                    (resolved_span_id or "")[:8], (event.parent_span_id or "-")[:8],
                )
            return resolved_span_id
        except Exception as e:
            logger.warning(f"Failed to emit observability event: {e}")
            return None

    @contextmanager
    def span(
        self,
        event_type_start: str | EventType,
        event_type_end: str | EventType,
        source: str | Source | None = None,
        parent_span_id: str | None = None,
        start_payload: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Context manager for tracking a span with start/end events.

        Emits a start event when entering, and an end event when exiting.
        The end payload can be set by modifying the yielded dict.

        Example:
            with publisher.span(
                EventType.AGENT_TOOL_INVOCATION,
                EventType.AGENT_TOOL_RESULT,
                start_payload={"tool": "mini_swe_agent"},
            ) as span_ctx:
                result = execute_tool()
                span_ctx["result"] = result

        Args:
            event_type_start: Event type for the start event.
            event_type_end: Event type for the end event.
            source: Source component.
            parent_span_id: Parent span ID.
            start_payload: Payload for the start event.

        Yields:
            A dict that will be used as the end event payload.
        """
        span_id = self.emit(
            event_type=event_type_start,
            source=source,
            parent_span_id=parent_span_id,
            payload=start_payload,
            name=name,
        )

        end_payload: dict[str, Any] = {}
        try:
            yield end_payload
        finally:
            self.emit(
                event_type=event_type_end,
                source=source,
                span_id=span_id,
                parent_span_id=parent_span_id,
                payload=end_payload,
                name=name,
            )

    def close(self) -> None:
        """Close the log file handle."""
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            self._enabled = False

    def __enter__(self) -> EventPublisher:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def init_publisher(
    runs_dir: str | Path | None = None,
    trace_id: str | None = None,
    source: str | Source | None = None,
    task_id: str | None = None,
    name: str | None = None,
) -> EventPublisher | None:
    """Initialize the global publisher singleton.

    Called at startup by each writer (agent, user-sim, orchestrator). The
    publisher appends to a *per-source* file, ``<runs_dir>/<trace_id>/events-<source>.jsonl``,
    so no file is ever shared by more than one writer. The tracer merges these
    logs on read (see ``storage.consolidate_trace``).

    If ``runs_dir`` / ``trace_id`` / ``task_id`` are not passed, they are read
    from the ``OBSERVABILITY_RUNS_DIR`` / ``OBSERVABILITY_TRACE_ID`` /
    ``OBSERVABILITY_TASK`` environment variables (set by the orchestrator;
    ``/runs`` inside containers). ``source`` is required -- it names the writer
    and therefore its file.

    Args:
        runs_dir: Base runs directory as seen by this process.
        trace_id: The trace ID for the run.
        source: This writer's source (also selects the log filename).
        task_id: The task this run is for; stamped onto every event.

    Returns:
        The initialized publisher, or None if observability is not enabled.
    """
    global _publisher

    # Check environment if not provided
    if runs_dir is None:
        runs_dir = os.environ.get("OBSERVABILITY_RUNS_DIR")
    if trace_id is None:
        trace_id = os.environ.get("OBSERVABILITY_TRACE_ID")
    if task_id is None:
        task_id = os.environ.get("OBSERVABILITY_TASK")

    if not runs_dir or not trace_id or source is None:
        logger.debug(
            "Observability not enabled (need runs dir, trace ID, and source)"
        )
        return None

    log_path = get_source_log_path(trace_id, source, runs_dir)

    with _publisher_lock:
        if _publisher is not None:
            _publisher.close()

        _publisher = EventPublisher(
            log_path=log_path,
            trace_id=trace_id,
            source=source,
            task_id=task_id,
            name=name,
        )

    return _publisher


def get_publisher() -> EventPublisher | None:
    """Get the global publisher singleton.

    Returns None if observability is not enabled (no publisher initialized).
    Safe to call even when observability is disabled - all operations will
    be no-ops.

    Returns:
        The global EventPublisher, or None if not initialized.
    """
    return _publisher


def emit(
    event_type: str | EventType,
    payload: dict[str, Any] | None = None,
    source: str | Source | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    truncate: bool = True,
    name: str | None = None,
) -> str | None:
    """Convenience function to emit an event using the global publisher.

    Safe to call even when observability is disabled - will return None.

    Args:
        event_type: The type of event.
        payload: Event-specific data.
        source: Source component.
        span_id: Span ID for this operation.
        parent_span_id: Parent span ID for nested operations.

    Returns:
        The span_id of the emitted event, or None.
    """
    publisher = get_publisher()
    if publisher is None:
        return None
    return publisher.emit(
        event_type=event_type,
        payload=payload,
        source=source,
        span_id=span_id,
        parent_span_id=parent_span_id,
        truncate=truncate,
        name=name,
    )
