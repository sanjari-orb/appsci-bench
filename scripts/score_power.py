#!/usr/bin/env python3
"""score^k -- what a set of builds scores when only one eval trial per build survives.

YOU SAY WHICH TRIALS BELONG TO WHICH BUILD. The buckets are an argument, not something this works
out: one `--build` per build, listing that build's eval trial run ids. Nothing here reads
`replicate.json`, parses a run id, resolves a re-submitted build slot or guesses which artifact a
trial scored. Every one of those was an inference, and an inference that goes wrong here does not
fail -- it reports a real-looking number for a set of builds that never existed.

WHAT IT COMPUTES, and nothing else:

  score^k. Keep ONE trial from each of the k builds. For every eval example, take the WORST of the k
      kept scores on that example, and average those over the examples. That is one value per way of
      keeping one -- 3 builds x 3 trials is 27 ways -- and score^k is their MEAN, reported with their
      STD. A case counts for the arm only as much as its weakest build got it right, so this charges
      the builds both for disagreeing with each other and for being wrong in different places.
  one draw. ONE of those same combination values, picked at random: what the arm would have scored
      if each build had been evaluated once and that was all you had. score^k is the expectation of
      it, and the std is the size of the gap between them, so a draw far from score^k is the arm
      telling you a single trial per build would have misled you. The draw is SEEDED, so it does not
      move between two runs over the same trials; `--seed` asks for a different one.
  worst. For every example, the worst score ANY trial got on it -- all of them, not one per build --
      averaged over the examples. No combinations are involved. It is the floor the whole set
      reached, and it sits at or below every combination value by construction, so score^k can never
      be below it. Where the two are close, the builds fail the same examples and running more of
      them buys little; where worst is far below, they fail different ones.
  the average. The mean of the per-build means of the trials' headline scores:
      `avg(avg(b0 trials), avg(b1 trials), ...)`. The optimistic number score^k is read against.

A BUILD THAT PRODUCED NOTHING IS AN EMPTY BUCKET. Pass `--build` with no run ids and that build
scores 0 on every example: it was asked for and returned nothing. score^k then goes to 0 while the
average stays well above it, which is the arm telling you it cannot build reliably. k does not
change -- an arm asked for three builds is a score^3 whatever came back.

k IS NOT A FREE PARAMETER. score^k falls as k rises: the worst of three draws is worse than the
worst of two. Compare two arms only where their k agrees.

Every trial named must have a readable eval report carrying a per-example view. One that does not
stops the run by name rather than being quietly dropped -- dropping it would silently change which
builds the number is about.

Usage:
    scripts/score_power.py --build T1 T2 T3 --build T4 T5 T6 --build T7 T8 T9
    scripts/score_power.py --build T1 T2 T3 --build T4 T5 --build      # third build made nothing
    scripts/score_power.py '[["T1","T2"],["T3","T4"]]'                 # same, as JSON
    scripts/score_power.py - < buckets.json                            # same, JSON on stdin

Runs are read from the local runs directory when it holds their eval report and from the S3 run
mirror otherwise, through `aws s3 cp`, cached under `scripts/_out/score_power/.cache/`. A mirror
failure that is not a plain "this object does not exist" ABORTS: expired credentials would otherwise
turn every run into a missing one.

Exit status: 0 computed, 2 bad usage, 3 no build given, 4 a named trial could not be read,
5 too many combinations to enumerate, 6 the mirror could not be read.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import pathlib
import random
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

_HERE = pathlib.Path(__file__).resolve().parent
#: The repo root, located by MARKER rather than by counting directories up from this file, so moving
#: this script deeper cannot leave it resolving to a plausible wrong directory.
_REPO = next((d for d in (_HERE, *_HERE.parents)
              if (d / "tasks").is_dir() and (d / "appsci_bench").is_dir()), None)
if _REPO is None:
    raise SystemExit(f"cannot locate the repo root at or above {_HERE}: no ancestor holds both "
                     f"tasks/ and appsci_bench/. Run this from inside a checkout.")
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from shared.observability import eval_report  # noqa: E402

BUCKET = "<your-bucket>"
PREFIX = "runs"
CACHE = _REPO / "scripts" / "_out" / "score_power" / ".cache"
#: Where runs land on this machine. Used only when it already holds a run's eval report.
LOCAL_RUNS = _REPO / "runs"
#: What is pulled from the mirror per run, and how a confirmed absence is remembered.
MIRRORED_OBJECT = str(eval_report.EVAL_REPORT)
ABSENT_SUFFIX = ".absent"

#: What a run id is allowed to contain. This value is interpolated into an S3 key and passed to a
#: subprocess, so it is checked before either: `../` would walk out of the run prefix, and a leading
#: dash would be read by `aws` as a flag. Bounded because an S3 key is bounded.
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")

#: Enumeration is the definition, so enumeration is what runs; this is the point past which it stops
#: being a computation and becomes a hang. A 3x3 arm is 27.
MAX_COMBINATIONS = 1_000_000
#: Most eval cases read out of one report. A report sits in a run directory the benchmark subject
#: can write, so the list it carries is bounded, as `metrics.MAX_DATA_POINTS` bounds it elsewhere.
MAX_CASES = 5_000
#: Longest case id kept. Same reason: these ids are printed.
MAX_NAME_CHARS = 200

#: What an empty bucket scores on every example. Not a missing value: the build was asked for and
#: returned nothing, and that is a result.
NO_EVAL_SCORE = 0.0

#: Seed for the single-draw figure. FIXED, so that two runs over the same trials report the same
#: number -- a figure people quote at each other must not move on its own. `--seed` changes the
#: draw, which is how you look at a different one.
RANDOM_PICK_SEED = 20260921

#: Where a trial's numbers came from.
SOURCE_LOCAL = "disk"
SOURCE_MIRROR = "mirror"

EXIT_OK, EXIT_NO_BUILD, EXIT_UNREADABLE, EXIT_TOO_MANY, EXIT_MIRROR = 0, 3, 4, 5, 6


class MirrorError(RuntimeError):
    """The mirror could not be read, for a reason that is not "this object does not exist"."""


@dataclass(frozen=True)
class Trial:
    """One eval trial: its headline score and its per-example scores."""
    run_id: str
    source: str
    score: float
    cases: dict[str, float]


# ----------------------------------------------------------------------------- reading

def per_case_scores(report: dict | None) -> dict[str, float] | None:
    """The report's per-example scores, or None when it carries no usable per-case view.

    Bounded, coerced and de-duplicated on the way out, because a run directory is not a trusted
    input. None rather than `{}` for a report that has no view at all: the caller stops on that,
    and "every case scored nothing" would be a different and much worse answer.
    """
    rows = (report or {}).get("views", {})
    rows = rows.get("per_data_point") if isinstance(rows, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    out: dict[str, float] = {}
    for row in rows[:MAX_CASES]:
        if not isinstance(row, dict):
            continue
        case_id, score = row.get("data_point_id"), row.get("score")
        if not isinstance(case_id, str) or not case_id:
            continue
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        if not math.isfinite(score):
            continue
        out[case_id[:MAX_NAME_CHARS]] = float(score)
    return out or None


def absent_marker(target: pathlib.Path) -> pathlib.Path:
    return target.with_name(target.name + ABSENT_SUFFIX)


def pending(dest: pathlib.Path, refresh: bool) -> bool:
    """Whether this run's report still has to be fetched. Absence is cached too."""
    target = dest / MIRRORED_OBJECT
    return refresh or not (target.is_file() or absent_marker(target).exists())


