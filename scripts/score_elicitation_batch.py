#!/usr/bin/env python
"""Score the elicitation rubric across every mirrored run in the S3 run store.

`scripts/score_elicitation.py` scores ONE conversation that is already on this machine. That is the
wrong shape for E22, whose denominator is every build conversation the team has ever produced -- and
those live in the shared mirror (s3://<bucket>/<prefix>/<run_id>/), not in any one laptop's runs/ dir.
This walks the mirror instead, and reuses that script's scorer unchanged.

WHY THIS DOES NOT PULL WHOLE RUNS. A mirrored run carries its artifact snapshot, per-source event
logs, model-call store and eval reports -- tens of megabytes. The elicitation scorer reads exactly one
file out of it (`events-usersim.jsonl`, see appsci_bench/eval/build_transcript.read_run_dir), so this
downloads that one object into a staging directory shaped like a run dir. A full mirror sweep is
therefore a few megabytes, not a few gigabytes.

SELECTION. A run is scoreable when it has a NON-EMPTY user-sim event log (an empty one means the
conversation was never recorded -- unmeasurable, not a zero) and its task declares an elicitation
rubric. Runs already carrying `eval/elicitation.json` are skipped unless --rescore.

CARVE DATES ARE A REPORTING ANNOTATION, NOT A FILTER. A conversation recorded before its task moved
the rubric's facts behind the customer simulator could not have elicited them: the agent could just
read them, so the rate is near-zero as a fact about the setup rather than the agent. Those runs are
still scored -- watching the rate move when material is actually withheld is how you see the metric
works -- but they are tagged `pre_carve` and must not be pooled with the rest. Dates below are the
merge dates of the commits that moved each task's facts; a run ON the boundary date needs checking by
hand, so treat the tag as approximate at the edges.

Usage:
    # see what would be scored, spend nothing
    python scripts/score_elicitation_batch.py --dry-run

    # smoke-test the pipeline on two runs, keep results local
    python scripts/score_elicitation_batch.py --limit 2

    # the real pass: score everything unscored and mirror each result back
    python scripts/score_elicitation_batch.py --upload
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# The judge reads MODEL_* / JUDGE_MODEL. A dev shell usually only has the gateway's ANTHROPIC_* pair,
# so apply the same overlay the human-baseline template applies before anything imports a model config.
sys.path.insert(0, str(_REPO / "human_baseline" / "template"))
import os  # noqa: E402

from gateway_env import gateway_eval_env  # noqa: E402

for _k, _v in gateway_eval_env().items():
    os.environ.setdefault(_k, _v)

from appsci_bench.core.spec import load_task_spec  # noqa: E402
from appsci_bench.eval.build_transcript import (  # noqa: E402
    USERSIM_EVENTS_FILENAME,
    read_run_dir,
)
from appsci_bench.eval.elicitation import score_elicitation  # noqa: E402
from appsci_bench.eval.datatypes import (  # noqa: E402
    STATUS_NO_EXCHANGES,
    STATUS_SCORED,
    EvalReport,
)
from appsci_bench.eval.elicitation_rubric import load_rubric  # noqa: E402
from appsci_bench.eval.report_html import render_html  # noqa: E402
from appsci_bench.eval.store import TranscriptStore  # noqa: E402
from shared.observability.s3_store import (  # noqa: E402
    S3Config,
    upload_subtree,
    S3Error,
    _with_retry,
)

#: Merge date of the commit that moved each task's rubric facts behind the customer simulator.
#: airline_rebooking: 9c0a1f91 (#100). order_to_cash: the persona carve-out cited in
#: human_baseline/attempts/airline_rebooking-*/logs/ELICITATION_NOTE.md. slides_generator: 304aefe9
#: (#118) + 0190398d (#127). compliance_review: ec7e6ae3 (#124) authored the rubric.
CARVE_DATES = {
    "airline_rebooking": "20260819",
    "order_to_cash": "20260819",
    "slides_generator": "20260825",
    "compliance_review": "20260827",
}

#: Where a score can already live. TWO conventions exist and both must be honoured before deciding a
#: run is unscored: the orchestrator's eval pipeline writes into the run's eval/ report dir, while
#: scripts/score_elicitation.py writes to the run root. Checking only the first re-scores every run
#: somebody scored by hand -- which is not just wasted judge calls, it leaves the same run carrying two
#: scores in two places that can disagree (the judge is not deterministic; observed 0.167 vs 0.083 on
#: one re-score of an identical conversation). Whichever is found, the run counts as scored.
ELICITATION_KEY = "eval/elicitation.json"
ROOT_ELICITATION_KEY = "elicitation.json"

#: The run's own eval report, and the transcripts its HTML rendering needs. THE SIDECAR ALONE IS
#: INVISIBLE IN THE UI: the tracer dashboard reads the elicitation block out of report.json
#: (templates/dashboard.html -- `(this.evalData.report || {}).elicitation`) and never opens
#: elicitation.json, so a score that is only a sidecar is correctly filed and unreadable where anyone
#: would look for it. Folding is therefore the default, not an option.
REPORT_KEY = "eval/report.json"
REPORT_HTML_KEY = "eval/report.html"
CONVERSATIONS_PREFIX = "eval/conversations/"

#: An eval trial's run id, and the file naming the build it came from. A trial has no stakeholder
#: conversation of its own -- it is scored against the conversation of the build that produced the
#: artifact it is testing. The link is READ FROM THE RUN, not parsed out of the id.
TRIAL_RE = re.compile(r"_b\d+\.t\d+_")
REPLICATE_KEY = "replicate.json"

#: The statuses that are an actual MEASUREMENT and must never be overwritten: a scored rubric, and a
#: conversation that provably never happened (a real zero). Every other status -- `unmeasurable`,
#: `no_rubric` -- is a placeholder recording that nothing was measured, and replacing one with a real
#: number is the whole point of a backfill rather than something to guard against.
MEASURED_STATUSES = frozenset({STATUS_SCORED, STATUS_NO_EXCHANGES})
MANIFEST_KEY = "run_manifest.json"
_RUN_TS = re.compile(r"(\d{8})_\d{6}")


# EVERY S3 CALL GOES THROUGH `_with_retry`. It is not generic backoff -- it is the store's
# expired-credential recovery: on an auth failure it rebuilds the boto3 session (re-reading the SSO
# token cache) and retries once. Without it a token lapsing mid-batch is a hard failure, and these
# batches run for twenty minutes or more unattended, which is longer than a session comfortably
# outlives. Reads bypassed it until a review caught it.


def _list_mirror(cfg):
    """Every mirrored run id -> {relative key: size}. One paginated sweep."""
    base = cfg.prefix.rstrip("/")

    def op(client):
        runs: dict[str, dict[str, int]] = collections.defaultdict(dict)
        for page in client.get_paginator("list_objects_v2").paginate(
                Bucket=cfg.bucket, Prefix=base + "/"):
            for obj in page.get("Contents", []) or []:
                rel = obj["Key"][len(base) + 1:]
                if "/" in rel:
                    run_id, name = rel.split("/", 1)
                    runs[run_id][name] = obj["Size"]
        return runs

    return _with_retry(cfg, op)


def _get_json(cfg, run_id, name):
    """One JSON object out of a run, or ``{}`` when the run genuinely does not have that file.

    THE EMPTY DICT MEANS ABSENT AND NOTHING ELSE. This used to swallow every exception, which turned a
    failed read into "this run has no manifest" -- so a credentials lapse during selection silently
    dropped runs from the work set, and the batch reported success over a scope it had quietly shrunk.
    A read that fails now raises, and the caller counts it as a failure rather than a skip.
    """
    key = f"{cfg.prefix.rstrip('/')}/{run_id}/{name}"

    def op(client):
        return json.loads(client.get_object(Bucket=cfg.bucket, Key=key)["Body"].read())

    try:
        return _with_retry(cfg, op)
    except Exception as exc:  # noqa: BLE001 -- narrowed immediately below
        if _is_missing_key(exc):
            return {}
        raise


def _is_missing_key(exc: BaseException) -> bool:
    """Whether ``exc`` says the object is not there, as opposed to not readable.

    Matched on the response code rather than the exception class: boto3 builds its error classes
    dynamically, so `except client.exceptions.NoSuchKey` needs a client instance to name, and the
    store's `_translate` may have wrapped this in an S3Error by the time we see it.
    """
    for err in (exc, exc.__cause__, exc.__context__):
        code = getattr(err, "response", {}).get("Error", {}).get("Code") if err is not None else None
        if code in ("NoSuchKey", "404", "NotFound"):
            return True
    return False


def _download(cfg, run_id, name, dest: pathlib.Path):
    key = f"{cfg.prefix.rstrip('/')}/{run_id}/{name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    _with_retry(cfg, lambda client: client.download_file(cfg.bucket, key, str(dest)))


def _carve_tag(task_id: str, run_id: str) -> str:
    carve = CARVE_DATES.get(task_id)
    match = _RUN_TS.search(run_id)
    if not carve or not match:
        return "unknown"
    return "post_carve" if match.group(1) >= carve else "pre_carve"


def _select(cfg, runs, rescore: bool):
    """Runs worth scoring, a census of everything skipped and why, and any run we could not read.

    A read that fails is returned as an ERROR, never folded into the skip census: a skip means "this
    run does not qualify", and a run we could not look at is not the same statement. One bad read also
    must not end the sweep -- the remaining 300 runs are still scoreable -- so it is recorded and
    execution continues, and the caller's exit status reflects it.
    """
    rubrics: dict[str, object] = {}
    selected, skipped, read_errors = [], collections.Counter(), []
    for run_id, files in sorted(runs.items()):
        # A log that EXISTS BUT IS EMPTY is a real zero, not a gap: the agent never contacted the
        # stakeholder, and the scorer returns `no_exchanges` with rate 0.0 before making a single model
        # call. Only a MISSING log is unmeasurable -- we cannot tell what was asked. Filtering on
        # size > 0 conflated the two and silently dropped 47 legitimate zeros.
        if USERSIM_EVENTS_FILENAME not in files:
            skipped["no conversation log (eval trial or pre-instrumentation)"] += 1
            continue
        try:
            task_id = _get_json(cfg, run_id, MANIFEST_KEY).get("task_id")
        except Exception as exc:  # noqa: BLE001 -- recorded and skipped, never fatal to the sweep
            read_errors.append({"run_id": run_id, "error": f"reading {MANIFEST_KEY}: {exc!r}"})
            continue
        if not task_id:
            skipped["no manifest / task id unknown"] += 1
            continue
        if task_id not in rubrics:
            try:
                rubrics[task_id] = load_rubric(load_task_spec(task_id))
            except Exception:
                rubrics[task_id] = None
        if rubrics[task_id] is None:
            skipped[f"no elicitation rubric ({task_id})"] += 1
            continue
        if not rescore:
            if ELICITATION_KEY in files:
                skipped["already scored (eval/)"] += 1
                continue
            if ROOT_ELICITATION_KEY in files:
                skipped["already scored (run root)"] += 1
                continue
        selected.append((run_id, task_id, rubrics[task_id]))
    return selected, skipped, read_errors



def _fold_into_report(cfg, run_id, files, stage, payload, rescore: bool) -> str:
    """Add the elicitation block to the run's own eval report, and re-render its HTML page.

    Returns a short word for the progress line: what was done, or why it was not.

    Two things make this safe to do to a file other people's numbers come from. The block is
    ADDITIVE -- a key no objective reads, so no score changes -- and the report round-trips through
    EvalReport losslessly, which is asserted here rather than assumed, so a schema drift that would
    silently drop a field aborts the fold instead of publishing a truncated report.

    THE HTML IS RE-RENDERED FROM THE RUN'S TRANSCRIPTS, NOT FROM THE REPORT ALONE. Per-case
    conversation turns live in the store beside the report, and rendering without it silently strips
    them -- an "update" that shrinks the page it was asked to add a section to. So the conversations
    are pulled first, and if they are not in the mirror the JSON is still folded (that is what the
    dashboard reads) and the HTML is left alone rather than rewritten lossily.
    """
    if REPORT_KEY not in files:
        return "no-report"

    report_path = stage / "eval" / "report.json"
    if not report_path.exists():
        _download(cfg, run_id, REPORT_KEY, report_path)
    original = json.loads(report_path.read_text())

    existing = original.get("elicitation") or {}
    if existing.get("status") in MEASURED_STATUSES and not rescore:
        # Somebody else's number. Overwriting it would erase a measurement, and the judge is not
        # deterministic, so the replacement would not even be the same value. A placeholder status
        # (`unmeasurable` / `no_rubric`) is NOT a measurement and is replaced.
        return "kept-existing"

    # What must hold is that nothing is LOST or ALTERED -- not that the round-trip is byte-identical.
    # A report written before a schema field existed round-trips with that field ADDED (as null), which
    # is not loss and is in fact the very key about to be set. Testing exact equality rejected every
    # pre-elicitation report, i.e. all of the ones that most need folding.
    roundtrip = EvalReport.from_dict(dict(original)).to_dict()
    if set(original) - set(roundtrip):
        return "ABORT-keys-lost"
    if any(json.dumps(original[k], sort_keys=True) != json.dumps(roundtrip[k], sort_keys=True)
           for k in set(original) & set(roundtrip)):
        return "ABORT-values-changed"

    merged = dict(original)
    merged["elicitation"] = payload
    report = EvalReport.from_dict(merged)
    report_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n")

    conv_keys = [k for k in files if k.startswith(CONVERSATIONS_PREFIX)]
    for key in conv_keys:
        dest = stage / key
        if not dest.exists():
            _download(cfg, run_id, key, dest)
    store_root = report_path.parent
    store = TranscriptStore(str(store_root)) if (store_root / "conversations").is_dir() else None
    if store is None and REPORT_HTML_KEY in files:
        return "json-only"
    (stage / "eval" / "report.html").write_text(render_html(report, store))
    return "folded"



def _select_trials(cfg, runs):
    """Eval trials whose report carries no elicitation score, paired with the build to score against.

    Idempotent by construction: a trial that already has a scored block is skipped, so re-running
    costs nothing and a future sweep's stragglers are picked up by the same command.
    """
    selected, skipped, read_errors = [], collections.Counter(), []
    for run_id in sorted(runs):
        files = runs[run_id]
        if not TRIAL_RE.search(run_id):
            continue
        if REPORT_KEY not in files:
            skipped["trial has no eval report to fold into"] += 1
            continue
        try:
            report = _get_json(cfg, run_id, REPORT_KEY)
            build = (_get_json(cfg, run_id, REPLICATE_KEY) or {}).get("build_trace_id")
        except Exception as exc:  # noqa: BLE001 -- recorded and skipped, never fatal to the sweep
            read_errors.append({"run_id": run_id, "error": f"reading trial metadata: {exc!r}"})
            continue
        if (report.get("elicitation") or {}).get("status") == "scored":
            skipped["trial already scored"] += 1
            continue
        if not build:
            skipped["trial names no build run"] += 1
            continue
        if USERSIM_EVENTS_FILENAME not in runs.get(build, {}):
            # No log at all on the build: we cannot know what was asked. This is the one case that
            # stays unmeasurable -- an EMPTY log is a real zero and does get scored.
            skipped["build has no conversation log (unmeasurable)"] += 1
            continue
        task_id = report.get("task_id")
        if not task_id:
            skipped["trial report names no task"] += 1
            continue
        selected.append((run_id, task_id, build))
    return selected, skipped, read_errors


def _backfill_trials(cfg, runs, selected, args, pool) -> tuple[list, list]:
    """Score each trial against its build's conversation, fold, upload.

    EACH TRIAL GETS ITS OWN JUDGE PASS, deliberately. The judge is not stable -- sibling trials of one
    build have been observed at 0.25 / 0.31 / 0.38 on the identical conversation -- so k trials are k
    samples of that instability, and their spread is the measurement of it. Copying one build's number
    to all its trials would be cheaper and would throw that away, reporting a single point as if it
    were certain.
    """
    rubrics: dict[str, object] = {}
    results, failures = [], []
    for i, (run_id, task_id, build) in enumerate(selected, 1):
        try:
            if task_id not in rubrics:
                rubrics[task_id] = load_rubric(load_task_spec(task_id))
            rubric = rubrics[task_id]

            # The build's conversation, staged once per build and shared by its trials. Read-only here:
            # nothing is ever written back into a build's staging dir by this path.
            build_stage = args.stage_dir / build
            build_log = build_stage / USERSIM_EVENTS_FILENAME
            if not build_log.exists():
                _download(cfg, build, USERSIM_EVENTS_FILENAME, build_log)

            transcript = read_run_dir(build_stage)
            result = score_elicitation(rubric, transcript, pool=pool, task_id=task_id)
            payload = asdict(result)
            # Name the BUILD's log, which is what was actually read. The pipeline records an absolute
            # path on whichever box ran it; a run-relative one means the same thing anywhere.
            payload["transcript_source"] = f"runs/{build}/{USERSIM_EVENTS_FILENAME}"

            trial_stage = args.stage_dir / run_id
            out = trial_stage / "eval" / "elicitation.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, indent=2, default=str) + "\n")

            folded = _fold_into_report(cfg, run_id, runs.get(run_id, {}), trial_stage,
                                      payload, args.rescore)
            uploaded = upload_subtree(trial_stage, "eval", cfg).uploaded if args.upload else None

            rate = "  --  " if payload["elicited_rate"] is None else f"{payload['elicited_rate']:.2f}"
            results.append({"run_id": run_id, "task_id": task_id, "build": build,
                            "carve": _carve_tag(task_id, build), "status": payload["status"],
                            "elicited_rate": payload["elicited_rate"],
                            "asked_not_elicited_rate": payload["asked_not_elicited_rate"],
                            "missed_rate": payload["missed_rate"],
                            "exchanges": payload["num_exchanges"],
                            "items": payload["total_items"], "judged": payload["judged_items"],
                            "errors": payload["error_items"], "uploaded": uploaded, "fold": folded})
            print(f"[{i:>3}/{len(selected)}] {task_id:20} {payload['status']:13} "
                  f"elicited={rate}  fold={folded:14} {run_id}")
        except Exception as exc:
            failures.append({"run_id": run_id, "task_id": task_id, "build": build,
                             "error": f"{type(exc).__name__}: {exc}",
                             "traceback": traceback.format_exc(limit=4)})
            print(f"[{i:>3}/{len(selected)}] {task_id:20} FAILED {type(exc).__name__}: {exc}  {run_id}")
    return results, failures


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Under runs/ so it inherits that directory's gitignore, but NESTED one level rather than beside
    # the real runs. A staged dir holds an `events-usersim.jsonl`, and the tracer's run lister
    # consolidates any directory matching that glob into a listable run -- staging directly into runs/
    # would file every partial copy as a phantom run next to the genuine ones.
    ap.add_argument("--stage-dir", type=pathlib.Path, default=_REPO / "runs" / "elicitation_stage",
                    help="where the one-file-per-run staging dirs go (default: runs/elicitation_stage/)")
    ap.add_argument("--out", type=pathlib.Path, default=_REPO / "elicitation_batch.json",
                    help="summary written here (default: elicitation_batch.json)")
    ap.add_argument("--limit", type=int, help="score at most N runs -- use for a smoke test")
    ap.add_argument("--task", action="append", help="restrict to this task id (repeatable)")
    ap.add_argument("--run-id", action="append",
                    help="restrict to these exact run ids (repeatable). Pair with --rescore to redo a "
                         "run whose items the judge could not finish -- a rate-limited batch leaves "
                         "items unjudged, which shrinks the rate's denominator rather than failing.")
    ap.add_argument("--rescore", action="store_true",
                    help="also re-score runs that already carry eval/elicitation.json")
    ap.add_argument("--upload", action="store_true",
                    help="mirror each result back to the run's eval/ prefix in S3")
    ap.add_argument("--no-fold-report", action="store_true",
                    help="write only the sidecar. Off by default because the dashboard reads the "
                         "elicitation block out of eval/report.json and never opens the sidecar, so "
                         "a sidecar-only score is invisible in the UI. An existing block is never "
                         "overwritten without --rescore.")
    ap.add_argument("--workers", type=int, default=12,
                    help="max concurrent judge calls across the whole batch (default: 12)")
    ap.add_argument("--fold-only", action="store_true",
                    help="score nothing. Take the results already staged on disk and fold each into "
                         "its run's eval report, then upload. This is the repair path for scores that "
                         "were written as sidecars only -- it spends no judge calls, needs no manifest "
                         "lookups, and is idempotent.")
    ap.add_argument("--backfill-trials", action="store_true",
                    help="score EVAL TRIALS that carry no elicitation, against the conversation of the "
                         "build each one names. Every trial gets its own independent judge pass, so k "
                         "trials of one build are k samples of an unstable judge rather than one number "
                         "repeated. Idempotent: already-scored trials are skipped, so this is also the "
                         "command to run for a later sweep's stragglers.")
    ap.add_argument("--dry-run", action="store_true", help="list what would be scored, then stop")
    args = ap.parse_args(argv)

    cfg = S3Config.from_env()
    print(f"mirror   s3://{cfg.bucket}/{cfg.prefix}")
    runs = _list_mirror(cfg)
    print(f"runs     {len(runs)} mirrored")

    if args.backfill_trials:
        selected, skipped, read_errors = _select_trials(cfg, runs)
        if args.task:
            selected = [x for x in selected if x[1] in set(args.task)]
        if args.run_id:
            selected = [x for x in selected if x[0] in set(args.run_id)]
        if args.limit:
            selected = selected[:args.limit]
        print("\nskipped:")
        for reason, n in skipped.most_common():
            print(f"  {n:>4}  {reason}")
        if read_errors:
            print(f"\n{len(read_errors)} run(s) COULD NOT BE READ and were left out of this batch:")
            for e in read_errors[:10]:
                print(f"  {e['run_id']}: {e['error']}")
        by_task = collections.Counter(t for _, t, _ in selected)
        print(f"\nto backfill: {len(selected)} trials")
        for task, n in by_task.most_common():
            builds = len({b for _, t, b in selected if t == task})
            print(f"  {task:24} {n:>4} trials across {builds} builds")
        if args.dry_run or not selected:
            return 1 if read_errors else 0
        pool = ThreadPoolExecutor(max_workers=args.workers)
        results, failures = _backfill_trials(cfg, runs, selected, args, pool)
        pool.shutdown(wait=True)
        failures = failures + read_errors
        args.out.write_text(json.dumps({"results": results, "failures": failures}, indent=2) + "\n")
        print(f"\nwrote {args.out}  ({len(results)} scored, {len(failures)} failed)")
        print("fold outcomes:", dict(collections.Counter(r["fold"] for r in results)))
        # Spread across the trials of ONE build is the judge-stability measurement this mode exists to
        # produce, so report it here rather than leaving it to be recomputed.
        per_build = collections.defaultdict(list)
        for r in results:
            if r["elicited_rate"] is not None:
                per_build[r["build"]].append(r["elicited_rate"])
        multi = {b: v for b, v in per_build.items() if len(v) > 1}
        if multi:
            import statistics
            spreads = [max(v) - min(v) for v in multi.values()]
            agree = sum(1 for v in multi.values() if len(set(v)) == 1)
            print(f"\njudge stability over {len(multi)} builds with >1 trial: "
                  f"{agree} agree exactly, median spread {statistics.median(spreads):.3f}, "
                  f"max spread {max(spreads):.3f}")
        return 1 if failures else 0

    if args.fold_only:
        # Selection comes off the staging dir, not the mirror: the point is precisely the runs the
        # mirror already counts as scored. The task id is read from the staged result itself, so this
        # path skips the per-run manifest fetches that dominate a normal sweep's wall clock.
        selected, skipped, read_errors = [], collections.Counter(), []
        for entry in sorted(args.stage_dir.iterdir()) if args.stage_dir.is_dir() else []:
            result_file = entry / "eval" / "elicitation.json"
            if not entry.is_dir() or not result_file.is_file():
                skipped["no staged result"] += 1
                continue
            try:
                prior = json.loads(result_file.read_text())
            except (OSError, json.JSONDecodeError):
                skipped["staged result unreadable"] += 1
                continue
            if not prior.get("status") or prior.get("error_items"):
                skipped["staged result incomplete"] += 1
                continue
            selected.append((entry.name, prior.get("task_id") or "unknown", None))
    else:
        selected, skipped, read_errors = _select(cfg, runs, args.rescore)
    if args.task:
        selected = [s for s in selected if s[1] in set(args.task)]
    if args.run_id:
        selected = [s for s in selected if s[0] in set(args.run_id)]
    if args.limit:
        selected = selected[:args.limit]

    print("\nskipped:")
    for reason, n in skipped.most_common():
        print(f"  {n:>4}  {reason}")
    if read_errors:
        print(f"\n{len(read_errors)} run(s) COULD NOT BE READ and were left out of this batch:")
        for e in read_errors[:10]:
            print(f"  {e['run_id']}: {e['error']}")
    by_task = collections.Counter(t for _, t, _ in selected)
    print(f"\nto {'fold' if args.fold_only else 'score'}: {len(selected)}")
    for task, n in by_task.most_common():
        tags = collections.Counter(_carve_tag(t, r) for r, t, _ in selected if t == task)
        print(f"  {task:24} {n:>4}   {dict(tags)}")
    if args.dry_run or not selected:
        return 1 if read_errors else 0

    pool = ThreadPoolExecutor(max_workers=args.workers)
    results, failures = [], []
    for i, (run_id, task_id, rubric) in enumerate(selected, 1):
        stage = args.stage_dir / run_id
        try:
            # A complete staged result from an earlier pass is reused rather than re-scored. An
            # interrupted batch leaves results on disk whose upload never happened, and re-scoring them
            # would both re-spend judge calls and produce a second, possibly different number for the
            # same conversation. Anything with unjudged items is NOT complete -- those are re-scored,
            # because excluded items shrink the rate's denominator rather than failing loudly.
            out = stage / "eval" / "elicitation.json"
            cached = None
            if out.exists() and not args.rescore:
                try:
                    prior = json.loads(out.read_text())
                    if prior.get("status") and not prior.get("error_items"):
                        cached = prior
                except (OSError, json.JSONDecodeError):
                    cached = None

            if cached is None:
                # Fetched only when it is actually going to be read. Downloading before the cache
                # check made --fold-only pull a conversation it never opens, which is the one mode
                # whose whole point is to touch neither the judge nor the run's bulk.
                log = stage / USERSIM_EVENTS_FILENAME
                if not log.exists():
                    _download(cfg, run_id, USERSIM_EVENTS_FILENAME, log)
                transcript = read_run_dir(stage)
                result = score_elicitation(rubric, transcript, pool=pool, task_id=task_id)
                # asdict, not vars: the per-item table is a list of nested dataclasses, and vars()
                # leaves them as objects for json.dumps to stringify -- producing an items list of repr
                # strings that reads as valid JSON and is useless to every consumer.
                payload = asdict(result)
                # The scorer records the path it read, which here is this machine's staging dir. Rewrite
                # it to the run-relative path so a mirrored result names the run's own log, not a local
                # scratch directory that exists on nobody else's disk.
                payload["transcript_source"] = f"runs/{run_id}/{USERSIM_EVENTS_FILENAME}"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(payload, indent=2, default=str) + "\n")
            else:
                payload = cached

            folded = "skipped" if args.no_fold_report else _fold_into_report(
                cfg, run_id, runs.get(run_id, {}), stage, payload, args.rescore)

            uploaded = None
            if args.upload:
                uploaded = upload_subtree(stage, "eval", cfg).uploaded

            row = {"run_id": run_id, "task_id": task_id,
                   "carve": _carve_tag(task_id, run_id),
                   "status": payload["status"],
                   "elicited_rate": payload["elicited_rate"],
                   "asked_not_elicited_rate": payload["asked_not_elicited_rate"],
                   "missed_rate": payload["missed_rate"],
                   "exchanges": payload["num_exchanges"],
                   "items": payload["total_items"], "judged": payload["judged_items"],
                   "errors": payload["error_items"], "uploaded": uploaded,
                   "reused": cached is not None, "fold": folded}
            results.append(row)
            rate = "  --  " if row["elicited_rate"] is None else f"{row['elicited_rate']:.2f}"
            print(f"[{i:>3}/{len(selected)}] {task_id:20} {row['carve']:10} "
                  f"{payload['status']:12} elicited={rate}  fold={folded:16}"
                  f"{'(reused) ' if cached is not None else ''}{run_id}")
        except Exception as exc:
            failures.append({"run_id": run_id, "task_id": task_id,
                             "error": f"{type(exc).__name__}: {exc}",
                             "traceback": traceback.format_exc(limit=4)})
            print(f"[{i:>3}/{len(selected)}] {task_id:20} FAILED  {type(exc).__name__}: {exc}  {run_id}")
    pool.shutdown(wait=True)

    failures = failures + read_errors
    args.out.write_text(json.dumps({"results": results, "failures": failures}, indent=2) + "\n")
    print(f"\nwrote {args.out}  ({len(results)} scored, {len(failures)} failed)")

    scored = [r for r in results if r["elicited_rate"] is not None]
    if scored:
        print(f"\n{'task':24} {'carve':11} {'n':>3} {'median':>7} {'min':>6} {'max':>6}")
        groups = collections.defaultdict(list)
        for r in scored:
            groups[(r["task_id"], r["carve"])].append(r["elicited_rate"])
        import statistics
        for (task, carve), rates in sorted(groups.items()):
            print(f"{task:24} {carve:11} {len(rates):>3} {statistics.median(rates):>7.2f} "
                  f"{min(rates):>6.2f} {max(rates):>6.2f}")
    print("\nfold outcomes:", dict(collections.Counter(r["fold"] for r in results)))
    unmeasurable = collections.Counter(r["status"] for r in results if r["elicited_rate"] is None)
    if unmeasurable:
        print(f"\nno rate produced: {dict(unmeasurable)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
