"""File-based (JSONL) storage for observability events.

Each writer process appends newline-delimited JSON to its own per-source log,
``runs/<trace_id>/events-<source>.jsonl`` -- **one writer per file**, so no
locking or cross-process coordination is needed and a host reader can tail it
over a Docker bind mount with no journaling sidecars (the reason we don't use a
DB: a single-writer append-only file is the simplest thing that survives the
container boundary).

On read, the tracer merges the per-source logs into an **append-only**
consolidated log, ``runs/<trace_id>/events.jsonl``. It is never reordered, so
each event's 1-based line number is a stable, monotonic ``id`` -- the cursor the
SSE stream polls with ``since_id``. Merge is dedup-by-``event_id`` and
idempotent, so it is safe to call on every poll of a live run.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator

from .events import ObservabilityEvent

logger = logging.getLogger(__name__)


def source_slug(source: Any) -> str:
    """Normalize a source identifier to a filesystem-safe slug.

    Accepts a ``Source`` enum, its ``.value`` (``"appsci_agent"``), or even the
    legacy enum repr (``"Source.APPSCI_AGENT"``) and returns a clean lowercase
    token suitable for a filename.
    """
    if isinstance(source, Enum):
        source = source.value
    s = str(source)
    if "." in s:  # tolerate a raw enum repr like "Source.APPSCI_AGENT"
        s = s.rsplit(".", 1)[-1]
    return s.lower()


def get_source_log_path(
    trace_id: str, source: Any, runs_dir: Path | str = "runs"
) -> Path:
    """Path to the per-source log a single writer owns.

    Each writer process (agent, user-sim, orchestrator) appends only to its own
    ``events-<source>.jsonl`` so that no file ever has more than one writer.

    Args:
        trace_id: The trace ID for the run.
        source: The writer's source identifier (enum or string).
        runs_dir: The base directory for run data (as seen by the writer).

    Returns:
        Path to this writer's JSONL log file.
    """
    return Path(runs_dir) / trace_id / f"events-{source_slug(source)}.jsonl"


def get_consolidated_log_path(trace_id: str, runs_dir: Path | str = "runs") -> Path:
    """Path to the merged, read-side log (``events.jsonl``) for a trace.

    Writers do not touch it directly; :func:`consolidate_trace` builds it from
    the per-source logs.
    """
    return Path(runs_dir) / trace_id / "events.jsonl"


def append_event(path: Path | str, event: ObservabilityEvent | dict[str, Any]) -> None:
    """Append one event as a JSON line to ``path`` (creating parents).

    ``event`` may be an :class:`ObservabilityEvent` or a plain dict. A single
    ``write`` of one newline-terminated line keeps records intact for a reader
    tailing concurrently (it only ever parses up to the last complete newline).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = event.to_dict() if isinstance(event, ObservabilityEvent) else event
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, separators=(",", ":")) + "\n")