def fetch_run(run_id: str, dest: pathlib.Path, bucket: str, prefix: str, refresh: bool) -> None:
    """Mirror one run's eval report into `dest`, shaped like a run directory.

    An absent object is recorded as absent -- the reader reports it by name -- but every other
    failure raises, so an expired credential cannot read as a corpus of runs that all lack reports.
    """
    if not pending(dest, refresh):
        return
    target = dest / MIRRORED_OBJECT
    target.parent.mkdir(parents=True, exist_ok=True)
    uri = f"s3://{bucket}/{prefix}/{run_id}/{MIRRORED_OBJECT}"
    try:
        # NOT `--quiet`: that flag suppresses the failure message as well as the progress line, and
        # a 404 then arrives as a bare exit 1 -- indistinguishable from an expired credential, which
        # this function exists to tell apart from an absent object.
        done = subprocess.run(["aws", "s3", "cp", uri, str(target)],
                              capture_output=True, text=True, timeout=300)
    except FileNotFoundError as exc:
        raise MirrorError("the `aws` CLI is not installed, so the run mirror cannot be read; "
                          "pass --runs-dir to read runs from disk instead") from exc
    except subprocess.TimeoutExpired as exc:
        raise MirrorError(f"timed out fetching {uri}") from exc
    if done.returncode == 0:
        absent_marker(target).unlink(missing_ok=True)
        return
    err = f"{done.stderr or ''}\n{done.stdout or ''}".strip()
    if re.search(r"404|Not Found|does not exist|NoSuchKey", err, re.I):
        absent_marker(target).touch()
        return
    detail = err.splitlines()[-1] if err else f"aws exited {done.returncode}"
    raise MirrorError(f"could not read {uri}: {detail}")


