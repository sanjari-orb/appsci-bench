"""Reading a run's eval report: the defensive parse, and the headline numbers a list view needs.

Extracted from `appsci_bench.experiments.metrics` so the tracer can read the same file without
importing the experiment runner. The parse is the interesting part and it must not be duplicated: its
exception clauses encode two production failures, and a second copy would inevitably lose one of them.

The cache exists because the tracer's run list reloads every 10 seconds and parses one of these per
run. A finished run's report never changes, so keying on (mtime, size) makes the steady state one
parse per run for the life of the process, while a still-being-written report is re-read as it grows.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Where the evaluator persists its report inside a run directory.
EVAL_REPORT = Path("eval") / "report.json"

#: How many parsed headlines to keep. Sized to exceed any plausible run count on one host rather than
#: to be a small cache, because of the access pattern: the tracer's run list scans EVERY run on every
#: render. A bounded cache under a full sequential scan larger than itself has a 0% hit rate no matter
#: what it evicts -- whatever it drops is exactly what the scan asks for next -- so the only cap that
#: helps is one the working set fits inside. Measured on the real workload at the old cap of 512 with
#: 700 runs: 700 parses per render, every render.
#:
#: Entries are small (a score, up to 12 objective scores, a few counts), so this bound is about tens of
#: MB in the worst case, and `reap_run_dirs` keeps real run counts far below it.
_CACHE_MAX = 8192
_cache: dict[str, tuple[tuple[float, int], dict[str, Any] | None]] = {}

#: A report names its objectives, and those names reach a UI. Bounded for the same reason every other
#: name read out of a run directory is.
_MAX_METRICS = 12
_MAX_NAME_CHARS = 40


def read_report(run_dir: Path | str) -> dict[str, Any] | None:
    """Parse `eval/report.json`, or None when it is missing or unreadable.

    The three exception types are deliberate and each was earned. This file lives in a run directory
    the benchmark subject can write, and `json.JSONDecodeError` covers neither of the shapes that
    actually broke a drain:

      * a 4,400-digit integer literal raises a bare `ValueError` (JSONDecodeError is a *subclass*, so
        only the wider clause catches both);
      * deeply nested JSON exhausts the parser's stack and raises `RecursionError`, which is a
        `RuntimeError` and so caught by no `ValueError` clause at all.

    Reproduced with one 200 KB line of `[` * 100000 + `]` * 100000: the exception escaped metrics
    collection, `worker.drain` has no `except`, and the drain died with a finished cell unreported --
    which the stale reaper then paid for twice.
    """
    path = Path(run_dir) / EVAL_REPORT
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def headline(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """The few numbers a list view shows: the score, the per-objective scores, and how measured it is.

    `final_score` stays None when the report has none. That is a real state -- every objective
    excluded -- and it is NOT 0.0: rendering it as zero would put a plausible-looking failure in a
    table for a run that was never scored.

    `n_errored` travels with the score because an errored result is aggregated as 0.0, so errors
    aggregate into a clean, confident-looking number. A score with errors behind it must be markable
    as such wherever it is shown.
    """
    if not report:
        return None
    score = report.get("final_score")
    views = report.get("views") or {}
    results = report.get("results") or []

    per_metric: dict[str, float | None] = {}
    for row in (views.get("per_metric") or [])[:_MAX_METRICS]:
        if not isinstance(row, dict):
            continue
        name = row.get("objective")
        if not name:
            continue
        value = row.get("score")
        per_metric[str(name)[:_MAX_NAME_CHARS]] = (
            float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        )

    return {
        "final_score": (
            float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
        ),
        "per_metric": per_metric,
        "n_data_points": len(views.get("per_data_point") or []),
        "n_results": len(results),
        "n_errored": sum(
            1 for r in results if isinstance(r, dict) and r.get("error")
        ),
        "n_warnings": len(report.get("warnings") or []),
        # How complete the run behind this score is. It travels WITH the score for the same reason
        # `n_errored` does: the evaluator rewrites this report after every scored case, so a score can
        # come from a run that is still going or one that was killed part-way, and a mean over the
        # cases that happened to finish first is not comparable with a full run's. `partial` is read
        # from the report rather than inferred from the counts -- an incomplete run whose report was
        # still being written could otherwise be mistaken for a complete short one.
        "partial": bool(report.get("partial", False)),
        "cases_scored": _count(report.get("cases_scored")),
        "cases_total": _count(report.get("cases_total")),
    }


def _count(value: Any) -> int | None:
    """A non-negative case count, or None when the report carries no usable one.

    Defensive for the same reason the rest of this module is: it parses a file inside a run directory,
    and a missing field (a report predating these counts) must read as "unknown", never as zero."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def cached_headline(run_dir: Path | str) -> dict[str, Any] | None:
    """`headline(read_report(run_dir))`, parsed once per (mtime, size) of the report.

    Keyed on the file's stat rather than a TTL: a finished report is immutable, so a TTL would
    re-parse it forever, while a report still being written changes its stat and is re-read.

    ONE PARSE PER RUN HOLDS ONLY WHILE THE RUN COUNT FITS THE CAP. The caller scans every run per
    render, so above `_CACHE_MAX` a bounded cache degrades to no cache at all -- see the note there.
    The cap is set to make that case unreachable in practice rather than to make eviction clever.
    """
    path = Path(run_dir) / EVAL_REPORT
    try:
        st = path.stat()
    except OSError:
        return None
    stamp = (st.st_mtime, st.st_size)
    key = str(path)
    hit = _cache.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    value = headline(read_report(run_dir))
    if len(_cache) >= _CACHE_MAX:
        # Clear rather than evict a slice. Under the full-scan access pattern, dropping the
        # oldest-inserted entries drops precisely the runs the next render asks for first, so a
        # partial eviction pays the bookkeeping and still misses everything. Clearing is O(1) and
        # costs one render's parses, after which the cache is warm again for the whole page.
        _cache.clear()
    _cache[key] = (stamp, value)
    return value


def aggregate(headlines: list[dict[str, Any] | None]) -> dict[str, Any]:
    """Summarise several trials' headlines into one row: mean score, its range, and coverage.

    `n_scored` and `n_total` are reported separately and BOTH shown, because a mean over 2 of 4 trials
    is a different claim from a mean over 4. Silently averaging whatever happened to be scored is how a
    partly-failed group comes to look like a complete measurement.

    The range is min/max rather than a standard deviation: on two trials an SD is barely meaningful,
    and the spread that matters to a reader scanning a list is "did these agree".
    """
    scores = [
        h["final_score"] for h in headlines
        if h and isinstance(h.get("final_score"), (int, float))
    ]
    errored = sum(h.get("n_errored", 0) for h in headlines if h)
    return {
        "n_total": len(headlines),
        "n_scored": len(scores),
        "mean": (sum(scores) / len(scores)) if scores else None,
        "min": min(scores) if scores else None,
        "max": max(scores) if scores else None,
        "n_errored": errored,
    }
