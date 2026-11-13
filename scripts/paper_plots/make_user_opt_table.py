"""LaTeX table: MMS@3 with and without a user, and with and without the optimization layer.

Four settings per model: the builder can or cannot ask a simulated stakeholder (user / no user),
and the optimization layer is on or off. The user rows come from data/results_score_pow_k.csv and
the no-user rows from data/results_nouser_score_pow_k.csv (written by extract_nouser_scores.py).
Only the models the no-user arm was run on appear.

Layout, one block per model, best model first:
  - columns: tasks easiest first (the same order as the other figures), then Overall;
  - cells: MMS@3 in percent with one decimal. User cells carry the std over the ways of picking one
    trial per build as a subscript. No-user cells have none: they ran one trial per build, so there
    is exactly one pick;
  - Overall: the mean over the tasks EVERY row of the table has a score on, with the std across
    those tasks. The caption names any task left out;
  - bold: best of the model's four settings in that column (ties all bold);
  - a dagger marks a cell with fewer than 3 builds, as in the other figures.

    python scripts/paper_plots/make_user_opt_table.py

writes figures/user_opt_ablation/user_opt_ablation_table.tex (the table, with its caption) and
figures/user_opt_ablation/user_opt_ablation_table.csv (the same numbers, one row per model x
setting x column), and prints the table.
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data import (  # noqa: E402
    FULL_BUILDS,
    MODELS,
    NOUSER_RESULTS_CSV,
    TASKS,
    Result,
    grid,
    load_results,
    rank_models,
    rank_tasks,
)

# style.FIGURES_DIR, spelled out: importing style pulls in plotly, and this script draws nothing.
OUT_DIR = Path(__file__).resolve().parent / "figures" / "user_opt_ablation"
TEX_NAME = "user_opt_ablation_table.tex"
CSV_NAME = "user_opt_ablation_table.csv"

# Short task headers; the table is too dense for the full names in data.TASKS.
TASK_HEADERS = {
    "airline_rebooking": r"$\tau$-Airline",
    "order_to_cash": "O2C",
    "customer_call_insights": "Calls",
    "compliance_review": "Compliance",
    "slides_generator": "Slides",
    "crm_insights": "CRM",
    "banking_support": r"$\tau$-Banking",
}
# (user enabled, optimization enabled), in row order: the main setting first.
SETTINGS: list[tuple[bool, bool]] = [(True, True), (True, False), (False, True), (False, False)]
OVERALL = "overall"
MARKS = {True: r"\cmark", False: r"\xmark"}

Cells = dict[tuple[str, str, bool, bool], Result]  # (model, task, user, opt) -> result


def load_cells() -> tuple[Cells, list[str], list[str]]:
    """Every scored or unscored cell, the models to show (best first), and the task order."""
    user = load_results()
    nouser = load_results(NOUSER_RESULTS_CSV)
    cells: Cells = {}
    for r in user:
        cells[(r.model, r.task, True, r.opt_enabled)] = r
    for r in nouser:
        cells[(r.model, r.task, False, r.opt_enabled)] = r
    on = grid(user, opt_enabled=True)
    shown = {r.model for r in nouser if r.score is not None}
    if not shown:
        raise ValueError(f"{NOUSER_RESULTS_CSV} has no scored cell")
    models = [m for m in rank_models(on) if m in shown]
    return cells, models, rank_tasks(on)


def score(cells: Cells, model: str, task: str, user: bool, opt: bool) -> float | None:
    r = cells.get((model, task, user, opt))
    return None if r is None else r.score


def shared_tasks(cells: Cells, models: list[str], tasks: list[str]) -> list[str]:
    """Tasks every row of the table has a score on, in `tasks` order."""
    shared = [t for t in tasks
              if all(score(cells, m, t, u, o) is not None for m in models for u, o in SETTINGS)]
    if not shared:
        raise ValueError("no task has a score in every row, so the Overall column would be empty")
    return shared


def table_values(cells: Cells, models: list[str], tasks: list[str], overall_tasks: list[str]
                 ) -> dict[tuple[str, bool, bool, str], tuple[float | None, float | None, int | None]]:
    """(model, user, opt, column) -> (MMS@3 %, std in points or None, k builds or None)."""
    out = {}
    for m in models:
        for u, o in SETTINGS:
            for t in tasks:
                r = cells.get((m, t, u, o))
                if r is None or r.score is None:
                    out[(m, u, o, t)] = (None, None, None)
                    continue
                # One trial per build leaves a single pick, so its std of 0 says nothing; not shown.
                std = r.score_std * 100 if u and r.score_std is not None else None
                out[(m, u, o, t)] = (r.score * 100, std, r.k_builds)
            vals = [cells[(m, t, u, o)].score * 100 for t in overall_tasks]  # type: ignore[operator]
            out[(m, u, o, OVERALL)] = (sum(vals) / len(vals), statistics.pstdev(vals), None)
    return out


def _cell(value: float | None, std: float | None, k: int | None, best: bool) -> str:
    if value is None:
        return "--"
    text = f"{value:.1f}"
    if best:
        text = rf"\textbf{{{text}}}"
    if k is not None and k < FULL_BUILDS:
        text += r"\dg"
    return text if std is None else rf"\ms{{{text}}}{{{std:.2f}}}"


def render(values, models: list[str], tasks: list[str], overall_tasks: list[str]) -> str:
    cols = tasks + [OVERALL]
    left_out = [TASK_HEADERS[t] for t in tasks if t not in overall_tasks]
    overall_note = ("the unweighted mean over the tasks" if not left_out else
                    "the unweighted mean over the tasks every row has a score on (leaving out "
                    + ", ".join(left_out) + ")")
    n = 2 + len(cols)
    lines = [
        "% MMS@3 with and without a user and the optimization layer. Written by",
        "% scripts/paper_plots/make_user_opt_table.py; rerun it rather than editing by hand.",
        r"% Preamble: \usepackage{booktabs} \usepackage{graphicx} \usepackage{amssymb}",
        r"\providecommand{\ms}[2]{#1\textsubscript{$\pm$#2}}",
        r"\providecommand{\dg}{\textsuperscript{\textdagger}}",
        r"\providecommand{\cmark}{\ensuremath{\checkmark}}",
        r"\providecommand{\xmark}{\ensuremath{\times}}",
        r"\begin{table}[t]",
        r"  \centering",
        r"  \caption{MMS@3 (\%) with and without a user, and with the optimization layer on and",
        r"  off. \emph{User}: the builder gathers requirements by asking a simulated stakeholder;",
        r"  without one, the same requirements are handed over as documents. For each case we",
        r"  take the lowest score among 3 independent builds (one trial from each) and average",
        r"  over cases. With a user each build has 3 trials; the value is the mean over the 27",
        r"  ways of picking one per build, and $\pm$ is the std over those picks. Without a user",
        r"  each build has 1 trial, so there is a single pick and no $\pm$. \emph{Overall} is",
        f"  {overall_note}, and its $\\pm$ is the std across those tasks. --: not run.",
        r"  \textbf{Bold}: best of a model's four settings in that column. \dg Fewer than 3 builds",
        r"  finished (timeout or turn limit); unfinished builds are dropped, not scored as 0.}",
        r"  \label{tab:user-opt-ablation}",
        r"  \setlength{\tabcolsep}{4pt}",
        r"  \resizebox{\textwidth}{!}{%",
        rf"  \begin{{tabular}}{{@{{}}cc {'c' * len(tasks)} c@{{}}}}",
        r"    \toprule",
        rf"    & & \multicolumn{{{len(tasks)}}}{{c}}{{MMS@3 per task}} & MMS@3 \\",
        rf"    \cmidrule(lr){{3-{2 + len(tasks)}}} \cmidrule(l){{{n}-{n}}}",
        "    User & Opt. & " + " & ".join(TASK_HEADERS[t] for t in tasks) + r" & Overall \\",
    ]
    for m in models:
        lines.append(r"    \midrule")
        lines.append(rf"    \multicolumn{{{n}}}{{@{{}}l}}{{\textit{{{MODELS[m]}}}}} \\")
        # Best per column over the model's settings, on the value as printed, so a tie that shows
        # as a tie is bolded as one.
        best = {}
        for c in cols:
            shown = [round(values[(m, u, o, c)][0], 1) for u, o in SETTINGS
                     if values[(m, u, o, c)][0] is not None]
            best[c] = max(shown) if shown else None
        for u, o in SETTINGS:
            marks = f"{MARKS[u]} & {MARKS[o]}"
            row = []
            for c in cols:
                v, s, k = values[(m, u, o, c)]
                row.append(_cell(v, s, k, v is not None and best[c] is not None and best[c] > 0
                                 and round(v, 1) == best[c]))
            lines.append(f"    {marks} & " + " & ".join(row) + r" \\")
    lines += [
        r"    \bottomrule",
        r"  \end{tabular}}",
        r"\end{table}",
    ]
    return "\n".join(lines) + "\n"


def write_csv(values, models: list[str], tasks: list[str], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["model", "user_enabled", "opt_enabled", "column", "mms3_pct",
                         "std_pts", "k_builds"])
        for m in models:
            for u, o in SETTINGS:
                for c in tasks + [OVERALL]:
                    v, s, k = values[(m, u, o, c)]
                    writer.writerow([m, str(u).lower(), str(o).lower(), c,
                                     "" if v is None else f"{v:.2f}",
                                     "" if s is None else f"{s:.2f}", "" if k is None else k])


def main() -> None:
    cells, models, tasks = load_cells()
    overall_tasks = shared_tasks(cells, models, tasks)
    values = table_values(cells, models, tasks, overall_tasks)
    tex = render(values, models, tasks, overall_tasks)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / TEX_NAME).write_text(tex, encoding="utf-8")
    write_csv(values, models, tasks, OUT_DIR / CSV_NAME)
    print(tex)
    missing = [f"{MODELS[m]} / {TASKS[t]} / {'user' if u else 'no user'} / opt {'on' if o else 'off'}"
               for m in models for t in tasks for u, o in SETTINGS
               if score(cells, m, t, u, o) is None]
    if missing:
        print("not run yet:\n  " + "\n  ".join(missing), file=sys.stderr)
    print(f"wrote {OUT_DIR / TEX_NAME} and {OUT_DIR / CSV_NAME}", file=sys.stderr)


if __name__ == "__main__":
    main()