def read_trial(run_id: str, run_dir: pathlib.Path, source: str) -> tuple[Trial | None, str]:
    """One run directory as a trial, or (None, why it cannot be used).

    The report is read with the shared, defensive parser: a run directory holds files the benchmark
    subject can write, and the parse that survives them must not be duplicated here.
    """
    raw = eval_report.read_report(run_dir)
    head = eval_report.headline(raw)
    if head is None:
        return None, "no eval report"
    if head["final_score"] is None:
        return None, "eval report carries no final_score"
    if head["partial"]:
        return None, "eval report is partial: the run was still going or was killed part-way"
    scored, total = head["cases_scored"], head["cases_total"]
    if scored is not None and total is not None and total > 0 and scored < total:
        return None, f"only {scored} of {total} cases scored"
    cases = per_case_scores(raw)
    if cases is None:
        return None, "eval report carries no per-example scores"
    return Trial(run_id=run_id, source=source, score=float(head["final_score"]), cases=cases), ""


def load_buckets(buckets: list[list[str]], runs_dir: pathlib.Path | None, cache: pathlib.Path,
                 bucket: str, prefix: str, refresh: bool) -> tuple[list[list[Trial]], list[str]]:
    """Every named run read into a trial, keeping the caller's buckets exactly as given."""
    bad = [r for ids in buckets for r in ids if not SAFE_RUN_ID.match(r)]
    if bad:
        return [], [f"not a run id: {r!r}" for r in bad]

    where: dict[str, tuple[pathlib.Path, str]] = {}
    for run_id in {r for ids in buckets for r in ids}:
        local = (runs_dir / run_id) if runs_dir else None
        if local is not None and (local / eval_report.EVAL_REPORT).is_file():
            where[run_id] = (local, SOURCE_LOCAL)
        else:
            where[run_id] = (cache / run_id, SOURCE_MIRROR)

    todo = [(r, p) for r, (p, source) in where.items()
            if source == SOURCE_MIRROR and pending(p, refresh)]
    if todo:
        print(f"fetching {len(todo)} eval report(s) from s3://{bucket}/{prefix}/ ...",
              file=sys.stderr)
        with ThreadPoolExecutor(max_workers=8) as pool:
            # list() so a MirrorError raised in a worker surfaces here rather than being swallowed
            # by an iterator nobody consumed.
            list(pool.map(lambda item: fetch_run(item[0], item[1], bucket, prefix, refresh), todo))

    loaded: dict[str, Trial] = {}
    problems: list[str] = []
    for run_id, (path, source) in where.items():
        trial, why = read_trial(run_id, path, source)
        if trial is None:
            problems.append(f"{run_id}: {why}")
        else:
            loaded[run_id] = trial
    if problems:
        return [], problems
    return [[loaded[r] for r in ids] for ids in buckets], []


# ----------------------------------------------------------------------------- the metric

def align(buckets: list[list[Trial]]) -> tuple[list[str], list[list[list[float]]], list[str]]:
    """(the example ids, one vector per trial aligned to them, any warnings).

    The UNION of every trial's examples, so a trial missing an example another trial scored counts
    as 0 on it rather than as defining a smaller eval. Trials of one task normally cover exactly the
    same set; where they do not, that is reported, because scoring each trial against its own list
    would compare builds on different work without saying so.

    An EMPTY bucket becomes one trial scoring `NO_EVAL_SCORE` everywhere: the build was asked for
    and produced nothing.
    """
    sets = [frozenset(t.cases) for ids in buckets for t in ids]
    if not sets:
        return [], [[[NO_EVAL_SCORE]] for _ in buckets], []
    case_ids = sorted(set().union(*sets))
    warnings = []
    if len(set(sets)) > 1:
        short = sum(1 for s in sets if len(s) < len(case_ids))
        warnings.append(f"{short} of {len(sets)} trials cover fewer than the {len(case_ids)} "
                        f"examples scored between them; the missing ones count as 0 for those "
                        f"trials")
    vectors = [[[t.cases.get(c, NO_EVAL_SCORE) for c in case_ids] for t in ids]
               or [[NO_EVAL_SCORE] * len(case_ids)]
               for ids in buckets]
    return case_ids, vectors, warnings


