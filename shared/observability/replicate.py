"""The replicate identity of a run, recorded in its own run directory.

A `builds: n` / `trials: k` experiment splits what used to be ONE run into 1 + k runs: a build cell
runs BUILD and stops, and each trial deploys that build's staged artifact and scores it. Each run
therefore holds half the story -- a build trace has agent turns and no eval, a trial trace has an
eval and no agent -- and nothing in the trace itself said which half you were looking at or where
the other half was. Opening a trial's trace showed an eval with no build phase and no way back to
the build that produced the artifact being scored.

WHY THE RUN DIRECTORY AND NOT THE JOB STORE. The link is derivable from the store already
(`depends_on` names the build cell, whose record carries its `trace_id`), so this file is a
projection, not a new source of truth. It lives here because the tracer reads `runs/` and knows
nothing about jobs or cells: putting the link in the run dir keeps that separation, travels with the
run when it is mirrored to S3, and still resolves for a run directory pulled back on its own. The
job store stays authoritative -- the dashboard reads it directly and can enumerate a build's trials,
which a single run directory cannot.

WHAT A BUILD'S RECORD DOES NOT CONTAIN: its trials. They do not exist yet when the build runs, and
back-filling a finished run directory would leave the copy already mirrored to S3 disagreeing with
the local one. A build's record names its job instead, and the dashboard is where the fan-out is
listed.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Written into `runs/<trace_id>/`, beside the event logs.
REPLICATE_FILENAME = "replicate.json"

#: Roles a run can have. Mirrors `appsci_bench.experiments.config`, which is the writer -- duplicated
#: rather than imported so the read side (the tracer) does not depend on the experiment runner.
ROLE_BUILD = "build"
ROLE_EVAL_TRIAL = "eval_trial"
ROLE_BUILD_AND_EVAL = "build_and_eval"

# Bounded because this is read back and rendered: a run directory is not a trusted input (it may have
# been pulled from S3, hand-edited, or written by a different version), and an unbounded string here
# would reach the tracer's UI.
_MAX_FIELD_CHARS = 256


def record_for(
    *,
    role: str,
    job_id: str | None = None,
    cell_id: str | None = None,
    label: str | None = None,
    build_index: int | None = None,
    trial_index: int | None = None,
    depends_on: str | None = None,
    build_trace_id: str | None = None,
) -> dict[str, Any]:
    """The payload to write for one run.

    `build_trace_id` is the whole point for a trial: it is the trace holding the build phase whose
    artifact this trial scored. It is optional because a remote worker cannot resolve it -- the build
    ran on another host and its record is not on this one -- and a link that cannot be resolved is
    better absent than wrong.
    """
    payload: dict[str, Any] = {"role": role}
    for key, value in (
        ("job_id", job_id),
        ("cell_id", cell_id),
        ("label", label),
        ("depends_on", depends_on),
        ("build_trace_id", build_trace_id),
    ):
        if value:
            payload[key] = str(value)[:_MAX_FIELD_CHARS]
    # 0 is meaningful for both indices (b0, t0), so test against None rather than truthiness.
    if build_index is not None:
        payload["build_index"] = int(build_index)
    if trial_index is not None and role == ROLE_EVAL_TRIAL:
        payload["trial_index"] = int(trial_index)
    return payload


def write_record(run_dir: Path | str, payload: dict[str, Any]) -> Path | None:
    """Write `replicate.json` into `run_dir`, atomically. Returns the path, or None on failure.

    Best-effort by contract: this file makes a trace navigable, and failing a cell over it would
    trade a real result for a broken link. Every failure is logged rather than raised.

    Atomic via same-directory temp + rename, for the same reason the job store is: the tracer polls
    this file while a run is live, and a reader must never see a half-written record.
    """
    run_dir = Path(run_dir)
    try:
        if not payload.get("role"):
            # Refused rather than written: a record with no role tells a reader nothing and would
            # render as an unlabelled badge, which is worse than no badge.
            logger.warning("Refusing to write %s with no role to %s", REPLICATE_FILENAME, run_dir)
            return None
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / REPLICATE_FILENAME
        fd, tmp = tempfile.mkstemp(dir=str(run_dir), prefix=".replicate-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                # allow_nan=False for the reason store._write_json_atomic documents: a non-finite
                # leaf is written as a bare literal that JSONResponse then refuses, turning one bad
                # value into a permanent 500 on the route that serves it.
                json.dump(payload, fh, indent=2, sort_keys=True, allow_nan=False)
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return target
    except Exception:  # noqa: BLE001 -- navigation metadata must never fail a run
        logger.warning("Could not write %s to %s", REPLICATE_FILENAME, run_dir, exc_info=True)
        return None


def read_record(run_dir: Path | str) -> dict[str, Any] | None:
    """Read a run's replicate record, or None if it has none or the file is unusable.

    Returns None rather than raising on every failure mode -- absent (the overwhelmingly common case:
    every run that is not part of a replicate experiment, and every run written before this existed),
    unreadable, not JSON, or not an object. A trace with no record renders without a badge.

    Values are clamped and coerced on the way out because a run directory is not a trusted input;
    see `_MAX_FIELD_CHARS`.
    """
    path = Path(run_dir) / REPLICATE_FILENAME
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- a corrupt file costs the badge, not the trace
        logger.warning("Ignoring unreadable %s in %s", REPLICATE_FILENAME, run_dir, exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None
    role = raw.get("role")
    if not isinstance(role, str) or not role:
        return None

    out: dict[str, Any] = {"role": role[:_MAX_FIELD_CHARS]}
    for key in ("job_id", "cell_id", "label", "depends_on", "build_trace_id"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            out[key] = value[:_MAX_FIELD_CHARS]
    for key in ("build_index", "trial_index"):
        value = raw.get(key)
        # bool is an int in Python and would render as "bTrue"; exclude it explicitly.
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
    return out


def short_label(record: dict[str, Any]) -> str:
    """A compact identity for a badge: `BUILD b0` / `TRIAL b0.t1`.

    Falls back to the role alone when the indices are missing, so an older or partial record still
    renders something true rather than `b None`.
    """
    role = record.get("role")
    build = record.get("build_index")
    trial = record.get("trial_index")
    if role == ROLE_EVAL_TRIAL:
        if build is None or trial is None:
            return "TRIAL"
        return f"TRIAL b{build}.t{trial}"
    if role == ROLE_BUILD:
        return "BUILD" if build is None else f"BUILD b{build}"
    if role == ROLE_BUILD_AND_EVAL:
        # The pre-replicate shape: one run that both builds and scores. Deliberately unbadged in the
        # UI -- every run before this feature is one of these, and badging them all "BUILD+EVAL"
        # would add noise to every historical trace to say "nothing special here".
        return ""
    return str(role)


def group_runs(rows: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> list[dict[str, Any]]:
    """Cluster a flat run list so a build and its trials read as one entry.

    Takes `(trace, record)` pairs -- a run-list row and its replicate record, None for a run with
    none -- and returns a list of either:

      * `{"kind": "run", "trace": ...}` -- an ordinary run, untouched
      * `{"kind": "group", "build": ..., "trials": [...]}` -- a build with its trials

    The clustering key is `(job_id, build cell id)`: the record's own `cell_id` for a build, its
    `depends_on` for a trial. That is what lets this work from the run directories ALONE, with no
    access to the job store -- a trial already records which build it scored. Job-qualified because
    `cell_id` is deliberately job-independent, so two submissions of one arm share it and an
    unqualified key would merge their separate builds into one row.

    INPUT ORDER IS PRESERVED and a group lands at the position of its first-seen member. The caller
    orders the run list (newest first today); re-sorting here would silently override that.

    A group may have `build: None` -- a trial whose build's run directory has been reaped, which is
    the steady state on a host under disk pressure. It still groups, because the trials remain
    siblings and dropping them would lose runs from the list; the renderer says the build is gone
    rather than implying the trials are unrelated.

    A trial with no `depends_on` names no build and stays an ordinary row: grouping it would mean
    inventing a parent for it.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    out: list[dict[str, Any]] = []

    for trace, record in rows:
        rec = record or {}
        role = rec.get("role")
        if role == ROLE_BUILD and rec.get("cell_id"):
            key = (rec.get("job_id", ""), rec["cell_id"])
        elif role == ROLE_EVAL_TRIAL and rec.get("depends_on"):
            key = (rec.get("job_id", ""), rec["depends_on"])
        else:
            # Anything that cannot state WHICH build it is, or belongs to, stays an ordinary row.
            #
            # A build with no `cell_id` used to fall through to the key `("", "")` and, because the
            # build slot is assigned unconditionally below, a second such record OVERWROTE the first
            # -- so the earlier run disappeared from the list altogether rather than degrading to an
            # ungrouped row. Unreachable from the worker (which always writes a cell id) but reachable
            # from exactly the inputs this module's docstring says to expect: a run dir pulled from S3,
            # or one written by a different version.
            out.append({"kind": "run", "trace": trace})
            continue

        group = groups.get(key)
        if group is None:
            group = {
                "kind": "group",
                "key": f"{key[0]}.{key[1]}",
                "job_id": key[0],
                "build_cell_id": key[1],
                "build": None,
                "build_index": rec.get("build_index"),
                "trials": [],
            }
            groups[key] = group
            out.append(group)

        if role == ROLE_BUILD:
            group["build"] = trace
            # A build states its own index authoritatively; a trial only reports what it was told.
            group["build_index"] = rec.get("build_index")
        else:
            group["trials"].append({"trace": trace, "record": rec})
            if group["build_index"] is None:
                group["build_index"] = rec.get("build_index")

    for group in groups.values():
        # By trial index, so t0 reads above t1 regardless of which finished first or how the caller
        # happened to order the flat list.
        group["trials"].sort(key=lambda t: (t["record"] or {}).get("trial_index", 0))
    return out
