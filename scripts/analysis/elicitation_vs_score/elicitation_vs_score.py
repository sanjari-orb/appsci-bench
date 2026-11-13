#!/usr/bin/env python
"""Does elicitation quality track task performance? Correlations, scatters and a bar chart.

Reads the SHARED S3 RUN MIRROR, not the experiments dashboard. Two reasons, both measured:

  1. The dashboard's job store on the jobs host holds 28 jobs; the mirror holds 63. Thirty-five
     jobs' records are simply absent from that box, so the API returns a subset with nothing
     marking it as one.
  2. Worse, the API is WRONG on a subset rather than merely short. The elicitation backfill
     (`scripts/score_elicitation_batch.py`) folded its results into each run's own eval report in
     the mirror and never updated the cell records the API reads. So the dashboard reports
     `unmeasurable` for runs that are in fact scored -- verified on one opus airline build the API
     calls unmeasurable and the mirror scores at 0.789. Those are the highest-elicitation builds in
     the corpus, so reading the API truncates the x-axis silently.

SCOPE: replicate trials only (`<task>_<model>_b<i>.t<j>_<persona>_<date>_<time>`). The ~56 runs
outside that naming carry no model anywhere readable -- `run_manifest.json` has task, trace and
event counts but no model -- so they cannot be placed on a model axis and are counted as skipped
rather than pooled in under an unknown label.

UNIT OF ANALYSIS IS THE BUILD, never the trial. Every trial of one build scores the SAME artifact
against the SAME stakeholder conversation, so trials are repeated measures: observed within-build
score spread is 0.002-0.04 against 0.2+ between builds. Pooling trials would multiply n by four
with near-duplicate points and shrink every interval by a factor nothing earned it. Averaging the
trials also averages out judge noise on the elicitation side, and the spread that averaging removes
is reported as the judge-noise floor rather than discarded.

BROKEN ARTIFACTS ARE KEPT IN THE PRIMARY SAMPLE. An errored eval result here is the artifact
answering HTTP 500 / 422 when queried -- it was built, deployed and is reachable, and it fails on
use. That is artifact quality, which is what the score measures, so excluding it would be excluding
the hypothesis's own counterexamples. A build that never produced an artifact has no score at all
and drops out on its own. The `--broken-removed` sample exists as a declared sensitivity cut, not as
the headline. Timeouts are counted as their own class because a timeout is genuinely ambiguous
between a slow artifact and our infrastructure.

Usage (from anywhere -- the repo root is located by marker, not by relative path):
    python scripts/analysis/elicitation_vs_score/elicitation_vs_score.py            # use the cache
    python scripts/analysis/elicitation_vs_score/elicitation_vs_score.py --refresh  # re-fetch
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime
import json
import math
import pathlib
import random
import re
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

_HERE = pathlib.Path(__file__).resolve().parent

#: The repo root, located by MARKER rather than by counting directories up from this file. This script
#: has already been moved one level deeper once, and the hardcoded `parents[2]` it used then silently
#: resolved to `scripts/` afterwards -- which does not fail where the mistake is. It fails later and
#: elsewhere, as a missing `tasks/` directory and an unimportable `shared.observability`, with nothing
#: pointing back at the path arithmetic. Two markers rather than one so a stray directory named `tasks`
#: cannot satisfy it.
_REPO = next((d for d in _HERE.parents if (d / "tasks").is_dir() and (d / "appsci_bench").is_dir()),
             None)
if _REPO is None:
    raise SystemExit(f"cannot locate the repo root above {_HERE}: no ancestor holds both tasks/ and "
                     f"appsci_bench/. Run this from inside a checkout.")
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

#: This file's path relative to the repo root, for the prose that cites it. Derived rather than typed
#: so a move cannot leave the report telling a reader to open a path that no longer exists.
_SELF = pathlib.Path(__file__).resolve().relative_to(_REPO)

BUCKET = "<your-bucket>"
PREFIX = "runs"
#: Outputs are written to `_out/elicitation_vs_score/<UTC timestamp>/` and never overwrite an earlier
#: generation: the
#: numbers move when the corpus grows or a constant is corrected, and a report someone has already
#: quoted must stay readable next to the one that replaced it. `latest` is a symlink to the newest, so
#: there is still one stable path to open. The download CACHE is deliberately OUTSIDE that tree and
#: shared across generations -- it holds mirror objects that cannot change, so dating it would re-pay
#: for the same fetch every run.
OUT_ROOT = _REPO / "scripts" / "_out" / "elicitation_vs_score"
CACHE = OUT_ROOT / ".cache"          # dotted so a listing shows only the dated generations

#: The COMMITTED copy of the most recent generation, in this script's OWN directory rather than under
#: `_out/`
#: (which is gitignored). Overwritten every run, on purpose: a reviewer reading the repo should see
#: the current numbers without an S3 credential, an AWS profile or a twenty-minute fetch, and a
#: directory that accumulated dated copies in git would put every superseded generation in the diff
#: forever. The dated archive under `_out/` keeps that history locally instead.
LATEST = _HERE / "latest_results"

#: What this script produces, and therefore the ONLY suffixes it will delete when refreshing `LATEST`.
#: Enumerated rather than clearing the directory, because "empty a path then write into it" is one
#: typo away from removing something that was never ours -- and this one is inside the source tree.
PRODUCED_SUFFIXES = (".md", ".png", ".csv", ".json")

#: The single objective each task's headline number is read from, as defined by the task owner.
#: Checked against each spec's `evaluation_data.objectives`: airline's is also its declared binary
#: `reward_objective` and carries all the weight, and compliance's 0.4 is the unique maximum;
#: order-to-cash and slides each sit at the top weight alongside one other objective.
HEADLINE = {
    "airline_rebooking": "task_success",
    "order_to_cash": "flag_accuracy",
    "compliance_review": "violation_set_f1",
    "slides_generator": "expectations_satisfaction",
}


#: Per task, the LAST commit that changed WHAT THE AGENT MAY READ DIRECTLY versus what only the
#: stakeholder holds -- with its merge date as a fallback. A conversation recorded before that commit
#: could not have elicited the rubric's facts, because the agent could simply open them, so its rate
#: is a fact about the setup and must not pool with the rest.
#:
#: VERIFIED AGAINST GIT HISTORY, not inherited. `scripts/score_elicitation_batch.py` carries dates for
#: the same purpose but records each task's FIRST gating commit, which is the wrong end: if that
#: constant ever bites it admits builds predating the final gated state. Two of its four entries also
#: disagree with the history. What was checked, per task:
#:
#: - airline_rebooking -- #100 moved the modify and compensation rules behind the simulator. Two later
#:   commits touch the same paths and neither gates anything: #108 (08-24) edits provenance pins and
#:   the reward gate, #107 (08-26) is a spec-field cleanup whose deletions under `sop/` are
#:   author-facing files that were never mounted. So the first gating commit is also the last.
#: - order_to_cash -- NOT 08-19, which the inherited constant claims and on which nothing gated this
#:   task. Its simulator docs arrived 08-11 (#89), but #133 (08-28) is the last gating change and a
#:   substantive one: it carves the build share that bounds what the agent can read from the store,
#:   and it removes a leaked scored-case answer (case p2p-0001) from a document that reaches every
#:   persona. Builds before it had a contaminated setup.
#: - slides_generator -- two commits landed 08-25; #127 is the later and the one that stops the
#:   mounted KPI document from stating the response contract.
#: - compliance_review -- #124 authored the rubric. No runs exist for this task, so it is untested here.
#: - banking_support -- no verified commit and no elicitation rubric, so this filter is never reached
#:   for it. The date is the inherited one and is marked unverified rather than trusted.
GATING = {
    "airline_rebooking": ("9c0a1f91", "20260819"),   # #100
    "order_to_cash": ("4ccf6e2a", "20260828"),       # #133
    "slides_generator": ("0190398d", "20260825"),    # #127
    "compliance_review": ("ec7e6ae3", "20260827"),   # #124
    "banking_support": (None, "20260819"),           # unverified
}

#: The two ways a run's gating status can be established, named once because each appears both as a
#: per-run label and as a census row, and they must not drift apart.
BASIS_COMMIT = "commit ancestry"
BASIS_DATE = "merge-date comparison (no commit recorded)"
#: A task with no entry in `GATING` at all. Reported as its own basis rather than folded into the date
#: comparison: with no carve commit and no carve date there is nothing to compare against, so counting
#: such a run under `BASIS_DATE` would report a tally for a check that never ran -- which is precisely
#: what the census exists to make impossible.
BASIS_NONE = "no carve recorded for this task (unchecked)"

#: Every reason a run can be left out, pre-seeded at zero. THE ZEROS ARE THE POINT. The census is built
#: by tallying reasons as they occur, so a reason that never fires produces no row -- and a missing row
#: cannot be told apart from a check that was never written or that silently broke. Since most analyses
#: of this kind do not check gating at all, absence reads as "not checked", which leaves the headline
#: coefficient carrying an unanswered question about whether contaminated builds are in it. A zero is a
#: measurement: the check ran and found nothing to drop.
SKIP_REASONS = (
    "no eval report",
    "eval report present but no elicitation score in it",
    "not a replicate trial (no attributable model)",
    "eval report unreadable",
    "no paired elicitation rate and score",
    f"pre-carve, by {BASIS_COMMIT}",
    f"pre-carve, by {BASIS_DATE}",
    f"pre-carve, by {BASIS_NONE}",
)

#: Where the experiments server mirrors its job records. Derived the same way `store._jobs_prefix`
#: derives it, so a different run prefix still lands in the matching jobs prefix.
JOBS_PREFIX = f"{PREFIX}-jobs"

#: Above this share of a build's eval results erroring with an ARTIFACT query error, the
#: `--broken-removed` sample drops the build. Declared here rather than chosen after seeing the
#: coefficients move.
BROKEN_THRESHOLD = 0.5

#: Categorical slots 1-3 of the validated default palette, in fixed order. Three is the documented
#: all-pairs cap for scatter forms, and there are exactly three tasks with data. Aqua sits below 3:1
#: on the light surface, so the relief rule applies: every facet is titled and every number also
#: appears in the report's tables.
COLORS = {"airline_rebooking": "#2a78d6", "order_to_cash": "#eb6834", "slides_generator": "#1baf7a"}

#: A SECOND, non-colour channel for the same identity, in the same fixed order as the hues. The palette
#: already clears the colour-vision gates on its own (worst all-pairs deutan dE 9.2, tritan 9.6, against
#: a floor of 8), so this is not a rescue -- it is what makes the figures readable with no colour at all:
#: full-severity CVD, greyscale printing, or a screenshot pasted into a document that flattens it. Shape
#: on the scatter and an inked hatch on the bars, because those are the channels each form has spare.
MARKERS = {"airline_rebooking": "o", "order_to_cash": "s", "slides_generator": "^"}

#: Solid, then 45 degrees, then its 135-degree mirror. Three states a reader can name with no colour at
#: all. Directional only, and only at those two angles: horizontal or vertical hatching reads as
#: gridlines or as more bars, and dots do not survive being scaled down.
HATCHES = ("", "///", chr(92) * 3)   # solid, 45 deg, 135 deg (chr(92) is "\")

#: One aqua step sits at 2.74:1 on the light surface, under the 3:1 line, so the relief rule applies:
#: every number in every figure also appears in one of the report's tables, and no figure is the sole
#: carrier of a value. Re-stepping the hue instead would fork the documented palette.
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#d9d8d4", "#fcfcfb"


def darken(hex_color: str, factor: float = 0.62) -> str:
    """A darker step of the same hue, for tone-on-tone hatching that stays equal in loudness."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(min(255, max(0, int(c * factor))) for c in (r, g, b))