def combination_case_minima(arrays: list):
    """For every combination of one trial per build, the per-example minimum.

    Shape `(n0, .., nk, examples)`, built by broadcasting rather than by looping over combinations:
    each build's array goes on its own axis, so `minimum` across them produces the whole product at
    once.
    """
    import numpy as np
    k = len(arrays)
    out = None
    for i, a in enumerate(arrays):
        n_trials, n_cases = a.shape[-2], a.shape[-1]
        shaped = a.reshape((1,) * i + (n_trials,) + (1,) * (k - 1 - i) + (n_cases,))
        out = shaped if out is None else np.minimum(out, shaped)
    return out


def score_power(vectors: list[list[list[float]]], seed: int = RANDOM_PICK_SEED) -> dict:
    """score^k over the given buckets: the mean and std of the combination values.

    `vectors[b][t][c]` is build b's trial t on example c, every trial aligned to the same example
    order. One combination is one trial kept per build; its value is the mean over examples of the
    worst kept score on that example; score^k is the mean of those values.

    The std is a POPULATION std: the combinations are the whole space, enumerated, not a sample of
    it. It says how far the answer moves with WHICH trials you keep, and it is not a standard error
    -- every trial appears in many combinations, so they are not independent draws.

    `random_pick` is ONE of those combination values, drawn at random: what the arm would have
    scored if each build had been evaluated once and that was all you had. score^k is the
    expectation of it, so the two differ by exactly the luck of the draw -- which is what the std
    above sizes.
    """
    import numpy as np
    if not vectors:
        raise ValueError("score^k needs at least one build")
    widths = {len(t) for trials in vectors for t in trials}
    if len(widths) != 1:
        raise ValueError(f"every trial must cover the same examples, got lengths {sorted(widths)}")
    n_combinations = math.prod(len(trials) for trials in vectors)
    if n_combinations > MAX_COMBINATIONS:
        raise ValueError(f"{n_combinations:,} combinations is past the {MAX_COMBINATIONS:,} this "
                         f"enumerates; that is more builds or trials than the metric was meant for")

    arrays = [np.asarray(trials, dtype=float) for trials in vectors]
    minima = combination_case_minima(arrays)
    per_combination = minima.mean(axis=-1).ravel()

    # The worst of EVERY trial on each example, averaged over the examples: no combinations, no
    # keeping one per build, just the floor the whole set reached. It is the strictest of these
    # figures by construction -- taking the minimum over more trials cannot raise it -- so it sits at
    # or below every combination value, and a test pins that.
    every_trial = np.concatenate(arrays, axis=0)
    worst = float(every_trial.min(axis=0).mean())

    # One trial per build, chosen at random: an index into each build's own trials, which picks out
    # one of the enumerated combinations rather than recomputing anything.
    rng = random.Random(seed)
    picked = [rng.randrange(len(trials)) for trials in vectors]

    return {
        "k": len(vectors),
        "n_examples": widths.pop(),
        "n_combinations": n_combinations,
        "score": float(per_combination.mean()),
        "std": float(per_combination.std()),
        "worst": worst,
        "n_trials": int(every_trial.shape[0]),
        "random_pick": float(minima[tuple(picked)].mean()),
        "random_pick_trials": picked,
        "seed": seed,
        "lowest": float(per_combination.min()),
        "highest": float(per_combination.max()),
        # One value per example behind each headline, in the order the caller passed the examples.
        # Each headline is exactly the mean of its own vector, which is what makes these the
        # decomposition of it: which examples the set loses, rather than how much.
        "per_example": [float(v) for v in minima.mean(axis=tuple(range(minima.ndim - 1)))],
        "per_example_draw": [float(v) for v in minima[tuple(picked)]],
        "per_example_worst": [float(v) for v in every_trial.min(axis=0)],
    }


def build_means(buckets: list[list[Trial]]) -> list[float]:
    """Each build's mean headline score; `NO_EVAL_SCORE` for a build that produced nothing."""
    return [sum(t.score for t in ids) / len(ids) if ids else NO_EVAL_SCORE for ids in buckets]


def average(buckets: list[list[Trial]]) -> float:
    """avg(avg(build 0's trials), avg(build 1's trials), ...).

    The mean of the build means rather than of all the trials pooled: each build counts once
    whatever number of trials it happens to carry, which is the same weighting score^k gives them.
    """
    means = build_means(buckets)
    return sum(means) / len(means) if means else NO_EVAL_SCORE