def _iter_log_lines(path: Path | str) -> Iterator[dict[str, Any]]:
    """Yield parsed events from a JSONL file.

    Tolerates a blank or truncated final line -- e.g. a partial line from a
    write still in flight, or a crash mid-append -- by skipping anything that
    doesn't parse rather than raising.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (ValueError, RecursionError):
                    # Wider than ``json.JSONDecodeError`` on purpose. These files are written inside
                    # the run, so a line can be hostile as well as truncated: an integer literal over
                    # ~4,300 digits raises a bare ``ValueError`` (``JSONDecodeError`` is a subclass,
                    # so only the wider clause catches both), and deeply nested JSON exhausts the
                    # parser's stack and raises ``RecursionError``, which is a ``RuntimeError`` and so
                    # caught by no ``ValueError`` clause at all. Reproduced with one 195 KB line of
                    # ``[`` * 100000 + ``]`` * 100000: it escaped this loop out of ``list_traces``,
                    # which the tracer's run-list page and ``/api/traces`` both call unguarded -- so
                    # one bad run broke the listing of every run, until its directory was deleted.
                    continue
    except FileNotFoundError:
        return


#: Fingerprint -> nothing-to-do, for :func:`consolidate_trace` and :func:`cached_trace_summary`.
#: Guarded by a lock because the tracer serves these from a threadpool: FastAPI runs sync route
#: bodies, and the SSE stream's polls, in worker threads, so two requests genuinely do reach these
#: dicts at once. A dict operation is atomic under the GIL, but the read-then-write pair here is
#: not, and the failure it buys is a wasted re-parse rather than corruption -- the lock is cheap
#: and makes that reasoning unnecessary.
_memo_lock = threading.Lock()
_consolidated: dict[str, tuple] = {}
_summaries: dict[str, tuple[tuple, dict[str, Any]]] = {}

#: Entries kept before the whole memo is dropped. Both callers scan every run per page, so evicting
#: a slice would drop exactly the runs the next scan asks for first; clearing is O(1) and costs one
#: scan's parses. Sized well past any plausible run count on one host.
_MEMO_MAX = 8192


def _memo_put(store: dict, key: str, value: Any) -> None:
    """Record ``value`` under ``key``, clearing the store wholesale if it is full."""
    with _memo_lock:
        if len(store) >= _MEMO_MAX:
            store.clear()
        store[key] = value


def _stat_key(path: Path) -> tuple | None:
    """(size, mtime_ns) for ``path``, or None if it is not there.

    Size is the load-bearing half: these logs are append-only, so any new event changes it. mtime
    is carried alongside to catch the out-of-band rewrite that happens to land on the same length.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def consolidate_trace(trace_dir: Path | str) -> Path | None:
    """Merge all per-source logs in a trace dir into one ``events.jsonl``.

    Reads every ``events-<source>.jsonl`` and appends any event not already in
    the consolidated log (dedup by ``event_id``) to its end. Because the
    consolidated log is **append-only and never reordered**, each event's 1-based
    line number is a stable, monotonic ``id``. Idempotent and safe to call on
    every poll.

    Args:
        trace_dir: Directory holding a run's per-source log files.

    Returns:
        Path to the consolidated ``events.jsonl``, or None if there is nothing
        to read (no per-source logs and no pre-existing consolidated log).
    """
    trace_dir = Path(trace_dir)
    consolidated = trace_dir / "events.jsonl"
    source_logs = sorted(trace_dir.glob("events-*.jsonl"))

    if not source_logs:
        return consolidated if consolidated.exists() else None

    # Nothing new since the last merge -> nothing to do. Worth a memo because the dedup set below
    # is built by parsing the WHOLE consolidated log, so an idempotent call is not a cheap call:
    # the tracer's run list did this for every run on every render (measured: 1.2s over 191 runs,
    # 111MB of JSONL) and the SSE stream does it on every poll of a finished run.
    #
    # The SOURCE logs are fingerprinted before the merge reads them, never after. Stat them
    # afterwards and the memo would record a size covering events appended while the merge was
    # reading -- events this pass never saw -- and the next call would skip them for good. Reading
    # first can only cost a redundant pass, which the next call absorbs.
    sources_fp = tuple((src.name, _stat_key(src)) for src in source_logs)
    memo_key = str(trace_dir)
    if _consolidated.get(memo_key) == (_stat_key(consolidated), sources_fp):
        return consolidated if consolidated.exists() else None

    seen = {e.get("event_id") for e in _iter_log_lines(consolidated)}
    new_events: list[dict[str, Any]] = []
    for src in source_logs:
        for e in _iter_log_lines(src):
            eid = e.get("event_id")
            if eid and eid not in seen:
                seen.add(eid)
                new_events.append(e)

    if new_events:
        # Order this batch by the shared host clock before appending. (Across
        # batches the log stays append-order, exactly like the previous DB's
        # import-order id -- the read path sorts by timestamp for display.)
        new_events.sort(key=lambda e: (e.get("timestamp") or "", e.get("source") or ""))
        consolidated.parent.mkdir(parents=True, exist_ok=True)
        with open(consolidated, "a", encoding="utf-8") as f:
            for e in new_events:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")

    # The consolidated half is re-read here, after the append, so a merge that DID find new events
    # leaves a usable memo instead of one that is stale the moment it is written. The source half
    # stays as it was read above -- that is the part whose staleness would lose events.
    _memo_put(_consolidated, memo_key, (_stat_key(consolidated), sources_fp))
    return consolidated


