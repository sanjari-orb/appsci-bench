"""FastAPI tracer server for BUILD phase observability.

Provides a real-time dashboard for viewing events from benchmark runs.
Runs as a background thread embedded in the orchestrator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db_reader import connect_db, query_events, get_trace_summary
from .harness_history import MAX_LOG_BYTES as MAX_HARNESS_LOG_BYTES, read_history
from .file_server import (
    FileServerError,
    list_artifact_files,
    read_artifact_file,
)
from shared.observability.s3_store import (
    RemoteTraceNotFoundError,
    S3Config,
    S3Error,
    download_trace,
    mirror_has_newer,
    list_mirrored_run_ids,
    manifest_present,
    pull_incomplete,
    validate_run_id,
)
from shared.observability import eval_report, replicate
from shared.observability.storage import consolidate_trace, list_traces, run_dir_mtimes

logger = logging.getLogger(__name__)


class TracerPortInUseError(RuntimeError):
    """Raised when the tracer/aggregator port is already bound by another process.

    Deliberately distinct from a generic startup failure: the caller (the
    orchestrator) treats this as a hard stop rather than a "continue without
    observability" degradation, since uvicorn's own bind failure happens inside
    a background thread and would otherwise be silently swallowed.
    """

    def __init__(self, port: int):
        super().__init__(
            f"Observability port {port} is already in use by another process. "
            f"Find it with `lsof -nP -iTCP:{port} -sTCP:LISTEN`, stop it, and retry "
            f"(or pass a different --aggregator-port)."
        )
        self.port = port


class TracerPortRangeExhaustedError(TracerPortInUseError):
    """Raised when every port in the orchestrator's retry range was occupied.

    A subclass of TracerPortInUseError (not a sibling) so callers that only
    handle the single-port case (e.g. `except TracerPortInUseError` for the
    CLI's hard-stop exit code) still catch this too.
    """

    def __init__(self, start_port: int, end_port: int):
        RuntimeError.__init__(
            self,
            f"Observability could not bind any port from {start_port} to {end_port} "
            f"(all already in use). Find what's holding them with "
            f"`lsof -nP -iTCP:{start_port}-{end_port} -sTCP:LISTEN`, free one, and "
            f"retry (or pass a different --aggregator-port)."
        )
        self.port = start_port

# Get template and static directories
PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"


def _fmt_score(value: Any) -> str:
    """Three decimals, or an em dash. Never 0.000 for an absent score.

    `None` means "not measured" and `0.0` means "measured, and it failed". Collapsing them is how an
    unscored run comes to look like a total failure in a table someone reads at a glance.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return f"{float(value):.3f}"


def _fmt_secs(value: Any) -> str:
    """A duration a human reads: `1h03m`, `4m04s`, `12s`. An em dash when unknown."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return "—"
    total = int(round(float(value)))
    if total >= 3600:
        return f"{total // 3600}h{(total % 3600) // 60:02d}m"
    if total >= 60:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total}s"


def _score_band(value: Any) -> str:
    """A coarse band for colouring: `good` / `warn` / `bad` / `na`.

    The names and thresholds are the dashboard's `scoreBand()` verbatim, because the two apps share
    this stylesheet -- `.chip.warn` and `.score-warn` exist, `.chip.ok` does not, so inventing a
    parallel vocabulary here would render as an unstyled chip.

    An absent score gets its own band rather than falling into the lowest one: grey for "not measured"
    versus red for "measured badly" is the same distinction `_fmt_score` protects.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "na"
    v = float(value)
    return "good" if v >= 0.8 else ("warn" if v >= 0.5 else "bad")


def _event_span_seconds(start: str | None, end: str | None) -> float | None:
    """Wall seconds between a run's first and last event, or None when it cannot be known.

    None rather than 0.0 for a run that is still going (no end) or whose stamps will not parse: 0.0 is
    a plausible duration and would render as an instantaneous run, which is exactly what the metrics
    layer's clamped-negative-duration bug used to produce.

    `TypeError` is caught alongside `ValueError` because the failure is not only in PARSING. A run
    whose two stamps disagree about tzinfo -- one naive, one offset-aware, which a run dir assembled
    from mixed sources or an older writer can produce -- parses fine and then raises on the
    subtraction. This is called per run while building the list, OUTSIDE any per-run guard, so one such
    run would 500 the whole page rather than costing itself a duration.
    """
    if not start or not end:
        return None
    try:
        t0 = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return max(0.0, (t1 - t0).total_seconds())
    except (ValueError, TypeError):
        return None


#: Days of history the run list shows by default. The page's cost is per RUN SHOWN -- each row
#: costs a merge of that run's per-source logs and a parse of the merged one -- so the window is
#: what keeps a render proportional to what you are looking at instead of to everything the host
#: has ever produced. Two days covers "what I ran today and yesterday"; older runs are reachable by
#: search, by widening the window, or by pasting an id.
RUN_LIST_DEFAULT_DAYS = 2

#: Widths offered in the UI. `0` means no window at all.
RUN_LIST_DAY_CHOICES = (2, 7, 30, 0)

#: Shown even when the window is empty. A box that ran nothing for a week should still open on its
#: last runs rather than on an empty table that reads like data loss.
RUN_LIST_MIN_ROWS = 20

#: Hard ceiling on rows in one render, whatever the window or the search asks for. Without it
#: `?days=0` on a host with a thousand runs is the slow page again, reachable in one click.
RUN_LIST_MAX_ROWS = 200

#: Longest search string accepted. It is only ever used as a substring test, so length buys nothing.
RUN_LIST_QUERY_MAX_CHARS = 200


def with_builds_of(runs_dir: Path, run_ids: list[str]) -> list[str]:
    """``run_ids`` plus the BUILD run of any eval trial among them that is still on disk.

    A build and its trials are rendered as one group, so a window that keeps a trial and drops its
    build breaks the group in a way that also lies about it: the parent row then reads "build run
    dir not on this host", which is what a REAPED build looks like. A build is older than its
    trials by construction, so any window narrow enough to be useful cuts exactly there.

    Only builds whose directory exists are added -- a genuinely reaped one still renders as the
    orphan group the template already handles.
    """
    have = set(run_ids)
    extra: list[str] = []
    for run_id in run_ids:
        record = replicate.read_record(runs_dir / run_id)
        if not record:
            continue
        build = record.get("build_trace_id")
        if not build or build in have:
            continue
        if (runs_dir / build).is_dir():
            have.add(build)
            extra.append(build)
    return run_ids + extra


def parse_days(raw: str | None) -> int:
    """Read the ``?days=`` window, falling back to the default on anything unusable.

    A junk value gets the default rather than an error: this is a view preference in a URL people
    hand-edit, and failing the whole page over it would be a worse answer than showing two days.
    """
    if raw is None:
        return RUN_LIST_DEFAULT_DAYS
    raw = raw.strip().lower()
    if raw in ("all", "0"):
        return 0
    try:
        days = int(raw)
    except ValueError:
        return RUN_LIST_DEFAULT_DAYS
    if days < 0:
        return RUN_LIST_DEFAULT_DAYS
    # A window wider than any plausible history is the same request as "all", and clamping keeps
    # `days * 86400` away from the overflow-shaped end of the range.
    return min(days, 3650)


def select_run_window(
    rows: list[tuple[str, float]],
    *,
    days: int,
    query: str,
    now: float,
) -> dict[str, Any]:
    """Choose which runs the list renders, from ids and mtimes alone.

    Args:
        rows: ``(run_id, mtime)`` newest first, from :func:`run_dir_mtimes`.
        days: Window width; 0 means every run.
        query: Case-insensitive substring of the run id. **Searching ignores the window** -- the
            reason to type an id fragment is that the run is old, so applying the window first
            would make search useless for the only case it exists for.
        now: Current epoch seconds.

    Returns:
        A dict of the selected ids plus what the page has to say about the selection: how many were
        selected out of how many exist, whether the cap or the minimum-rows floor changed the
        answer. The page states this rather than silently showing a subset, because "no runs from
        Tuesday" and "runs from Tuesday, not displayed" look identical otherwise.
    """
    total = len(rows)
    query = (query or "").strip()[:RUN_LIST_QUERY_MAX_CHARS]

    if query:
        needle = query.lower()
        matched = [row for row in rows if needle in row[0].lower()]
        selected = matched[:RUN_LIST_MAX_ROWS]
        return {
            "ids": [run_id for run_id, _ in selected],
            "query": query,
            "days": days,
            "total": total,
            "matched": len(matched),
            "shown": len(selected),
            "truncated": len(matched) > len(selected),
            "floored": False,
            "showing_all": False,
        }

    if days <= 0:
        windowed = list(rows)
        floored = False
    else:
        cutoff = now - days * 86400.0
        windowed = [row for row in rows if row[1] >= cutoff]
        # An empty or nearly-empty window is not informative; fall back to the newest few and say so.
        floored = len(windowed) < RUN_LIST_MIN_ROWS and total > len(windowed)
        if floored:
            windowed = rows[:RUN_LIST_MIN_ROWS]

    selected = windowed[:RUN_LIST_MAX_ROWS]
    return {
        "ids": [run_id for run_id, _ in selected],
        "query": "",
        "days": days,
        "total": total,
        "matched": None,
        "shown": len(selected),
        "truncated": len(windowed) > len(selected),
        "floored": floored,
        "showing_all": days <= 0 and len(selected) == total,
    }


def _asset_version() -> str:
    """Cache-buster for everything served out of the shared static dir, as the newest mtime in it.

    Keyed on the WHOLE directory rather than styles.css alone, which is what it used to be. The
    Alpine bundle is now served from here too and carries this same `?v=`, so a stylesheet-only
    version would have left a JS upgrade cached in every browser indefinitely -- the exact bug this
    parameter exists to prevent, just moved to the file nobody was watching.
    """
    try:
        return str(int(max(p.stat().st_mtime for p in STATIC_DIR.glob("*") if p.is_file())))
    except (OSError, ValueError):
        return "0"

def url_base(request: Request) -> str:
    """The path prefix this app is being served under, for the templates' `base` variable.

    Every URL in the templates is written `{{ base }}/api/...` rather than `/api/...` so this app
    works both on its own port and mounted inside another one (the experiments server mounts it at
    `/tracer` so one port serves both dashboards). Starlette sets ``root_path`` to the mount path
    when mounted and leaves it empty otherwise, so standalone rendering is byte-identical to a
    hardcoded absolute path -- which is the property `test_server.py` pins.

    Templated rather than `url_for` because most of these URLs live inside JavaScript string
    literals (``fetch(`...`)``, ``new EventSource(`...`)``), not HTML attributes.
    """
    return str(request.scope.get("root_path") or "").rstrip("/")


@dataclass
class TracerServer:
    """FastAPI server for the observability tracer.

    Runs in a background thread and provides:
    - Run list page showing all available traces
    - Timeline dashboard for viewing events
    - SSE endpoint for live event streaming
    - Reading a run's persisted artifact / agent-workspace snapshot and eval report
    - Pulling a run out of the S3 mirror by id, so a run produced on another
      machine renders through exactly the same dashboard (see s3_config)

    Example usage:
        server = TracerServer(runs_dir=Path("runs"), port=8080)
        await server.start()
        # ... run completes ...
        await server.stop()
    """

    runs_dir: Path = field(default_factory=lambda: Path("runs"))
    #: Loopback by default. Every route is unauthenticated and ``/api/traces`` enumerates every run on
    #: the host, including each one's eval report -- which carries the withheld elicitation facts, the
    #: answer key for the task. The agent container is given a ``host.docker.internal`` alias (it needs
    #: it for the submit/deploy server), so a wildcard bind would put an earlier run's answer key one
    #: unauthenticated GET away from the agent being scored. Bind wider only deliberately.
    host: str = "127.0.0.1"
    port: int = 8080
    #: Where mirrored runs live. None disables the S3 panel and routes entirely.
    #: A pulled run is written into ``runs_dir`` and then served by the ordinary
    #: local read path -- there is no separate "remote" rendering mode.
    s3_config: S3Config | None = None

    # Internal state
    _server: uvicorn.Server | None = field(default=None)
    _thread: threading.Thread | None = field(default=None)
    _app: FastAPI | None = field(default=None)

    def __post_init__(self) -> None:
        """Initialize mutable fields."""
        self._server = None
        self._thread = None
        self._app = None

    def _create_app(self) -> FastAPI:
        """Create the FastAPI application."""
        # `/docs`, `/redoc` and `/openapi.json` off, matching the experiments app -- and for a
        # sharper reason than "less surface". FastAPI's `/docs` loads Swagger UI from
        # `cdn.jsdelivr.net/npm/swagger-ui-dist@5`: a third-party script on a FLOATING major version,
        # executing same-origin with the dashboard's HttpOnly session cookie now that this app is
        # mounted inside the experiments server. Script on that origin cannot read the token and does
        # not need to -- it can `POST /api/jobs`, cancel a job, or `DELETE`, with the cookie attached
        # and `SameSite=Strict` satisfied. This PR removed the dashboard's CDN dependencies for
        # exactly that reason, so leaving the tracer half loading a CDN was inconsistent as well as
        # unnecessary. One construction covers both modes: the experiments server mounts
        # `_create_app()`, and standalone `start()` serves the same object.
        app = FastAPI(
            title="BUILD Phase Tracer", docs_url=None, redoc_url=None, openapi_url=None
        )

        # Mount static files
        if STATIC_DIR.exists():
            app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        # Set up templates
        templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
        # Formatting helpers as globals rather than repeated Jinja expressions, so the run list and
        # the dashboard cannot disagree about what an absent score looks like -- the distinction
        # between "not measured" and "measured 0.0" is the one that matters most here.
        templates.env.globals.update(
            fmt_score=_fmt_score,
            fmt_secs=_fmt_secs,
            score_band=_score_band,
        )

        def empty_summary(trace_id: str) -> dict:
            """Summary skeleton for a trace that has produced no events yet."""
            return {
                "trace_id": trace_id,
                "event_count": 0,
                "start_time": None,
                "end_time": None,
                "sources": {},
                "event_types": {},
            }

        def prepare_trace_db(trace_id: str) -> Path | None:
            """Merge per-source DBs into events.db and return its path.

            Writers each own an ``events-<source>.db``; this consolidates them
            into the single ``events.db`` the read path queries. Idempotent, so
            it is safe to call on every request / poll.

            Raises 404 only if the trace *directory* is missing. Returns None
            (not 404) when the directory exists but no events have been written
            yet -- callers render an empty shell so a freshly-started run loads
            immediately and populates live over SSE.
            """
            trace_dir = self.runs_dir / trace_id
            if not trace_dir.is_dir():
                raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
            db_path = consolidate_trace(trace_dir)
            if db_path is None or not db_path.exists():
                return None
            return db_path

        # One lock per run id, so two browser tabs (or a page load racing the pull API) don't pull
        # the same run at once.
        #
        # NOT for write integrity: s3transfer downloads to `<name>.<random>` and renames on
        # completion, so concurrent pulls cannot interleave writes *within* a file -- worst case is
        # last-writer-wins on whole files. What the lock actually buys is avoiding duplicate transfer
        # cost and marker churn (two pulls racing to clear PULL_MARKER). An earlier version of this
        # comment claimed interleaving; that was wrong, and it is called out here because the same
        # mistaken premise was cited as partly justifying the marker design.
        #
        # Bounded because entries are only ever created for *validated* ids, and validation caps the
        # shape of an id; an unbounded map keyed on arbitrary request strings would be a slow leak on
        # a 0.0.0.0-bound server.
        pull_locks: dict[str, asyncio.Lock] = {}

        def checked_run_id(run_id: str) -> str:
            """Validate and normalize a run/trace id, as a 400 rather than a 502.

            Normalizing before locking matters: `abc` and `abc ` would otherwise take different
            locks while writing the same directory, defeating the serialization above.

            Applied to EVERY route that takes an id, not just the S3 pull it was written for, for two
            reasons that are independent of S3:

            * `{{ trace_id }}` is interpolated into JS **template literals** in dashboard.html
              (``fetch(`{{ base }}/api/events/{{ trace_id }}`)``). Jinja's HTML autoescape does not
              neutralise a backtick or `${` there, so a run directory named
              ``x`+document.cookie+` `` renders as executable script. Reproduced: an id shaped that
              way came back 200 with the payload raw in eleven template literals. The bar is high --
              a payload has to be a real directory name -- but the fix is one call, and this app is
              now mounted same-origin with the experiments dashboard's session cookie, so script on
              this origin can `fetch` `/api/jobs` and `/finish` with the cookie attached.
            * every id is joined onto `runs_dir`. `?trace_id=../<something>` reached 200 and rendered
              a directory outside the runs dir; `validate_run_id` confines an id to one path segment.
            """
            try:
                return validate_run_id(run_id)
            except S3Error as e:
                # A malformed id is the caller's mistake, not an upstream failure.
                raise HTTPException(status_code=400, detail=str(e)) from e

        def pull_lock(trace_id: str) -> asyncio.Lock:
            return pull_locks.setdefault(trace_id, asyncio.Lock())

        #: run id -> (monotonic timestamp, was_stale). Bounds the LIST traffic the freshness check
        #: adds; see needs_pull. Not an eviction risk worth managing -- one small tuple per run id
        #: this process has actually rendered.
        _freshness_cache: dict[str, tuple[float, bool]] = {}
        _FRESHNESS_TTL_S = 30.0

        def needs_pull(trace_id: str) -> bool:
            """Whether ``trace_id`` has to be fetched before it can be rendered.

            Three reasons, cheapest first. It is not here at all. A previous pull was interrupted and
            left a marker -- without that check an aborted download would render as complete forever.
            Or the mirror has moved on: a run produced on this box is never re-read, so once another
            machine re-scores it or folds a diagnostic into its report, this box keeps serving the old
            numbers with nothing on the page to say so. Local presence answers "can I render this",
            not "is this current", and only the first two questions used to be asked.

            The third check costs one LIST, so it is memoised for `_FRESHNESS_TTL_S`: a dashboard page
            issues several requests for one run, and re-listing on each would put S3 latency on every
            panel. A stale-by-seconds view is the deliberate trade.
            """
            local = self.runs_dir / trace_id
            if not local.is_dir() or pull_incomplete(local):
                return True
            if self.s3_config is None:
                return False
            now = time.monotonic()
            cached = _freshness_cache.get(trace_id)
            if cached is not None and now - cached[0] < _FRESHNESS_TTL_S:
                return cached[1]
            stale = mirror_has_newer(trace_id, self.runs_dir, self.s3_config)
            _freshness_cache[trace_id] = (now, stale)
            return stale

        async def resolve_trace(trace_id: str) -> None:
            """Make ``trace_id`` available locally, pulling it from the mirror if it isn't.

            This is what makes ``/?trace_id=<id>`` a **single source-agnostic entry point**: the URL
            is the same whether the run was produced on this machine or by someone else, so it can
            be edited, bookmarked and shared without the reader needing to know where it came from.
            Once pulled, every downstream route reads it as an ordinary local run.

            Silent no-op when the run is already complete locally or S3 isn't configured; the
            caller's normal 404 then covers a genuinely unknown id.
            """
            if self.s3_config is None or not needs_pull(trace_id):
                return
            try:
                trace_id = validate_run_id(trace_id)
            except S3Error:
                # Unreachable from `index`, which validates first; kept because this is the function
                # that builds an S3 key out of the id, and a future caller must not be able to skip
                # that check by calling here directly.
                return
            async with pull_lock(trace_id):
                # Re-check: another request may have pulled it while we waited for the lock.
                if not needs_pull(trace_id):
                    return
                try:
                    # boto3 is blocking; keep it off the loop that also serves SSE.
                    await asyncio.to_thread(
                        download_trace, trace_id, self.runs_dir, self.s3_config
                    )
                    # Drop the memo: it still says "stale" from before this pull, and the re-check
                    # that guards against a concurrent double-pull consults it. Left in place, the
                    # next caller would pull again (harmlessly, skipping every file) and the lock's
                    # whole purpose would lapse for the length of the TTL.
                    _freshness_cache.pop(trace_id, None)
                except RemoteTraceNotFoundError:
                    return  # not in the mirror either -> caller raises the normal 404
                except S3Error as e:
                    # Credentials/permissions. Surface the remediation rather than a bare 404, since
                    # "this run doesn't exist" and "your AWS session lapsed" need different fixes.
                    raise HTTPException(status_code=502, detail=str(e)) from e

        @app.get("/", response_class=HTMLResponse)
        async def index(
            request: Request,
            trace_id: str | None = None,
            days: str | None = Query(default=None, description="Run-list window in days; 0 = all"),
            q: str | None = Query(default=None, description="Substring of a run id to search for"),
        ):
            """Render the run list or dashboard page.

            ``?trace_id=`` accepts *any* run id -- local or mirrored. An id that isn't on disk yet
            is pulled from S3 first (see resolve_trace), so this one URL loads any run.

            The list is windowed (``?days=``) and searchable (``?q=``) because every part of
            rendering a row costs a read of that run: the window decides how much work the page
            does, not just how much of it you look at.
            """
            if trace_id:
                # Show dashboard for specific trace. Render the shell even if no
                # events exist yet -- the SSE stream fills it in as they arrive.
                trace_id = checked_run_id(trace_id)
                await resolve_trace(trace_id)
                db_path = prepare_trace_db(trace_id)
                if db_path is not None:
                    conn = connect_db(db_path)
                    try:
                        summary = get_trace_summary(conn, trace_id)
                    finally:
                        conn.close()
                else:
                    summary = empty_summary(trace_id)

                return templates.TemplateResponse(
                    request,
                    "dashboard.html",
                    {
                        "trace_id": trace_id,
                        "summary": summary,
                        "asset_ver": _asset_version(),
                        "base": url_base(request),
                    },
                )
            else:
                # Show run list, annotated with whether each run is mirrored. A run that only exists
                # locally is still perfectly usable -- it just isn't shareable yet -- so the page
                # says so and names the command that uploads it, rather than failing or staying silent.
                #
                # Which runs, first, from mtimes alone -- then read only those. Reading every run to
                # display a window of it is what made this page take seconds: `list_traces` merges
                # and parses each run's logs, so its cost tracked the host's whole history.
                def choose_runs() -> dict[str, Any]:
                    picked = select_run_window(
                        run_dir_mtimes(self.runs_dir),
                        days=parse_days(days),
                        query=q or "",
                        now=time.time(),
                    )
                    picked["ids"] = with_builds_of(self.runs_dir, picked["ids"])
                    picked["extra_builds"] = len(picked["ids"]) - picked["shown"]
                    return picked

                window = await asyncio.to_thread(choose_runs)
                traces = await asyncio.to_thread(
                    list_traces, self.runs_dir, only=window["ids"]
                )
                mirrored: set[str] = set()
                complete: set[str] = set()
                s3_error: str | None = None
                if self.s3_config is not None:
                    try:
                        # Two cheap calls in place of one expensive one. The delimited LIST answers
                        # "in the mirror at all" for every run in a single round-trip; the manifest
                        # HEADs -- which is what distinguishes a finished upload from an abandoned
                        # one -- are asked only about the rows being rendered. boto3 is blocking, so
                        # both stay off the event loop.
                        mirrored = await asyncio.to_thread(
                            list_mirrored_run_ids, self.s3_config
                        )
                        shown_and_mirrored = [
                            t["trace_id"] for t in traces if t["trace_id"] in mirrored
                        ]
                        complete = await asyncio.to_thread(
                            manifest_present, shown_and_mirrored, self.s3_config
                        )
                    except S3Error as e:
                        # Can't tell mirrored from local-only. Say that, don't guess.
                        s3_error = str(e)

                for trace in traces:
                    tid = trace["trace_id"]
                    if self.s3_config is None or s3_error:
                        trace["mirrored"] = None
                    elif tid in complete:
                        trace["mirrored"] = True
                    else:
                        trace["mirrored"] = False
                    # Present in the bucket but with no manifest: an upload that did not finish.
                    trace["mirror_partial"] = bool(
                        self.s3_config is not None
                        and not s3_error
                        and tid in mirrored
                        and tid not in complete
                    )
                    # Read once here and carried on the row, so the template does not do IO and the
                    # badge on this page cannot disagree with the badge on the run's own page.
                    trace["replicate"] = replicate.read_record(self.runs_dir / tid)
                    trace["replicate_badge"] = (
                        replicate.short_label(trace["replicate"]) if trace["replicate"] else ""
                    )
                    # Accuracy and latency, the two things you scan a run list for. The score comes
                    # from the run's own persisted eval report (cached on the file's stat, because
                    # this page re-renders every 10s and a finished report never changes); the duration
                    # is the event span already computed for the columns beside it.
                    trace["eval"] = eval_report.cached_headline(self.runs_dir / tid)
                    trace["duration_seconds"] = _event_span_seconds(
                        trace.get("start_time"), trace.get("end_time")
                    )

                # A build and its k trials are one thing that happened, and listing them as 1 + k
                # peers is what made a replicate sweep unreadable here: 15 rows per arm, ordered by
                # start time, with nothing saying which trial belonged to which build.
                entries = replicate.group_runs(
                    [(t, t["replicate"]) for t in traces]
                )
                for entry in entries:
                    if entry["kind"] != "group":
                        continue
                    # The group's headline: what its trials scored, and how long they took together.
                    # A build carries no score, so a group row that showed only the build's own
                    # numbers would show nothing -- which is what made the scores invisible once the
                    # trials were nested underneath it.
                    entry["eval"] = eval_report.aggregate(
                        [t["trace"].get("eval") for t in entry["trials"]]
                    )
                    entry["trials_duration_seconds"] = sum(
                        (t["trace"].get("duration_seconds") or 0.0) for t in entry["trials"]
                    ) or None

                return templates.TemplateResponse(
                    request,
                    "index.html",
                    {
                        "traces": traces,
                        "entries": entries,
                        "s3_enabled": self.s3_config is not None,
                        "s3_uri": self.s3_config.uri() if self.s3_config else None,
                        "s3_error": s3_error,
                        "local_only": [t["trace_id"] for t in traces
                                       if t["mirrored"] is False and not t["mirror_partial"]],
                        "partial_uploads": [t["trace_id"] for t in traces if t["mirror_partial"]],
                        "window": window,
                        "day_choices": RUN_LIST_DAY_CHOICES,
                        "base": url_base(request),
                        # The run list was the one template linking styles.css with no `?v=`, so a
                        # stylesheet change reached this page only after a manual cache clear -- and
                        # this page is where the replicate badges and grouping styles live.
                        "asset_ver": _asset_version(),
                    },
                )

        @app.get("/api/traces")
        async def get_traces():
            """Get list of all available traces.

            Unwindowed, unlike the page: this one is for scripts that want the whole set. It reads
            every run, which is bounded only by how many the host has -- the summary cache makes a
            repeat call cheap, but the first one after a restart is not.
            """
            return list_traces(self.runs_dir)

        @app.get("/api/events/{trace_id}")
        async def get_events(
            trace_id: str,
            since_id: int = Query(default=0, description="Only return events after this ID"),
            limit: int = Query(default=1000, description="Maximum events to return"),
        ):
            """Get events for a trace."""
            trace_id = checked_run_id(trace_id)
            db_path = prepare_trace_db(trace_id)
            if db_path is None:
                return {"events": [], "count": 0}

            conn = connect_db(db_path)
            try:
                events = query_events(conn, trace_id=trace_id, since_id=since_id, limit=limit)
                return {"events": events, "count": len(events)}
            finally:
                conn.close()

        @app.get("/api/events/{trace_id}/stream")
        async def stream_events(
            request: Request,
            trace_id: str,
            since_id: int = Query(default=0, description="Start from this event ID"),
        ):
            """Stream events via Server-Sent Events.

            Takes `request` solely to notice when the client has gone. Without that the generator
            below runs forever: the loop has no exit, so a browser tab that navigated away, slept, or
            was closed mid-write left a stream consolidating the trace twice a second with nobody
            reading it. That is not just wasted work -- a held-open connection counts against the
            browser's ~6-per-origin budget, and since this app is mounted inside the experiments
            dashboard, forgotten trace tabs starved the dashboard until a fresh load took 15-20
            seconds. The client now closes the stream when its tab is hidden; this is the other half,
            for the cases where the client never got to say goodbye.
            """
            # Validate the directory exists up front (404 if not); then poll,
            # re-consolidating each tick to pick up newly written rows. Tolerates
            # a run that has not produced any events yet.
            trace_id = checked_run_id(trace_id)
            trace_dir = self.runs_dir / trace_id
            if not trace_dir.is_dir():
                raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")

            async def event_generator():
                last_id = since_id
                while True:
                    if await request.is_disconnected():
                        logger.debug("SSE client for %s disconnected; ending stream", trace_id)
                        break
                    try:
                        db_path = consolidate_trace(trace_dir)
                        if db_path is not None and db_path.exists():
                            conn = connect_db(db_path)
                            events = query_events(conn, trace_id=trace_id, since_id=last_id)
                            conn.close()

                            for event in events:
                                # Watermark on the HIGHEST id in the batch, never on the last one:
                                # the read path returns events in TIMESTAMP order while an id is a
                                # line number in append order, and the two orders disagree whenever
                                # one writer's log is picked up after another's but holds older
                                # events. That is routine here -- the agent, the user sim and the
                                # evaluator each append to their own file and this loop merges them
                                # on a poll -- and taking the last event's id walked the watermark
                                # BACKWARDS, so the next tick re-sent everything above it. Twice a
                                # second, for as long as the tab was open: the page appended those
                                # events again and rebuilt its whole timeline each time, which is
                                # the "the tracer refreshes every couple of seconds" report.
                                #
                                # This cannot skip an event: ids are positions in an append-only
                                # log, so anything written after this read lands above every id in
                                # it.
                                last_id = max(last_id, event["id"])
                                yield f"data: {json.dumps(event)}\n\n"

                        # Send keepalive
                        yield ": keepalive\n\n"

                    except (OSError, ValueError):
                        # Transient read hiccup (e.g. file mid-append); retry.
                        pass
                    except Exception as e:
                        logger.warning(f"SSE error: {e}")
                        yield f"event: error\ndata: {str(e)}\n\n"
                        break

                    await asyncio.sleep(0.5)  # Poll interval

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        @app.get("/api/summary/{trace_id}")
        async def get_summary(trace_id: str):
            """Get summary for a trace."""
            trace_id = checked_run_id(trace_id)
            db_path = prepare_trace_db(trace_id)
            if db_path is None:
                return empty_summary(trace_id)

            conn = connect_db(db_path)
            try:
                return get_trace_summary(conn, trace_id)
            finally:
                conn.close()

        @app.get("/api/artifact/{trace_id}")
        async def get_artifact_listing(trace_id: str):
            """List files in the persisted artifact snapshot for a trace.

            Reads from runs/<trace_id>/artifact/ on disk -- works after the
            run's containers are gone (no docker required).
            """
            artifact_dir = self.runs_dir / checked_run_id(trace_id) / "artifact"
            return {
                "trace_id": trace_id,
                "exists": artifact_dir.is_dir(),
                "files": list_artifact_files(artifact_dir),
            }

        @app.get("/api/artifact/{trace_id}/file")
        async def get_artifact_file(
            trace_id: str,
            path: str = Query(..., description="File path relative to the artifact root"),
        ):
            """Return the content of one file from a persisted artifact snapshot."""
            artifact_dir = self.runs_dir / checked_run_id(trace_id) / "artifact"
            try:
                content, content_type = read_artifact_file(artifact_dir, path)
                return {"content": content, "content_type": content_type, "path": path}
            except FileServerError as e:
                raise HTTPException(status_code=404, detail=str(e))

        @app.get("/api/agent_fs/{trace_id}")
        async def get_agent_fs_listing(trace_id: str):
            """List files in the persisted agent-workspace snapshot for a trace.

            Reads runs/<trace_id>/agent_fs/ on disk (the agent container's /workspace + /work,
            snapshotted at cleanup) -- works after the container is gone, like the artifact listing.
            """
            fs_dir = self.runs_dir / checked_run_id(trace_id) / "agent_fs"
            return {
                "trace_id": trace_id,
                "exists": fs_dir.is_dir(),
                "files": list_artifact_files(fs_dir),
            }

        @app.get("/api/agent_fs/{trace_id}/file")
        async def get_agent_fs_file(
            trace_id: str,
            path: str = Query(..., description="File path relative to the agent_fs root"),
        ):
            """Return the content of one file from the persisted agent-workspace snapshot."""
            fs_dir = self.runs_dir / checked_run_id(trace_id) / "agent_fs"
            try:
                content, content_type = read_artifact_file(fs_dir, path)
                return {"content": content, "content_type": content_type, "path": path}
            except FileServerError as e:
                raise HTTPException(status_code=404, detail=str(e))

        @app.get("/api/replicate/{trace_id}")
        async def get_replicate(trace_id: str):
            """This run's replicate identity, if it has one (runs/<trace_id>/replicate.json).

            Lets a trace say which half of a `builds` x `trials` pair it is, and -- for a trial --
            name the build trace whose artifact it scored. Absent for every run that is not part of a
            replicate experiment, which is most of them, so `replicate` is null rather than an error.

            `sibling_available` is answered here rather than in the browser because only this side can
            see the runs dir: a link to a build whose run dir has since been reaped would otherwise
            render as a live link that 404s. It reports local presence only -- a reaped run may still
            be in S3 and is materialised on demand by `?trace_id=`, so a false here means "not on this
            box", not "gone".
            """
            run_dir = self.runs_dir / checked_run_id(trace_id)
            record = replicate.read_record(run_dir)
            sibling = (record or {}).get("build_trace_id")
            sibling_local = False
            if sibling:
                try:
                    sibling_local = (self.runs_dir / checked_run_id(sibling)).is_dir()
                except Exception:  # noqa: BLE001 -- a bad id in the file costs the link, not the page
                    sibling_local = False
            return {
                "trace_id": trace_id,
                "replicate": record,
                "badge": replicate.short_label(record) if record else "",
                "sibling_available": sibling_local,
            }

        @app.get("/api/eval/{trace_id}")
        async def get_eval_report_status(trace_id: str):
            """Whether a persisted eval report exists for this trace (runs/<trace_id>/eval/)."""
            eval_dir = self.runs_dir / checked_run_id(trace_id) / "eval"
            return {
                "trace_id": trace_id,
                "has_report": (eval_dir / "report.html").is_file(),
                "has_json": (eval_dir / "report.json").is_file(),
            }

        @app.get("/eval/{trace_id}/report", response_class=HTMLResponse)
        async def get_eval_report(trace_id: str):
            """Serve the persisted self-contained eval report.html for this trace.

            Reads runs/<trace_id>/eval/report.html. This is the only route that returns **stored
            HTML raw**, which is fine on its own -- `eval/report_html.py` puts every dynamic value
            (model output, judge reasoning) through one `html.escape` chokepoint, and the report's
            inline JS is a static blob -- but it is served same-origin with the experiments
            dashboard now that the tracer is mounted inside it, so a future field that forgets that
            chokepoint would run with the dashboard's session cookie attached.

            `sandbox allow-scripts` costs nothing here and closes that: the document loads into an
            opaque origin, so its script still runs (the tab switcher and the case filter keep
            working) but cannot read `document.cookie` or make credentialed requests to `/api/jobs`.
            The report is self-contained -- no external fetches, no localStorage, which would throw
            in an opaque origin -- so nothing in it depends on having a real origin.
            """
            report_path = self.runs_dir / checked_run_id(trace_id) / "eval" / "report.html"
            if not report_path.is_file():
                raise HTTPException(status_code=404, detail="No eval report for this trace")
            return HTMLResponse(
                report_path.read_text(encoding="utf-8", errors="replace"),
                headers={"Content-Security-Policy": "sandbox allow-scripts"},
            )

        @app.get("/api/eval/{trace_id}/report_json")
        async def get_eval_report_json(trace_id: str):
            """Parsed eval report.json (metrics + aggregate views + per-case objectives/judge
            metadata) for the native Eval tab."""
            p = self.runs_dir / checked_run_id(trace_id) / "eval" / "report.json"
            if not p.is_file():
                raise HTTPException(status_code=404, detail="No eval report.json for this trace")
            return json.loads(p.read_text(encoding="utf-8"))

        @app.get("/api/eval/{trace_id}/conversations")
        async def get_eval_conversations(trace_id: str):
            """Per-case transcript docs (turns + response.trace + final_state) for the native Eval
            tab. report.json has no transcripts, so these come from the persisted conversations/ dir."""
            conv_dir = self.runs_dir / checked_run_id(trace_id) / "eval" / "conversations"
            docs = []
            if conv_dir.is_dir():
                for f in sorted(conv_dir.glob("*.json")):
                    try:
                        docs.append(json.loads(f.read_text(encoding="utf-8")))
                    except Exception:  # noqa: BLE001 -- skip a malformed transcript, don't 500
                        continue
            return {"conversations": docs}

        @app.get("/api/model_io/{trace_id}")
        async def get_model_io(trace_id: str, ref: str = Query(..., description="ModelCall call_id")):
            """Full prompt+completion for one model call (by its call_id `ref`), loaded from the
            per-source model_calls store. Backs the Timeline's expand-a-model-call view.

            Streams the store for the one record rather than loading all of them: these files reach
            gigabytes on a build whose sub-agent ran uncached, and the load-everything path could not
            open one (see `load_model_call`). Runs off the event loop because the scan is blocking I/O
            and this server also serves SSE."""
            from shared.observability.model_io import load_model_call
            call = await asyncio.to_thread(
                load_model_call, self.runs_dir / checked_run_id(trace_id), ref
            )
            if call is None:
                raise HTTPException(status_code=404, detail="Unknown model-call ref")
            return call

        @app.get("/api/harness/{trace_id}")
        async def get_harness_history(
            trace_id: str,
            offset: int = Query(default=0, ge=0, le=MAX_HARNESS_LOG_BYTES),
            limit: int = Query(default=100, ge=1, le=200),
        ):
            """Read persisted native messages; no Docker access or model calls from the tracer."""
            run = self.runs_dir / checked_run_id(trace_id)
            try:
                return await asyncio.to_thread(read_history, run, offset=offset, limit=limit)
            except OSError as exc:
                logger.warning("Could not read native harness history for %s: %s", trace_id, exc)
                raise HTTPException(status_code=503, detail="Native harness history is temporarily unreadable") from exc

        # -------------------------------------------------------------------
        # S3 mirror. There is no separate "view a remote run" page: `/?trace_id=`
        # pulls an unknown id from the mirror itself (see resolve_trace), so one
        # URL loads any run regardless of where it came from. This route exists
        # for explicit/scripted pulls and to back the search box's inline errors.
        # -------------------------------------------------------------------

        def require_s3() -> S3Config:
            """The configured mirror, or a 400 explaining how to configure one."""
            if self.s3_config is None:
                raise HTTPException(
                    status_code=400,
                    detail="S3 mirroring is not configured for this tracer. Restart it with "
                           "--s3-bucket (or set APPSCI_S3_BUCKET).",
                )
            return self.s3_config

        @app.post("/api/s3/pull/{run_id}")
        async def pull_s3_trace(
            run_id: str,
            force: bool = Query(default=False, description="Re-download files that already match"),
        ):
            """Materialize one mirrored run into the local runs dir."""
            cfg = require_s3()
            run_id = checked_run_id(run_id)
            try:
                async with pull_lock(run_id):
                    # boto3 is blocking; keep it off the loop that also serves SSE.
                    result = await asyncio.to_thread(
                        download_trace, run_id, self.runs_dir, cfg, force=force
                    )
            except RemoteTraceNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e))
            except S3Error as e:
                raise HTTPException(status_code=502, detail=str(e))
            return {
                "trace_id": result.trace_id,
                "downloaded": result.downloaded,
                "skipped": result.skipped,
                "bytes_downloaded": result.bytes_downloaded,
                "dest": str(result.dest),
            }

        @app.get("/health")
        async def health():
            """Health check endpoint."""
            return {"status": "ok"}

        return app

    async def start(self) -> str:
        """Start the tracer server in a background thread.

        Returns:
            The URL where the server is accessible.

        Raises:
            TracerPortInUseError: If the port is already bound by another process.
                Checked with an explicit preflight bind here because uvicorn's own
                bind happens inside the background thread below, where a failure
                would otherwise be silently swallowed instead of raised.
        """
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # SO_REUSEADDR so the probe is no stricter than the bind it predicts: uvicorn sets it on
            # its own listener, so without it this fails on lingering TIME-WAIT sockets and reports
            # a port "in use" that nothing is listening on. That cost three deploys on the
            # experiments server, which has the identical probe -- fixed there too.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((self.host, self.port))
        except OSError as e:
            raise TracerPortInUseError(self.port) from e
        finally:
            probe.close()

        self._app = self._create_app()

        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)

        # Run server in a background thread
        def run_server() -> None:
            asyncio.run(self._server.serve())  # type: ignore[union-attr]

        self._thread = threading.Thread(target=run_server, daemon=True)
        self._thread.start()

        # Wait for server to be ready
        await asyncio.sleep(0.5)

        url = f"http://localhost:{self.port}"
        logger.info(f"Tracer server started at {url}")
        return url

    async def stop(self) -> None:
        """Stop the tracer server."""
        if self._server is not None:
            self._server.should_exit = True
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=5)
            logger.info("Tracer server stopped")