#: The file in a trial's run dir that names the build it scored. The authoritative trial->build link.
REPLICATE_KEY = "replicate.json"

#: Hours of quiet that separate one build's trials from the next build's, used ONLY for trials whose
#: run dir carries no `replicate.json` (nine kimi-k3 builds, mirrored before that file existed).
#: Measured rather than guessed: across every group here, consecutive trials of one build are at most
#: 3.6 hours apart and the shortest gap between two genuinely different builds of the same arm is 7.0
#: hours, so anything in that range separates them. Dropping these trials instead would drop kimi-k3
#: from the model axis entirely, and it holds the highest airline elicitation rate in the corpus.
FALLBACK_GAP_HOURS = 6.0

BOOTSTRAP = 10000
PERMUTATIONS = 10000
SEED = 20260904


# ----------------------------------------------------------------------------- mirror

def _cfg():
    from shared.observability.s3_store import S3Config
    return S3Config.from_env(bucket=BUCKET, prefix=PREFIX)


def list_keys(refresh: bool) -> list[str]:
    """Every object key under the run mirror. One paginated sweep, cached on disk."""
    path = CACHE / "keys.txt"
    if path.exists() and not refresh:
        return [l.strip() for l in path.read_text().splitlines() if l.strip()]
    from shared.observability.s3_store import _with_retry
    cfg = _cfg()

    def op(client):
        out = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PREFIX + "/"):
            out.extend(o["Key"] for o in (page.get("Contents") or []))
        return out

    keys = _with_retry(cfg, op)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(keys))
    return keys


def fetch_objects(run_ids: list[str], name: str, subdir: str, refresh: bool,
                  optional: bool = False, prefix: str = PREFIX) -> dict[str, dict]:
    """One named JSON object per run, cached on disk. Small objects, fetched in parallel.

    `optional` silences a 404 -- some runs legitimately predate `replicate.json`, and reporting each
    absence as a fetch failure buries the failures that are real.
    """
    d = CACHE / subdir
    d.mkdir(parents=True, exist_ok=True)
    from shared.observability.s3_store import _with_retry
    cfg = _cfg()
    todo = [r for r in run_ids
            if refresh or not ((d / f"{r}.json").exists() or (d / f"{r}.absent").exists())]

    def one(rid):
        key = f"{prefix}/{rid}/{name}"
        try:
            _with_retry(cfg, lambda c: c.download_file(BUCKET, key, str(d / f"{rid}.json")))
        except Exception as exc:  # noqa: BLE001 -- recorded, never fatal to the sweep
            if optional and "404" in repr(exc):
                (d / f"{rid}.absent").touch()
                return None
            return (rid, repr(exc))
        return None

    failures = []
    if todo:
        print(f"  fetching {len(todo)} x {name}...", flush=True)
        with ThreadPoolExecutor(max_workers=16) as pool:
            failures = [f for f in pool.map(one, todo) if f]
    for rid, err in failures:
        print(f"  ! could not fetch {rid}: {err}", file=sys.stderr)
    out = {}
    for rid in run_ids:
        p = d / f"{rid}.json"
        if p.exists():
            try:
                out[rid] = json.loads(p.read_text())
            except Exception as exc:  # noqa: BLE001
                print(f"  ! unreadable {rid}: {exc!r}", file=sys.stderr)
    return out


