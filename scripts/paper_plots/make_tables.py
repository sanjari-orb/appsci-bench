"""LaTeX tables for the paper, from the same results table the figures use.

  headline_table.tex  per model, optimization enabled: MMS@3 and mean task accuracy, each the mean
                      over tasks ± the population std across tasks, and the number of valid builds
                      the cells were computed on (out of 3 per task).
  ablation_table.tex  MMS@3 per model and task under each setting: optimization enabled, disabled,
                      and the two no-user arms (blank until they have results).
  internal_external_table.tex  per task, Claude Opus 4.8: when the self-grade changed, how often
                      the held-out score changed too (and in the same direction); when the
                      held-out score changed, how often the self-grade missed it; and the mean
                      first-to-final change on the agent's own and the held-out eval.

    python scripts/paper_plots/make_tables.py

writes figures/tables/*.tex and prints them.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data import (  # noqa: E402
    CO_BOTH,
    CO_BOTH_SAME,
    CO_HELD_OUT,
    CO_INTERNAL,
    CO_MISSED,
    CONSISTENCY_MODEL,
    FULL_BUILDS,
    MODELS,
    TASKS,
    TASKS_SHORT,
    Result,
    Steps,
    grid,
    load_results,
    load_steps,
    rank_models,
    shared_tasks,
)

# Beside the figures, but without importing style: nothing here needs plotly.
OUT_DIR = Path(__file__).resolve().parent / "figures" / "tables"


def _tex(text: str) -> str:
    return text.replace("τ", r"$\tau$")


def _pct(value: float | None, digits: int = 1) -> str:
    return "--" if value is None else f"{value * 100:.{digits}f}"


def _mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        raise ValueError("cannot average over zero values")
    mean = sum(values) / len(values)
    return mean, math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _cell_values(cells: dict[tuple[str, str], Result], model: str, tasks: list[str],
                 attr: str) -> list[float]:
    values = [getattr(cells[(model, t)], attr) for t in tasks]
    if any(v is None for v in values):
        raise ValueError(f"{model} has no {attr} on one of {tasks}")
    return values  # type: ignore[return-value]


def headline_table(results: list[Result]) -> str:
    cells = grid(results, opt_enabled=True)
    tasks = shared_tasks(cells)
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\small",
        r"\caption{Benchmark results with optimization enabled. Both scores are the mean over the "
        rf"{len(tasks)} tasks $\pm$ the standard deviation across tasks, in percent. MMS@3 takes, "
        r"for each evaluation sample, the lowest score among a model's three builds; mean task "
        r"accuracy averages each build's trial scores, then averages the builds. Valid builds "
        r"counts the builds each cell was computed on, out of "
        rf"{FULL_BUILDS * len(tasks)}; builds that failed to produce a usable artifact count and "
        r"score 0, builds excluded from scoring do not "
        r"(Appendix~\ref{sec:appendix-coverage}).}",
        r"\label{tab:headline}",
        r"\begin{tabular}{@{}lccc@{}}",
        r"\toprule",
        r"\textbf{Model} & \textbf{MMS@3} & \textbf{Mean task accuracy} & \textbf{Valid builds} \\",
        r"\midrule",
    ]
    for m in rank_models(cells):
        mms, mms_sd = _mean_std(_cell_values(cells, m, tasks, "score"))
        acc, acc_sd = _mean_std(_cell_values(cells, m, tasks, "trial_mean"))
        builds = sum(cells[(m, t)].k_builds or 0 for t in tasks)
        lines.append(rf"{MODELS[m]} & {_pct(mms)} $\pm$ {_pct(mms_sd)} & "
                     rf"{_pct(acc)} $\pm$ {_pct(acc_sd)} & {builds} / {FULL_BUILDS * len(tasks)} \\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(lines) + "\n"


def ablation_table(results: list[Result]) -> str:
    on, off = grid(results, opt_enabled=True), grid(results, opt_enabled=False)
    models = rank_models(on)
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\small",
        r"\caption{MMS@3 (\%) for every model and task under each setting. \emph{Default}: the "
        r"builder elicits requirements from the simulated user. \emph{No user}: the builder gets "
        r"the task documents instead of a user. \emph{Opt.}: the optimization step is enabled "
        r"(on) or disabled (off). -- marks a setting without results.}",
        r"\label{tab:ablation}",
        r"\begin{tabular}{@{}llcccc@{}}",
        r"\toprule",
        r" & & \multicolumn{2}{c}{\textbf{Default}} & \multicolumn{2}{c}{\textbf{No user}} \\",
        r"\cmidrule(lr){3-4}\cmidrule(lr){5-6}",
        r"\textbf{Task} & \textbf{Model} & Opt.\ on & Opt.\ off & Opt.\ on & Opt.\ off \\",
    ]

    def row(label: str, model: str, a: float | None, b: float | None, digits: int) -> str:
        return (rf"{label} & {MODELS[model]} & {_pct(a, digits)} & {_pct(b, digits)} & -- & -- \\")

    for t in TASKS:
        lines.append(r"\midrule")
        for i, m in enumerate(models):
            label = rf"\multirow{{{len(models)}}}{{*}}{{{_tex(TASKS[t])}}}" if i == 0 else ""
            lines.append(row(label, m, on[(m, t)].score, off[(m, t)].score, 0))
    lines.append(r"\midrule")
    tasks_on, tasks_off = shared_tasks(on), shared_tasks(off)
    for i, m in enumerate(models):
        label = (rf"\multirow{{{len(models)}}}{{*}}{{\textit{{Mean over tasks}}}}"
                 if i == 0 else "")
        a = sum(_cell_values(on, m, tasks_on, "score")) / len(tasks_on)
        b = sum(_cell_values(off, m, tasks_off, "score")) / len(tasks_off)
        lines.append(row(label, m, a, b, 1))
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(lines) + "\n"


def _change(value: float | None) -> str:
    """A change in percentage points, signed, with a real minus sign; 0.0 unsigned."""
    if value is None:
        return "--"
    text = f"{value * 100:.1f}"
    if text in ("0.0", "-0.0"):
        return "0.0"
    return f"$-${text[1:]}" if text.startswith("-") else f"+{text}"


def _head(*lines: str) -> str:
    """A column header, stacked one line per argument and bottom-aligned, with no extra package."""
    if len(lines) == 1:
        return lines[0]
    return r"\begin{tabular}[b]{@{}c@{}}" + r"\\".join(lines) + r"\end{tabular}"


def _count_share(count: int, share: float | None) -> str:
    r""""12 (60\%)", or "--" when the share is undefined (nothing to divide by)."""
    return "--" if share is None else rf"{count} ({share * 100:.0f}\%)"


def internal_external_table(steps: dict[str, Steps]) -> str:
    """Did the two evals change together (data.co_change)? When the self-grade changed, how often
    the held-out score changed too; when the held-out score changed, how often the self-grade
    missed it; and the first-to-final change on both evals."""
    if not steps:
        raise ValueError("no task has a step to tabulate")
    total = sum(steps.values(), Steps())
    # Held-out most often following the self-grade first, then more steps.
    tasks = sorted(steps, key=lambda t: (steps[t].held_out_follows is None,
                                         -(steps[t].held_out_follows or 0), -steps[t].total))
    groups = [r"\multicolumn{3}{c}{\textbf{Self-grade changed}}",
              r"\multicolumn{2}{c}{\textbf{Held-out changed}}",
              r"\multicolumn{2}{c}{\textbf{" + _head(r"First $\to$ final", "change (pts)") + "}}"]
    columns = [r"\textbf{Task}", "Builds", "Steps", "Steps", _head("Held-out", "also changed"),
               _head("Same", "direction"), "Steps", _head("Self-grade", "missed"),
               _head("Own", "eval"), _head("Held-out", "eval")]
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\small",
        r"\setlength{\tabcolsep}{3pt}",
        rf"\caption{{Do the builder's own evaluation (self-grade) and the held-out evaluation "
        rf"change together? {MODELS[CONSISTENCY_MODEL]}, optimization enabled. A step is a move to "
        r"a new program within a build. The self-grade \emph{changed} if its score moved at all; "
        r"the held-out score \emph{changed} if it rose or fell by at least one standard deviation "
        r"of its noise. Left: of the steps where the self-grade changed, how many also changed "
        r"the held-out score, and how many of those in the same direction. Middle: of the steps "
        r"where the held-out score changed, how many the self-grade missed by staying flat. "
        r"First $\to$ final: mean change per build from the first to the final graded deploy.}",
        r"\label{tab:internal-external}",
        r"\footnotesize",
        r"\begin{tabular}{@{}lcc ccc cc cc@{}}",
        r"\toprule",
        " & & & " + " & ".join(groups) + r" \\",
        r"\cmidrule(lr){4-6}\cmidrule(lr){7-8}\cmidrule(lr){9-10}",
        " & ".join(columns) + r" \\",
        r"\midrule",
    ]

    def row(label: str, s: Steps) -> str:
        c = s.co
        same = (_count_share(c[CO_BOTH_SAME], c[CO_BOTH_SAME] / c[CO_INTERNAL])
                if c[CO_INTERNAL] else "--")
        return (rf"{label} & {s.builds} & {s.total} & {c[CO_INTERNAL]} & "
                rf"{_count_share(c[CO_BOTH], s.held_out_follows)} & {same} & "
                rf"{c[CO_HELD_OUT]} & {_count_share(c[CO_MISSED], s.internal_misses)} & "
                rf"{_change(s.mean_internal_change)} & {_change(s.mean_external_change)} \\")

    lines += [row(_tex(TASKS_SHORT[t]), steps[t]) for t in tasks]
    lines += [r"\midrule", row(r"\textit{All tasks}", total),
              r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    results = load_results()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in (("headline_table", headline_table(results)),
                       ("ablation_table", ablation_table(results)),
                       ("internal_external_table", internal_external_table(load_steps()))):
        path = OUT_DIR / f"{name}.tex"
        path.write_text(text, encoding="utf-8")
        print(f"% {path}\n{text}")


if __name__ == "__main__":
    main()
