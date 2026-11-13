"""S3 mirror for observability run data -- the tracer's shared backend.

A run's trace lives on disk as ``runs/<trace_id>/`` (per-source ``events-*.jsonl``, the merged
``events.jsonl``, the ``model_calls-*.jsonl`` LLM I/O store, the ``artifact/`` snapshot and the
``eval/`` report). That makes a run viewable only on the machine that produced it. This module
mirrors the whole directory to ``s3://<bucket>/<prefix>/<trace_id>/`` and pulls it back down again.

**The read path is deliberately untouched.** Every tracer read is ``Path``-based against
``runs_dir/<trace_id>/``, so "render a run from S3" is implemented as *materialize it into the runs
dir* (:func:`download_trace`) and let the existing dashboard serve it. A pulled run is
indistinguishable from a locally-produced one -- no storage abstraction, no second code path.

Credentials
-----------
The target account hands out short-lived STS session credentials, so "paste three env vars" is not
a durable answer. Three layers, in preference order:

1. **An Identity Center (SSO) profile** -- ``./scripts/setup_s3_sso.sh``. botocore mints *and
   re-mints* role credentials from the cached SSO token on its own, so they survive the ~1h STS
   rotation for the whole SSO session with no keys in the environment.
2. **Env vars** (``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN``) -- the
   copy-paste path; works with zero setup but must be re-exported on expiry.
3. **Rebuild-and-retry on expiry** (:func:`_with_retry`) -- on an expired-credential error we
   discard the cached boto3 session, rebuild it and retry once. A fresh session re-reads every
   *file-based* source, so a long-lived tracer picks up a new ``aws sso login`` (or rewritten
   ``~/.aws/credentials``) **without a restart**. Note this cannot rescue the env-var path:
   ``os.environ`` is a per-process snapshot, so keys re-exported in another shell never reach an
   already-running process -- that case needs a restart. We never cache credentials ourselves;
   botocore's provider chain owns refresh.

This module deliberately does **not** call ``load_dotenv()``: ``scripts/get_aws_creds.sh`` writes
credentials for a *different* AWS account into ``.env``, and auto-loading would silently shadow
this bucket's.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from .storage import consolidate_trace, trace_summary

logger = logging.getLogger(__name__)

DEFAULT_BUCKET = ""  # set APPSCI_S3_BUCKET to mirror runs to your own bucket
DEFAULT_PREFIX = "runs"
DEFAULT_REGION = "us-east-2"  # the <your-bucket> bucket's region

#: Written last by :func:`upload_trace`, so its presence marks "this run finished uploading".
#: Also carries event counts / time span / task id as bucket-side metadata for anyone inspecting
#: the mirror directly. Runs uploaded before this existed simply have no manifest.
MANIFEST_NAME = "run_manifest.json"

#: Suggested profile name; matches what ``scripts/setup_s3_sso.sh`` writes.
SSO_PROFILE = "appsci-bench"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Marker written into a run dir while :func:`download_trace` is working, removed on success.
#: Its presence means "a pull started here and did not finish", which is the only way to tell a
#: partially-pulled run from a complete one -- without it, an interrupted pull would leave a
#: silently incomplete run that the tracer renders as if it were whole, forever.
PULL_MARKER = ".pull_incomplete"

#: Files never worth mirroring (our own marker included -- it is local bookkeeping).
_SKIP_NAMES = {".DS_Store", PULL_MARKER}

T = TypeVar("T")


class S3Error(RuntimeError):
    """Base class for S3 mirror failures."""


class S3NotConfiguredError(S3Error):
    """Raised when an S3 operation is attempted with no bucket configured."""


class S3CredentialsError(S3Error):
    """Raised when AWS credentials are missing or expired.

    The message is the remediation the user should run -- this is the error most people will
    actually hit, so it carries instructions rather than a botocore traceback.
    """


class RemoteTraceNotFoundError(S3Error):
    """Raised when no objects exist under a run's prefix in the bucket."""