_ANCESTRY: dict[tuple[str, str], bool | None] = {}


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(_REPO), capture_output=True, text=True)


def contains_commit(run_commit: str, gate_commit: str) -> bool | None:
    """Whether the code a run was submitted from already contained the gating commit.

    None when the question cannot be answered here -- either commit missing from this checkout -- which
    the caller must treat as "fall back to the date", never as a pass or a fail. Memoised because the
    same handful of commit pairs recurs across every trial of every build.
    """
    key = (run_commit, gate_commit)
    if key in _ANCESTRY:
        return _ANCESTRY[key]
    verdict: bool | None = None
    if run_commit and gate_commit:
        have = all(_git("cat-file", "-e", f"{c}^{{commit}}").returncode == 0
                   for c in (run_commit, gate_commit))
        if have:
            verdict = _git("merge-base", "--is-ancestor", gate_commit, run_commit).returncode == 0
    _ANCESTRY[key] = verdict
    return verdict


def gating_verdict(task: str, run_commit: str | None, run_date: str) -> tuple[bool, str]:
    """(is this run post-gating, on what basis).

    Commit ancestry is exact and is used whenever both commits are reachable. The date comparison is
    the fallback, and it is approximate at the boundary by construction -- a run merged the same day as
    the gating commit could sit either side of it -- so which test decided is reported rather than
    assumed.
    """
    if task not in GATING:
        # No carve recorded, so nothing to test. Admitted -- refusing every run of a task nobody has
        # dated would silently drop it -- but under a basis that says the check did not happen.
        return True, BASIS_NONE
    gate_commit, gate_date = GATING[task]
    if gate_commit and run_commit:
        verdict = contains_commit(run_commit, gate_commit)
        if verdict is not None:
            return verdict, BASIS_COMMIT
    return run_date >= gate_date, BASIS_DATE


def known_tasks() -> list[str]:
    return sorted((p.name for p in (_REPO / "tasks").iterdir()
                   if p.is_dir() and (p / "spec.yaml").exists() and p.name != "_TEMPLATE"),
                  key=len, reverse=True)


def parse_run_id(rid: str, tasks: list[str]):
    """A replicate trial's identity, or None when the id is not one.

    Task ids are matched against the checked-out task list, longest first, rather than inferred from
    the underscore layout: `banking_support_b0.t0_l3_...` otherwise parses as task `banking` with
    model `support`, which is a silently wrong row rather than a skipped one.
    """
    for task in tasks:
        if not rid.startswith(task + "_"):
            continue
        m = re.match(r"^(?P<model>.+?)_b(?P<b>\d+)\.t(?P<t>\d+)_(?P<persona>l\d)_(?P<date>\d{8})_\d{6}$",
                     rid[len(task) + 1:])
        if m:
            return dict(task=task, model=m["model"], build=int(m["b"]), trial=int(m["t"]),
                        persona=m["persona"], date=m["date"], run_id=rid)
        return None
    return None


# ----------------------------------------------------------------------------- parsing

def error_classes(report: dict) -> dict[str, int]:
    kinds = collections.Counter()
    for r in report.get("results") or []:
        if not isinstance(r, dict) or not r.get("error"):
            continue
        e = str(r["error"])
        if "ArtifactQueryError" in e:
            kinds["artifact"] += 1
        elif re.search(r"timeout|timed out", e, re.I):
            kinds["timeout"] += 1
        elif re.search(r"rate.?limit|429|too many requests", e, re.I):
            kinds["rate_limit"] += 1
        else:
            kinds["other"] += 1
    return dict(kinds)


def trial_row(ident: dict, report: dict) -> dict | None:
    """One trial as a flat row, or None when it carries no pairable measurement."""
    elic = report.get("elicitation") or {}
    rate, score = elic.get("elicited_rate"), report.get("final_score")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return None
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return None
    per = {}
    for row in (report.get("views") or {}).get("per_metric") or []:
        if isinstance(row, dict) and row.get("objective") is not None:
            v = row.get("score")
            per[str(row["objective"])] = float(v) if isinstance(v, (int, float)) else None
    n_res = len(report.get("results") or [])
    kinds = error_classes(report)
    return dict(
        **ident, elic=float(rate), score=float(score),
        status=elic.get("status"), judged=elic.get("judged_items"), total=elic.get("total_items"),
        headline=per.get(HEADLINE.get(ident["task"], "")),
        n_results=n_res, n_errored=sum(kinds.values()),
        artifact_frac=kinds.get("artifact", 0) / n_res if n_res else 0.0,
        timeout_frac=kinds.get("timeout", 0) / n_res if n_res else 0.0,
    )


def assign_fallback_traces(trials: list[dict]) -> int:
    """Give every trial lacking a recorded build link a synthetic one, by clustering on time.

    Trials of one build run back to back; a re-submitted arm is a day later. So within each
    (task, model, persona, build index) the trials are sorted and cut wherever more than
    `FALLBACK_GAP_HOURS` of quiet separates two of them. This is only reached for runs mirrored before
    `replicate.json` existed -- where a recorded link exists it always wins, because a threshold is an
    inference and the link is a fact.
    """
    import datetime
    groups = collections.defaultdict(list)
    for t in trials:
        if t["build_trace"] is None:
            groups[(t["task"], t["model"], t["persona"], t["build"])].append(t)
    n = 0
    for key, rows in groups.items():
        def when(r):
            return datetime.datetime.strptime(r["run_id"][-15:], "%Y%m%d_%H%M%S")
        rows.sort(key=when)
        cut, prev = 0, None
        for r in rows:
            if prev is not None and (when(r) - prev).total_seconds() / 3600 > FALLBACK_GAP_HOURS:
                cut += 1
            prev = when(r)
            r["build_trace"] = f"gap:{'|'.join(map(str, key))}#{cut}"
            r["grouped_by"] = "time gap"
            n += 1
    for t in trials:
        t.setdefault("grouped_by", "recorded link")
    return n


def to_builds(trials: list[dict]) -> list[dict]:
    """Trials collapsed to one row per build: the mean of every measure, plus the judge-noise spread.

    KEYED ON THE BUILD'S OWN TRACE ID, read from each trial's `replicate.json`, never derived from
    the run id. The same arm was re-submitted on several days, so those are genuinely different
    builds -- different conversations, different artifacts -- that must not merge into one averaged
    point; but a build's four trials also straddle midnight, so keying on a date instead splits one
    build in two and reports the same artifact twice as though it were independent evidence. Five of
    fifty groups did exactly that before this used the recorded link.
    """
    split = collections.defaultdict(list)
    for t in trials:
        split[t["build_trace"]].append(t)

    def mean(vals):
        clean = [v for v in vals if isinstance(v, (int, float))]
        return sum(clean) / len(clean) if clean else None

    out = []
    for build_trace, rows in sorted(split.items()):
        first = rows[0]
        elics = [r["elic"] for r in rows]
        out.append(dict(
            task=first["task"], model=first["model"], persona=first["persona"],
            build=first["build"], date=min(r["date"] for r in rows), n_trials=len(rows),
            build_trace=build_trace, job_id=first.get("job_id") or "",
            grouped_by=first.get("grouped_by", "recorded link"),
            gating_basis=first.get("gating_basis", ""),
            run_commit=first.get("run_commit", "")[:12],
            elic=mean(elics),
            elic_sd=statistics.stdev(elics) if len(elics) > 1 else 0.0,
            score=mean([r["score"] for r in rows]),
            headline=mean([r["headline"] for r in rows]),
            judged=rows[0]["judged"], total=rows[0]["total"],
            statuses=",".join(sorted({str(r["status"]) for r in rows})),
            artifact_frac=mean([r["artifact_frac"] for r in rows]),
            timeout_frac=mean([r["timeout_frac"] for r in rows]),
            n_results=sum(r["n_results"] for r in rows),
            n_errored=sum(r["n_errored"] for r in rows),
            run_ids=";".join(sorted(r["run_id"] for r in rows)),
        ))
    return out