def read_events(
    events_file: Path | str,
    since_id: int | None = None,
    event_type: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read events from a consolidated ``events.jsonl``.

    Each event's ``id`` is its 1-based line number (stable because the log is
    append-only). Results are sorted by ``(timestamp, id)`` for display.

    Args:
        events_file: Path to the consolidated log.
        since_id: Only return events with ``id`` greater than this (for polling).
        event_type: Only return events of this type (server-side filter).
        limit: Maximum number of events to return.

    Returns:
        List of event dicts, each with an added ``id`` field.
    """
    events: list[dict[str, Any]] = []
    for idx, e in enumerate(_iter_log_lines(events_file), start=1):
        e = dict(e)
        e["id"] = idx
        events.append(e)

    if since_id is not None:
        events = [e for e in events if e["id"] > since_id]
    if event_type is not None:
        events = [e for e in events if e.get("event_type") == event_type]

    events.sort(key=lambda e: (e.get("timestamp") or "", e["id"]))

    if limit:
        events = events[:limit]
    return events


def trace_summary(events_file: Path | str, trace_id: str) -> dict[str, Any]:
    """Summary for a trace: event count, time span, and counts by source/type."""
    events = list(_iter_log_lines(events_file))
    timestamps = [e.get("timestamp") for e in events if e.get("timestamp")]
    return {
        "trace_id": trace_id,
        "event_count": len(events),
        "start_time": min(timestamps) if timestamps else None,
        "end_time": max(timestamps) if timestamps else None,
        "sources": dict(Counter(e.get("source") for e in events)),
        "event_types": dict(Counter(e.get("event_type") for e in events)),
    }


def run_dir_mtimes(runs_dir: Path | str = "runs") -> list[tuple[str, float]]:
    """``(run_id, mtime)`` for every run directory, newest first, **without parsing anything**.

    The ordering key a caller can afford before it has decided what to show. ``list_traces`` has to
    read each run's events to learn its start time, which is precisely the cost a windowed run list
    exists to avoid, so the window is chosen from mtime instead.

    mtime is the newest of the directory itself and its direct entries. The directory's own mtime
    moves only when an entry is added or removed, so a run whose logs are appended to for an hour
    would otherwise date from its first file -- and a pulled-from-S3 run from the moment the pull
    created its files, which is the honest answer for "recently touched on this host".

    Sub-entries are not recursed into: one ``scandir`` plus a stat per entry is a few milliseconds
    over a few hundred runs, and a run's own files are all one level down.

    Only directories that look like runs are returned -- an event log or an ``eval/`` -- so a
    caller can count these as runs. (Same predicate ``reap_run_dirs`` uses to decide what it may
    delete, for the same reason: neither should mistake a scratch directory for a run.)
    """
    runs_path = Path(runs_dir)
    out: list[tuple[str, float]] = []
    try:
        entries = list(os.scandir(runs_path))
    except OSError:
        return out

    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            newest = entry.stat().st_mtime
            looks_like_run = False
            for child in os.scandir(entry.path):
                name = child.name
                if (name.startswith("events") and name.endswith(".jsonl")) or name == "eval":
                    looks_like_run = True
                try:
                    newest = max(newest, child.stat().st_mtime)
                except OSError:
                    continue  # vanished mid-scan (a reap, another worker); the dir's own mtime stands
        except OSError:
            # The run dir itself went away between scandir and here. Skipping it is right: a caller
            # asking "what is on disk" must not fail over a directory that no longer is.
            continue
        if looks_like_run:
            out.append((entry.name, newest))

    out.sort(key=lambda row: (-row[1], row[0]))
    return out


def cached_trace_summary(events_file: Path | str, trace_id: str) -> dict[str, Any]:
    """:func:`trace_summary`, parsed once per (size, mtime) of the log.

    Keyed on the file's stat rather than a TTL, for the same reason the eval headline cache is: a
    finished run's log is immutable, so a TTL re-parses it forever, while a live run's log changes
    its stat on every append and is re-read. Callers that scan many runs per render (the tracer's
    run list) pay for the runs that actually changed.
    """
    path = Path(events_file)
    stamp = _stat_key(path)
    if stamp is None:
        return trace_summary(path, trace_id)  # gone: let the normal read path produce the empty answer

    key = str(path)
    hit = _summaries.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    value = trace_summary(path, trace_id)
    _memo_put(_summaries, key, (stamp, value))
    return value


def list_traces(
    runs_dir: Path | str = "runs", *, only: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """List available traces in the runs directory (most recent first).

    Consolidates each trace's per-source logs, then reads a lightweight summary
    (event count + time span) from the merged ``events.jsonl``.

    Args:
        runs_dir: Directory holding the run directories.
        only: Restrict the scan to these run ids -- the windowed path. Both the consolidation and
            the summary read whole logs, so a caller rendering one page of a long history should
            pick its ids first (see :func:`run_dir_mtimes`) and pass them here rather than
            summarizing every run on the host to display forty of them. Ids that are not
            directories under ``runs_dir`` are skipped. None means every run directory.
    """
    runs_path = Path(runs_dir)
    traces: list[dict[str, Any]] = []

    if not runs_path.exists():
        return traces

    if only is None:
        candidates: Iterable[Path] = runs_path.iterdir()
    else:
        # Resolved against runs_dir by name only: an id carrying a separator or `..` would reach
        # another directory, and these ids reach us from a query string.
        candidates = [
            runs_path / rid
            for rid in only
            if rid and "/" not in rid and "\\" not in rid and rid not in (".", "..")
        ]

    for trace_dir in candidates:
        if not trace_dir.is_dir():
            continue

        try:
            # Consolidation is inside the per-trace guard, not above it: it reads and rewrites this
            # run's logs, so a failure there is exactly as run-local as a failure to summarise them.
            # Outside, one unreadable run took the whole listing down with it.
            events_file = consolidate_trace(trace_dir)
            if events_file is None or not events_file.exists():
                continue
            summary = cached_trace_summary(events_file, trace_dir.name)
            traces.append({
                "trace_id": trace_dir.name,
                "event_count": summary["event_count"],
                "start_time": summary["start_time"],
                "end_time": summary["end_time"],
            })
        except Exception as e:
            logger.warning(f"Failed to read trace {trace_dir.name}: {e}")
            continue

    traces.sort(key=lambda t: t.get("start_time") or "", reverse=True)
    return traces