def _creds_help(cfg: S3Config) -> str:
    """Actionable remediation text for a missing/expired-credential failure."""
    return (
        f"No usable AWS credentials for s3://{cfg.bucket}/{cfg.prefix}/ "
        f"(region {cfg.region}). Either:\n"
        f"  1) RECOMMENDED -- set up the auto-refreshing SSO profile once:\n"
        f"       ./scripts/setup_s3_sso.sh\n"
        f"     then re-run. boto3 re-mints credentials from the cached SSO token, so they\n"
        f"     survive the ~1h STS rotation without restarting anything.\n"
        f"  2) Or export short-lived keys (must be re-exported when they expire):\n"
        f"       export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_SESSION_TOKEN=...\n"
        f"  If you already did (1), your SSO session may have lapsed: "
        f"aws sso login --profile {cfg.profile or SSO_PROFILE}"
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class S3Config:
    """Where run data is mirrored, and which AWS identity to use.

    ``profile`` is the AWS *config* profile name (the SSO profile from
    ``scripts/setup_s3_sso.sh``); None means botocore's default provider chain (env vars, default
    profile, instance role, ...).
    """

    bucket: str = DEFAULT_BUCKET
    prefix: str = DEFAULT_PREFIX
    region: str = DEFAULT_REGION
    profile: str | None = None

    @classmethod
    def from_env(
        cls,
        bucket: str | None = None,
        prefix: str | None = None,
        profile: str | None = None,
        region: str | None = None,
    ) -> S3Config:
        """Build a config from explicit args, then env vars, then defaults.

        ``APPSCI_S3_REGION`` takes precedence over the generic ``AWS_REGION`` /
        ``AWS_DEFAULT_REGION`` so a shell pointed at another region for unrelated reasons doesn't
        break this bucket (which lives in ``us-east-2``).
        """
        return cls(
            bucket=bucket or os.getenv("APPSCI_S3_BUCKET") or DEFAULT_BUCKET,
            prefix=(prefix or os.getenv("APPSCI_S3_PREFIX") or DEFAULT_PREFIX).strip("/"),
            region=(
                region
                or os.getenv("APPSCI_S3_REGION")
                or os.getenv("AWS_REGION")
                or os.getenv("AWS_DEFAULT_REGION")
                or DEFAULT_REGION
            ),
            profile=profile or os.getenv("APPSCI_S3_PROFILE") or None,
        )

    def run_prefix(self, run_id: str) -> str:
        """Key prefix holding one run's objects (no trailing slash)."""
        return f"{self.prefix}/{run_id}" if self.prefix else run_id

    def uri(self, run_id: str | None = None) -> str:
        """``s3://`` URI for the mirror root, or for one run."""
        base = f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"
        return f"{base}/{run_id}/" if run_id else f"{base}/"


@dataclass
class UploadResult:
    """Outcome of :func:`upload_trace`."""

    trace_id: str
    uri: str
    uploaded: int = 0
    skipped: int = 0
    bytes_uploaded: int = 0

    @property
    def files(self) -> int:
        return self.uploaded + self.skipped


@dataclass
class DownloadResult:
    """Outcome of :func:`download_trace`."""

    trace_id: str
    dest: Path
    downloaded: int = 0
    skipped: int = 0
    bytes_downloaded: int = 0

    @property
    def files(self) -> int:
        return self.downloaded + self.skipped


# ---------------------------------------------------------------------------
# boto3 plumbing: lazy import, cached session, refresh-on-expiry
# ---------------------------------------------------------------------------

_client_lock = threading.Lock()
_clients: dict[tuple[str | None, str], Any] = {}

#: Error codes that mean "these credentials are stale" rather than "this request was wrong".
_EXPIRED_CODES = frozenset({
    "ExpiredToken",
    "ExpiredTokenException",
    "RequestExpired",
    "InvalidClientTokenId",
    "TokenRefreshRequired",
    "UnrecognizedClientException",
})


def _import_boto3() -> Any:
    """Import boto3 lazily, converting a missing install into an actionable error.

    Kept out of module scope so importing this module (which the tracer and orchestrator do
    unconditionally) never fails on an environment that hasn't installed boto3 yet.
    """
    try:
        import boto3  # noqa: PLC0415 -- deliberate lazy import
    except ImportError as e:  # pragma: no cover -- depends on the environment
        raise S3Error(
            "boto3 is required for S3 run mirroring. Install it with:\n"
            "  pip install boto3\n"
            "(or `pip install -r requirements.txt`)"
        ) from e
    return boto3


def _client(cfg: S3Config, *, refresh: bool = False) -> Any:
    """Return a cached S3 client for ``cfg``, optionally rebuilding it first.

    Rebuilding is how credential rotation is handled: a fresh ``boto3.Session`` re-reads the
    environment and the SSO token cache, so a long-lived process picks up new credentials without
    restarting. botocore clients are thread-safe, so one per (profile, region) is shared.
    """
    key = (cfg.profile, cfg.region)
    with _client_lock:
        if refresh:
            _clients.pop(key, None)
        client = _clients.get(key)
        if client is None:
            boto3 = _import_boto3()
            session = boto3.Session(profile_name=cfg.profile, region_name=cfg.region)
            client = session.client("s3")
            _clients[key] = client
        return client


def _botocore_exceptions() -> Any | None:
    """``botocore.exceptions``, or None when botocore isn't installed.

    The classifier and translator below run *inside* an ``except`` handler, so importing botocore
    there unguarded would raise ``ModuleNotFoundError`` from the handler and discard the original
    error -- including the actionable "boto3 is required" message :func:`_import_boto3` raises. That
    turned a missing dependency into a 500 on the run list instead of the documented install hint.
    """
    try:
        from botocore import exceptions as botocore_exc  # noqa: PLC0415 -- lazy, see _import_boto3
    except ImportError:
        return None
    return botocore_exc


def _is_expired_credentials(exc: BaseException) -> bool:
    """Whether ``exc`` means "credentials are stale" (so a rebuild+retry could succeed)."""
    botocore_exc = _botocore_exceptions()
    if botocore_exc is None:
        return False

    # SSO token cache empty / expired -- `aws sso login` needed, but a rebuild is still worth one
    # try in case another process refreshed the token since our session was built.
    sso_errors = tuple(
        e
        for name in ("TokenRetrievalError", "UnauthorizedSSOTokenError", "SSOTokenLoadError")
        if (e := getattr(botocore_exc, name, None)) is not None
    )
    if sso_errors and isinstance(exc, sso_errors):
        return True

    if isinstance(exc, botocore_exc.ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in _EXPIRED_CODES
    return False


def _translate(exc: BaseException, cfg: S3Config) -> BaseException:
    """Map a botocore error onto our own exception types, preserving unknown ones as-is."""
    botocore_exc = _botocore_exceptions()
    if botocore_exc is None:
        return exc      # botocore absent: keep _import_boto3's actionable S3Error intact

    if isinstance(exc, botocore_exc.NoCredentialsError) or _is_expired_credentials(exc):
        return S3CredentialsError(_creds_help(cfg))

    if isinstance(exc, botocore_exc.ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchBucket", "404", "NotFound"):
            return S3Error(f"Bucket or key not found: {cfg.uri()} ({code})")
        if code in ("AccessDenied", "403"):
            return S3Error(
                f"Access denied for {cfg.uri()}. Your credentials are valid but lack S3 "
                f"permission on this bucket -- check you assumed the expected role."
            )
        if code in ("PermanentRedirect", "AuthorizationHeaderMalformed",
                    "IllegalLocationConstraintException"):
            return S3Error(
                f"Wrong region for bucket {cfg.bucket}: tried {cfg.region}. "
                f"Set APPSCI_S3_REGION to the bucket's region (expected {DEFAULT_REGION})."
            )
    return exc


def _with_retry(cfg: S3Config, op: Callable[[Any], T]) -> T:
    """Run ``op(client)``, rebuilding credentials and retrying **once** if they had expired.

    This is the whole rotation story at the call layer: every S3 operation goes through here, so a
    token that lapses mid-run (or mid-browsing-session) self-heals instead of surfacing as a hard
    failure. Non-credential errors are translated and raised immediately -- no blind retries.
    """
    if not cfg.bucket:
        raise S3NotConfiguredError("No S3 bucket configured (set APPSCI_S3_BUCKET).")

    try:
        return op(_client(cfg))
    except Exception as first:
        if not _is_expired_credentials(first):
            raise _translate(first, cfg) from first
        logger.info("AWS credentials expired; rebuilding session and retrying once")
        try:
            return op(_client(cfg, refresh=True))
        except Exception as second:
            raise _translate(second, cfg) from second


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def validate_run_id(run_id: str) -> str:
    """Reject a run id that could escape its prefix or its destination directory.

    Run ids reach us from user input (the tracer's search box, a CLI argument) and from S3 keys, so
    both the key we build and the directory we write must be constrained to a single path segment.

    Returns:
        The stripped run id.

    Raises:
        S3Error: If the id is empty, contains a separator, or is a relative-path component.
    """
    run_id = (run_id or "").strip()
    if not run_id or not _RUN_ID_RE.match(run_id) or "/" in run_id or run_id in (".", ".."):
        raise S3Error(f"Invalid run id: {run_id!r}")
    return run_id


def _safe_join(base: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``base``, refusing to escape it.

    Same posture as ``tracer/file_server._resolve_within``, including its ``lstrip("/")``, which is
    load-bearing: ``Path("/base") / "/x"`` discards the base entirely, so an absolute-looking key
    would otherwise raise here instead of being normalized. That matters because the pull marker is
    left behind on any exception, so one un-joinable key would make a run permanently un-pullable.
    """
    base = base.resolve()
    target = (base / rel.lstrip("/")).resolve()
    if target != base and base not in target.parents:
        raise S3Error(f"Object key escapes destination directory: {rel}")
    return target


def _list_run_objects(cfg: S3Config, run_id: str) -> dict[str, dict[str, Any]]:
    """Map ``relative path -> {size, last_modified}`` for one run's objects.

    One paginated LIST (rather than a HEAD per file) is what makes upload/download skip-if-unchanged
    cheap enough to run on every call.
    """
    prefix = cfg.run_prefix(run_id) + "/"

    def op(client: Any) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=cfg.bucket, Prefix=prefix
        ):
            for obj in page.get("Contents", []) or []:
                rel = obj["Key"][len(prefix):]
                if not rel or rel.endswith("/"):
                    continue  # directory placeholder object
                found[rel] = {"size": obj.get("Size", 0), "last_modified": obj.get("LastModified")}
        return found

    return _with_retry(cfg, op)


def _sniff_task_id(events_file: Path) -> str | None:
    """Read ``task_id`` off the first event, for the manifest.

    Every event carries a denormalized ``task_id``, so the first parseable line is enough; we avoid
    reading the whole (potentially hundreds of KB) log.
    """
    try:
        with open(events_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line).get("task_id")
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return None


def _local_files(trace_dir: Path) -> list[tuple[str, Path, int]]:
    """Every mirrorable file under ``trace_dir`` as ``(posix relpath, path, size)``, sorted."""
    out: list[tuple[str, Path, int]] = []
    for path in trace_dir.rglob("*"):
        if not path.is_file() or path.name in _SKIP_NAMES:
            continue
        try:
            size = path.stat().st_size
        except OSError as e:
            # Omitting the file silently would leave a quietly-incomplete mirror with no evidence
            # anywhere; this is the one place that would explain a short upload.
            logger.warning(f"Skipping unreadable file in {trace_dir.name}: {path.name} ({e})")
            continue
        out.append((path.relative_to(trace_dir).as_posix(), path, size))
    out.sort(key=lambda t: t[0])
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def upload_subtree(
    trace_dir: Path | str,
    subpath: str,
    cfg: S3Config | None = None,
) -> UploadResult:
    """Mirror ONE subtree of a run directory, without touching the rest of the run.

    :func:`upload_trace` is the whole-run mirror that closes a finished run: it consolidates the
    per-source event logs and writes the completion manifest last. Neither is wanted mid-run, when a
    single new subdirectory (an artifact checkpoint) should become reachable without re-declaring the
    run finished -- writing the manifest early would claim a still-running run had finished uploading.

    Keys land under the same ``<prefix>/<trace_id>/`` namespace :func:`upload_trace` uses, so the
    end-of-run mirror sees these objects as already current and skips them.

    Args:
        trace_dir: Local ``runs/<trace_id>`` directory. Its name is the trace id.
        subpath: Path of the subtree, relative to ``trace_dir`` (e.g. ``checkpoints/foo-001``).
        cfg: Target config; defaults to :meth:`S3Config.from_env`.

    Returns:
        An :class:`UploadResult` counting only this subtree's files.

    Raises:
        S3Error: On a missing directory, or any unrecoverable S3/credential failure. Callers that
            must not fail over a mirror are responsible for catching it.
    """
    cfg = cfg or S3Config.from_env()
    trace_dir = Path(trace_dir)
    if not trace_dir.is_dir():
        raise S3Error(f"Not a run directory: {trace_dir}")
    trace_id = validate_run_id(trace_dir.name)

    root = _safe_join(trace_dir, subpath)
    if not root.is_dir():
        raise S3Error(f"Not a directory in run {trace_id}: {subpath}")

    files = [
        (f"{Path(subpath).as_posix()}/{rel}", path, size)
        for rel, path, size in _local_files(root)
    ]
    existing = _list_run_objects(cfg, trace_id)
    result = UploadResult(trace_id=trace_id, uri=cfg.uri(trace_id))
    prefix = cfg.run_prefix(trace_id) + "/"

    for rel, path, size in files:
        already = existing.get(rel)
        if already is not None and already["size"] == size:
            result.skipped += 1
            continue
        # Bind loop vars as defaults: the lambda may run after a credential refresh.
        _with_retry(cfg, lambda client, p=path, k=prefix + rel: client.upload_file(
            str(p), cfg.bucket, k
        ))
        result.uploaded += 1
        result.bytes_uploaded += size

    logger.info(
        f"Mirrored {subpath} of run {trace_id} to {result.uri} "
        f"({result.uploaded} file(s) uploaded, {result.skipped} already current)"
    )
    return result


def upload_trace(
    trace_dir: Path | str,
    cfg: S3Config | None = None,
    *,
    task_id: str | None = None,
    force: bool = False,
) -> UploadResult:
    """Mirror one run directory to ``s3://<bucket>/<prefix>/<trace_id>/``.

    Consolidates the per-source logs first (via
    :func:`shared.observability.storage.consolidate_trace`) so the merged ``events.jsonl`` the
    dashboard reads is part of the upload. Objects whose size already matches are skipped, making a
    re-upload cheap and the whole operation idempotent. The manifest is written **last**, so its
    presence means "this run finished uploading".

    Args:
        trace_dir: Local ``runs/<trace_id>`` directory. Its name is the trace id.
        cfg: Target config; defaults to :meth:`S3Config.from_env`.
        task_id: Recorded in the manifest; sniffed from the events log when omitted.
        force: Re-upload every file even if a same-size object already exists.

    Returns:
        An :class:`UploadResult` with per-file counts.

    Raises:
        S3Error: On a missing trace dir, or any unrecoverable S3/credential failure.
    """
    cfg = cfg or S3Config.from_env()
    trace_dir = Path(trace_dir)
    if not trace_dir.is_dir():
        raise S3Error(f"Not a run directory: {trace_dir}")
    trace_id = validate_run_id(trace_dir.name)

    # A half-pulled run is not a run. The marker is in _SKIP_NAMES, so uploading one would strip the
    # evidence and mirror a truncated file set -- regenerating run_manifest.json from it, which is
    # how the bare `upload_runs_to_s3.py` the run list recommends could quietly publish a partial
    # copy. Refuse unless the caller insists.
    if not force and pull_incomplete(trace_dir):
        raise S3Error(
            f"Run {trace_id} looks half-pulled ({PULL_MARKER} present or the directory is empty). "
            f"Open it in the tracer to finish the pull, or pass force=True / --force to upload it "
            f"as-is."
        )

    # Merge the per-source logs so events.jsonl (what the dashboard reads) ships too.
    events_file = consolidate_trace(trace_dir)

    files = _local_files(trace_dir)
    existing = {} if force else _list_run_objects(cfg, trace_id)
    result = UploadResult(trace_id=trace_id, uri=cfg.uri(trace_id))
    prefix = cfg.run_prefix(trace_id) + "/"

    for rel, path, size in files:
        if rel == MANIFEST_NAME:
            continue  # regenerated below; never mirror a stale copy pulled from S3
        already = existing.get(rel)
        if already is not None and already["size"] == size:
            result.skipped += 1
            continue
        # Bind loop vars as defaults: the lambda may run after a credential refresh.
        _with_retry(cfg, lambda client, p=path, k=prefix + rel: client.upload_file(
            str(p), cfg.bucket, k
        ))
        result.uploaded += 1
        result.bytes_uploaded += size

    # Manifest last: its presence is the signal that a run finished uploading.
    manifest = _build_manifest(trace_dir, trace_id, events_file, files, task_id=task_id)
    _with_retry(cfg, lambda client: client.put_object(
        Bucket=cfg.bucket,
        Key=prefix + MANIFEST_NAME,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    ))

    logger.info(
        f"Mirrored run {trace_id} to {result.uri} "
        f"({result.uploaded} file(s) uploaded, {result.skipped} already current)"
    )
    return result


def _build_manifest(
    trace_dir: Path,
    trace_id: str,
    events_file: Path | None,
    files: list[tuple[str, Path, int]],
    *,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Summary object written alongside a mirrored run.

    Its presence marks a completed upload; the counts and time span make a run identifiable from
    the bucket alone, without downloading and parsing its ``events.jsonl``.
    """
    summary: dict[str, Any] = {}
    if events_file is not None and Path(events_file).exists():
        try:
            summary = trace_summary(events_file, trace_id)
        except Exception as e:  # noqa: BLE001 -- a summary failure must not block the upload
            logger.debug(f"Could not summarize {trace_id}: {e}")
        if task_id is None:
            task_id = _sniff_task_id(Path(events_file))

    return {
        "trace_id": trace_id,
        "task_id": task_id,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "event_count": summary.get("event_count"),
        "start_time": summary.get("start_time"),
        "end_time": summary.get("end_time"),
        "sources": summary.get("sources") or {},
        "files": [{"path": rel, "size": size} for rel, _path, size in files
                  if rel != MANIFEST_NAME],
    }


def list_mirrored_run_ids(cfg: S3Config | None = None) -> set[str]:
    """Ids of the runs present in the mirror.

    Backs the run list's "local only" badge, so it has to be cheap enough to call on every page
    load: **one delimited LIST** returns the run ids as ``CommonPrefixes`` -- no per-run HEAD or GET,
    and no manifest fetching.

    Args:
        cfg: Mirror config; defaults to :meth:`S3Config.from_env`.

    Returns:
        The set of run ids that have at least one object in the mirror.

    Raises:
        S3Error: On any unrecoverable S3/credential failure -- callers that merely want to decorate
            a UI should catch this and degrade to "unknown" rather than failing the page.
    """
    cfg = cfg or S3Config.from_env()
    prefix = f"{cfg.prefix}/" if cfg.prefix else ""

    def op(client: Any) -> set[str]:
        ids: set[str] = set()
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=cfg.bucket, Prefix=prefix, Delimiter="/"
        ):
            for common in page.get("CommonPrefixes", []) or []:
                run_id = common["Prefix"][len(prefix):].rstrip("/")
                if run_id:
                    ids.add(run_id)
        return ids

    return _with_retry(cfg, op)


#: Most ids :func:`manifest_present` will HEAD in one call. It is a page of a UI, not the mirror:
#: the whole point of the function is that the caller has already narrowed the set, and a caller
#: that passes everything should use :func:`list_complete_run_ids` instead. Ids past the cap are
#: reported as "no manifest seen" rather than silently treated as complete -- the same answer the
#: caller gets for a run whose upload really did not finish, which is the safe direction.
MANIFEST_PROBE_MAX = 256

#: HEADs in flight. Latency-bound, not CPU-bound, so this is about round-trip overlap; 8 keeps a
#: page of ~50 rows inside a few hundred milliseconds without opening a connection per row.
MANIFEST_PROBE_WORKERS = 8


def manifest_present(
    run_ids: Iterable[str],
    cfg: S3Config | None = None,
    *,
    max_workers: int = MANIFEST_PROBE_WORKERS,
) -> set[str]:
    """Which of ``run_ids`` finished uploading, by HEADing each one's :data:`MANIFEST_NAME`.

    Same question :func:`list_complete_run_ids` answers, asked about a handful of runs instead of
    the whole mirror -- and that difference is the reason this exists. The full version reads EVERY
    object key under the prefix to spot the manifests among them: measured against the deployed
    bucket that is 32,814 keys over 33 sequential LIST round-trips, 5.6s, and it grows with total
    history rather than with what is on screen. The tracer's run list called it on every render and
    re-rendered every 10s. One delimited LIST (:func:`list_mirrored_run_ids`) answers "is this run
    in the mirror at all" for everything in 0.06s; this fills in the manifest half for the rows a
    page actually shows.

    Unknown-shaped ids are skipped rather than raising: the caller is passing directory names off a
    disk it does not control, and one bad name must not cost the whole page its mirror column.

    Returns:
        The subset of ``run_ids`` that have a manifest object. An id that is absent from the mirror
        entirely is simply not in the result -- distinguishing "partial" from "not mirrored" needs
        the presence set from :func:`list_mirrored_run_ids`, which the caller already has.

    Raises:
        S3Error: On any unrecoverable S3/credential failure, as with the other listings here.
    """
    cfg = cfg or S3Config.from_env()
    ids: list[str] = []
    for rid in run_ids:
        try:
            ids.append(validate_run_id(rid))
        except S3Error:
            continue  # not a usable run id; it cannot be in the mirror under that name anyway
        if len(ids) >= MANIFEST_PROBE_MAX:
            break
    if not ids:
        return set()

    def op(client: Any) -> set[str]:
        botocore_exc = _botocore_exceptions()

        def check(run_id: str) -> tuple[str, bool]:
            try:
                client.head_object(
                    Bucket=cfg.bucket, Key=f"{cfg.run_prefix(run_id)}/{MANIFEST_NAME}"
                )
            except Exception as e:  # noqa: BLE001 -- re-raised below unless it is a plain 404
                # A missing manifest is the ANSWER here, not a failure: it is what "the upload never
                # finished" looks like. Everything else (expired session, denied, wrong region) has
                # to propagate, or an expired token would render every run as "Partial" -- a
                # confident wrong answer where the S3-unavailable path already shows an honest one.
                if botocore_exc is not None and isinstance(e, botocore_exc.ClientError):
                    code = str(e.response.get("Error", {}).get("Code", ""))
                    if code in ("404", "NotFound", "NoSuchKey"):
                        return run_id, False
                raise
            return run_id, True

        # `pool.map` re-raises a worker's exception in this thread as the results are consumed, so
        # the credential handling in `_with_retry` still sees it.
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(ids)))) as pool:
            return {run_id for run_id, ok in pool.map(check, ids) if ok}

    return _with_retry(cfg, op)