# ----------------------------------------------------------------------------- statistics

def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _ranks(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(xs, ys):
    return pearson(_ranks(list(xs)), _ranks(list(ys)))


def group_key(row, by):
    return tuple(row[b] for b in by)


def centered(rows, key, by):
    """`key` with each group's own mean removed -- see the report's own section on what this buys."""
    groups = collections.defaultdict(list)
    for r in rows:
        groups[group_key(r, by)].append(r[key])
    mu = {k: statistics.mean(v) for k, v in groups.items()}
    return [r[key] - mu[group_key(r, by)] for r in rows]


def group_diagnostics(rows, by) -> dict:
    """How much a centering scheme actually leaves to work with.

    Three numbers, because a centered coefficient over 47 observations is not 47 observations' worth of
    evidence and nothing in the coefficient itself says so:

    * `n_eff` -- observations less one degree of freedom per group mean estimated.
    * `groups_informative` -- groups that can move the estimate at all. A group holding one observation
      contributes a deviation of exactly zero on both axes; so does a group whose members all share the
      same elicitation rate, which happens here wherever a model never contacted the stakeholder on a
      task. Both are invisible in `n` and in `n_eff`.
    * `x_variance_retained` -- the share of the raw spread in elicitation that survives the centering.
      Near 1.0 means the scheme absorbed almost nothing and the row is close to a restatement of the
      uncentered one; small means the row is asking a much harder question of much less data.
    """
    groups = collections.defaultdict(list)
    for r in rows:
        groups[group_key(r, by)].append(r["elic"])
    informative = sum(1 for v in groups.values()
                      if len(v) > 1 and statistics.pvariance(v) > 0)
    raw = statistics.pvariance([r["elic"] for r in rows]) if len(rows) > 1 else 0.0
    resid = statistics.pvariance(centered(rows, "elic", by)) if len(rows) > 1 else 0.0
    return dict(n_eff=len(rows) - len(groups), groups=len(groups),
                groups_informative=informative,
                builds_informative=sum(len(v) for v in groups.values()
                                       if len(v) > 1 and statistics.pvariance(v) > 0),
                x_variance_retained=(resid / raw) if raw else None)


def _resampled_r(rows, ykey, by, idx):
    """Pearson r for one bootstrap resample, RE-CENTERED inside the resample when centering applies.

    This is the correction the review asked for. Centering once up front and then resampling the
    resulting deviations treats each group mean as a known constant, when it is itself estimated from
    the same data -- so the interval came out narrower than the evidence supports. Re-deriving the means
    from each resample propagates that uncertainty.
    """
    sub = [rows[i] for i in idx]
    if by is None:
        xs, ys = [r["elic"] for r in sub], [r[ykey] for r in sub]
    else:
        xs, ys = centered(sub, "elic", by), centered(sub, ykey, by)
    return pearson(xs, ys)


def bootstrap_ci_rows(rows, ykey, by, rng, reps=BOOTSTRAP, level=0.95):
    """Percentile interval over resampled BUILDS -- the unit of analysis, never the trial."""
    n = len(rows)
    if n < 4:
        return (None, None)
    rs = []
    for _ in range(reps):
        r = _resampled_r(rows, ykey, by, [rng.randrange(n) for _ in range(n)])
        if r is not None:
            rs.append(r)
    if len(rs) < reps // 2:
        return (None, None)
    rs.sort()
    lo = rs[int((1 - level) / 2 * len(rs))]
    hi = rs[min(len(rs) - 1, int((1 + level) / 2 * len(rs)))]
    return (lo, hi)


def permutation_p_rows(rows, ykey, by, rng, reps=PERMUTATIONS):
    """Two-sided permutation p, with the reassignment RESTRICTED TO EACH GROUP when centering applies.

    Also from the review, and the same principle as above: a within-group estimator's null hypothesis
    reassigns within groups. Shuffling deviations across groups tests a different, easier null. Since a
    within-group shuffle leaves every group mean untouched, permuting the already-centered values is
    equivalent to permuting and re-centering, so no second centering pass is needed here.
    """
    xs = [r["elic"] for r in rows] if by is None else centered(rows, "elic", by)
    ys = [r[ykey] for r in rows] if by is None else centered(rows, ykey, by)
    obs = pearson(xs, ys)
    if obs is None:
        return None
    blocks = [list(range(len(rows)))] if by is None else list(
        collections.defaultdict(list, _index_by(rows, by)).values())
    hits = 0
    for _ in range(reps):
        yp = list(ys)
        for block in blocks:
            vals = [ys[i] for i in block]
            rng.shuffle(vals)
            for i, v in zip(block, vals):
                yp[i] = v
        r = pearson(xs, yp)
        if r is not None and abs(r) >= abs(obs) - 1e-12:
            hits += 1
    return (hits + 1) / (reps + 1)


def _index_by(rows, by) -> dict:
    out = collections.defaultdict(list)
    for i, r in enumerate(rows):
        out[group_key(r, by)].append(i)
    return dict(out)


def stats_block(rows, ykey, by, rng) -> dict:
    """One row of a results table: the coefficient, its interval, its p, and what stands behind it."""
    xs = [r["elic"] for r in rows] if by is None else centered(rows, "elic", by)
    ys = [r[ykey] for r in rows] if by is None else centered(rows, ykey, by)
    block = dict(n=len(rows), pearson=pearson(xs, ys), spearman=spearman(xs, ys),
                 ci=bootstrap_ci_rows(rows, ykey, by, rng),
                 p=permutation_p_rows(rows, ykey, by, rng))
    block.update(group_diagnostics(rows, by) if by else dict(n_eff=len(rows)))
    return block


def analyse(rows: list[dict], ykey: str, rng) -> dict:
    """Per task, then pooled under four centering schemes."""
    usable = [r for r in rows if r["elic"] is not None and r.get(ykey) is not None]
    per_task = {}
    for task in sorted({r["task"] for r in usable}):
        g = [r for r in usable if r["task"] == task]
        per_task[task] = stats_block(g, ykey, None, rng)
    pooled = {"raw": stats_block(usable, ykey, None, rng)}
    for label, by in (("within_task", ["task"]), ("within_model", ["model"]),
                      ("within_task_model", ["task", "model"])):
        pooled[label] = stats_block(usable, ykey, by, rng)
    return dict(per_task=per_task, pooled=pooled, n=len(usable))


# ----------------------------------------------------------------------------- figures

def _style(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.9)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK2, labelsize=8, length=0)


