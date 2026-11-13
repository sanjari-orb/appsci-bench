"""Is mean task accuracy lower with the optimization layer on than off? Permutation tests on builds.

Data: the two per-trial run logs in data/ (optimization enabled and disabled runs). Each model x task
cell has about three builds per arm. A build's accuracy is the mean of its valid trials' task
accuracy (VALID and VALID*; INVALID rows are infrastructure failures and are dropped). A build that
errored (no submission, conformance failure, a crash, running out of context) scores 0; one that did
not finish (a build timeout or the turn limit) is dropped, not scored. These are the rules behind
the trial_mean column of results_score_pow_k.csv, and every cell is checked against that column
before anything is tested.

The unit is the build: builds are the independent draws, while the trials of one build share its
code. Scores are only comparable WITHIN one model on one task: not across tasks for a model, and not
across models on a task. So nothing here averages, subtracts or ranks scores from different cells.
Every comparison is on-arm builds against off-arm builds of the same model and task, and evidence is
combined across cells only through the direction of those comparisons.

Tests:
  1. per cell: an exact permutation test of the difference in mean build accuracy, over every split
     of the cell's builds into groups of the observed sizes. With 3 builds per arm there are 20
     splits, so the smallest one-sided p is 0.05: a single cell can barely reach significance;
  2. sign test over cells: how many cells have the lower mean with optimization on, exact binomial
     (cells with equal means dropped);
  3. stratified rank test (van Elteren): builds are ranked within their own cell (ties averaged),
     the off arm's rank sum is weighted by 1 / (builds in the cell + 1) and summed over cells. The
     null distribution comes from shuffling arm labels within each cell only;
  4. per model: test 3 over that model's cells, with Holm's correction over the models.
All one-sided tests ask whether optimization on scores LOWER; two-sided p-values are given too.

The arms were not randomized against each other: they ran at different times on different
commits. A significant delta says the gap is larger than build-to-build noise, not that the
optimization layer alone caused it.

    python scripts/paper_plots/compute_opt_significance.py

writes data/opt_arm_builds.csv (one row per build used) and
figures/opt_significance/opt_significance.csv (the combined tests) and
figures/opt_significance/opt_significance_cells.csv (one row per cell), and prints a summary.
"""

from __future__ import annotations

import csv
import itertools
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data import MODELS, TASKS, grid, load_results  # noqa: E402

HERE = Path(__file__).resolve().parent
RUN_LOGS = {
    True: HERE / "data" / "results_per_trial_sheet_optimization_enabled_runs.csv",
    False: HERE / "data" / "results_per_trial_sheet_optimization_disabled_runs.csv",
}
BUILDS_CSV = HERE / "data" / "opt_arm_builds.csv"
OUT_DIR = HERE / "figures" / "opt_significance"

SEED = 20260925
N_PERMUTATIONS = 200_000
ALPHA = 0.05

# The run logs' task names, and the spellings of model names they use.
SHEET_TASKS = {
    "Airline": "airline_rebooking", "Airlines": "airline_rebooking", "O2C": "order_to_cash",
    "Customer-Call Insights": "customer_call_insights", "Compliance": "compliance_review",
    "Slides": "slides_generator", "CRM Insights": "crm_insights", "Banking": "banking_support",
}
VALID = {"VALID", "VALID*"}
# Build errors that mean the build never finished: dropped, as in the results table. Every other
# build error scores the build 0.
UNFINISHED = ("timeout", "turn limit")
# Cells whose trial_mean in results_score_pow_k.csv is known to disagree with the run log, and why.
# The run log is used for these.
KNOWN_TABLE_ERRORS = {
    ("gpt-oss-120b", "order_to_cash", True):
        "results table repeats GPT-5.5's row (0.09); the run log gives 0.046, 0, 0",
}
TABLE_TOLERANCE = 0.006  # the table stores trial_mean to 2 decimals

Cell = tuple[str, str]  # model, task


def model_id(raw: str) -> str | None:
    """'fireworks_ai/accounts/fireworks/models/kimi-k3', 'kimi k3', 'openai/gpt-5.5' -> repo id."""
    name = raw.strip().lower().split("/")[-1].replace(" ", "-")
    return name if name in MODELS else None