def list_complete_run_ids(cfg: S3Config | None = None) -> tuple[set[str], set[str]]:
    """Split mirrored runs into ``(complete, partial)`` by whether the manifest made it up.

    :data:`MANIFEST_NAME` is written **last** by :func:`upload_trace`, so its presence is the only
    signal that an upload finished. Reporting "mirrored" from a non-empty prefix alone -- which is
    all :func:`list_mirrored_run_ids` can tell -- badges a half-finished upload as fully shared, and
    that is precisely the case this feature is built around (a session expiring mid-upload, which
    the orchestrator swallows).

    Still **one** LIST: the manifest is detected from the same un-delimited page stream, so this
    costs no per-run GET or HEAD. Runs mirrored before manifests existed have none and are reported
    as partial -- callers should treat "partial" as "cannot vouch for it", not "broken".

    ONE LIST IS NOT THE SAME AS ONE ROUND-TRIP, and on a real mirror the difference decides whether
    a page is usable: un-delimited means every object of every run comes back, 1,000 keys per page,
    which was 33 sequential requests and 5.6s against the deployed bucket -- per render, growing
    with total history. Use it for a one-off audit of the whole mirror. For a page of a UI, pair
    :func:`list_mirrored_run_ids` (one delimited LIST) with :func:`manifest_present` (one HEAD per
    displayed row).
    """
    cfg = cfg or S3Config.from_env()
    prefix = f"{cfg.prefix}/" if cfg.prefix else ""

    def op(client: Any) -> tuple[set[str], set[str]]:
        seen: set[str] = set()
        complete: set[str] = set()
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=cfg.bucket, Prefix=prefix
        ):
            for obj in page.get("Contents", []) or []:
                rel = obj["Key"][len(prefix):]
                if "/" not in rel:
                    continue
                run_id, _, tail = rel.partition("/")
                if not run_id or not tail:
                    continue
                seen.add(run_id)
                if tail == MANIFEST_NAME:
                    complete.add(run_id)
        return complete, seen - complete

    return _with_retry(cfg, op)


