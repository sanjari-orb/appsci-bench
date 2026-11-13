"""Safety scores per model per task, from the per-trial run log (optimization enabled).

The evals score safety alongside the task, with 0 weight in the task accuracy, so it tells whether
a build stayed safe while doing the work. The scores come from each trial's per-metric breakdown
(data/elicitation_trials.csv, "breakdown" column); every metric named safety_* counts:

  safety_no_fabrication        no invented facts or records
  safety_safe_by_construction  no unsafe actions or outputs

Not every task has them: order to cash and τ-bench: Banking report none, and slides reports only
safe-by-construction. Those gaps are shown as blank, never as 0.

Steps:
  1. keep valid trials (VALID, VALID*) that have a breakdown; a trial with no breakdown, or a metric
     marked DROPPED on a trial, has no score for it and is skipped, not counted as 0;
  2. per build, average each safety metric over the build's trials; per model x task, average the
     builds, so each build counts once however many trials it has;
  3. "safety" for a cell is the mean of its safety metrics, each weighted equally;
  4. two overall means per model: "shared", over the tasks where every model has a safety score
     (comparable across models), and "own tasks", over every task the model has (more of its data,
     but not the same tasks as the others; the count is printed next to it).

    python scripts/paper_plots/compute_safety.py

prints the tables and writes figures/safety/safety_by_model_task.csv (one row per task x model x
metric, including the combined "safety", with build and trial counts).
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style  # noqa: E402
from data import (  # noqa: E402
    MODELS,
    MODELS_SHORT,
    TASKS,
    TASKS_SHORT,
    VALID_STATUSES,
    Trial,
    load_trials,
)

OUT_CSV = style.FIGURES_DIR / "safety" / "safety_by_model_task.csv"
SAFETY_PREFIX = "safety_"
COMBINED = "safety"


@dataclass(frozen=True)
class Cell:
    value: float
    n_builds: int
    n_trials: int


def _mean(values: list[float]) -> float:
    if not values:
        raise ValueError("cannot average zero values")
    return sum(values) / len(values)


def safety_cells(trials: list[Trial]) -> tuple[dict[tuple[str, str, str], Cell], dict[str, int]]:
    """(model, task, metric) -> Cell for every safety metric, plus the combined "safety".

    Also returns counts of valid trials skipped for having no breakdown: "no eval" (the build's
    eval never ran, so there is no accuracy either) and "no breakdown" (the eval ran but reported
    only an overall score).
    """
    skipped = {"no eval": 0, "no breakdown": 0}
    # (model, task, build) -> metric -> scores over the build's trials
    builds: dict[tuple[str, str, str], dict[str, list[float]]] = {}
    for t in trials:
        if t.status not in VALID_STATUSES:
            continue
        if t.metrics is None:
            skipped["no eval" if t.accuracy is None else "no breakdown"] += 1
            continue
        for name, score in t.metrics.items():
            if name.startswith(SAFETY_PREFIX) and score is not None:
                builds.setdefault((t.model, t.task, t.build_id), {}).setdefault(name, []).append(
                    score)

    # (model, task, metric) -> per-build means, and the trial count behind them
    per_build: dict[tuple[str, str, str], list[float]] = {}
    trial_counts: dict[tuple[str, str, str], int] = {}
    for (model, task, _), metrics in builds.items():
        for name, scores in metrics.items():
            key = (model, task, name)
            per_build.setdefault(key, []).append(_mean(scores))
            trial_counts[key] = trial_counts.get(key, 0) + len(scores)
    cells = {key: Cell(_mean(vals), len(vals), trial_counts[key]) for key, vals in per_build.items()}

    # The combined score: each metric the cell has, weighted equally. Its counts are the largest
    # of its metrics', since a trial scored on any metric took part.
    for model in MODELS:
        for task in TASKS:
            parts = [c for (m, t, _), c in cells.items() if m == model and t == task]
            if parts:
                cells[(model, task, COMBINED)] = Cell(
                    _mean([c.value for c in parts]), max(c.n_builds for c in parts),
                    max(c.n_trials for c in parts))
    return cells, skipped


def main() -> None:
    cells, skipped = safety_cells(load_trials())
    if not cells:
        raise SystemExit("no valid trial has a safety score")
    metrics = sorted({name for _, _, name in cells if name != COMBINED})
    tasks = [t for t in TASKS if any((m, t, COMBINED) in cells for m in MODELS)]
    no_safety = [TASKS[t] for t in TASKS if t not in tasks]
    shared = [t for t in tasks if all((m, t, COMBINED) in cells for m in MODELS)]

    def pct(key: tuple[str, str, str]) -> str:
        return style.pct(cells[key].value, 1) if key in cells else "–"

    print(f"Safety (mean of {', '.join(metrics)}), per model and task, optimization enabled.")
    print(f"No safety metrics: {', '.join(no_safety) or 'none'}. Valid trials skipped: "
          f"{skipped['no eval']} whose build was never evaluated, {skipped['no breakdown']} "
          f"evaluated with no per-metric breakdown.\n")
    header = (f"{'':14}" + "".join(f"{TASKS_SHORT[t]:>12}" for t in tasks)
              + f"{'Shared':>10}{'Own tasks':>14}")
    print(header)
    for m in MODELS:
        line = f"{MODELS_SHORT[m]:14}" + "".join(f"{pct((m, t, COMBINED)):>12}" for t in tasks)
        # Shared: the tasks every model has, so models compare. Own: every task this model has,
        # which uses more of its data but not the same tasks as the others.
        shared_mean = (style.pct(_mean([cells[(m, t, COMBINED)].value for t in shared]), 1)
                       if shared else "–")
        own = [cells[(m, t, COMBINED)].value for t in tasks if (m, t, COMBINED) in cells]
        own_mean = f"{style.pct(_mean(own), 1)} ({len(own)})" if own else "–"
        print(line + f"{shared_mean:>10}{own_mean:>14}")
    print(f"\nShared: mean over the {len(shared)} tasks every model has "
          f"({', '.join(TASKS_SHORT[t] for t in shared) or 'none'}); comparable across models.")
    print("Own tasks: mean over every task the model has (count in brackets); not comparable "
          "across models with different counts.")

    for name in metrics:
        print(f"\n{name}")
        print(f"{'':14}" + "".join(f"{TASKS_SHORT[t]:>12}" for t in tasks))
        for m in MODELS:
            print(f"{MODELS_SHORT[m]:14}" + "".join(f"{pct((m, t, name)):>12}" for t in tasks))

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["task", "model", "metric", "mean", "n_builds", "n_trials"])
        for t in TASKS:
            for m in MODELS:
                for name in [*metrics, COMBINED]:
                    if (m, t, name) in cells:
                        c = cells[(m, t, name)]
                        writer.writerow([t, m, name, f"{c.value:.4f}", c.n_builds, c.n_trials])
    print(f"\nwrote {OUT_CSV}")


if __name__ == "__main__":
    main()
