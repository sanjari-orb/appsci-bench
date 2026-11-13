"""Is MMS@3 with optimization enabled different from optimization disabled? A paired test.

The pair is one (model, task) cell scored under both arms, from the same results table the figures
use. For each cell, d = MMS@3 on - MMS@3 off. Reported, pooled over cells and then per model and
per task:
  mean d with a paired-t 95% CI;
  a bootstrap 95% CI (percentile, resampling cells);
  a sign-flip permutation p-value (exact when there are few pairs, Monte Carlo otherwise), which
  assumes only that d is symmetric about 0 under no effect;
  the Wilcoxon signed-rank p-value (cells with d = 0 dropped).

Cells from one model (or one task) are not independent, so the pooled test also has cluster
bootstraps that resample whole tasks or whole models. When the cluster CIs are much wider than the
cell CI, trust them over it.

Cells where the two arms have a different number of builds are flagged: MMS@k falls as k rises, so
those pairs are not like for like. The pooled test is also run without them, and without cells
that score 0 in both arms (models at the floor, which add zeros and shrink the spread).

Build level, per model. MMS@3 is one number per cell, so the cell test above has at most 7 pairs per
model. The per-trial sheets (both arms) give each build its own score: the mean task accuracy over
its valid trials, or 0 for a build that failed (a build error, or no scored trial), as in the
headline table. Invalid rows are dropped, and a build with no valid row is left out. Per model the
statistic is the mean over tasks of (mean build score on - mean build score off), so each task
counts once. Its CI resamples builds within each task and arm; its p-value shuffles the on / off
labels among a task's builds, within each task. This tests mean task accuracy, not MMS@3: MMS@3
takes a per-case minimum across builds, and the sheets have no per-case scores.

    uv run --no-project --with numpy --with scipy python scripts/paper_plots/opt_paired_test.py
"""

from __future__ import annotations

import csv
import itertools
import sys
import tempfile
from pathlib import Path

import numpy as np
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data import (  # noqa: E402
    MODELS,
    TASKS,
    USABLE,
    VALID_STATUSES,
    build_outcomes,
    grid,
    load_results,
    load_trials,
)
from import_elicitation_sheet import OUT_FIELDS, convert  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
SHEETS = {True: DATA_DIR / "results_per_trial_sheet_optimization_enabled_runs.csv",
          False: DATA_DIR / "results_per_trial_sheet_optimization_disabled_runs.csv"}

SEED = 0
N_BOOT = 20_000
N_PERM = 200_000
EXACT_PERM_MAX = 16  # up to 2^16 sign patterns are enumerated


def _t_ci(d: np.ndarray) -> tuple[float, float]:
    if len(d) < 2:
        return float("nan"), float("nan")
    half = stats.t.ppf(0.975, len(d) - 1) * d.std(ddof=1) / np.sqrt(len(d))
    return d.mean() - half, d.mean() + half


def _boot_ci(d: np.ndarray, rng: np.random.Generator) -> tuple[float, float]:
    means = d[rng.integers(0, len(d), size=(N_BOOT, len(d)))].mean(axis=1)
    return tuple(np.percentile(means, [2.5, 97.5]))


def _cluster_boot_ci(d: np.ndarray, groups: list[str], rng: np.random.Generator
                     ) -> tuple[float, float]:
    """Resample whole groups with replacement; the statistic is the mean over the cells drawn."""
    names = sorted(set(groups))
    by = [d[[g == n for g in groups]] for n in names]
    means = np.empty(N_BOOT)
    for b in range(N_BOOT):
        pick = rng.integers(0, len(names), size=len(names))
        means[b] = np.concatenate([by[i] for i in pick]).mean()
    return tuple(np.percentile(means, [2.5, 97.5]))


