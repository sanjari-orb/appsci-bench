"""Artifact checkpoints: one durable copy of the artifact's files per deploy attempt.

The artifact directory is a single mutable directory, bind-mounted from the host into the agent's
container. Every deploy builds whatever is in it at that moment, and the next deploy overwrites it --
so the only lasting record of what an agent deployed is whatever it happened to leave behind at the
end. Once the optimization sub-agent starts iterating, that directory holds a succession of distinct
candidates and all but the last are lost.

This module keeps one copy per deploy attempt, so a later pass can rebuild and score any candidate the
agent stood up rather than only the one it finally submitted.

Four properties are deliberate:

**Captured in the agent's deploy route, not in the shared deployer.** Three places build an artifact
container: the agent's own deploy route, the forced redeploy at the build->eval handover, and the
eval-only path that stands a pre-built directory up (also used by each trial of a replicate sweep).
Only the first is an agent decision. Hooking the shared deployer instead would add a spurious
checkpoint at every handover whose bytes duplicate the previous one, would checkpoint a checkpoint
when a later scoring pass deploys one, and would run in a context that has no run directory to write
into.

**Every attempt, including failures.** A candidate whose build or health check failed is precisely the
one whose source the next attempt overwrites, and it gets no score later, so the files are the only
evidence it existed. The outcome is recorded so a later reader can tell the three cases apart.

**Nothing but the artifact's files inside a checkpoint.** A checkpoint directory is the Docker build
context when it is rebuilt, so a metadata file dropped inside would ship into the image and change the
directory's content hash away from what was actually deployed. Metadata lives in a manifest *beside*
the checkpoints directory, mirroring how ``artifact_manifest.json`` sits beside ``artifact/``.

**A failed capture fails the RUN, loudly.** A missed copy is not a missed diagnostic. The artifact
directory is overwritten by the next deploy, so the candidate is gone for good, and the meta-eval it
was captured for would draw its conclusions from an incomplete set of candidates without anyone
knowing. A lost run can be re-run; a silently incomplete result cannot be detected. So a capture
failure aborts the run.

This deliberately does NOT follow observability's "never break a real run" rule, because that rule is
explicitly conditioned on never losing data: the run mirror can swallow a failure precisely because
the run is already complete on disk and one idempotent command re-uploads it later
(``shared/observability/README.md``, design goal 3 and the S3 section). Nothing can re-read an
overwritten artifact directory, so the condition does not hold here.

**The S3 upload is synchronous and equally fatal.** A checkpoint is not considered captured until it is
in the mirror, so the push happens inline and a failure ends the run like any other capture failure. A
run whose candidates exist only on one laptop's disk is not a run the later meta-eval pass can read, so
"copied locally but never uploaded" is treated as not captured rather than as a deferred upload. The
practical cost is that a live AWS session is now a prerequisite for running the benchmark at all, and a
slow upload delays the deploy response by however long it takes. Runs configured with no mirror at all
(no observability, or uploads turned off) pass no S3 target and skip this entirely.

Byte-for-byte, a checkpoint is the artifact directory minus dependency and build trees -- the container
snapshot's own exclusion list (``SNAPSHOT_EXCLUDE_DIRS``: caches, ``.git``, ``node_modules``,
``.venv``, ``dist``, ``build`` and the rest), plus ``*.pyc``. None of those are authored files: they
are reproducible from the source beside them, a stale cache copied forward can shadow a source change
on rebuild, and they are where an unbounded per-deploy copy actually gets big.

Every hash here is taken with that same exclusion set, on both sides of every comparison. Hashing a
pruned copy against an unpruned original would differ by exactly the directories the copy dropped, and
would report a mismatch every time one was present.

Nothing writes into the artifact directory per-deploy, which is what keeps a checkpoint's hash a stable
identity for a candidate. ``optimize_agent`` scores these candidates repeatedly and keeps its score
history OUTSIDE the artifact directory for exactly that reason, referring back to a candidate by the
``content_hash``/``ordinal`` recorded here rather than by writing a file next to the code.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.agent_container import SNAPSHOT_EXCLUDE_DIRS
from .deployer import artifact_fingerprint

logger = logging.getLogger(__name__)

#: Directory under a run that holds the per-deploy copies.
CHECKPOINTS_DIRNAME = "checkpoints"

#: Manifest file, written BESIDE the checkpoints directory (never inside a checkpoint).
MANIFEST_NAME = "checkpoints_manifest.json"

#: Directories left out of a checkpoint. Reuses the container snapshot's list rather than keeping a
#: second one: it is the same problem on the same directory (dependency and build trees an agent may
#: write into its output dir), and two hand-maintained copies would drift. `.pyc` files go too -- the
#: same suffix rule `artifact_fingerprint` applies.
_EXCLUDE_DIRS = frozenset(SNAPSHOT_EXCLUDE_DIRS)
_EXCLUDE_SUFFIXES = (".pyc",)

class CheckpointError(RuntimeError):
    """A checkpoint could not be captured, so a candidate is permanently lost.

    Its own type so the deploy route can tell "this run is no longer able to produce a complete
    candidate set" apart from every recoverable problem, and abort rather than log.
    """


def is_credentials_failure(error: BaseException) -> bool:
    """Whether `error`, or what raised it, is a missing/expired-AWS-credentials failure.

    Read at the log site to choose between remediation text and a traceback -- the same split
    `_mirror_run_to_s3` makes for the whole-run mirror. An expired session is by far the commonest
    failure and its message already IS the remediation, so a botocore traceback would bury the one
    line the reader needs; any other cause is undiagnosable without one. That mattered less when a
    failed push was swallowed, and matters more now it ends the run.
    """
    try:
        from shared.observability.s3_store import S3CredentialsError
    except ImportError:  # pragma: no cover -- s3_store is a first-party module
        return False
    return any(
        isinstance(exc, S3CredentialsError)
        for exc in (error, getattr(error, "__cause__", None))
        if exc is not None
    )


# Outcome of a deploy attempt, as recorded on its checkpoint. `unhealthy` is the middle case the
# deployer reports separately: the container answers (so the deploy succeeded and eval could query it)
# but the health route did not return 200.
OUTCOME_DEPLOYED = "deployed"
OUTCOME_UNHEALTHY = "unhealthy"
OUTCOME_FAILED = "failed"

# Guards the two read-modify-write steps: claiming an ordinal, and appending to the manifest. The
# deploy route is served by a threaded server, so without this two concurrent deploys would scan the
# same directory listing and pick the same ordinal, or read the same manifest and lose an entry. The
# copy itself runs OUTSIDE the lock -- the directory claim is what has to be atomic, not the bytes.
_CHECKPOINT_LOCK = threading.Lock()

_ORDINAL_WIDTH = 3
_NAME_RE = re.compile(r"-(\d+)$")


@dataclass
class CheckpointRecord:
    """One checkpoint's manifest entry."""

    ordinal: int
    name: str
    outcome: str
    content_hash: str
    file_count: int
    total_bytes: int
    created_at: str
    base_images: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "name": self.name,
            "outcome": self.outcome,
            "content_hash": self.content_hash,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "created_at": self.created_at,
            "base_images": self.base_images,
        }