def scatter_figure(rows, ykey, ylabel, title, path, stats):
    """Four panels: one per task, then every build pooled. Each panel is titled, so identity never
    rests on hue alone -- which the palette's light-surface contrast warning requires."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    usable = [r for r in rows if r["elic"] is not None and r.get(ykey) is not None]
    tasks = sorted({r["task"] for r in usable})
    fig, axes = plt.subplots(1, len(tasks) + 1, figsize=(3.2 * (len(tasks) + 1), 3.5),
                             facecolor=SURFACE)
    fig.suptitle(title, color=INK, fontsize=12, x=0.01, ha="left", y=1.02)

    def draw(ax, subset, name, r, n):
        _style(ax)
        for r_ in subset:
            broken = r_["artifact_frac"] > BROKEN_THRESHOLD
            colour = COLORS.get(r_["task"], INK)
            marker = MARKERS.get(r_["task"], "o")
            ax.scatter(r_["elic"], r_[ykey], s=52 if marker == "^" else 46, zorder=3,
                       marker=marker,
                       facecolor="none" if broken else colour,
                       edgecolor=colour, linewidth=1.6 if broken else 1.2, alpha=0.95)
        if len(subset) >= 3:
            xs = [r_["elic"] for r_ in subset]
            ys = [r_[ykey] for r_ in subset]
            mx, my = statistics.mean(xs), statistics.mean(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            if sxx > 0:
                b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
                lo, hi = min(xs), max(xs)
                ax.plot([lo, hi], [my + b * (lo - mx), my + b * (hi - mx)],
                        color=INK2, linewidth=1.4, alpha=0.55, zorder=2)
        sub = f"r = {r:+.2f}   n = {n}" if r is not None else f"n = {n}"
        ax.set_title(f"{name}\n{sub}", color=INK, fontsize=9.5, loc="left")
        ax.set_xlim(-0.05, 1.02)
        ax.set_ylim(-0.05, 1.05)
        ax.set_xlabel("elicitation rate", color=INK2, fontsize=8.5)

    for ax, task in zip(axes, tasks):
        g = [r for r in usable if r["task"] == task]
        st = stats["per_task"].get(task, {})
        draw(ax, g, task.replace("_", " "), st.get("pearson"), st.get("n", len(g)))
    draw(axes[-1], usable, "all tasks pooled",
         stats["pooled"]["raw"].get("pearson"), stats["pooled"]["raw"].get("n"))
    # The pooled panel is the one place three series share an axis, so it carries a legend. The
    # per-task panels are titled with the task, which is the direct label that makes them exempt.
    handles = [Line2D([], [], marker=MARKERS.get(t, "o"), linestyle="none", markersize=6.5,
                      markerfacecolor=COLORS[t], markeredgecolor=COLORS[t],
                      label=t.replace("_", " ")) for t in tasks]
    leg = axes[-1].legend(handles=handles, frameon=False, fontsize=7.5, loc="lower right",
                          handletextpad=0.4, borderpad=0.2)
    for txt in leg.get_texts():
        txt.set_color(INK2)
    axes[0].set_ylabel(ylabel, color=INK2, fontsize=8.5)
    # Only where such a build is actually plotted. On the broken-excluded figures none can be, by
    # construction, and a legend for an absent mark sends the reader looking for it.
    if any(r["artifact_frac"] > BROKEN_THRESHOLD for r in usable):
        fig.text(0.01, -0.06, "hollow marks: artifact errored on more than half its eval queries",
                 color=INK2, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=200, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)


def bar_figure(rows, path):
    """Per-task means of the three measures. Grouped bars, one scale -- all three are 0-1 rates, so
    they belong on one axis; a second axis here would be the dual-axis mistake."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tasks = sorted({r["task"] for r in rows})
    series = [("final score", "score", "#2a78d6"),
              ("headline objective", "headline", "#eb6834"),
              ("elicitation rate", "elic", "#1baf7a")]
    fig, ax = plt.subplots(figsize=(1.9 * len(tasks) + 4.0, 3.6), facecolor=SURFACE)
    _style(ax)
    width = 0.26
    for i, (label, key, color) in enumerate(series):
        xs, ys = [], []
        for j, task in enumerate(tasks):
            vals = [r[key] for r in rows if r["task"] == task and r.get(key) is not None]
            xs.append(j + (i - 1) * (width + 0.02))
            ys.append(statistics.mean(vals) if vals else 0.0)
        # The gap between adjacent bars is geometric (the 0.02 above), which frees the edge colour to
        # carry the hatch -- matplotlib draws a hatch in the edge colour, so a surface-coloured edge
        # would render the texture invisible and silently drop the channel.
        bars = ax.bar(xs, ys, width, label=label, color=color, zorder=3,
                      hatch=HATCHES[i % len(HATCHES)],
                      edgecolor=darken(color), linewidth=0.8)
        for b, v in zip(bars, ys):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.02, f"{v:.2f}",
                    ha="center", va="bottom", color=INK2, fontsize=8)
    ax.set_xticks(range(len(tasks)))
    ax.set_xticklabels([t.replace("_", "\n") for t in tasks], color=INK, fontsize=9)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("mean over builds (0-1)", color=INK2, fontsize=8.5)
    ax.set_title("Final score, headline objective and elicitation rate, by task",
                 color=INK, fontsize=11, loc="left")
    leg = ax.legend(frameon=False, fontsize=8.5, loc="upper left", bbox_to_anchor=(0, -0.14), ncol=3)
    for t in leg.get_texts():
        t.set_color(INK2)
    fig.tight_layout()
    fig.savefig(path, dpi=200, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)


# ----------------------------------------------------------------------------- report

def fmt(v, nd=3):
    return "--" if v is None else f"{v:+.{nd}f}" if isinstance(v, float) else str(v)


#: The four centering schemes, each with its standard name and the question it answers. Keyed by the
#: internal label so the tables and the explanation cannot drift apart, and ordered so that each row
#: absorbs one more nuisance dimension than the row above it.
CENTERING_ROWS = (
    ("raw", "pooled (uncentered)",
     "Across the whole corpus, do builds with a higher elicitation rate score higher?"),
    ("within_task", "within-task centered",
     "Within a single task, do builds with a higher elicitation rate score higher?"),
    ("within_model", "within-model centered",
     "Within a single model, do its higher-elicitation builds score higher?"),
    ("within_task_model", "within task x model centered",
     "With task and model both absorbed, does build-to-build variation in elicitation track "
     "build-to-build variation in artifact quality?"),
)

STATS_HEADER = ("| {first} | n | Pearson r | 95% CI (bootstrap) | permutation p | Spearman rho |\n"
                "|---|---|---|---|---|---|")
POOLED_HEADER = ("| centering | n | n_eff | informative groups | x-var retained | Pearson r | "
                 "95% CI (bootstrap) | permutation p | Spearman rho |\n"
                 "|---|---|---|---|---|---|---|---|---|")


def stats_row(label, st) -> str:
    lo, hi = st.get("ci", (None, None))
    ci = "--" if lo is None else f"[{lo:+.2f}, {hi:+.2f}]"
    p = st.get("p")
    return (f"| {label} | {st['n']} | {fmt(st.get('pearson'))} | {ci} | "
            f"{'--' if p is None else f'{p:.3f}'} | {fmt(st.get('spearman'))} |")