def _sign_flip_p(d: np.ndarray, rng: np.random.Generator) -> float:
    """Two-sided: the share of sign patterns whose |mean| is at least the observed |mean|."""
    nz = d[d != 0]
    if len(nz) == 0:
        return 1.0
    observed = abs(nz.mean()) - 1e-12
    if len(nz) <= EXACT_PERM_MAX:
        signs = np.array(list(itertools.product((1, -1), repeat=len(nz))))
    else:
        signs = rng.choice((1, -1), size=(N_PERM, len(nz)))
    return float((np.abs((signs * nz).mean(axis=1)) >= observed).mean())


def _wilcoxon_p(d: np.ndarray) -> float:
    nz = d[d != 0]
    return float("nan") if len(nz) == 0 else float(stats.wilcoxon(nz).pvalue)


def _line(label: str, d: np.ndarray, rng: np.random.Generator) -> str:
    lo, hi = _t_ci(d)
    blo, bhi = _boot_ci(d, rng)
    up, down = int((d > 0).sum()), int((d < 0).sum())
    return (f"{label:24} n={len(d):2}  mean {d.mean() * 100:+6.1f}  t-CI [{lo * 100:+6.1f}, "
            f"{hi * 100:+6.1f}]  boot-CI [{blo * 100:+6.1f}, {bhi * 100:+6.1f}]  "
            f"on>off {up:2} / on<off {down:2}  sign-flip p {_sign_flip_p(d, rng):.3f}  "
            f"Wilcoxon p {_wilcoxon_p(d):.3f}")


