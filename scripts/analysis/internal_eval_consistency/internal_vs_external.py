#!/usr/bin/env python
"""Does the agent's own grade predict the real one? Internal eval against external, per deploy.

`trend.py` answers whether the external eval agrees the artifact improved. This answers the question it
explicitly declines: whether the agent's OWN eval -- the thing the optimization loop actually steers on
-- tracks the external score at all. That matters before anyone tunes against the internal number.

TWO INSTRUMENTS, NOT TWO RUNS OF ONE. Keeping them apart is the whole discipline here:

  internal   the agent's own eval, invented during the build -- a handful of cases it wrote itself,
             judged by an LLM it configured. Recorded for free at build time in the run's agent
             filesystem, at `.appsci/optimize/score_history.json`.
  external   the benchmark's held-out block with the real environment oracle, produced afterwards by
             `score_checkpoints.py` re-deploying each saved checkpoint. Minutes of Docker and model
             time per point.

EACH BUILD WRITES A DIFFERENT INTERNAL EXAM, so internal LEVELS do not compare across builds: one
build's 1.00 on five self-written cases is not another's 0.97 on ten. Only shape and rank within a
build mean anything. That is why every build gets its own correlation and no pooled fit is drawn --
pooling would be three different rulers on one axis.

THE JOIN IS BY DEPLOY ORDINAL, NOT CONTENT HASH. Both sides carry both, and the hash is the more
obvious key, but it is WRONG here: the agent re-deploys programs it has already built (a roll-back
restores byte-identical code), so a hash can name two deploys with two different external scores.
Keying on it silently drops points -- it cost one of six on the first run of this analysis.

ONLY FULL-BLOCK RECORDS ARE READ. Anything scored at a smaller `--limit` is excluded rather than
mixed in, the same refusal `trend.py` makes, because points drawn from different case sets are not one
measurement.

Usage:
    internal_vs_external.py                     # every run with both halves on disk
    internal_vs_external.py --run-dir PATH      # a specific fetched run. Repeatable.
    internal_vs_external.py --limit N           # read records scored at this limit instead of the full block
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import pathlib
import statistics
import sys


def _repo_root() -> pathlib.Path:
    """The repo root, found by marker rather than by counting parents.

    `trend.py`'s own note applies: a hardcoded `parents[N]` survives a file being moved one level and
    then resolves somewhere wrong, which fails later than the mistake.
    """
    here = pathlib.Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / "tasks").is_dir():
            return candidate
    raise SystemExit("could not locate the repo root from this file")


REPO = _repo_root()
FETCH_ROOT = REPO / "runs" / "checkpoint_scoring"
OUT_ROOT = REPO / "scripts" / "_out" / "internal_vs_external"

#: Where the agent leaves its own score history inside the mirrored run. `score_checkpoints.py` does not
#: fetch this -- it only needs the checkpoints -- so a run fetched purely for external scoring will not
#: have it, and that is reported as a missing half rather than as a zero.
HISTORY_RELPATH = pathlib.Path("agent_fs/appsci_agent/workspace/.appsci/optimize/score_history.json")

SKIP_NO_EXTERNAL = "no_external_scores"
SKIP_NO_INTERNAL = "no_internal_history"
SKIP_NO_OVERLAP = "no_shared_deploys"
SKIP_TOO_FEW = "fewer_than_two_points"


def read_external_repeats(run_dir: pathlib.Path,
                          limit: int | None) -> dict[int, tuple[list[float], str, bool]]:
    """{deploy ordinal: ([every repeat's score], short hash, is_submission)} for scored records.

    Keeps the repeats APART rather than averaging on the way in, because they are the material for the
    noise floor. Averaging here -- which this did first -- also averaged the running mean pairwise, so a
    capture scored three times weighted its last repeat at one half rather than one third.

    `limit=None` means the full block, which the records spell as a null limit. Excluded and unservable
    verdicts carry no number by design and are skipped rather than read as zero -- `trend.py` owns that
    distinction and this tool must not quietly disagree with it.
    """
    out: dict[int, tuple[list[float], str, bool]] = {}
    scores_dir = run_dir / "checkpoint_scores"
    if not scores_dir.is_dir():
        return out
    for path in sorted(scores_dir.glob("*__r*.json")):
        if path.name.endswith(".report.json"):
            continue
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("verdict") != "scored" or rec.get("score") is None:
            continue
        if rec.get("limit") != limit:
            continue
        capture = rec.get("capture") or {}
        ordinal = capture.get("ordinal")
        if ordinal is None:
            continue
        entry = out.setdefault(int(ordinal), ([], str(capture.get("content_hash", ""))[:8],
                                              bool(capture.get("is_submission"))))
        entry[0].append(float(rec["score"]))
    return out


def read_external(run_dir: pathlib.Path, limit: int | None) -> dict[int, tuple[float, str, bool]]:
    """{deploy ordinal: (mean score across repeats, short hash, is_submission)}."""
    return {ordinal: (statistics.mean(scores), content_hash, is_submission)
            for ordinal, (scores, content_hash, is_submission)
            in read_external_repeats(run_dir, limit).items()}


def read_internal(run_dir: pathlib.Path) -> dict[int, float]:
    """{deploy ordinal: the agent's own headline score for that deploy}.

    A record whose `checkpoint` is null is honest rather than broken -- the agent could not tell which
    files were running -- and simply has no ordinal to join on.
    """
    path = run_dir / HISTORY_RELPATH
    if not path.exists():
        return {}
    try:
        records = (json.loads(path.read_text()) or {}).get("records") or []
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[int, float] = {}
    for rec in records:
        checkpoint = rec.get("checkpoint") or {}
        ordinal, metric = checkpoint.get("ordinal"), rec.get("headline_metric")
        if ordinal is not None and isinstance(metric, (int, float)):
            out[int(ordinal)] = float(metric)
    return out


def read_verdicts(run_dir: pathlib.Path) -> list[dict]:
    """The loop's OWN accept/reject decisions, in the order it made them.

    READ, NOT RE-DERIVED. The optimize tool writes `verdict` onto each attempt at the moment it decides,
    against `score_to_beat + MIN_IMPROVEMENT`, and its own docstring says why a later reader must not
    reconstruct it from adjacent scores: "a later reader re-deriving it from adjacent scores would have
    to re-implement that rule and would get it wrong the moment the rule moves". `decisions()` above does
    exactly that re-derivation, which is why both are reported -- one is what the loop DID, the other is
    what the two evals SAID, and they answer different questions.

    `incumbent` is the deploy the change was measured against: the previous record's checkpoint, which is
    what was in front of traffic before this one went up.

    `reverted` is carried separately from the verdict because they can disagree -- a change judged no
    improvement whose snapshot could not be restored is rejected AND still running.
    """
    path = run_dir / HISTORY_RELPATH
    if not path.exists():
        return []
    try:
        records = (json.loads(path.read_text()) or {}).get("records") or []
    except (OSError, json.JSONDecodeError):
        return []
    out, previous = [], None
    for rec in records:
        ordinal = (rec.get("checkpoint") or {}).get("ordinal")
        verdict = rec.get("verdict")
        if verdict and ordinal is not None and previous is not None:
            out.append({
                "deploy": int(ordinal),
                "incumbent": previous,
                "verdict": verdict,
                "reverted": bool(rec.get("reverted")),
                "technique": rec.get("technique"),
                "note": rec.get("change_note"),
                "internal": rec.get("headline_metric"),
            })
        if ordinal is not None:
            previous = int(ordinal)
    return out


#: How the loop's decision compares with the real eval's opinion of the same change.
VERDICT_CORRECT_KEEP = "correct_keep"
VERDICT_WRONG_KEEP = "wrong_keep"
VERDICT_CORRECT_REJECT = "correct_reject"
VERDICT_WRONG_REJECT = "wrong_reject"


def judge_verdicts(verdicts: list[dict], external: dict[int, tuple[float, str, bool]],
                   noise_sd: float | None) -> list[dict]:
    """Was each of the loop's own decisions right, according to the external eval?

    The four outcomes are not equally interesting. A WRONG REJECT is the expensive one: the loop built
    something genuinely better and threw it away, and the real gain it gave up is a number with a cost
    attached. cci b0 did this twice, discarding +0.043 and +0.086 -- the second being the best program it
    ever built -- because its saturated exam expressed the gain as 0.008 against a 0.01 threshold.

    `within_noise` is carried because several decisions are about differences the external eval cannot
    resolve either; counting those as the loop's mistakes would be blaming it for our own resolution.
    """
    out = []
    for v in verdicts:
        if v["deploy"] not in external or v["incumbent"] not in external:
            continue
        delta = external[v["deploy"]][0] - external[v["incumbent"]][0]
        kept = v["verdict"] == "kept"
        if kept:
            kind = VERDICT_CORRECT_KEEP if delta > 0 else VERDICT_WRONG_KEEP
        else:
            kind = VERDICT_CORRECT_REJECT if delta < 0 else VERDICT_WRONG_REJECT
        out.append({**v, "external_delta": delta, "outcome": kind,
                    "within_noise": bool(noise_sd is not None and abs(delta) < noise_sd)})
    return out


def external_repeat_sd(run_dir: pathlib.Path, limit: int | None) -> float | None:
    """External sd of ONE score, pooled across captures that were scored more than once.

    The DIRECT estimate, and the one to prefer whenever repeats exist: re-scoring the same capture is
    exactly a repeated measurement, with nothing else varying. The identical-program fallback below
    exists only because a sweep at one repeat has no repeated measurement to use.

    Preferring it also fixes a task-shaped hole. The fallback reads per-case PASS/FAIL, which only
    exists for a task declaring a binary reward objective; order_to_cash declares none, so its reports
    carry no `views.reward` at all and the fallback returns None -- reporting an unmeasured noise floor
    on a sweep that had three repeats per capture and could pin it to four decimal places.

    Returns None when no capture was scored twice.
    """
    variances = []
    for scores, _hash, _is_sub in read_external_repeats(run_dir, limit).values():
        if len(scores) >= 2:
            variances.append(statistics.variance(scores))
    if not variances:
        return None
    return math.sqrt(statistics.mean(variances))


def external_noise_sd(run_dir: pathlib.Path, limit: int | None) -> float | None:
    """External sd of ONE score: from repeats where they exist, else from byte-identical programs."""
    direct = external_repeat_sd(run_dir, limit)
    if direct is not None:
        return direct
    return _noise_from_identical_programs(run_dir, limit)


def _noise_from_identical_programs(run_dir: pathlib.Path, limit: int | None) -> float | None:
    """External sd of ONE score, measured from re-scorings of byte-identical programs.

    Two deploys sharing a content hash are the same program, so every difference between them is the
    instrument rather than the artifact. Taking that from the score difference alone understates it
    badly, because per-case flips cancel in the total: on one airline build the identical pairs differed
    by 0.02 in score while disagreeing on 5 and 7 of 50 individual cases. So this counts DISAGREEING
    CASES, which do not cancel, and converts that to a score sd.

    Returns None when no program was scored twice -- an unmeasured floor, which the caller must report
    as unmeasured rather than assume is small.
    """
    scores_dir = run_dir / "checkpoint_scores"
    if not scores_dir.is_dir():
        return None
    passes: dict[int, dict] = {}
    by_hash: dict[str, list[int]] = {}
    for path in sorted(scores_dir.glob("*__r*.json")):
        if path.name.endswith(".report.json"):
            continue
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("verdict") != "scored" or rec.get("limit") != limit:
            continue
        report_path = scores_dir / str(rec.get("report") or "")
        if not report_path.exists():
            continue
        try:
            report = json.loads(report_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        per_case = ((report.get("views") or {}).get("reward") or {}).get("passes")
        capture = rec.get("capture") or {}
        ordinal = capture.get("ordinal")
        if per_case and ordinal is not None:
            passes[int(ordinal)] = per_case
            by_hash.setdefault(str(capture.get("content_hash", "")), []).append(int(ordinal))

    flips = []
    for ordinals in by_hash.values():
        for i in range(len(ordinals)):
            for j in range(i + 1, len(ordinals)):
                a, b = passes[ordinals[i]], passes[ordinals[j]]
                shared = set(a) & set(b)
                if shared:
                    flips.append(sum(1 for case in shared if a[case] != b[case]))
    if not flips:
        return None
    n_cases = len(next(iter(passes.values())))
    if not n_cases:
        return None
    # sd of the DIFFERENCE between two runs is sqrt(mean disagreements)/n; one score is that over root 2.
    return (math.sqrt(statistics.mean(flips)) / n_cases) / math.sqrt(2)


def pearson(a: list[float], b: list[float]) -> float:
    if len(a) < 2:
        return float("nan")
    ma, mb = statistics.mean(a), statistics.mean(b)
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if not da or not db:
        # One axis never moved. Undefined, not zero -- reporting 0.0 would read as "measured, no
        # relationship" when nothing was measured at all.
        return float("nan")
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (da * db)


def spearman(a: list[float], b: list[float]) -> float:
    """Rank correlation, with TIES SHARING A MIDRANK rather than being ordered arbitrarily.

    Midranks are not a refinement here, they are the difference between a number and a fabricated one.
    External scores tie constantly -- the reward is binary per case, so a 50-case block moves in steps
    of 0.02 and repeated values are the norm. Breaking those ties by list position invents an ordering
    out of the order the deploys happen to be stored in, and then measures agreement with it: six b1
    deploys whose external scores tie three ways reported +0.60 that way, and an axis where EVERY value
    was identical reported +1.00 -- a perfect correlation with a constant.

    With midranks a fully tied axis becomes constant, which `pearson` reports as undefined, which is the
    truth about it.
    """
    def ranked(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        position = 0
        while position < len(order):
            end = position
            while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
                end += 1
            midrank = (position + end) / 2
            for index in order[position:end + 1]:
                out[index] = midrank
            position = end + 1
        return out

    return pearson(ranked(a), ranked(b))


def format_sd(sd: float | None) -> str:
    """A noise floor at a precision that does not round it away.

    Fixed three decimals is fine for a task whose reward is binary per case -- airline's sd is 0.035 --
    and prints order_to_cash's 0.00024 as "0.000", which reads as no noise at all on the one task
    precise enough to resolve small real differences. Small values therefore keep two significant
    figures instead.
    """
    if sd is None:
        return "unmeasured"
    if sd == 0:
        return "0"
    return format(sd, ".3f") if sd >= 0.001 else format(sd, ".2g")


def kendall_tau_b(a: list[float], b: list[float]) -> float:
    """Kendall's tau-b: concordant minus discordant pairs, with ties in the denominator.

    Preferred over Spearman on this data because it handles ties EXPLICITLY rather than by convention,
    and because it reads as something: the numerator is the excess of correctly-ordered pairs over
    incorrectly-ordered ones, which is close to the question the optimization loop is really asking.
    External rewards are binary per case, so a 50-case block moves in steps of 0.02 and ties are
    everywhere -- three of one build's six deploys sit at the same score.

    ITS CEILING IS NOT 1.0 WHEN THE TWO AXES TIE DIFFERENTLY, which is a trap when reading the number.
    Tied pairs leave the numerator but enter the denominator once per side, under a square root, so the
    attainable maximum shrinks with the tie structure: three deploys whose external scores tie once cap
    tau-b at 0.816 even when every orderable pair agrees. Compare a value against what this data COULD
    have produced, not against 1.
    """
    n = len(a)
    if n < 2:
        return float("nan")
    concordant = discordant = 0
    ties_a = ties_b = 0
    for i in range(n):
        for j in range(i + 1, n):
            da, db = a[i] - a[j], b[i] - b[j]
            if da == 0 and db == 0:
                ties_a += 1
                ties_b += 1
            elif da == 0:
                ties_a += 1
            elif db == 0:
                ties_b += 1
            elif (da > 0) == (db > 0):
                concordant += 1
            else:
                discordant += 1
    n0 = n * (n - 1) / 2
    denominator = math.sqrt((n0 - ties_a) * (n0 - ties_b))
    if denominator == 0:
        # Every pair tied on one side. No pair can be ordered, so there is nothing to agree about.
        return float("nan")
    return (concordant - discordant) / denominator


def decisions(points: list[dict], noise_sd: float | None) -> list[dict]:
    """One entry per change the loop made: did the two instruments agree on its DIRECTION?

    This is the unit the optimization loop actually operates in -- it does not rank deploys, it accepts
    or rejects one change at a time -- and unlike a score level it POOLS ACROSS BUILDS. Each build writes
    its own internal exam so the levels are incomparable, but "the internal eval said better and the
    external eval agreed" is the same proposition in every build, which is what lets a sign test over
    many builds eventually have power that no single build can.

    An external delta of exactly zero is recorded as `tied`, not as disagreement. The loop can only be
    wrong about a direction that exists, and folding ties into the failures overstates the error rate --
    it read 2/5 before this distinction, where the honest figure is 2 of 4 resolvable changes.

    `within_noise` marks a change whose external delta is smaller than one standard deviation of the
    measurement. Those are counted, because dropping them would select on the outcome, but a reader
    should know how much of the agreement is being decided by the instrument.
    """
    out = []
    by_ordinal = {p["deploy"]: p for p in points}
    seen: set[str] = set()
    first = min(by_ordinal) if by_ordinal else None
    if first is not None:
        seen.add(by_ordinal[first].get("hash"))
    for ordinal in sorted(by_ordinal):
        before = by_ordinal.get(ordinal - 1)
        if before is None:
            continue
        after = by_ordinal[ordinal]
        # A DECISION IS A TRANSITION TO A PROGRAM NOT SEEN BEFORE. Anything else is the loop putting an
        # incumbent back, which it does by restoring the source tree and redeploying it -- and that is
        # not a second judgement, it is the other side of the one already counted when the rejected
        # program was first tried. Counting both scores the same event twice, once in each direction,
        # which on a build that reverts twice fills half the denominator with non-events.
        # Only when the points actually carry program identity. Without it there is no way to tell a
        # revert from a revision, and silently dropping every transition would be far worse than
        # counting one twice -- a caller with no hashes would get an empty decision list and no reason.
        if after.get("hash") is not None and after["hash"] in seen:
            continue
        internal_delta = after["internal"] - before["internal"]
        external_delta = after["external"] - before["external"]
        if external_delta == 0 or internal_delta == 0:
            verdict = "tied"
        elif (internal_delta > 0) == (external_delta > 0):
            verdict = "agree"
        else:
            verdict = "disagree"
        out.append({
            "cycle": f"{ordinal - 1}->{ordinal}",
            "from": ordinal - 1, "to": ordinal,
            "internal_delta": internal_delta,
            "external_delta": external_delta,
            "verdict": verdict,
            "within_noise": bool(noise_sd is not None and abs(external_delta) < noise_sd),
        })
        seen.add(after.get("hash"))
    return out


def regret(external: dict[int, tuple[float, str, bool]]) -> dict | None:
    """What the loop gave up: the best version it BUILT, minus the one it SUBMITTED.

    The operationally meaningful number, and the one a reader without any statistics can act on. It also
    pools across builds in a way a correlation cannot, since it is a difference within one instrument.

    None when no capture is flagged as the submission -- a run whose submitted artifact was never
    identified cannot have this computed, and guessing "the last one" would be wrong exactly when the
    loop rolled back.
    """
    submissions = [o for o, (_s, _h, is_sub) in external.items() if is_sub]
    if not submissions or not external:
        return None
    submitted = submissions[-1]

    # Compare PROGRAMS, not deploys. The submission is usually a copy of an earlier deploy, so treating
    # the two as separate candidates measures the instrument against itself: on two cci builds that
    # reported a regret of +0.002 and +0.007 where the true answer is exactly zero, because the loop
    # shipped the very program it had picked. Grouping by content hash makes "shipped its best" report 0
    # by construction, and averaging every measurement of a program also gives each candidate the
    # benefit of all its repeats rather than whichever deploy happened to be its last.
    by_program: dict[str, list[float]] = {}
    for score, content_hash, _is_sub in external.values():
        by_program.setdefault(content_hash, []).append(score)
    means = {h: statistics.mean(v) for h, v in by_program.items()}

    submitted_hash = external[submitted][1]
    best_hash = max(means, key=lambda h: means[h])
    best = next(o for o in sorted(external) if external[o][1] == best_hash)
    return {
        "best_deploy": best, "best_score": means[best_hash],
        "submitted_deploy": submitted, "submitted_score": means[submitted_hash],
        "submitted_is_best_program": submitted_hash == best_hash,
        "regret": means[best_hash] - means[submitted_hash],
    }


def by_program(points: list[dict]) -> list[dict]:
    """Collapse deploys to distinct PROGRAMS, which is the unit a correlation may be computed on.

    Deploying one program twice is a repeated measure, not a second candidate. Correlating over deploys
    lets a program that happened to be deployed three times outvote one deployed once, in every pairwise
    comparison it takes part in -- and the agent redeploys constantly, because rejecting a change means
    restoring the previous source and putting it back in front of traffic. On cci b0 that inflation ran
    the other way and HID the result: six deploys of three programs reported tau-b +0.36, where the three
    programs themselves rank +1.00. The internal eval had ordered every program correctly.

    This is the rule `elicitation_vs_score.py` states in its own header -- "UNIT OF ANALYSIS IS THE
    BUILD, never the trial ... trials are repeated measures" -- applied one level down.

    Each program's two scores are the mean over its deploys, so every measurement still informs the
    answer; it just does not get a second vote.
    """
    grouped: dict[str, list[dict]] = {}
    for point in points:
        grouped.setdefault(point["hash"], []).append(point)
    out = []
    for content_hash, members in grouped.items():
        first = min(members, key=lambda p: p["deploy"])
        out.append({
            "hash": content_hash,
            "program": first.get("program"),
            "internal": statistics.mean([p["internal"] for p in members]),
            "external": statistics.mean([p["external"] for p in members]),
            "deploys": sorted(p["deploy"] for p in members),
            "first_deploy": first["deploy"],
        })
    return sorted(out, key=lambda p: p["first_deploy"])


def collect(run_dirs: list[pathlib.Path], limit: int | None) -> tuple[dict, dict[str, list[str]]]:
    series, skipped = {}, {}

    def skip(reason: str, name: str) -> None:
        skipped.setdefault(reason, []).append(name)

    for run_dir in run_dirs:
        name = run_dir.name
        external, internal = read_external(run_dir, limit), read_internal(run_dir)
        if not external:
            skip(SKIP_NO_EXTERNAL, name)
            continue
        if not internal:
            skip(SKIP_NO_INTERNAL, name)
            continue
        shared = sorted(set(external) & set(internal))
        if not shared:
            skip(SKIP_NO_OVERLAP, name)
            continue
        if len(shared) < 2:
            skip(SKIP_TOO_FEW, name)
            continue
        # Letters first: both the chart and the program-level statistics need them, and assigning them
        # by first appearance over EVERY scored deploy keeps the naming stable whether or not a given
        # deploy happens to carry an internal score.
        letters: dict[str, str] = {}
        for ordinal in sorted(external):
            content_hash = external[ordinal][1]
            if content_hash not in letters:
                letters[content_hash] = chr(ord("A") + len(letters))
        submitted_hash = next((h for _s, h, is_sub in external.values() if is_sub), None)

        points = [{"deploy": o, "internal": internal[o], "external": external[o][0],
                   "hash": external[o][1], "program": letters[external[o][1]],
                   "is_submission": external[o][2]} for o in shared]
        sd = external_noise_sd(run_dir, limit)
        made = decisions(points, sd)
        judged = judge_verdicts(read_verdicts(run_dir), external, sd)

        # CORRELATED OVER PROGRAMS, NOT DEPLOYS -- see `by_program`. The chart still draws every deploy;
        # only the statistics dedupe, because a redeployed program is the same candidate measured twice.
        programs = by_program(points)
        xs = [p["internal"] for p in programs]
        ys = [p["external"] for p in programs]

        # THE DEPLOY SEQUENCE IS NOT A LADDER. The agent re-deploys programs it has already built, so a
        # run of six deploys can be three programs: build A, try B, revert to A, try C, revert to A,
        # ship A. Lettering programs by first appearance makes a repeat visible as a repeated letter,
        # which is the difference between "five candidates" and "two rejected experiments".
        #
        # Drawn over EVERY scored deploy, not just the paired ones. The external score exists for all of
        # them; only the internal side has gaps -- the submitted copy never carries one, and neither do
        # deploys the agent's history skipped. Plotting only the intersection silently deleted deploys
        # that really happened (cci b1 lost three of nine) and left an x-axis with unexplained holes.
        external_all = [
            {"deploy": o, "external": external[o][0], "hash": external[o][1],
             "program": letters[external[o][1]],
             "is_submission": external[o][2],
             "is_submitted_program": external[o][1] == submitted_hash,
             "has_internal": o in internal}
            for o in sorted(external)
        ]
        series[name] = {
            # Leading underscore: where this series came from, for the per-case figure. Stripped before
            # stats.json is written, so the published record stays machine-readable data only.
            "_run_dir": str(run_dir), "_limit": limit,
            # `n` is the number of distinct PROGRAMS the correlation was computed on. `n_deploys` is how
            # many times they were put in front of traffic, which is a different and larger number.
            "n": len(programs),
            "n_deploys": len(points),
            "pearson": pearson(xs, ys),
            "spearman": spearman(xs, ys),
            "kendall_tau_b": kendall_tau_b(xs, ys),
            "external_noise_sd": sd,
            "programs": [{k: v for k, v in p.items() if k != "hash"} for p in programs],
            "internal_first": points[0]["internal"], "internal_last": points[-1]["internal"],
            "external_first": points[0]["external"], "external_last": points[-1]["external"],
            "external_all": external_all,
            "programs_built": len(letters),
            "programs_abandoned": sorted({e["program"] for e in external_all
                                          if not e["is_submitted_program"]}),
            "shipped_program": letters.get(submitted_hash) if submitted_hash else None,
            # What the LOOP did, read from its own history -- kept alongside `decisions`, which is what
            # the two evals SAID. They answer different questions and can disagree.
            "loop_verdicts": judged,
            "loop_correct_keep": sum(1 for v in judged if v["outcome"] == VERDICT_CORRECT_KEEP),
            "loop_wrong_keep": sum(1 for v in judged if v["outcome"] == VERDICT_WRONG_KEEP),
            "loop_correct_reject": sum(1 for v in judged if v["outcome"] == VERDICT_CORRECT_REJECT),
            "loop_wrong_reject": sum(1 for v in judged if v["outcome"] == VERDICT_WRONG_REJECT),
            "loop_value_discarded": sum(v["external_delta"] for v in judged
                                        if v["outcome"] == VERDICT_WRONG_REJECT),
            "decisions": made,
            "decisions_agree": sum(1 for d in made if d["verdict"] == "agree"),
            "decisions_disagree": sum(1 for d in made if d["verdict"] == "disagree"),
            "decisions_tied": sum(1 for d in made if d["verdict"] == "tied"),
            "decisions_within_noise": sum(1 for d in made if d["within_noise"]),
            "regret": regret(external),
            "points": points,
        }
    return series, skipped


def task_and_model(run_ids: list[str]) -> str:
    """"airline_rebooking · claude-opus-4-8" when every series shares them, else "".

    A figure that does not name its task and model cannot be read away from the directory it was written
    into, which is where a submission figure always ends up.
    """
    pairs = set()
    for run_id in run_ids:
        head, sep, _rest = run_id.partition("_claude-")
        if not sep:
            head, sep, _rest = run_id.partition("_gpt-")
        if not sep:
            return ""
        model = run_id[len(head) + 1:].split("_b")[0]
        pairs.add((head, model))
    if len(pairs) != 1:
        return ""
    task, model = pairs.pop()
    return f"{task} · {model}"


#: {run id: label} for the runs in THIS invocation, set by `assign_labels`. Empty means every label is
#: the bare build index, which is what a single run or a test sees.
_LABELS: dict[str, str] = {}


def _build_index(run_id: str) -> str:
    for part in run_id.split("_"):
        if len(part) == 2 and part[0] == "b" and part[1].isdigit():
            return part
    return run_id[:18]


def short_label(run_id: str) -> str:
    """`..._b1_l3_...` -> `b1`. The build index is what distinguishes these in every conversation.

    Unless two runs in the same invocation share it -- see `assign_labels`.
    """
    return _LABELS.get(run_id) or _build_index(run_id)


def assign_labels(run_ids) -> dict[str, str]:
    """Label each run by its build index, adding the launch stamp only where two runs share one.

    The index is not unique: two separate launches can both be `b0` (banking_support's opus builds of
    09-20 and 09-21 are). Left alone, the second silently overwrites the first's `cases__b0.png` and the
    legend and table show `b0` twice with no way to tell which line is which. A unique index keeps its
    bare label, so outputs for runs that never collided are unchanged. `-MMDD` is tried first because it
    is what a reader recognises; the full `-YYYYMMDD_HHMMSS` stamp is the fallback for same-day launches.
    """
    ids = sorted(set(run_ids))
    by_index: dict[str, list[str]] = {}
    for run_id in ids:
        by_index.setdefault(_build_index(run_id), []).append(run_id)

    def stamp(run_id: str) -> tuple[str, str]:
        parts = run_id.split("_")
        for i, part in enumerate(parts[:-1]):
            if len(part) == 8 and part.isdigit() and parts[i + 1].isdigit():
                return part[4:], f"{part}_{parts[i + 1]}"
        return run_id, run_id   # no stamp to go on: the whole id is the only thing guaranteed distinct

    labels: dict[str, str] = {}
    for index, group in by_index.items():
        if len(group) == 1:
            labels[group[0]] = index
            continue
        short = {r: f"{index}-{stamp(r)[0]}" for r in group}
        if len(set(short.values())) < len(group):
            short = {r: f"{index}-{stamp(r)[1]}" for r in group}
        labels.update(short)
    return labels


def read_case_outcomes(run_dir: pathlib.Path, limit: int | None) -> dict[int, dict[str, bool]]:
    """{deploy ordinal: {case id: passed}} from the per-case reports.

    The binary the reward is derived from, per case, which is what makes the aggregate legible: a score
    that barely moves can still be hiding heavy churn underneath, and the two have completely different
    implications for whether another sweep would help.
    """
    out: dict[int, dict[str, bool]] = {}
    scores_dir = run_dir / "checkpoint_scores"
    if not scores_dir.is_dir():
        return out
    for path in sorted(scores_dir.glob("*__r*.json")):
        if path.name.endswith(".report.json"):
            continue
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("verdict") != "scored" or rec.get("limit") != limit:
            continue
        report_path = scores_dir / str(rec.get("report") or "")
        if not report_path.exists():
            continue
        try:
            report = json.loads(report_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        passes = ((report.get("views") or {}).get("reward") or {}).get("passes")
        ordinal = (rec.get("capture") or {}).get("ordinal")
        if passes and ordinal is not None and int(ordinal) not in out:
            out[int(ordinal)] = {str(k): bool(v) for k, v in passes.items()}
    return out


def draw_cases(run_dir: pathlib.Path, series_entry: dict, out_dir: pathlib.Path,
               name: str) -> pathlib.Path | None:
    """Per-case pass/fail across deploys, for ONE build.

    This is the evidence behind the noise band, and it is worth drawing because the aggregate hides it.
    Cases are ordered by how often they pass, which separates three populations the score cannot: the
    ones that always pass, the ones that never do (stable headroom, and the only thing here another
    sweep can act on), and the churning middle that is mostly instrument.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import ListedColormap
    except ImportError:
        return None

    outcomes = read_case_outcomes(run_dir, series_entry.get("_limit"))
    ordinals = sorted(outcomes)
    if len(ordinals) < 2:
        return None
    cases = sorted(set().union(*(set(outcomes[o]) for o in ordinals)),
                   key=lambda c: (-sum(outcomes[o].get(c, False) for o in ordinals), c))
    grid = [[1 if outcomes[o].get(c) else 0 for o in ordinals] for c in cases]

    never = [c for c in cases if not any(outcomes[o].get(c) for o in ordinals)]
    always = [c for c in cases if all(outcomes[o].get(c) for o in ordinals)]
    churn = len(cases) - len(never) - len(always)

    fig, ax = plt.subplots(figsize=(max(5.5, .9 * len(ordinals) + 3.0), max(4.5, .155 * len(cases) + 1.5)))
    ax.imshow(grid, aspect="auto", cmap=ListedColormap(["#f2f2f2", "#3b7dd8"]), vmin=0, vmax=1)
    ax.set_xticks(range(len(ordinals)))
    ax.set_xticklabels([f"#{o}" for o in ordinals], fontsize=8)
    ax.set_yticks(range(len(cases)))
    ax.set_yticklabels([c.replace("tau2_airline_", "") for c in cases], fontsize=6)
    ax.set_xlabel("deploy order")
    ax.set_ylabel("eval case")
    ax.set_title(f"{short_label(name)} — per-case outcome across deploys\n"
                 f"{len(always)} always pass · {churn} churn · {len(never)} never pass",
                 fontsize=10)
    for spine in ax.spines.values():
        spine.set_visible(False)
    # Gridlines on BOTH axes, in white. Without the horizontal ones a run of passing cases merges into
    # one solid block and the reader cannot count rows against the labels.
    ax.set_xticks([x - .5 for x in range(1, len(ordinals))], minor=True)
    ax.set_yticks([y - .5 for y in range(1, len(cases))], minor=True)
    ax.grid(which="minor", color="white", lw=.7)
    ax.tick_params(which="minor", length=0)
    fig.tight_layout()
    path = out_dir / f"cases__{short_label(name)}.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def draw_verdicts(series: dict, out_dir: pathlib.Path) -> pathlib.Path | None:
    """What the loop DID with each change, against what the change was actually worth.

    One bar per decision, length = the real score the change moved. Colour says whether the loop's own
    call was right. The asymmetry is the point: a wrong REJECT has a number attached -- the gain it threw
    away -- while a wrong keep merely costs whatever the worse program loses, and several of those sit
    inside the noise floor anyway.

    Drawn from the loop's recorded verdict rather than re-derived from adjacent scores, because the rule
    it decides by (`score_to_beat + MIN_IMPROVEMENT`) lives in the optimize tool and would be wrong here
    the moment it moves.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except ImportError:
        return None

    rows = [(name, v) for name in sorted(series) for v in series[name]["loop_verdicts"]]
    if not rows:
        return None

    palette = {
        VERDICT_CORRECT_KEEP: "#2e9e5b",
        VERDICT_CORRECT_REJECT: "#7fb069",
        VERDICT_WRONG_KEEP: "#e08a3c",
        VERDICT_WRONG_REJECT: "#c0392b",
    }
    pretty = {
        VERDICT_CORRECT_KEEP: "kept, and it was better",
        VERDICT_CORRECT_REJECT: "rejected, and it was worse",
        VERDICT_WRONG_KEEP: "kept, but it was worse",
        VERDICT_WRONG_REJECT: "REJECTED, but it was better",
    }

    rows.sort(key=lambda r: r[1]["external_delta"])
    fig, ax = plt.subplots(figsize=(10.5, max(3.2, .42 * len(rows) + 1.8)))

    # Task prefix only when the chart pools more than one: "b0" is ambiguous across tasks, and
    # redundant within one.
    tasks = {task_and_model([n]).split(" · ")[0] for n in series}
    multi_task = len(tasks) > 1

    labels, values, colors, errors = [], [], [], []
    for name, v in rows:
        prefix = f"{task_and_model([name]).split(' · ')[0][:12]} " if multi_task else ""
        labels.append(f"{prefix}{short_label(name)}  #{v['incumbent']}→#{v['deploy']}  "
                      f"{str(v['technique'] or '—')[:22]}")
        values.append(v["external_delta"])
        colors.append(palette[v["outcome"]])
        # EACH BAR AGAINST ITS OWN INSTRUMENT. A single band drawn at the widest build's noise made a
        # change worth six sigma on a precise task look marginal, because a different task in the same
        # chart happened to be five times noisier. Noise floors here span 0.0005 to 0.035.
        errors.append(series[name]["external_noise_sd"] or 0)

    y = range(len(rows))
    ax.barh(list(y), values, color=colors, height=.62,
            xerr=errors, error_kw=dict(ecolor="#444", elinewidth=1.1, capsize=3, alpha=.75))

    # Anchored past the END OF THE ERROR BAR, not the end of the bar: anchored to the bar the value sat
    # on top of its own whisker and became unreadable exactly where the whisker matters.
    for i, (_name, v) in enumerate(rows):
        delta, err = v["external_delta"], errors[i]
        tip = delta + (err if delta >= 0 else -err)
        ax.annotate(f"{delta:+.3f}", (tip, i), textcoords="offset points",
                    xytext=(7 if delta >= 0 else -7, 0),
                    ha="left" if delta >= 0 else "right",
                    va="center", fontsize=7.5, color="#333")

    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=8)
    ax.axvline(0, color="#333", lw=.9)
    ax.set_xlabel("what the change was really worth  (change in the external score)\n"
                  "error bars are ±1 sd of THAT build's own measurement noise")
    ax.set_title("What the loop did with each change, and whether it was right", fontsize=12)
    ax.grid(axis="x", alpha=.25)
    ax.margins(x=.18)

    order = [VERDICT_CORRECT_KEEP, VERDICT_CORRECT_REJECT, VERDICT_WRONG_KEEP, VERDICT_WRONG_REJECT]
    counts = {k: sum(1 for _n, v in rows if v["outcome"] == k) for k in order}
    ax.legend(handles=[Patch(facecolor=palette[k], label=f"{pretty[k]}  ({counts[k]})")
                       for k in order if counts[k]],
              fontsize=8, loc="lower right", framealpha=.92)

    discarded = sum(v["external_delta"] for _n, v in rows if v["outcome"] == VERDICT_WRONG_REJECT)
    if discarded:
        ax.text(.01, .01, f"total real score thrown away by wrong rejects: {discarded:+.3f}",
                transform=ax.transAxes, fontsize=8, color="#c0392b", va="bottom")

    fig.tight_layout()
    path = out_dir / "loop_decisions.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def draw_decisions(series: dict, out_dir: pathlib.Path) -> pathlib.Path | None:
    """Every change the loop made, as internal delta against external delta.

    The quadrants ARE the result: top-right and bottom-left are changes the two instruments agreed
    about, the other two are changes the internal eval got backwards. That is a more honest picture than
    a correlation, because it shows the disagreements individually instead of averaging them into one
    coefficient that a small sample cannot support.

    The horizontal band is one sd of external measurement noise. A point inside it has an external delta
    the instrument cannot resolve, so its quadrant was decided by a coin flip -- visible here, and
    invisible in any summary statistic.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    entries = [(name, d) for name in sorted(series) for d in series[name]["decisions"]]
    if not entries:
        return None

    palette = ["#3b7dd8", "#d8703b", "#3bd888", "#9b59b6", "#c0392b"]
    colors = {name: palette[i % len(palette)] for i, name in enumerate(sorted(series))}

    fig, ax = plt.subplots(figsize=(8.0, 6.4))
    span = max(max(abs(d["internal_delta"]) for _n, d in entries),
               max(abs(d["external_delta"]) for _n, d in entries)) * 1.35 or 0.1

    ax.axhspan(-span, 0, xmin=.5, color="#c0392b", alpha=.05)
    ax.axhspan(0, span, xmin=0, xmax=.5, color="#c0392b", alpha=.05)
    ax.axhspan(0, span, xmin=.5, color="#2e9e5b", alpha=.05)
    ax.axhspan(-span, 0, xmin=0, xmax=.5, color="#2e9e5b", alpha=.05)

    # The WIDEST build's noise, not the mean: the band is there to answer "could this change have been
    # measured at all", and a mean would draw some builds a band narrower than their own instrument.
    sds = [series[name]["external_noise_sd"] for name in sorted(series)
           if series[name]["external_noise_sd"]]
    if sds:
        sd = max(sds)
        ax.axhspan(-sd, sd, color="#777", alpha=.13, zorder=0)
        ax.text(-span * .96, sd, f"  widest external noise band, ±1 sd = {format_sd(sd)}",
                fontsize=7, color="#555", va="bottom")

    # Changes cluster hard near the origin -- most are small, and one large one sets the scale -- so
    # labels alternate above and below rather than all sitting on top of each other.
    for index, (name, d) in enumerate(sorted(entries, key=lambda e: e[1]["internal_delta"])):
        color = colors[name]
        ax.plot(d["internal_delta"], d["external_delta"], "o", color=color, ms=9,
                mec="white", mew=1.2, zorder=3)
        ax.annotate(f"{short_label(name)} {d['cycle']}",
                    (d["internal_delta"], d["external_delta"]),
                    textcoords="offset points", xytext=(0, 12 if index % 2 else -17),
                    ha="center", fontsize=7, color=color, zorder=4)

    ax.axhline(0, color="#333", lw=.9)
    ax.axvline(0, color="#333", lw=.9)
    ax.set_xlim(-span, span)
    ax.set_ylim(-span, span)
    ax.set_xlabel("change in the agent's own score  (what the loop steered on)")
    ax.set_ylabel("change in the real score")

    totals = [f"{short_label(n)} {series[n]['decisions_agree']}/"
              f"{series[n]['decisions_agree'] + series[n]['decisions_disagree']}"
              for n in sorted(series)]
    ax.set_title("Did the loop's own signal point the right way?\n"
                 "agreed on direction: " + ", ".join(totals), fontsize=11)
    ax.grid(alpha=.2)
    fig.tight_layout()
    path = out_dir / "decisions.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def draw(series: dict, out_dir: pathlib.Path, limit_label: str) -> pathlib.Path | None:
    """One panel: both instruments against deploy order, each build's correlation stated on it.

    A scatter of internal against external is the obvious way to show a correlation and the worse one
    here, for two reasons this data makes plain. The agent re-deploys programs it has already built, so
    points land on top of each other and deploy order -- the only axis along which the loop actually
    acts -- disappears. And with the external spread barely exceeding its own noise, a scatter invites
    reading a slope that is not there. Against deploy order the sequence stays visible, the noise band
    sits at the same scale as the movement, and the correlation is reduced to what it honestly is: one
    number, printed, rather than a shape to be eyeballed.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not installed, so no figure was drawn. The numbers above are complete. "
              "Install it with `pip install -r requirements.txt`.")
        return None

    palette = ["#3b7dd8", "#d8703b", "#3bd888", "#9b59b6", "#c0392b"]
    colors = {name: palette[i % len(palette)] for i, name in enumerate(sorted(series))}

    # The summary table and the legend live OUTSIDE the plotting area, in their own column. Floated
    # inside they sat on the data -- a box in the top right covered b1's internal line exactly where it
    # rises, which is the most important thing on that curve. Nothing should be drawn over the lines.
    fig, (ax, ax_side) = plt.subplots(
        1, 2, figsize=(13.2, 6.2), gridspec_kw={"width_ratios": [2.6, 1.0], "wspace": .04})
    ax_side.axis("off")

    for name in sorted(series):
        s, color = series[name], colors[name]
        tag = short_label(name)
        ordinals = [p["deploy"] for p in s["points"]]
        internal = [p["internal"] for p in s["points"]]
        external = [p["external"] for p in s["points"]]
        sd = s["external_noise_sd"]
        every = s["external_all"]
        all_x = [e["deploy"] for e in every]
        all_y = [e["external"] for e in every]

        ax.plot(ordinals, internal, "-o", color=color, ms=6)
        ax.plot(all_x, all_y, "--s", color=color, ms=6, alpha=.6)
        if sd:
            ax.fill_between(all_x, [y - sd for y in all_y], [y + sd for y in all_y],
                            color=color, alpha=.15, lw=0)

        # The program letter under each external point. A repeated letter is a rollback, and a letter
        # appearing once that is not the shipped one is an experiment the loop threw away.
        # Each build's letters sit on their own row below the axis line. Two builds can run within a
        # few thousandths of each other -- cci b0 and b2 differ by 0.006 at deploy 4 -- and a shared
        # offset put their letters on top of each other exactly where the reader needs them most.
        row = -15 - 11 * sorted(series).index(name)
        for e in every:
            ax.annotate(e["program"], (e["deploy"], e["external"]),
                        textcoords="offset points", xytext=(0, row), ha="center",
                        fontsize=7.5, color=color,
                        fontweight="bold" if e["is_submitted_program"] else "normal",
                        alpha=1.0 if e["is_submitted_program"] else .55)

        # Ring the best version built and the one actually submitted. When they differ, that gap IS the
        # regret, and it is the one thing on this chart a reader can act on without any statistics.
        reg = s["regret"]
        if reg:
            ax.plot([reg["best_deploy"]], [reg["best_score"]], "o", ms=15, mfc="none",
                    mec="#2e9e5b", mew=2, zorder=5)
            # Drawn even when the submitted deploy is OUTSIDE the paired trajectory, which is the normal
            # case: the submission is a copy of an earlier deploy and carries no internal score of its
            # own, so it has no paired point -- but it is the version that actually shipped, and leaving
            # it off the chart hides the regret the chart exists to show.
            ax.plot([reg["submitted_deploy"]], [reg["submitted_score"]], "o", ms=15, mfc="none",
                    mec="#c0392b", mew=2, zorder=5)

    ax.set_xlabel("deploy order within the build")
    ax.set_ylabel("score")
    subject = task_and_model(sorted(series))
    ax.set_title("Does the agent's own eval predict the real one?"
                 + (f"\n{subject}" if subject else ""),
                 fontsize=12.5, linespacing=1.4)
    ax.set_ylim(0, 1.05)
    ax.grid(alpha=.25)
    # Integer ordinals only -- there is no deploy 2.5, and matplotlib will invent one given the chance.
    # Includes the submitted deploy, which is usually outside the paired trajectory.
    ticks = {e["deploy"] for s in series.values() for e in s["external_all"]}
    ax.set_xticks(sorted(ticks))

    # A legend with one entry per build per series ran to fifteen rows at three builds and covered the
    # data it was labelling. Colour carries the build and line style carries the instrument, so the
    # legend only has to teach those two alphabets once; the per-build numbers go in a text block where
    # they can be read as a table instead of a list of sentences.
    from matplotlib.lines import Line2D
    key = [Line2D([], [], color=colors[n], lw=2, label=short_label(n)) for n in sorted(series)]
    key += [
        Line2D([], [], color="#555", lw=2, ls="-", marker="o", ms=5, label="internal (its own exam)"),
        Line2D([], [], color="#555", lw=2, ls="--", marker="s", ms=5, alpha=.6,
               label=f"external ({limit_label})"),
        Line2D([], [], color="none", marker="o", ms=11, mfc="none", mec="#2e9e5b", mew=2,
               label="best built"),
        Line2D([], [], color="none", marker="o", ms=11, mfc="none", mec="#c0392b", mew=2,
               label="submitted"),
        Line2D([], [], color="none", marker="$A$", ms=9, mec="#555", mfc="#555",
               label="program identity (bold = shipped)"),
    ]
    # Stacked top to bottom in the side column: summary table, then key, then definitions. The three
    # anchors are hand-set because each block's height depends on how many builds are plotted, and an
    # automatic layout let the key and the definitions draw over each other.
    ax_side.legend(handles=key, fontsize=8.5, loc="upper left", frameon=False,
                   bbox_to_anchor=(0, .70), handlelength=2.4, labelspacing=.55)

    # A bare "tau-b / dirs / regret" header assumes the reader already knows all three, which nobody
    # outside this analysis does. Each column is spelled out, the arrow says which way is good, and the
    # footnote defines the two that are not self-evident -- so the figure travels without its author.
    w = max([4] + [len(short_label(n)) + 1 for n in series])   # a disambiguated label is wider than `b0`
    rows = [f"{'':<{w}}{'programs':>9}{'deploys':>9}{'τb':>7}{'directions':>12}{'regret':>9}",
            f"{'':<{w}}{'built':>9}{'':>9}{'':>7}{'agreed':>12}{'':>9}",
            "─" * (46 + w)]
    for name in sorted(series):
        s = series[name]
        tau = s["kendall_tau_b"]
        resolvable = s["decisions_agree"] + s["decisions_disagree"]
        reg = s["regret"]
        rows.append(
            f"{short_label(name):<{w}}{s['n']:>9}{s['n_deploys']:>9}"
            f"{('n/a' if math.isnan(tau) else format(tau, '+.2f')):>7}"
            f"{(str(s['decisions_agree']) + ' of ' + str(resolvable)):>12}"
            f"{('n/a' if reg is None else format(reg['regret'], '.3f')):>9}"
        )
    # Sized at 8pt for the 50-column table a bare `b0` label gives; a wider label shrinks the font in
    # proportion rather than pushing the right-hand column off the figure.
    ax_side.text(0, .99, "\n".join(rows), transform=ax_side.transAxes, ha="left", va="top",
                 fontsize=8 * min(1.0, 50 / (46 + w)), family="monospace", linespacing=1.5)

    # Spelled out rather than abbreviated: a reader meeting this figure cold knows none of the three,
    # and a summary nobody can decode is decoration.
    ax_side.text(0, .30,
                 "τb  Kendall rank agreement between\n"
                 "    the two evals, over distinct PROGRAMS:\n"
                 "    a redeploy is the same candidate\n"
                 "    measured twice, not a second one.\n"
                 "    +1 = identical ranking, 0 = unrelated.\n\n"
                 "directions  revisions where both evals\n"
                 "    agreed on the sign of the change.\n\n"
                 "regret  real score given up by shipping\n"
                 "    a worse program than the best built.\n"
                 "    0 = shipped its best.",
                 transform=ax_side.transAxes, ha="left", va="top", fontsize=7.4,
                 color="#444", linespacing=1.5)

    # subplots_adjust, not tight_layout: the side column is a borderless axes holding only text, which
    # tight_layout cannot measure and warns about on every run.
    fig.subplots_adjust(left=.07, right=.99, top=.88, bottom=.10, wspace=.04)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "internal_vs_external.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", action="append", type=pathlib.Path, default=None, metavar="PATH",
                        help="A fetched run directory. Repeatable. Defaults to every run under "
                             f"{FETCH_ROOT.relative_to(REPO)} that has both halves.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Read records scored at this sample limit. Default: the full eval block. "
                             "Records at any other limit are ignored rather than pooled.")
    parser.add_argument("--out", type=pathlib.Path, default=None, metavar="PATH",
                        help=f"Where the dated generation is written (default: "
                             f"{OUT_ROOT.relative_to(REPO)}).")
    parser.add_argument("--json", action="store_true", help="Machine-readable output to stdout.")
    args = parser.parse_args(argv)

    run_dirs = args.run_dir or (sorted(p for p in FETCH_ROOT.glob("*") if (p / "checkpoint_scores").is_dir())
                                if FETCH_ROOT.is_dir() else [])
    if not run_dirs:
        print(f"nothing to read: no scored run under {FETCH_ROOT}", file=sys.stderr)
        return 1

    series, skipped = collect(run_dirs, args.limit)
    _LABELS.clear()
    _LABELS.update(assign_labels([p.name for p in run_dirs]))
    limit_label = "the full eval block" if args.limit is None else f"{args.limit} cases"

    if args.json:
        print(json.dumps({"series": series, "skipped": skipped}, indent=2))
    else:
        if not series:
            print(f"no run has both halves at {limit_label}.")
        def fmt(value: float) -> str:
            return "undefined" if math.isnan(value) else format(value, "+.3f")

        for name in sorted(series):
            s = series[name]
            sd = s["external_noise_sd"]
            print(f"{name}  [{limit_label}]")
            print(f"  n = {s['n']}   Pearson {fmt(s['pearson'])}   Spearman {fmt(s['spearman'])}"
                  f"   Kendall tau-b {fmt(s['kendall_tau_b'])}")
            print(f"  internal {s['internal_first']:.3f} -> {s['internal_last']:.3f}"
                  f"   external {s['external_first']:.3f} -> {s['external_last']:.3f}"
                  f"   external noise sd {format_sd(sd)}")
            resolvable = s["decisions_agree"] + s["decisions_disagree"]
            print(f"  decisions: {s['decisions_agree']}/{resolvable} agreed on direction"
                  f"   ({s['decisions_tied']} tied, {s['decisions_within_noise']} within noise)")
            for d in s["decisions"]:
                flag = "  <- within noise" if d["within_noise"] else ""
                print(f"    {d['cycle']:<7} internal {d['internal_delta']:+.3f}"
                      f"   external {d['external_delta']:+.3f}   {d['verdict']}{flag}")
            j = s["loop_verdicts"]
            if j:
                print(f"  loop's own decisions: {s['loop_correct_keep']} correct keep, "
                      f"{s['loop_wrong_keep']} wrong keep, {s['loop_correct_reject']} correct reject, "
                      f"{s['loop_wrong_reject']} WRONG REJECT")
                for v in j:
                    flag = "  <- within noise" if v["within_noise"] else ""
                    print(f"    #{v['incumbent']}->#{v['deploy']} {str(v['technique'])[:22]:<24}"
                          f" loop {v['verdict']:<9} real {v['external_delta']:+.4f}"
                          f"  {v['outcome']}{flag}")
                if s["loop_wrong_reject"]:
                    print(f"    value thrown away by wrong rejects: {s['loop_value_discarded']:+.4f}")
            print(f"  lineage: {len(s['external_all'])} deploys, {s['programs_built']} distinct "
                  f"program(s); shipped {s['shipped_program']}, "
                  f"abandoned {', '.join(s['programs_abandoned']) or 'none'}")
            print("    " + "  ".join(
                f"#{e['deploy']}:{e['program']}" + ("*" if e["is_submission"] else "")
                for e in s["external_all"]))
            reg = s["regret"]
            if reg:
                print(f"  regret: submitted #{reg['submitted_deploy']} at {reg['submitted_score']:.3f}, "
                      f"best built was #{reg['best_deploy']} at {reg['best_score']:.3f}"
                      f"  -> gave up {reg['regret']:+.3f}")
        for reason, names in sorted(skipped.items()):
            print(f"  skipped [{reason}]: {', '.join(short_label(n) for n in names)}")

    if not series:
        return 1

    out_dir = (args.out or OUT_ROOT) / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    published = {name: {k: v for k, v in entry.items() if not k.startswith("_")}
                 for name, entry in series.items()}
    (out_dir / "stats.json").write_text(json.dumps({"series": published, "skipped": skipped,
                                                    "limit": args.limit}, indent=2))
    with (out_dir / "points.csv").open("w") as fh:
        fh.write("run_id,build,deploy,content_hash,is_submission,internal,external\n")
        for name in sorted(series):
            for p in series[name]["points"]:
                fh.write(f"{name},{short_label(name)},{p['deploy']},{p['hash']},"
                         f"{int(p['is_submission'])},{p['internal']:.4f},{p['external']:.4f}\n")
    figures = [draw(series, out_dir, limit_label), draw_decisions(series, out_dir),
               draw_verdicts(series, out_dir)]
    for name in sorted(series):
        figures.append(draw_cases(pathlib.Path(series[name]["_run_dir"]), series[name], out_dir, name))

    written = 2 + sum(1 for f in figures if f)
    print(f"\nwrote {written} file(s) to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