def pooled_row(label, st) -> str:
    lo, hi = st.get("ci", (None, None))
    ci = "--" if lo is None else f"[{lo:+.2f}, {hi:+.2f}]"
    p = st.get("p")
    groups, inf = st.get("groups"), st.get("groups_informative")
    g = "--" if groups is None else f"{inf} of {groups}"
    xv = st.get("x_variance_retained")
    return (f"| {label} | {st['n']} | {st.get('n_eff', st['n'])} | {g} | "
            f"{'--' if xv is None else f'{xv:.0%}'} | {fmt(st.get('pearson'))} | {ci} | "
            f"{'--' if p is None else f'{p:.3f}'} | {fmt(st.get('spearman'))} |")


def how_to_read(zero_counts) -> list[str]:
    """Variables, statistical conventions, and what group-mean centering does.

    Standard terminology throughout, each term glossed once on first use. The centering block is the
    longest because it is the part of this analysis most easily misread -- and because the row that
    bears on the question being asked is not the first one.
    """
    return [
        "## Variables",
        "",
        "Two measurements per build.",
        "",
        "**Elicitation rate** (`x`). Of the rubric items only the simulated stakeholder held, the "
        "proportion the agent obtained by asking. Items satisfiable from anything mounted into the "
        "agent's container are excluded by construction, so this measures elicitation rather than "
        "retrieval.",
        "",
        f"A rate of exactly 0.000 is a true zero rather than a missing measurement, and it arises two "
        f"ways: `no_exchanges`, where the agent never contacted the stakeholder at all "
        f"({zero_counts['no_exchanges']} builds), and `scored`, where it did make contact and obtained "
        f"none of the rubric's items ({zero_counts['scored']} {'build' if zero_counts['scored'] == 1 else 'builds'}). {zero_counts['total']} of "
        f"{zero_counts['all']} builds sit there. A run whose rate could not be measured is excluded "
        f"rather than scored as zero -- see the exclusion census.",
        "",
        "**Task score** (`y`), reported in two forms. The **final score** is the weighted mean over "
        "every objective the task declares. The **headline objective** is the single "
        "highest-weighted objective, reported separately because the blended figure can mask "
        "movement in the objective a task actually exists to measure.",
        "",
        "## Statistical conventions",
        "",
        "**Unit of analysis: the build** -- one stakeholder conversation and the single artifact "
        "produced from it. Each build's eval trials are averaged into one observation. Trials are "
        "repeated measures of the same artifact against the same conversation (observed within-build "
        "score SD 0.002 to 0.04, against 0.2+ between builds), so treating them as independent would "
        "inflate `n` roughly fourfold and understate every interval.",
        "",
        "**Pearson r** is the product-moment correlation; **Spearman rho** is the same statistic on "
        "ranks and is reported alongside as a robustness check. A material gap between them indicates "
        "leverage from one or two observations.",
        "",
        "**95% CI (bootstrap)** is a percentile interval over 10,000 resamples of builds. Where a row "
        "is centered, the group means are re-derived inside each resample rather than carried in from "
        "the full sample: a mean estimated from the same data is not a known constant, and treating it "
        "as one narrows the interval below what the evidence supports. An interval spanning zero means "
        "the data do not exclude a null association.",
        "",
        "**Permutation p** is two-sided, from 10,000 random reassignments of `y` against `x`. Where a "
        "row is centered, the reassignment is restricted to within each group, which is the null that "
        "matches a within-group estimator; shuffling deviations across groups tests an easier one. "
        "Simulated rather than taken from the t-approximation, whose distributional assumptions are "
        "weak at the smallest per-task counts here (n = 9 to 10).",
        "",
        "**n_eff, informative groups, x-var retained** qualify the centered rows, because `n` alone "
        "overstates them. Estimating a group mean costs one degree of freedom, which `n_eff` reflects. "
        "A group holding a single build contributes a deviation of exactly zero on both axes, and so "
        "does a group whose builds all share one elicitation rate, so **informative groups** counts "
        "those that can move the estimate at all. **x-var retained** is the share of the raw spread in "
        "elicitation that survives the centering: near 100% means the scheme absorbed almost nothing "
        "and the row nearly restates the uncentered one, while a small share means a much harder "
        "question asked of much less data.",
        "",
        "## Group-mean centering",
        "",
        "The first row of each pooled table is the uncentered correlation over all builds. It is also "
        "the row most vulnerable to confounding, because builds differ along two dimensions besides "
        "elicitation.",
        "",
        "**Task.** Score distributions differ by task for reasons unrelated to any agent -- different "
        "objectives, different weights, different difficulty. If the task with the more generous score "
        "distribution is also the task where elicitation is easier, the pooled correlation partly "
        "reflects that coincidence rather than any within-task relationship.",
        "",
        "**Model.** Capability is common to both variables. A stronger model elicits better and builds "
        "better, and both measurements come from the same agent in the same run, so general capability "
        "is a shared cause that produces a positive correlation with elicitation on no causal path at "
        "all.",
        "",
        "Group-mean centering (equivalently, demeaning, or absorbing a fixed effect for the grouping) "
        "removes one of those dimensions. Each group's own mean is subtracted from both variables, so "
        "an observation is expressed as its deviation from its group rather than as its raw value, and "
        "the correlation is computed on those deviations. Only within-group covariation remains.",
        "",
        "The intuition, if it helps: to test whether study time raises exam marks using students drawn "
        "from schools that mark to different standards, the pooled correlation can be driven by "
        "\"the school that marks generously also sets more homework\". Subtracting each school's mean "
        "from both variables recovers the intended question -- does a student who studies more than "
        "their school's average also score above their school's average?",
        "",
        "The four rows apply that at three levels:",
        "",
        "| centering | question it answers |",
        "|---|---|",
    ] + [f"| {label} | {question} |" for _key, label, question in CENTERING_ROWS] + [
        "",
        "Each row absorbs one further confound and leaves less residual variance to work with, so the "
        "estimates fall in magnitude and rise in standard error together.",
        "",
        "**The last row is underpowered and must not be read as a null.** It exists because the models "
        "were not run evenly across tasks -- so within-model centering still leaves task composition "
        "in, and this is the only row that removes both -- and because it asks the operationally useful "
        "question: if the same model runs the same task again and elicits more, is the artifact better? "
        "But its cells hold about three builds each, several contribute nothing, and its interval spans "
        "roughly -0.45 to +0.58. It cannot distinguish a moderate positive association from a moderate "
        "negative one, so a coefficient near zero there is an absence of evidence and not evidence of "
        "absence.",
        "",
        "**The row that bears on construct validity -- whether elicitation is a capability or an "
        "artefact of the metric -- is within-model centered.** If the association survives absorbing "
        "the model, elicitation carries information not already supplied by general capability. If it "
        "collapses, the pooled coefficient largely restates the model ranking.",
        "",
        "**Centering applies to the tables only. Every scatter plots raw values**, both axes on the "
        "0-1 scale, so a point can be read off as that build's actual elicitation rate and actual "
        "score. The `r` printed on each panel is the uncentered coefficient for the points shown, and "
        "the line is the least-squares fit through them.",
        "",
    ]


