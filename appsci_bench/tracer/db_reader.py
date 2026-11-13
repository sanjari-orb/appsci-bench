"""File-based reader for the tracer.

Thin adapter over :mod:`shared.observability.storage`. Preserves the
``connect_db`` / ``query_events`` / ``get_trace_summary`` call shape the tracer
server already uses, but backed by the consolidated ``events.jsonl`` log rather
than a SQLite connection. ``connect_db`` returns a lightweight handle whose
``close()`` is a no-op (there is no connection to close) so existing call sites
keep working unchanged.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from shared.observability import storage

logger = logging.getLogger(__name__)


class EventLog:
    """Read handle over a consolidated ``events.jsonl`` file.

    Exists only to preserve the ``conn = connect_db(...); ...; conn.close()``
    idiom in the server; it holds a path, not an open resource.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def close(self) -> None:  # no-op; nothing to release for a file read
        pass


def connect_db(path: Path | str) -> EventLog:
    """Open a read handle over a consolidated events log."""
    return EventLog(path)


def query_events(
    log: EventLog,
    trace_id: str | None = None,
    since_id: int | None = None,
    event_type: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Query events from the log.

    Args:
        log: Handle from :func:`connect_db`.
        trace_id: Accepted for call-site compatibility; a log already holds a
            single trace's events, so it is not used for filtering.
        since_id: Only return events with ``id`` greater than this (for polling).
        event_type: Only return events of this type.
        limit: Maximum number of events to return.

    Returns:
        List of event dictionaries (each with an ``id`` field).
    """
    return storage.read_events(
        log.path, since_id=since_id, event_type=event_type, limit=limit
    )


def get_trace_summary(log: EventLog, trace_id: str) -> dict[str, Any]:
    """Summary information about a trace (counts, time span, by source/type)."""
    return storage.trace_summary(log.path, trace_id)