def outcome_for(success: bool, healthy: bool) -> str:
    """Map a deploy result onto the recorded outcome.

    `success` and `healthy` are different claims (see `DeploymentResult`): a container that answers on
    its port but returns non-200 from its health route is a successful deploy of a non-conformant
    artifact, and flattening that into either neighbour would lose the distinction a later reader
    needs.
    """
    if not success:
        return OUTCOME_FAILED
    return OUTCOME_DEPLOYED if healthy else OUTCOME_UNHEALTHY


def checkpoints_dir(run_dir: str | Path) -> Path:
    """The directory holding one run's checkpoints."""
    return Path(run_dir) / CHECKPOINTS_DIRNAME


def manifest_path(run_dir: str | Path) -> Path:
    """The manifest for one run's checkpoints -- beside the directory, not inside it."""
    return Path(run_dir) / MANIFEST_NAME


def _ignore_build_leftovers(_dir: str, names: list[str]) -> set[str]:
    """`shutil.copytree` ignore callback: drop caches and VCS metadata."""
    return {
        n for n in names
        if n in _EXCLUDE_DIRS or any(n.endswith(s) for s in _EXCLUDE_SUFFIXES)
    }


def _next_ordinal(dest_root: Path, prefix: str) -> int:
    """Lowest unused ordinal for `prefix` under `dest_root`.

    Derived from the directory listing rather than an in-memory counter so it survives anything that
    rebuilds the server object mid-run, and so a partially-written run's numbering never restarts and
    collides.
    """
    highest = 0
    if dest_root.is_dir():
        for child in dest_root.iterdir():
            if not child.is_dir() or not child.name.startswith(f"{prefix}-"):
                continue
            match = _NAME_RE.search(child.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def _base_image_refs(artifact_dir: Path) -> list[str]:
    """Base-image references the artifact's Dockerfile starts from, in file order.

    Multi-stage builds name earlier stages in later `FROM` lines; those are internal and are dropped,
    leaving only refs that resolve against a registry. Returns an empty list when there is no
    Dockerfile or it cannot be read -- callers treat that as "nothing to record".
    """
    dockerfile = artifact_dir / "Dockerfile"
    try:
        lines = dockerfile.read_text(errors="replace").splitlines()
    except OSError:
        return []

    stages: set[str] = set()
    refs: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or not line.upper().startswith("FROM "):
            continue
        tokens = [t for t in line.split()[1:] if not t.startswith("--")]
        if not tokens:
            continue
        ref = tokens[0]
        # `FROM <ref> AS <alias>` -- remember the alias so a later stage referring to it is skipped.
        if len(tokens) >= 3 and tokens[1].upper() == "AS":
            stages.add(tokens[2].lower())
        if ref.lower() in stages:
            continue
        if ref not in refs:
            refs.append(ref)
    return refs


def _image_digest(ref: str) -> str | None:
    """The registry digest `ref` resolved to locally, or None if there isn't one.

    A tag moves; the digest is what a rebuild months later can be compared against. An image built
    locally (or pulled from a registry that reports no repo digest) legitimately has none, which is
    recorded as null rather than as a failure.
    """
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", ref],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    digest = proc.stdout.strip()
    # `index` on an empty RepoDigests list renders as "<no value>" rather than failing.
    if not digest or "no value" in digest:
        return None
    return digest


def _base_images(artifact_dir: Path) -> list[dict[str, Any]]:
    """Each base-image ref paired with the digest it currently resolves to."""
    return [{"ref": ref, "digest": _image_digest(ref)} for ref in _base_image_refs(artifact_dir)]


def _read_manifest(run_dir: Path) -> dict[str, Any]:
    path = manifest_path(run_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(run_dir: Path, data: dict[str, Any]) -> None:
    path = manifest_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _measure(root: Path) -> tuple[int, int]:
    """(file count, total bytes) under `root`."""
    count = 0
    total = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
            count += 1
    return count, total


def write_checkpoint(
    artifact_dir: str | Path,
    run_dir: str | Path,
    name_prefix: str,
    *,
    success: bool,
    healthy: bool,
) -> CheckpointRecord | None:
    """Copy the artifact directory as the next checkpoint of `run_dir`. Never raises.

    Args:
        artifact_dir: The live artifact directory that was just deployed from.
        run_dir: The run's directory (``runs/<trace_id>``); the copy lands under its
            ``checkpoints/`` and the manifest beside that.
        name_prefix: Directory-name prefix, the task's artifact container name, so a checkpoint
            still says what it is once copied away from its run.
        success: Whether the deploy attempt produced a container that answers.
        healthy: Whether the artifact's health route returned 200.

    Returns:
        The manifest entry, or None when there was nothing to capture -- no artifact directory, which
        means no candidate exists to lose and the deploy will fail on its own merits.

    Raises:
        CheckpointError: The copy could not be taken. Not swallowed: the next deploy overwrites this
            directory, so the candidate is gone and no later pass can tell that it is missing.
    """
    try:
        src = Path(artifact_dir)
        if not src.is_dir():
            logger.warning("Checkpoint skipped: artifact dir not found (%s)", src)
            return None

        run_root = Path(run_dir)
        dest_root = checkpoints_dir(run_root)

        with _CHECKPOINT_LOCK:
            dest_root.mkdir(parents=True, exist_ok=True)
            ordinal = _next_ordinal(dest_root, name_prefix)
            name = f"{name_prefix}-{ordinal:0{_ORDINAL_WIDTH}d}"
            dest = dest_root / name
            # exist_ok=False: a name that already exists means the scan raced with another writer, and
            # silently merging two candidates into one directory is the one outcome worth failing over.
            dest.mkdir(exist_ok=False)

        shutil.copytree(src, dest, ignore=_ignore_build_leftovers, dirs_exist_ok=True)

        file_count, total_bytes = _measure(dest)
        record = CheckpointRecord(
            ordinal=ordinal,
            name=name,
            outcome=outcome_for(success, healthy),
            content_hash=artifact_fingerprint(dest, _EXCLUDE_DIRS),
            file_count=file_count,
            total_bytes=total_bytes,
            created_at=datetime.now(timezone.utc).isoformat(),
            base_images=_base_images(dest),
        )

        with _CHECKPOINT_LOCK:
            manifest = _read_manifest(run_root)
            manifest.setdefault("trace_id", run_root.name)
            manifest.setdefault("checkpoints", [])
            manifest["checkpoints"].append(record.as_dict())
            manifest["checkpoints"].sort(key=lambda c: c.get("ordinal", 0))
            _write_manifest(run_root, manifest)

        logger.info(
            "Artifact checkpoint %s (%s): %d file(s), %d bytes -> %s",
            name, record.outcome, file_count, total_bytes, dest,
        )
        return record
    except Exception as e:  # noqa: BLE001 -- re-raised as CheckpointError below
        raise CheckpointError(
            f"Could not checkpoint the deployed artifact from {artifact_dir} into "
            f"{checkpoints_dir(run_dir)}: {e}. The next deploy overwrites that directory, so this "
            f"candidate cannot be recovered and the run can no longer produce a complete set for "
            f"meta-eval -- failing the run rather than continuing with a silent gap."
        ) from e


def find_by_hash(run_dir: str | Path, content_hash: str) -> dict[str, Any] | None:
    """The manifest entry whose content hash is `content_hash`, or None. Never raises.

    The join from a *score* back to the candidate it was measured on: the deploy-status route hands the
    agent this entry so `optimize_agent` can stamp a checkpoint reference onto every score it records.

    None is a real answer, not only an error: a run with no run directory writes no checkpoints at all,
    and an artifact edited since its last deploy matches nothing (the same case `mark_submitted`
    reports as unmatched). Callers must record "no checkpoint" rather than guessing at the latest one.
    """
    try:
        if not content_hash:
            return None
        with _CHECKPOINT_LOCK:
            entries = _read_manifest(Path(run_dir)).get("checkpoints") or []
        # Last match wins, for the same reason `mark_submitted` takes the highest ordinal: identical
        # bytes legitimately deploy more than once (a retry after a transient build failure), and the
        # newest is the one the running container was actually built from.
        matches = [e for e in entries if e.get("content_hash") == content_hash]
        return max(matches, key=lambda e: e.get("ordinal", 0)) if matches else None
    except Exception as e:  # noqa: BLE001 -- a missing reference must not fail the caller
        logger.warning("Failed to look up checkpoint by hash: %s", e, exc_info=True)
        return None


def find_for_artifact(run_dir: str | Path, artifact_dir: str | Path) -> dict[str, Any] | None:
    """The checkpoint whose files match what is in `artifact_dir` right now, or None. Never raises.

    This is the entry point the deploy-status route uses, and hashing here rather than reusing the
    staleness fingerprint is the point of it. The two are taken with different exclusion sets: the
    staleness check hashes with `artifact_fingerprint`'s default, while checkpoints are hashed with
    `_EXCLUDE_DIRS`, which also drops dependency and build trees. On any run where the agent left a
    `.venv`, `node_modules` or a `dist/` in its output directory the two hashes differ, so keying the
    lookup off the staleness fingerprint would find nothing and every score would record "no
    checkpoint" -- on exactly the runs where an agent installed its own dependencies.

    Only meaningful when the directory has not changed since the deploy; the caller checks that first,
    since otherwise this reports the checkpoint for code that is not running.
    """
    return find_by_hash(run_dir, artifact_fingerprint(artifact_dir, _EXCLUDE_DIRS))


def mark_submitted(run_dir: str | Path, artifact_dir: str | Path) -> str | None:
    """Record which checkpoint matches the artifact the run actually submitted. Never raises.

    Matched on content hash, and the honest answer is sometimes *none*: an agent that deploys and then
    keeps editing without redeploying submits an artifact that matches no checkpoint at all. That is
    the case the pre-eval staleness check exists to catch, so a later reader must not be allowed to
    assume the last checkpoint is the deliverable.

    A hash can match SEVERAL captures, so the match is the last of them and every matching ordinal is
    recorded beside it. Identical bytes legitimately deploy more than once -- a retry the agent did not
    edit before, after a transient build failure or a health check that timed out on a slow first boot
    -- and those attempts carry different outcomes. Naming the earliest would report the delivered
    artifact as `failed` on the strength of an attempt that was retried successfully straight after.
    The last one is the deploy whose container eval actually queried, which is what makes it the
    deliverable; picking the healthiest match instead would hide an artifact left unhealthy.

    Returns the matching checkpoint's name, or None when nothing matches.

    Unlike the capture itself this stays best-effort: it only reads bytes already safely on disk, so a
    failure here loses no candidate and costs a convenience for the later pass, not the run.
    """
    try:
        run_root = Path(run_dir)
        # Same exclusion set the checkpoints were hashed with: the live artifact dir still holds the
        # dependency trees a checkpoint dropped, so hashing it plainly would match nothing whenever the
        # agent had one.
        wanted = artifact_fingerprint(artifact_dir, _EXCLUDE_DIRS)
        with _CHECKPOINT_LOCK:
            manifest = _read_manifest(run_root)
            entries = manifest.get("checkpoints") or []
            if not entries:
                return None
            # Highest ordinal rather than list order: the manifest is written sorted, but selecting the
            # deliverable must not depend on a file another writer produced.
            matches = [e for e in entries if wanted and e.get("content_hash") == wanted]
            match = max(matches, key=lambda e: e.get("ordinal", 0)) if matches else None
            manifest["submitted"] = {
                "content_hash": wanted or None,
                "matched": match is not None,
                "ordinal": match.get("ordinal") if match else None,
                "name": match.get("name") if match else None,
                # Every capture holding these bytes. The hash identifies all of them equally, so a
                # reader comparing outcomes across the retries can see them rather than inferring one.
                "matching_ordinals": sorted(e.get("ordinal", 0) for e in matches),
            }
            _write_manifest(run_root, manifest)
        if match is None:
            logger.info(
                "Submitted artifact matches no checkpoint -- it was edited after the last deploy."
            )
        return match.get("name") if match else None
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to mark the submitted checkpoint: %s", e, exc_info=True)
        return None


def upload_checkpoint(run_dir: str | Path, name: str, s3_config: Any) -> None:
    """Push one checkpoint to the run's S3 mirror, synchronously.

    Blocks the deploy request until the objects are in the bucket, and raises if they do not get there:
    a candidate that exists only on the host that built it is not one a later meta-eval pass can read,
    so an upload failure is a capture failure, not a deferred upload.

    A run with no mirror configured (observability off, or uploads disabled) passes no target here and
    nothing is uploaded -- that is a deliberate configuration, not a failure.

    Raises:
        CheckpointError: The upload did not complete. Ends the run, like any capture failure.
    """
    if s3_config is None:
        return
    try:
        from shared.observability.s3_store import upload_subtree

        result = upload_subtree(run_dir, f"{CHECKPOINTS_DIRNAME}/{name}", s3_config)
    except Exception as e:  # noqa: BLE001 -- re-raised as CheckpointError below
        raise CheckpointError(
            f"Checkpoint {name} was copied locally but could not be mirrored to S3: {e}. A candidate "
            f"reachable only on this host cannot be scored by a later pass, so the run is failing "
            f"rather than continuing with a checkpoint that is effectively absent."
        ) from e
    logger.info("Mirrored checkpoint %s (%d file(s))", name, result.uploaded)