def load_builds(path: Path) -> dict[Cell, dict[str, float]]:
    """cell -> build id -> build accuracy, under the rules in the module docstring."""
    trials: dict[Cell, dict[str, list[float | None]]] = defaultdict(lambda: defaultdict(list))
    errors: dict[Cell, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row_no, row in enumerate(csv.DictReader(f), start=2):
            build = row["Build ID"].strip()
            if not build or row["Valid/Invalid"].strip() not in VALID:
                continue
            model, task = model_id(row["Model "]), SHEET_TASKS.get(row["Task"].strip())
            if model is None or task is None:
                raise ValueError(f"{path.name} row {row_no}: unknown model or task "
                                 f"{row['Model ']!r} / {row['Task']!r}")
            errors[(model, task)][build].add(row["Build Error"].strip().lower())
            raw = row["Task Accuracy"].strip()
            if raw:
                value = float(raw)
                if not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError(f"{path.name} row {row_no}: task accuracy {raw!r}")
                trials[(model, task)][build].append(value)
            else:
                trials[(model, task)][build].append(None)
    out: dict[Cell, dict[str, float]] = {}
    for cell, builds in trials.items():
        for build, accs in builds.items():
            err = "; ".join(sorted(e for e in errors[cell][build] if e))
            if any(u in err for u in UNFINISHED):
                continue
            scored = [a for a in accs if a is not None]
            # An errored build scores 0 even where the log gives its trials a number: that number
            # is the 0 the sheet writes for a build that produced nothing.
            out.setdefault(cell, {})[build] = 0.0 if err or not scored else sum(scored) / len(scored)
    return out


def check_against_table(arms: dict[bool, dict[Cell, dict[str, float]]]) -> None:
    """Every cell's build count and mean must match results_score_pow_k.csv, or be a known error."""
    results = load_results()
    problems = []
    for opt, cells in arms.items():
        table = grid(results, opt_enabled=opt)
        for m in MODELS:
            for t in TASKS:
                builds = cells.get((m, t), {})
                ref = table[(m, t)]
                mean = sum(builds.values()) / len(builds) if builds else None
                if len(builds) != ref.k_builds:
                    problems.append(f"{m}/{t}/opt={opt}: {len(builds)} builds, table has "
                                    f"{ref.k_builds}")
                elif mean is None or ref.trial_mean is None:
                    problems.append(f"{m}/{t}/opt={opt}: no mean to compare")
                elif abs(mean - ref.trial_mean) > TABLE_TOLERANCE:
                    known = KNOWN_TABLE_ERRORS.get((m, t, opt))
                    if known:
                        print(f"note: {m}/{t}/opt={opt}: run log {mean:.3f}, table "
                              f"{ref.trial_mean:.2f} ({known})", file=sys.stderr)
                    else:
                        problems.append(f"{m}/{t}/opt={opt}: run log {mean:.3f}, table "
                                        f"{ref.trial_mean:.2f}")
    if problems:
        raise ValueError("run logs disagree with results_score_pow_k.csv:\n  " + "\n  ".join(problems))


@dataclass(frozen=True)
class CellData:
    cell: Cell
    on: np.ndarray  # build accuracies, optimization on
    off: np.ndarray

    @property
    def diff(self) -> float:
        return float(self.off.mean() - self.on.mean())


def cell_data(arms: dict[bool, dict[Cell, dict[str, float]]]) -> list[CellData]:
    out = []
    for m in MODELS:
        for t in TASKS:
            on, off = arms[True].get((m, t), {}), arms[False].get((m, t), {})
            if not on or not off:
                raise ValueError(f"{m}/{t} lacks builds in one arm")
            out.append(CellData((m, t), np.array(list(on.values())), np.array(list(off.values()))))
    return out


def splits(n_on: int, n: int) -> list[np.ndarray]:
    """Every on-arm mask for a cell of n builds with n_on in the on arm."""
    out = []
    for on_idx in itertools.combinations(range(n), n_on):
        mask = np.zeros(n, dtype=bool)
        mask[list(on_idx)] = True
        out.append(mask)
    return out


def cell_exact_test(c: CellData) -> tuple[float, float]:
    """(one-sided p for off mean > on mean, two-sided p), exact over the cell's splits."""
    pooled = np.concatenate([c.on, c.off])
    diffs = np.array([pooled[~m].mean() - pooled[m].mean() for m in splits(len(c.on), len(pooled))])
    eps = 1e-12
    return (float(np.mean(diffs >= c.diff - eps)),
            float(np.mean(np.abs(diffs) >= abs(c.diff) - eps)))


def within_cell_ranks(values: np.ndarray) -> np.ndarray:
    """Ranks 1..n within one cell, ties given their average rank."""
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values))
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and abs(values[order[j + 1]] - values[order[i]]) < 1e-12:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def van_elteren_test(cells: list[CellData], rng: np.random.Generator) -> tuple[float, float]:
    """(one-sided p for on scoring lower, two-sided p) for the stratified rank-sum statistic.

    Each cell contributes its off arm's within-cell rank sum over (builds + 1), centred on its null
    mean; a shuffle picks one split per cell uniformly at random. Only within-cell ranks enter, so
    no score is ever compared outside its own cell.
    """
    observed = 0.0
    total = np.zeros(N_PERMUTATIONS)
    for c in cells:
        pooled = np.concatenate([c.on, c.off])
        ranks = within_cell_ranks(pooled)
        weight = 1 / (len(pooled) + 1)
        null_mean = len(c.off) * (len(pooled) + 1) / 2
        options = np.array([(ranks[~m].sum() - null_mean) * weight
                            for m in splits(len(c.on), len(pooled))])
        n_on = len(c.on)
        observed += (ranks[n_on:].sum() - null_mean) * weight
        total += options[rng.integers(0, len(options), N_PERMUTATIONS)]
    eps = 1e-9
    one = (np.sum(total >= observed - eps) + 1) / (N_PERMUTATIONS + 1)
    two = (np.sum(np.abs(total) >= abs(observed) - eps) + 1) / (N_PERMUTATIONS + 1)
    return float(one), float(two)


