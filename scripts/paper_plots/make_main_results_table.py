"""LaTeX main results table: MMS@3 per task for every model, in the AppSci agent and, for the
models also run in their own coding harness, in that harness.

Two sections, split by a rule:
  1. models run only in the AppSci agent: one row each, the optimization setting (opt or no opt) with
     the higher overall MMS@3, marked by a superscript on the harness name. The whole row comes
     from that one setting; picking the best setting per task would mix two configurations into
     a score no single setup reached. A tie goes to optimization on, the benchmark's main setting.
     Rows are ordered best first by the overall shown;
  2. the models in FULL_SECTION_MODELS (Opus 5 in Claude Code, GPT-5.5 in Codex): the AppSci
     agent with optimization on, with it off, and the native harness. Models in the order of the
     other figures (optimization-on mean).

Cells are MMS@3 in percent with one decimal; no standard deviations are shown. Overall is the
unweighted mean over the tasks. Bold is the best value in each column over all rows, underline the
second best, a dagger a cell with fewer than 3 builds.

AppSci rows come from data/results_score_pow_k.csv. Harness rows come from
data/harness_score_pow_k.csv, a copy of the harness comparison table (score_power.py over the
harness runs' eval reports; the per-run results are not in the repo). A native harness can be run
with its optimization guidance and without it (`opt_enabled`); each run is its own row, marked like
the AppSci rows, optimization on first.

    python scripts/paper_plots/make_main_results_table.py

writes figures/main_results/main_results_table.tex (printed) and, with every model, setting and
harness, figures/main_results/main_results_detailed_table.tex.
"""

from __future__ import annotations

import csv
import math
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data import (  # noqa: E402
    FULL_BUILDS,
    MODELS,
    MODELS_SHORT,
    TASKS,
    grid,
    load_results,
    rank_models,
    rank_tasks,
)

HERE = Path(__file__).resolve().parent
HARNESS_CSV = HERE / "data" / "harness_score_pow_k.csv"
# style.FIGURES_DIR, spelled out: importing style pulls in plotly, and this script draws nothing.
OUT_DIR = HERE / "figures" / "main_results"
TEX_NAME = "main_results_table.tex"

TASK_HEADERS = {
    "airline_rebooking": r"$\tau$-Airline",
    "order_to_cash": "O2C",
    "customer_call_insights": "Calls",
    "compliance_review": "Compl.",  # the widest header; spelled out in the caption
    "slides_generator": "Slides",
    "crm_insights": "CRM",
    "banking_support": r"$\tau$-Banking",
}
HARNESS_NAMES = {"claude_code": "Claude Code", "codex": "Codex"}
OVERALL = "overall"
# Models shown with every setting in the main table's bottom section; every other model gets one
# row, its best AppSci setting. A model may have a native-harness run and still sit in the top
# section (Opus 4.8): its harness row then appears only in the detailed table.
FULL_SECTION_MODELS = ("claude-opus-5", "gpt-5.5")
DETAILED_TEX_NAME = "main_results_detailed_table.tex"
BEST_HEADING = "Best AppSci setting per model"
FULL_HEADING = "All harnesses"


@dataclass(frozen=True)
class Cell:
    score: float  # MMS@3, 0-1
    std: float | None  # std over trial picks, 0-1; None when not reported
    k: int


@dataclass(frozen=True)
class Row:
    model: str
    harness: str  # LaTeX for the harness column
    cells: dict[str, Cell]  # task -> cell; every task present
    first_of_model: bool  # the model's name is printed only on its first row
    native: str | None = None  # the native harness's plain name; None for an AppSci row

    @property
    def overall(self) -> float:
        return sum(c.score for c in self.cells.values()) / len(self.cells)


def _fraction(raw: str, field: str, row_no: int, *, optional: bool = False) -> float | None:
    raw = raw.strip()
    if not raw and optional:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{HARNESS_CSV.name} row {row_no}: {field}={raw!r} is not a number") from exc
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{HARNESS_CSV.name} row {row_no}: {field}={value} is outside [0, 1]")
    return value