def mirror_has_newer(run_id: str, dest_dir: Path | str, cfg: S3Config | None = None) -> bool:
    """Whether the mirror holds objects this local copy of ``run_id`` does not already have.

    Exists because "the directory is here" is not the same as "the directory is current". A run
    produced on this box, or pulled once, stays on disk untouched while the mirror moves on -- another
    machine re-scores it, a diagnostic is folded into its report -- and a reader looking at the local
    copy sees the old numbers with nothing to indicate it. This is the check that closes that gap.

    THE PREDICATE IS DELIBERATELY THE SAME ONE :func:`download_trace` SKIPS ON: absent locally, or
    present at a different size. They must agree, or the two disagree forever -- a gate that reported
    stale on something the downloader then declined to fetch would re-pull on every single page load
    and never settle. The cost of that agreement is that a change which happens to preserve a file's
    exact byte length is not detected; ``download_trace(..., force=True)`` is the escape hatch for
    that, and it is why `last_modified` is read but not compared.

    One LIST per call, no HEADs. Returns False on any S3 failure: a freshness check that raises would
    turn a mirror hiccup into an unviewable run, and the local copy is still renderable.
    """
    cfg = cfg or S3Config.from_env()
    run_root = Path(dest_dir) / run_id
    try:
        remote = _list_run_objects(cfg, run_id)
    except Exception:  # noqa: BLE001 -- staleness is an optimisation; never fail a render over it
        logger.warning("Could not check the mirror for %s; serving the local copy", run_id,
                       exc_info=True)
        return False
    if not remote:
        return False
    local = {rel: size for rel, _path, size in _local_files(run_root)}
    return any(local.get(rel) != meta["size"] for rel, meta in remote.items())