def sign_test(diffs: np.ndarray) -> tuple[int, int, float]:
    """(cells where optimization on is lower, cells not tied, exact one-sided p).

    Uses only the direction of each within-cell difference, never its size."""
    d = diffs[np.abs(diffs) > 1e-12]
    n, k = len(d), int(np.sum(d > 0))
    p = sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n if n else 1.0
    return k, n, p


def holm(pvalues: list[float]) -> list[float]:
    """Holm-adjusted p-values, in the input order."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted = [0.0] * len(pvalues)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted


def write_builds(arms: dict[bool, dict[Cell, dict[str, float]]]) -> None:
    with BUILDS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["model", "task", "opt_enabled", "build_id", "build_accuracy"])
        for opt in (True, False):
            for m in MODELS:
                for t in TASKS:
                    for build, acc in sorted(arms[opt].get((m, t), {}).items()):
                        w.writerow([m, t, str(opt).lower(), build, f"{acc:.4f}"])


def main() -> None:
    arms = {opt: load_builds(path) for opt, path in RUN_LOGS.items()}
    check_against_table(arms)
    write_builds(arms)
    cells = cell_data(arms)
    rng = np.random.default_rng(SEED)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Per cell, exact.
    cell_rows = []
    for c in cells:
        p1, p2 = cell_exact_test(c)
        cell_rows.append({"model": c.cell[0], "task": c.cell[1], "builds_on": len(c.on),
                          "builds_off": len(c.off), "mean_on_pct": 100 * c.on.mean(),
                          "mean_off_pct": 100 * c.off.mean(), "off_minus_on_pts": 100 * c.diff,
                          "p_one_sided": p1, "p_two_sided": p2})
    with (OUT_DIR / "opt_significance_cells.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(cell_rows[0]))
        w.writeheader()
        for r in cell_rows:
            w.writerow({k: (f"{v:.4f}" if isinstance(v, float) else v) for k, v in r.items()})
    lowest = min(r["p_one_sided"] for r in cell_rows)
    sig_worse = [r for r in cell_rows if r["p_one_sided"] <= ALPHA]
    sig_better = [r for r in cell_rows if r["p_one_sided"] >= 1 - ALPHA + 1e-9
                  and r["p_two_sided"] <= 2 * ALPHA]
    print(f"Per cell (exact, {len(cells)} cells): smallest one-sided p = {lowest:.3f}")
    print(f"  cells where every off build beats every on build (p = 0.05): {len(sig_worse)}")
    for r in sig_worse:
        print(f"    {MODELS[r['model']]} / {TASKS[r['task']]}")
    print(f"  cells where every on build beats every off build: {len(sig_better)}")
    for r in sig_better:
        print(f"    {MODELS[r['model']]} / {TASKS[r['task']]}")

    # 2 and 3. Across cells, by direction only.
    rows = []
    diffs = np.array([c.diff for c in cells])
    k_up, n_sign, p_sign = sign_test(diffs)
    ve1, ve2 = van_elteren_test(cells, rng)
    rows.append({"scope": "all models", "cells": len(cells),
                 "builds": sum(len(c.on) + len(c.off) for c in cells),
                 "cells_on_lower": k_up, "cells_not_tied": n_sign, "sign_p_one_sided": p_sign,
                 "van_elteren_p_one_sided": ve1, "van_elteren_p_two_sided": ve2,
                 "van_elteren_p_holm_one_sided": ""})
    print(f"\nAll models: optimization on is lower in {k_up} of {n_sign} cells that differ "
          f"(sign test one-sided p = {p_sign:.3f})")
    print(f"  stratified rank test: one-sided p = {ve1:.3f}, two-sided p = {ve2:.3f}")

    # 4. Per model.
    per_model = []
    for m in MODELS:
        mc = [c for c in cells if c.cell[0] == m]
        md = np.array([c.diff for c in mc])
        k, n, ps = sign_test(md)
        p1, p2 = van_elteren_test(mc, rng)
        per_model.append((m, mc, k, n, ps, p1, p2))
    adjusted = holm([x[5] for x in per_model])
    print("\nPer model (one-sided = optimization on lower; Holm over the models):")
    for (m, mc, k, n, ps, p1, p2), padj in zip(per_model, adjusted, strict=True):
        rows.append({"scope": m, "cells": len(mc), "builds": sum(len(c.on) + len(c.off) for c in mc),
                     "cells_on_lower": k, "cells_not_tied": n, "sign_p_one_sided": ps,
                     "van_elteren_p_one_sided": p1, "van_elteren_p_two_sided": p2,
                     "van_elteren_p_holm_one_sided": padj})
        print(f"  {MODELS[m]:18} on lower in {k} of {n} tasks   rank test p one-sided {p1:.3f}  "
              f"Holm {padj:.3f}  two-sided {p2:.3f}")

    out = OUT_DIR / "opt_significance.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            w.writerow({k: (f"{v:.4f}" if isinstance(v, float) else v) for k, v in r.items()})
    print(f"\nwrote {BUILDS_CSV}, {out} and {OUT_DIR / 'opt_significance_cells.csv'}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