def build_scores(sheet: Path) -> dict[tuple[str, str], list[float]]:
    """(model, task) -> one score per valid build: the mean accuracy of its valid trials, or 0 when
    the build failed. Goes through the same importer and loader as the figures."""
    with tempfile.NamedTemporaryFile("w", suffix=".csv", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        writer.writerows(convert(sheet))
        f.flush()
        trials = load_trials(Path(f.name))
    by_build: dict[str, list[float]] = {}
    for t in trials:
        if t.status in VALID_STATUSES and t.accuracy is not None:
            by_build.setdefault(t.build_id, []).append(t.accuracy)
    out: dict[tuple[str, str], list[float]] = {}
    for o in build_outcomes(trials):
        acc = by_build.get(o.build_id)
        score = sum(acc) / len(acc) if o.outcome == USABLE and acc else 0.0
        out.setdefault((o.model, o.task), []).append(score)
    return out


def _build_stat(on: list[np.ndarray], off: list[np.ndarray]) -> float:
    return float(np.mean([a.mean() - b.mean() for a, b in zip(on, off, strict=True)]))


def build_level_line(label: str, on: list[np.ndarray], off: list[np.ndarray],
                     rng: np.random.Generator) -> str:
    """Stratified by task: the bootstrap resamples builds within each task and arm, the permutation
    shuffles arm labels within each task."""
    observed = _build_stat(on, off)
    boots = np.empty(N_BOOT)
    for b in range(N_BOOT):
        boots[b] = _build_stat([a[rng.integers(0, len(a), len(a))] for a in on],
                               [a[rng.integers(0, len(a), len(a))] for a in off])
    lo, hi = np.percentile(boots, [2.5, 97.5])
    pooled = [np.concatenate([a, b]) for a, b in zip(on, off, strict=True)]
    n_perm = N_BOOT
    hits = 0
    for _ in range(n_perm):
        perm_on, perm_off = [], []
        for a, p in zip(on, pooled, strict=True):
            shuffled = rng.permutation(p)
            perm_on.append(shuffled[:len(a)])
            perm_off.append(shuffled[len(a):])
        hits += abs(_build_stat(perm_on, perm_off)) >= abs(observed) - 1e-12
    n_on, n_off = sum(len(a) for a in on), sum(len(a) for a in off)
    return (f"{label:24} cells={len(on)} builds on/off={n_on}/{n_off}  mean {observed * 100:+6.1f}"
            f"  boot-CI [{lo * 100:+6.1f}, {hi * 100:+6.1f}]  permutation p {hits / n_perm:.3f}")


def build_level(rng: np.random.Generator) -> None:
    on, off = build_scores(SHEETS[True]), build_scores(SHEETS[False])
    print("\nBuild level: mean task accuracy per build, on minus off, in percentage points")
    # All models together: one stratum per (model, task) cell, each counting once.
    both = [c for c in on if off.get(c)]
    print(build_level_line("All models", [np.array(on[c]) for c in both],
                           [np.array(off[c]) for c in both], rng))
    for m in MODELS:
        tasks = [t for t in TASKS if on.get((m, t)) and off.get((m, t))]
        if not tasks:
            print(f"{MODELS[m]:24} no task has builds in both arms")
            continue
        a = [np.array(on[(m, t)]) for t in tasks]
        b = [np.array(off[(m, t)]) for t in tasks]
        if all(np.all(x == 0) for x in a + b):
            print(f"{MODELS[m]:24} every build scores 0 in both arms")
            continue
        print(build_level_line(MODELS[m], a, b, rng))
    print("\nBuilds per cell (on / off), where not 3 / 3:")
    for m in MODELS:
        for t in TASKS:
            n_on, n_off = len(on.get((m, t), [])), len(off.get((m, t), []))
            if (n_on, n_off) != (3, 3):
                print(f"  {MODELS[m]} / {TASKS[t]}: {n_on} / {n_off}")


def main() -> None:
    rng = np.random.default_rng(SEED)
    results = load_results()
    on, off = grid(results, opt_enabled=True), grid(results, opt_enabled=False)
    cells = [(m, t) for m in MODELS for t in TASKS
             if (m, t) in on and (m, t) in off
             and on[(m, t)].score is not None and off[(m, t)].score is not None]
    missing = [(m, t) for m in MODELS for t in TASKS if (m, t) not in cells]
    if not cells:
        raise SystemExit("no (model, task) cell has a score in both arms")
    d = np.array([on[c].score - off[c].score for c in cells])
    k_mismatch = [c for c in cells if on[c].k_builds != off[c].k_builds]
    both_zero = [c for c in cells if on[c].score == 0 and off[c].score == 0]

    print("MMS@3, optimization on minus off, in percentage points\n")
    if missing:
        print(f"no pair for: {missing}")
    for c in k_mismatch:
        print(f"builds differ: {MODELS[c[0]]} / {TASKS[c[1]]}: "
              f"on k={on[c].k_builds}, off k={off[c].k_builds}")
    print()
    print(_line("All cells", d, rng))
    blo, bhi = _cluster_boot_ci(d, [t for _, t in cells], rng)
    print(f"{'':24} cluster bootstrap over tasks  [{blo * 100:+6.1f}, {bhi * 100:+6.1f}]")
    blo, bhi = _cluster_boot_ci(d, [m for m, _ in cells], rng)
    print(f"{'':24} cluster bootstrap over models [{blo * 100:+6.1f}, {bhi * 100:+6.1f}]")
    keep = np.array([c not in k_mismatch for c in cells])
    print(_line("Same k only", d[keep], rng))
    keep = np.array([c not in both_zero for c in cells])
    print(_line("Without 0 / 0 cells", d[keep], rng))

    # The benchmark-level number: each model's mean over tasks, paired over models.
    shared = [t for t in TASKS if all((m, t) in cells for m in MODELS)]
    per_model = np.array([np.mean([on[(m, t)].score - off[(m, t)].score for t in shared])
                          for m in MODELS if all((m, t) in cells for t in shared)])
    print(_line(f"Model means ({len(shared)} tasks)", per_model, rng))

    print("\nPer model (pairs are tasks)")
    for m in MODELS:
        idx = [i for i, c in enumerate(cells) if c[0] == m]
        if idx:
            print(_line(MODELS[m], d[idx], rng))
    print("\nPer task (pairs are models)")
    for t in TASKS:
        idx = [i for i, c in enumerate(cells) if c[1] == t]
        if idx:
            print(_line(TASKS[t], d[idx], rng))
    build_level(rng)


if __name__ == "__main__":
    main()