def write_report(path, builds, samples, results, skipped, judge_noise, basis_counts,
                 *, stamp, head):  # noqa: C901
    zeros = [b for b in builds if b["elic"] == 0.0]
    zero_counts = {
        "no_exchanges": sum(1 for b in zeros if "no_exchanges" in (b["statuses"] or "")),
        "scored": sum(1 for b in zeros if "no_exchanges" not in (b["statuses"] or "")),
        "total": len(zeros),
        "all": len(builds),
    }
    L = []
    A = L.append
    A("# Elicitation quality vs task performance")
    A("")
    A(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]} UTC, code at commit "
      f"`{head}`. Source: the shared S3 run mirror (`s3://<your-bucket>/runs/`). Fetched reports "
      f"are cached on disk and re-read only when the analysis is run with `--refresh`, so a run "
      f"re-scored in the mirror since the first sweep is represented here by its earlier numbers "
      f"until then.")
    A("")
    L.extend(how_to_read(zero_counts))

    A("## Sample and exclusions")
    A("")
    A("A zero means the criterion was applied and matched no runs.")
    A("")
    A("| exclusion criterion | runs |")
    A("|---|---|")
    for k, v in sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0])):
        A(f"| {k} | {v} |")
    A("")
    for (_slug, name), rows in samples.items():
        tasks = collections.Counter(r["task"] for r in rows)
        models = collections.Counter(r["model"] for r in rows)
        A(f"**{name}**: n = {len(rows)} builds over {sum(r['n_trials'] for r in rows)} eval trials. "
          f"By task: {', '.join(f'{k} {v}' for k, v in sorted(tasks.items()))}. "
          f"By model: {', '.join(f'{k} {v}' for k, v in sorted(models.items()))}.")
        A("")
    A(f"The second sample excludes builds whose artifact-error rate exceeds "
      f"{BROKEN_THRESHOLD:.0%}. Those errors are the artifact returning HTTP 5xx/4xx under query: it "
      f"built, deployed and is reachable, and fails on use. That is artifact quality, so such builds "
      f"remain in the primary sample. Builds that produced no artifact at all have no score and enter "
      f"neither sample.")
    A("")

    A(f"The third sample excludes every build at an elicitation rate of exactly zero -- "
      f"{zero_counts['total']} of {zero_counts['all']}, of which {zero_counts['no_exchanges']} never "
      f"contacted the stakeholder and {zero_counts['scored']} made contact and obtained nothing. They "
      f"are a quarter of the corpus and they score well below the rest, so without this cut a reader "
      f"cannot tell how much of the association is the narrower claim that an agent which talks at all "
      f"does better than one which does not. They belong in the primary sample -- never contacting the "
      f"stakeholder is a real elicitation outcome, not a failed measurement -- and the cut is here so "
      f"the two claims can be separated.")
    A("")
    A("### Gating filter: post-carve runs only")
    A("")
    A("Each task was carved at some point: material formerly readable from the agent's own container "
      "was moved behind the stakeholder simulator. Pre-carve runs are excluded, because an agent that "
      "could read the rubric's facts had no reason to elicit them, making the rate a property of the "
      "setup rather than of the agent. Inclusion is decided by commit ancestry -- whether the commit a "
      "run was submitted from already contained that task's carve commit -- falling back to a "
      "merge-date comparison for runs with no recorded commit, which is approximate at the boundary.")
    A("")
    A("Basis: " + ", ".join(f"{v} trials by {k}" for k, v in sorted(basis_counts.items()))
      + ". Carve commit per task: "
      + "; ".join(f"{t} at `{c}`, {d[:4]}-{d[4:6]}-{d[6:8]}" if c else f"{t} unverified"
                  for t, (c, d) in sorted(GATING.items()) if t in {b["task"] for b in builds}) + ".")
    A("")

    A("### Judge reliability")
    A("")
    A(f"The elicitation rate is assigned by an LLM judge, and each of a build's trials re-scores the "
      f"same conversation independently, so the trials constitute free replicate measurements of judge "
      f"reliability. Mean within-build SD is {judge_noise['mean']:.3f}, max {judge_noise['max']:.3f} on "
      f"the 0-1 scale, over {judge_noise['n']} builds with more than one trial. Averaging trials into "
      f"one observation per build removes most of it. Residual measurement error in `x` attenuates the "
      f"correlations below toward zero rather than inflating them, so the reported coefficients are "
      f"conservative with respect to this source.")
    A("")

    A("## Results")
    A("")
    for (slug, name), rows in samples.items():
        A(f"### {name.capitalize()}")
        A("")
        for ykey, ylabel in (("score", "final score"), ("headline", "headline objective")):
            st = results[(slug, ykey)]
            A(f"#### y = {ylabel}")
            A("")
            A("By task, one observation per build:")
            A("")
            A(STATS_HEADER.format(first="task"))
            for task, b in sorted(st["per_task"].items()):
                A(stats_row(task, b))
            A("")
            A("Pooled over tasks, under the four centering schemes defined above:")
            A("")
            A(POOLED_HEADER)
            for key, label, _q in CENTERING_ROWS:
                A(pooled_row(label, st["pooled"][key]))
            A("")
            A(f"![elicitation rate vs {ylabel}, {name}]({slug}__{ykey}__scatter.png)")
            A("")

    A("## Per-task means")
    A("")
    A("![the three measures per task](task_means__bars.png)")
    A("")
    A("| task | builds | final score | headline objective | elicitation rate | rubric items judged |")
    A("|---|---|---|---|---|---|")
    primary = next(v for k, v in samples.items() if k[0] == "all_builds")
    for task in sorted({r["task"] for r in primary}):
        g = [r for r in primary if r["task"] == task]

        def m(k, g=g):
            v = [r[k] for r in g if r.get(k) is not None]
            return f"{statistics.mean(v):.3f}" if v else "--"

        A(f"| {task} | {len(g)} | {m('score')} | {m('headline')} | {m('elic')} | {g[0]['judged']} |")
    A("")
    A("Two caveats when reading across. Airline's final score and headline objective are identical by "
      "construction, not coincidence: `task_success` carries the entire weight and `policy_compliance` "
      "is weighted zero. And the rubrics differ in size, so an elicitation rate over 12 judged items is "
      "quantised in steps of 0.083 against 0.053 for 19 items.")
    A("")

    A("## Per-build data")
    A("")
    A("One row per build. **Persona** is the stakeholder's disclosure level, L1 (most guarded) to L3 "
      "(most forthcoming); the corpus is almost entirely L3. **Within-build SD** is the spread of the "
      "elicitation rate across that build's trials, i.e. judge variance rather than agent behaviour. "
      "**Artifact-error rate** is the fraction of eval results that errored under query.")
    A("")
    A("| task | model | persona | build | date | trials | elicitation rate | within-build SD | final "
      "score | headline objective | errored results | artifact-error rate |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(builds, key=lambda r: (r["task"], r["model"], r["date"], r["build"])):
        score = "--" if r["score"] is None else f"{r['score']:.3f}"
        top = "--" if r["headline"] is None else f"{r['headline']:.3f}"
        d = f"{r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:8]}"
        A(f"| {r['task']} | {r['model']} | {r['persona']} | b{r['build']} | {d} | "
          f"{r['n_trials']} | {r['elic']:.3f} | {r['elic_sd']:.3f} | {score} | {top} | "
          f"{r['n_errored']}/{r['n_results']} | {r['artifact_frac']:.2f} |")
    A("")
    path.write_text("\n".join(L) + "\n")


def publish_latest(src: pathlib.Path) -> int:
    """Copy this generation over the committed `LATEST` directory, and return the file count.

    ORDERED SO THE COMMITTED COPY IS NEVER EMPTY. Each file is written under a temporary name and then
    moved into place, and only afterwards is anything the new generation did not replace removed. An
    earlier version cleared the directory first, which left a window -- short, but real, and across
    several large PNGs -- where a failure mid-copy would leave the committed numbers deleted rather
    than merely stale. Stale is recoverable by re-running; deleted is a hole in the repo.

    Removing what the new generation did not produce still matters: a run that stops emitting a figure
    must not leave the old one behind reading as current. But only files carrying a suffix this script
    writes are ever touched, and the directory itself is never removed.
    """
    import shutil
    LATEST.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    staged: list[tuple[pathlib.Path, pathlib.Path]] = []
    try:
        for f in sorted(src.iterdir()):
            if not (f.is_file() and f.suffix in PRODUCED_SUFFIXES):
                continue
            tmp = LATEST / f".{f.name}.new"
            shutil.copy2(f, tmp)
            staged.append((tmp, LATEST / f.name))
        for tmp, final in staged:
            tmp.replace(final)          # atomic within one filesystem
            written.add(final.name)
    finally:
        # Any temporary left behind is from a failed copy and must not linger as a dotfile.
        for tmp, _final in staged:
            if tmp.exists():
                tmp.unlink()
    for old in LATEST.iterdir():
        if old.is_file() and old.suffix in PRODUCED_SUFFIXES and old.name not in written:
            old.unlink()
    return len(written)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true",
                    help="re-list the mirror and re-fetch every report instead of using the cache")
    ap.add_argument("--out-dir", default=None,
                    help="write here instead of a new timestamped directory (overwrites it)")
    args = ap.parse_args()

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    OUT = pathlib.Path(args.out_dir) if args.out_dir else OUT_ROOT / stamp
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    head = _git("rev-parse", "--short", "HEAD").stdout.strip() or "unknown"

    print("listing the run mirror...")
    keys = list_keys(args.refresh)
    files = collections.defaultdict(set)
    for k in keys:
        rel = k[len(PREFIX) + 1:]
        if "/" in rel:
            rid, name = rel.split("/", 1)
            files[rid].add(name)
    print(f"  {len(files)} mirrored runs")

    tasks = known_tasks()
    skipped = collections.Counter({reason: 0 for reason in SKIP_REASONS})
    candidates = []
    for rid, names in files.items():
        if "eval/report.json" not in names:
            skipped["no eval report"] += 1
            continue
        ident = parse_run_id(rid, tasks)
        if ident is None:
            skipped["not a replicate trial (no attributable model)"] += 1
            continue
        candidates.append(ident)
    print(f"  {len(candidates)} replicate trials to read")

    run_ids = [c["run_id"] for c in candidates]
    reports = fetch_objects(run_ids, "eval/report.json", "reports", args.refresh)
    replicates = fetch_objects(run_ids, REPLICATE_KEY, "replicates", args.refresh, optional=True)
    trials = []
    for ident in candidates:
        rep = reports.get(ident["run_id"])
        if rep is None:
            skipped["eval report unreadable"] += 1
            continue
        if not isinstance(rep.get("elicitation"), dict):
            skipped["eval report present but no elicitation score in it"] += 1
            continue
        row = trial_row(ident, rep)
        if row is None:
            skipped["no paired elicitation rate and score"] += 1
            continue
        rpl = replicates.get(ident["run_id"]) or {}
        trace = rpl.get("build_trace_id")
        row["build_trace"] = str(trace) if trace else None
        row["job_id"] = str(rpl.get("job_id") or "")
        trials.append(row)

    # The submitting commit, from each trial's job record. Fetched AFTER the build links because the
    # job id comes from them, and it is what makes the gating test exact rather than calendar-based.
    job_ids = sorted({t["job_id"] for t in trials if t["job_id"]})
    jobs = fetch_objects(job_ids, "job.json", "jobs", args.refresh, optional=True,
                         prefix=JOBS_PREFIX)
    kept, basis_counts = [], collections.Counter()
    for t in trials:
        commit = (jobs.get(t["job_id"]) or {}).get("git_commit")
        t["run_commit"] = str(commit) if commit else ""
        post, basis = gating_verdict(t["task"], t["run_commit"], t["date"])
        t["gating_basis"] = basis
        if post:
            kept.append(t)
            basis_counts[basis] += 1
        else:
            skipped[f"pre-carve, by {basis}"] += 1
    trials = kept
    assign_fallback_traces(trials)
    print(f"  {len(trials)} trials paired and post-gating "
          f"({', '.join(f'{v} by {k}' for k, v in sorted(basis_counts.items()))})")

    builds = to_builds(trials)
    n_fallback = sum(1 for b in builds if b["grouped_by"] == "time gap")
    print(f"  {len(builds)} builds ({n_fallback} grouped by time gap, the rest by recorded link)")
    for k, v in skipped.items():
        print(f"  skipped: {v} {k}" + ("   <- checked, nothing excluded" if v == 0 else ""))

    # (slug, display name) -> builds. The slug names files; the display name is for prose. Kept
    # separate because a display name carrying "> 50%" makes a path that markdown will not link to.
    samples = {
        ("all_builds", "all builds"): builds,
        ("broken_removed", f"broken artifacts excluded (artifact-error rate > {BROKEN_THRESHOLD:.0%})"):
            [b for b in builds if b["artifact_frac"] <= BROKEN_THRESHOLD],
        ("zero_removed", "zero-elicitation builds excluded"):
            [b for b in builds if b["elic"] is not None and b["elic"] > 0.0],
    }

    results = {}
    for (slug, _name), rows in samples.items():
        for ykey in ("score", "headline"):
            results[(slug, ykey)] = analyse(rows, ykey, rng)

    for (slug, name), rows in samples.items():
        for ykey, ylabel in (("score", "final score"), ("headline", "headline objective")):
            scatter_figure(rows, ykey, ylabel,
                           f"Elicitation rate vs {ylabel} -- {name}",
                           OUT / f"{slug}__{ykey}__scatter.png",
                           results[(slug, ykey)])
    bar_figure(builds, OUT / "task_means__bars.png")

    if not builds:
        # The census above is the whole answer in this case, so print it and stop rather than raising
        # from `builds[0]`. An empty sample is a legible outcome, not a crash.
        print("\nNo build survived the filters, so there is nothing to correlate. The exclusion "
              "census above accounts for every run considered.")
        return 1

    with (OUT / "builds.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(builds[0].keys()))
        w.writeheader()
        w.writerows(builds)
    (OUT / "stats.json").write_text(json.dumps(
        {f"{slug} | y={y}": v for (slug, y), v in results.items()}, indent=2, default=str))

    sds = [b["elic_sd"] for b in builds if b["n_trials"] > 1]
    noise = dict(mean=statistics.mean(sds) if sds else 0.0, max=max(sds) if sds else 0.0, n=len(sds))
    write_report(OUT / "REPORT.md", builds, samples, results, skipped, noise, basis_counts,
                 stamp=stamp, head=head)

    print(f"\nwrote {OUT}/REPORT.md and {len(list(OUT.glob('*.png')))} figures")
    if args.out_dir is None:
        n = publish_latest(OUT)
        print(f"  refreshed the committed copy at {LATEST.relative_to(_REPO)} ({n} files)")
        prior = sorted(d.name for d in OUT_ROOT.iterdir()
                       if d.is_dir() and not d.name.startswith("."))
        if len(prior) > 1:
            print(f"  {len(prior) - 1} earlier generation(s) kept under _out/: "
                  f"{', '.join(prior[:-1])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