def across_build_spread(buckets: list[list[Trial]]) -> tuple[float, float]:
    """(std, variance) of the build means -- how far the builds sit from the average.

    A POPULATION spread, over these builds rather than over some population they were drawn from:
    the k builds are the whole thing being measured, which is the same reason the spread of the
    combination values is a population one.

    This is the quantity score^k exists to charge for, shown on its own. Builds that agree leave the
    average close to score^k; builds that disagree pull them apart, and this says by how much before
    any minimum is taken.
    """
    means = build_means(buckets)
    if not means:
        return 0.0, 0.0
    mean = sum(means) / len(means)
    variance = sum((m - mean) ** 2 for m in means) / len(means)
    return math.sqrt(variance), variance


# ----------------------------------------------------------------------------- the tool

def report(buckets: list[list[Trial]], metric: dict, warnings: list[str],
           per_example: dict[str, dict[str, float]] | None) -> str:
    """The whole result as text. Every number that was averaged is also shown unaveraged."""
    k = metric["k"]
    counts = ", ".join(str(len(ids)) for ids in buckets)
    drawn = ", ".join(
        f"build {i} trial {t}" if buckets[i] else f"build {i} (no trials)"
        for i, t in enumerate(metric["random_pick_trials"]))
    headline = [
        (f"score^{k}", f"{metric['score']:.4f}   std {metric['std']:.4f}",
         f"(mean of all {metric['n_combinations']:,} combinations)"),
        ("one draw", f"{metric['random_pick']:.4f}",
         f"(one trial per build, seed {metric['seed']}: {drawn})"),
        ("worst", f"{metric['worst']:.4f}",
         f"(per example, the worst of all {metric['n_trials']} trials -- no combinations)"),
        ("average", f"{average(buckets):.4f}   std {across_build_spread(buckets)[0]:.4f}",
         f"(mean of the per-build trial means, spread across the {k} builds)"),
    ]
    label_width = max(len(row[0]) for row in headline)
    value_width = max(len(row[1]) for row in headline)
    lines = [
        "",
        *(f"{label:<{label_width}}   {value:<{value_width}}   {note}"
          for label, value, note in headline),
        "",
        f"{k} builds of {counts} trial(s), {metric['n_combinations']:,} combinations, "
        f"{metric['n_examples']} examples",
        f"the combination values range {metric['lowest']:.4f} to {metric['highest']:.4f}",
        "",
    ]
    for i, (ids, mean) in enumerate(zip(buckets, build_means(buckets), strict=True)):
        if ids:
            scores = "  ".join(f"{t.score:.3f}" for t in ids)
            lines.append(f"  build {i}  mean {mean:.3f}   {scores}")
            lines += [f"              {t.run_id}" for t in ids]
        else:
            lines.append(f"  build {i}  mean {mean:.3f}   no trials given: scores "
                         f"{NO_EVAL_SCORE:.3f} on every example")
    if per_example:
        rows = sorted(per_example.items(), key=lambda kv: (kv[1]["score"], kv[0]))
        width = max(len(c) for c in per_example)
        lines += ["", "  per example, worst first -- each column's mean is its headline above:",
                  f"    {'':<{width}}  {f'score^{k}':>8}  {'one draw':>8}  {'worst':>8}"]
        lines += [f"    {case:<{width}}  {v['score']:>8.4f}  {v['one_draw']:>8.4f}  "
                  f"{v['worst']:>8.4f}" for case, v in rows]
    if warnings:
        lines += [""] + [f"  ! {w}" for w in warnings]
    lines += ["",
              f"  score^{k} is the mean, over every way of keeping one trial per build, of the "
              "per-example worst kept",
              "  score averaged over the examples. It falls as the number of builds rises, so "
              "compare it only with",
              f"  another score^{k}. The std is over those combination values, not a standard "
              "error.",
              ""]
    return "\n".join(lines)