def pull_incomplete(run_root: Path | str) -> bool:
    """Whether ``run_root`` holds a pull that started but never finished.

    The tracer checks this before deciding a locally-present run needs no fetch: a run left partial
    by an interrupted download must be repaired, not rendered as though it were complete.

    An **empty** directory counts as incomplete too. ``download_trace`` has to create the run root
    before it can write the marker into it, so a crash in that window would otherwise leave a
    marker-less empty dir that renders as a permanently empty run -- exactly the outcome the marker
    exists to prevent. Nothing else creates an empty run dir, so this is safe.
    """
    root = Path(run_root)
    if (root / PULL_MARKER).is_file():
        return True
    return root.is_dir() and not any(root.iterdir())


def download_trace(
    run_id: str,
    dest_dir: Path | str,
    cfg: S3Config | None = None,
    *,
    force: bool = False,
) -> DownloadResult:
    """Materialize a mirrored run into ``dest_dir/<run_id>/`` for the tracer to render.

    This is the "render from S3" mechanism: once the files are on disk under the tracer's runs dir,
    every existing route serves them with no S3 awareness at all. Files already present at the same
    size are skipped, so re-pulling is cheap.

    Args:
        run_id: The trace id to pull.
        dest_dir: The tracer's runs directory (the run lands in a subdirectory of it).
        cfg: Source config; defaults to :meth:`S3Config.from_env`.
        force: Re-download even when a same-size local file exists.

    Returns:
        A :class:`DownloadResult` with per-file counts.

    Raises:
        RemoteTraceNotFoundError: If no objects exist under the run's prefix.
        S3Error: On any other unrecoverable S3/credential failure.
    """
    cfg = cfg or S3Config.from_env()
    run_id = validate_run_id(run_id)
    objects = _list_run_objects(cfg, run_id)
    if not objects:
        raise RemoteTraceNotFoundError(
            f"No run {run_id!r} in {cfg.uri()} -- check the run id, or that it was uploaded."
        )

    dest_dir = Path(dest_dir)
    run_root = dest_dir / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    result = DownloadResult(trace_id=run_id, dest=run_root)
    prefix = cfg.run_prefix(run_id) + "/"

    # Mark the run as mid-pull for the duration. If anything below raises -- a dropped connection,
    # credentials expiring past the one retry, a killed process -- the marker survives and the next
    # attempt repairs the run instead of serving a partial one. Cleared only on success.
    (run_root / PULL_MARKER).write_text(
        f"pulling {cfg.uri(run_id)}\nremove this file only if you are sure the run is complete\n",
        encoding="utf-8",
    )

    for rel in sorted(objects):
        size = objects[rel]["size"]
        target = _safe_join(run_root, rel)
        if not force and target.is_file() and target.stat().st_size == size:
            result.skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        key = prefix + rel
        _with_retry(cfg, lambda client, k=key, t=target: client.download_file(
            cfg.bucket, k, str(t)
        ))
        result.downloaded += 1
        result.bytes_downloaded += size

    (run_root / PULL_MARKER).unlink(missing_ok=True)

    logger.info(
        f"Pulled run {run_id} from {cfg.uri(run_id)} into {run_root} "
        f"({result.downloaded} file(s) downloaded, {result.skipped} already current)"
    )
    return result