def harness_label(harness: str, opt_enabled: bool) -> str:
    """LaTeX for a native-harness row, marked with its optimization setting like the AppSci rows."""
    return HARNESS_NAMES[harness] + (r"\optOn" if opt_enabled else r"\optOff")


def load_harness(path: Path = HARNESS_CSV) -> dict[tuple[str, str, bool], dict[str, Cell]]:
    """(model, harness, opt_enabled) -> task -> cell. Every harness run must cover every task."""
    out: dict[tuple[str, str, bool], dict[str, Cell]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row_no, row in enumerate(csv.DictReader(f), start=2):
            model, harness, task = row["model"].strip(), row["harness"].strip(), row["task"].strip()
            if model not in MODELS or harness not in HARNESS_NAMES or task not in TASKS:
                raise ValueError(f"{path.name} row {row_no}: unknown model, harness or task "
                                 f"{(model, harness, task)}")
            opt_raw = (row.get("opt_enabled") or "").strip().lower()
            if opt_raw not in ("true", "false"):
                raise ValueError(f"{path.name} row {row_no}: opt_enabled={opt_raw!r} is not true "
                                 f"or false")
            opt = opt_raw == "true"
            k_raw = row["k_builds"].strip()
            if not k_raw.isdigit() or not 1 <= int(k_raw) <= FULL_BUILDS:
                raise ValueError(f"{path.name} row {row_no}: k_builds={k_raw!r} is not 1 to "
                                 f"{FULL_BUILDS}")
            cells = out.setdefault((model, harness, opt), {})
            if task in cells:
                raise ValueError(f"{path.name} row {row_no}: duplicate "
                                 f"{(model, harness, opt_raw, task)}")
            cells[task] = Cell(
                score=_fraction(row["score_pow_k_mean"], "score_pow_k_mean", row_no),  # type: ignore[arg-type]
                std=_fraction(row["score_pow_k_std"], "score_pow_k_std", row_no, optional=True),
                k=int(k_raw))
    for key, cells in out.items():
        missing = [t for t in TASKS if t not in cells]
        if missing:
            raise ValueError(f"{path.name}: {key} has no row for {missing}")
    # Optimization on before off, as for the AppSci rows.
    return dict(sorted(out.items(), key=lambda kv: not kv[0][2]))


def native_rows(harness: dict[tuple[str, str, bool], dict[str, Cell]], model: str) -> list[Row]:
    """The model's native-harness rows, optimization on first."""
    return [Row(model, harness_label(h, opt), cells, False, HARNESS_NAMES[h])
            for (hm, h, opt), cells in harness.items() if hm == model]


def appsci_cells(cells, model: str, arm: str) -> dict[str, Cell]:
    got = {}
    for t in TASKS:
        r = cells[(model, t)]
        if r.score is None or r.k_builds is None:
            raise ValueError(f"{model} / {t} has no score with optimization {arm}")
        got[t] = Cell(r.score, r.score_std, r.k_builds)
    return got


def build_sections() -> tuple[list[Row], list[Row], list[str]]:
    """(best-setting rows, all-settings rows, task column order)."""
    results = load_results()
    on, off = grid(results, opt_enabled=True), grid(results, opt_enabled=False)
    harness = load_harness()
    best, full = [], []
    for m in rank_models(on):
        rows = [Row(m, r"AppSci\optOn", appsci_cells(on, m, "on"), True),
                Row(m, r"AppSci\optOff", appsci_cells(off, m, "off"), False)]
        if m in FULL_SECTION_MODELS:
            native = native_rows(harness, m)
            if not native:
                raise ValueError(f"{m} is listed for the all-settings section but has no "
                                 f"native-harness run in {HARNESS_CSV.name}")
            full += rows + native
        else:
            # Higher overall wins; on a tie, max keeps the first, optimization on.
            pick = max(rows, key=lambda r: round(r.overall, 3))
            best.append(Row(m, pick.harness, pick.cells, True))
    best.sort(key=lambda r: -r.overall)
    return best, full, rank_tasks(on)


def build_detailed() -> tuple[list[Row], list[str]]:
    """Every model (optimization-on order), each with both AppSci settings and any native harness."""
    results = load_results()
    on, off = grid(results, opt_enabled=True), grid(results, opt_enabled=False)
    harness = load_harness()
    rows = []
    for m in rank_models(on):
        rows += [Row(m, r"AppSci\optOn", appsci_cells(on, m, "on"), True),
                 Row(m, r"AppSci\optOff", appsci_cells(off, m, "off"), False)]
        rows += native_rows(harness, m)
    return rows, rank_tasks(on)


def _round1(value: float) -> float:
    """One decimal, halves rounded up: 24.25 (stored as 24.2499...) prints as 24.3, not 24.2."""
    return math.floor(value * 10 + 0.5 + 1e-9) / 10


def _fmt(value: float, k: int | None, mark: str) -> str:
    text = f"{_round1(value):.1f}"
    if mark == "best":
        text = rf"\textbf{{{text}}}"
    elif mark == "second":
        text = rf"\underline{{{text}}}"
    if k is not None and k < FULL_BUILDS:
        text += r"\dg"
    return text


def _harness_list(runs: list[tuple[str, str]]) -> str:
    """[(Opus 5, Claude Code), (Opus 4.8, Claude Code), (GPT-5.5, Codex)] ->
    'Opus 5 and Opus 4.8 in Claude Code, and GPT-5.5 in Codex'."""
    by_harness: dict[str, list[str]] = {}
    for model, harness in runs:
        by_harness.setdefault(harness, []).append(model)
    parts = [f"{' and '.join(models)} in {h}" for h, models in by_harness.items()]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + ", and " + parts[-1]


def render(best: list[Row], full: list[Row], tasks: list[str], *, detailed: bool = False) -> str:
    """The main table, or with detailed=True the full one: every row in `full`, one block per model,
    no section headings."""
    rows = best + full
    cols = tasks + [OVERALL]
    values: dict[tuple[int, str], tuple[float, int | None]] = {}
    for i, row in enumerate(rows):
        for t in tasks:
            values[(i, t)] = (row.cells[t].score * 100, row.cells[t].k)
        values[(i, OVERALL)] = (row.overall * 100, None)
    marks: dict[tuple[int, str], str] = {}
    for c in cols:
        # On the value as printed, so a tie that shows as a tie is marked as one.
        shown = sorted({_round1(values[(i, c)][0]) for i in range(len(rows))}, reverse=True)
        for i in range(len(rows)):
            v = _round1(values[(i, c)][0])
            if shown[0] > 0 and v == shown[0]:
                marks[(i, c)] = "best"
            elif len(shown) > 1 and shown[1] > 0 and v == shown[1]:
                marks[(i, c)] = "second"

    native = [h for h in HARNESS_NAMES.values() if any(r.native == h for r in full)]
    # One entry per model and harness, however many optimization settings it was run in.
    native_runs = list(dict.fromkeys((MODELS_SHORT[r.model], r.native) for r in full if r.native))
    full_models = [MODELS_SHORT[m] for m in dict.fromkeys(r.model for r in full)]
    # Said once in the caption when some native harness was run in both optimization settings.
    both = any(sum(1 for r in full if (r.model, r.native) == (m, h)) > 1 for m, h in
               {(r.model, r.native) for r in full if r.native})
    native_settings = (r" Native-harness rows carry the same marks: \textsuperscript{opt} with the"
                       r" optimization guidance, \textsuperscript{no opt} without it."
                       if both else "")
    n = 2 + len(cols)
    lines = [
        "% Main results: MMS@3 per task. Written by scripts/paper_plots/make_main_results_table.py;",
        "% rerun it rather than editing by hand.",
        r"% Preamble: \usepackage{booktabs}",
        r"\providecommand{\dg}{\textsuperscript{\textdagger}}",
        r"\providecommand{\optOn}{\textsuperscript{opt}}",
        r"\providecommand{\optOff}{\textsuperscript{no opt}}",
        r"\begin{table}[t]",
        r"  \centering",
    ]
    method = [
        r"  \caption{MMS@3 (\%) per task and overall. For each case we take the lowest score among 3",
        r"  independent builds (one trial from each) and average over cases, then average over the",
        r"  27 ways of picking one trial per build. \emph{Overall} is the unweighted mean over the 7",
        r"  tasks. AppSci\optOn{} and AppSci\optOff{} are the AppSci agent with and without its",
        r"  optimization layer.",
    ]
    common = [
        r"  Compl.: compliance review. A build that delivers no usable artifact scores 0 on every",
        r"  case. Tasks are ordered from easiest to hardest. \textbf{Bold}: best in column;",
        r"  \underline{underline}: second best. \dg Fewer than 3 builds finished (timeout or turn",
        r"  limit); unfinished builds are dropped, not scored as 0.}",
    ]
    if detailed:
        lines += method + [
            r"  Every model in both AppSci settings, and " + _harness_list(native_runs) + r"."
            + native_settings,
            r"  Models are ordered by overall score with AppSci\optOn.",
        ] + common + [r"  \label{tab:main-results-detailed}"]
    else:
        lines += method + [
            r"  \emph{" + BEST_HEADING + r"}: models shown in whichever AppSci setting scores",
            r"  higher overall.",
            r"  \emph{" + FULL_HEADING + r"}: " + " and ".join(full_models)
            + r" in both \mbox{AppSci} settings and in their",
            r"  own coding harness (" + " and ".join(native) + r")." + native_settings,
        ] + common + [r"  \label{tab:main-results}"]
    lines += [
        # \small fits the 5.5 in text width; scaling would shrink the text further.
        r"  \small",
        r"  \setlength{\tabcolsep}{3pt}",
        rf"  \begin{{tabular}}{{@{{}}ll {'c' * len(tasks)} c@{{}}}}",
        r"    \toprule",
        rf"    & & \multicolumn{{{len(tasks)}}}{{c}}{{MMS@3 per task}} & MMS@3 \\",
        rf"    \cmidrule(lr){{3-{2 + len(tasks)}}} \cmidrule(l){{{n}-{n}}}",
        "    Model & Harness & " + " & ".join(TASK_HEADERS[t] for t in tasks) + r" & Overall \\",
        r"    \midrule",
    ]
    # A heading row per section, so the table says which rows are one pick per model and which
    # show every harness, without the caption.
    def heading(text: str) -> str:
        return rf"    \multicolumn{{{n}}}{{@{{}}l}}{{\textit{{{text}}}}} \\"

    if best and not detailed:
        lines.append(heading(BEST_HEADING))
    for i, row in enumerate(rows):
        if detailed:
            if i > 0 and row.first_of_model:
                lines.append(r"    \midrule")
        elif i == len(best) and full:
            if best:
                lines.append(r"    \midrule")
            lines.append(heading(FULL_HEADING))
        elif i > len(best) and row.first_of_model:
            lines.append(r"    \addlinespace")
        # Short names, as in the figures: the full ones push the table past the text width.
        name = MODELS_SHORT[row.model] if row.first_of_model else ""
        cells = [_fmt(*values[(i, c)], marks.get((i, c), "")) for c in cols]
        lines.append(f"    {name} & {row.harness} & " + " & ".join(cells) + r" \\")
    lines += [r"    \bottomrule", r"  \end{tabular}", r"\end{table}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    best, full, tasks = build_sections()
    tex = render(best, full, tasks)
    detailed_rows, _ = build_detailed()
    detailed = render([], detailed_rows, tasks, detailed=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / TEX_NAME).write_text(tex, encoding="utf-8")
    (OUT_DIR / DETAILED_TEX_NAME).write_text(detailed, encoding="utf-8")
    print(tex)
    print(f"wrote {OUT_DIR / TEX_NAME} and {OUT_DIR / DETAILED_TEX_NAME}", file=sys.stderr)


if __name__ == "__main__":
    main()