def parse_buckets(text: str) -> list[list[str]]:
    """A JSON list of lists of run ids. Raises ValueError with what was wrong."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("expected a non-empty list of lists, one list per build")
    out = []
    for i, group in enumerate(raw):
        if not isinstance(group, list) or any(not isinstance(r, str) for r in group):
            raise ValueError(f"build {i} is not a list of run id strings")
        out.append([r.strip() for r in group if r.strip()])
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="score^k over buckets of eval trials, one bucket per build.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="One --build per build. Pass --build with no run ids for a build that produced "
               "nothing; it scores 0 on every example.")
    p.add_argument("buckets_json", nargs="?", metavar="JSON",
                   help='a JSON list of lists, e.g. \'[["t1","t2"],["t3"]]\', or `-` for stdin')
    p.add_argument("--build", action="append", nargs="*", metavar="RUN_ID", dest="builds",
                   help="one build's eval trial run ids; repeat per build")
    p.add_argument("--runs-dir", type=pathlib.Path, default=LOCAL_RUNS,
                   help=f"run directory read before the mirror (default: {LOCAL_RUNS})")
    p.add_argument("--bucket", default=BUCKET, help=f"mirror bucket (default: {BUCKET})")
    p.add_argument("--prefix", default=PREFIX, help=f"mirror run prefix (default: {PREFIX})")
    p.add_argument("--cache", type=pathlib.Path, default=CACHE,
                   help=f"where mirrored reports are kept (default: {CACHE})")
    p.add_argument("--refresh", action="store_true", help="re-fetch reports already cached")
    p.add_argument("--seed", type=int, default=RANDOM_PICK_SEED,
                   help=f"which single draw to report (default: {RANDOM_PICK_SEED})")
    p.add_argument("--per-example", action="store_true",
                   help="also print the score for every example, worst first")
    p.add_argument("--json", type=pathlib.Path, metavar="PATH", help="also write the result as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.builds and args.buckets_json:
        print("give either --build groups or one JSON argument, not both", file=sys.stderr)
        return EXIT_NO_BUILD
    if args.builds:
        buckets = [[r.strip() for r in ids if r.strip()] for ids in args.builds]
    elif args.buckets_json:
        try:
            buckets = parse_buckets(sys.stdin.read() if args.buckets_json == "-"
                                    else args.buckets_json)
        except ValueError as exc:
            print(f"could not read the buckets: {exc}", file=sys.stderr)
            return EXIT_NO_BUILD
    else:
        print("no build given: pass --build per build, or a JSON list of lists", file=sys.stderr)
        return EXIT_NO_BUILD

    runs_dir = args.runs_dir if args.runs_dir and args.runs_dir.is_dir() else None
    try:
        loaded, problems = load_buckets(buckets, runs_dir, args.cache, args.bucket, args.prefix,
                                        args.refresh)
    except MirrorError as exc:
        print(f"the run mirror could not be read, so no metric is reported: {exc}", file=sys.stderr)
        return EXIT_MIRROR
    if problems:
        print("these runs could not be used, so no metric is reported -- drop them from the "
              "buckets, or pass an empty --build for a build that produced nothing:",
              file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_UNREADABLE

    case_ids, vectors, warnings = align(loaded)
    try:
        metric = score_power(vectors, seed=args.seed)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_TOO_MANY

    # One entry per example, carrying the value behind each of the three headlines. Keyed on the
    # example so a reader can ask "what happened to this case" in one lookup, which is the question
    # these are for; each headline is the mean of its own column.
    per_example = {
        case: {"score": score, "one_draw": draw, "worst": worst}
        for case, score, draw, worst in zip(case_ids, metric["per_example"],
                                            metric["per_example_draw"],
                                            metric["per_example_worst"], strict=False)
    }
    print(report(loaded, metric, warnings, per_example if args.per_example else None))

    if args.json:
        payload = {
            "generated_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "k": metric["k"],
            "score": metric["score"],
            "std": metric["std"],
            "worst": metric["worst"],
            "n_trials": metric["n_trials"],
            "random_pick": metric["random_pick"],
            "random_pick_run_ids": [ids[t].run_id if ids else None
                                    for ids, t in zip(loaded, metric["random_pick_trials"],
                                                      strict=True)],
            "seed": metric["seed"],
            "average": average(loaded),
            # How far the builds sit from that average, before any minimum is taken. Both forms,
            # because the std is what compares with the scores and the variance is what adds up.
            "average_std_across_builds": across_build_spread(loaded)[0],
            "average_variance_across_builds": across_build_spread(loaded)[1],
            "n_combinations": metric["n_combinations"],
            "n_examples": metric["n_examples"],
            "builds": [{"run_ids": [t.run_id for t in ids],
                        "scores": [t.score for t in ids],
                        "mean": mean}
                       for ids, mean in zip(loaded, build_means(loaded), strict=True)],
            "per_example": per_example,
            "warnings": warnings,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json}", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
