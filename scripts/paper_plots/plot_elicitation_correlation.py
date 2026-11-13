"""Does drawing out the user's requirements go with a better built agent? One figure, three panels.

Steps, from the per-trial run log (data/elicitation_trials.csv, optimization enabled):
  1. drop invalid trials, and valid builds whose eval never ran (they have no accuracy);
  2. average each build's trials, for one point per build: the share of hidden requirements it
     elicited, and its task accuracy;
  3. correlate the two (Pearson r) within task, over groups of builds:
     (a) each task alone, then all tasks together;
     (b) each capability tier (data.MODEL_TIERS), then all models together;
  4. (c) each model's mean elicited share, to show how the tiers differ in elicitation itself.

Only within-task correlations are computed. Neither accuracy nor the elicited share is comparable
from one task to another, so each task's mean is subtracted from both values before builds from
different tasks are put together; only differences between builds on the same task count. For a
single task that changes nothing, so its r is the plain correlation over its builds.

The unit is the build, not the model x task cell: a tier has only 2 or 3 models, so per-cell points
would leave 2 or 3 per task, too few to correlate. Panel (a) uses builds too, so its "all tasks"
row equals panel (b)'s "all models" row.

Intervals are 95%, from the Fisher z transform, giving up one degree of freedom per task mean
beyond the first. P-values come from permutation tests that shuffle only inside each task, with a
fixed seed; they are printed for the caption, not drawn.

Panel (c) needs no accuracy, so it also counts valid builds whose eval never ran. Per task it takes
the mean over the model's builds, then the mean over the tasks every model has, so every model is
averaged over the same tasks.

Full text width, no figure title: panel letters a, b, c and one shared legend; what each panel
shows and how to read it go in the caption. Both correlation panels share one x range so their bars compare.

    python scripts/paper_plots/plot_elicitation_correlation.py

writes figures/elicitation/elicitation_overview.* (all three panels) and elicitation_tiers.*
(only (b) and (c), relettered a and b, 196 px tall instead of 286: the per-task intervals mostly
cross 0, so the main text uses the tier view and the per-task panel can go in the appendix).
"""

from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style  # noqa: E402
from data import (  # noqa: E402
    MODEL_TIERS,
    MODELS,
    MODELS_SHORT,
    TASKS,
    TASKS_SHORT,
    VALID_STATUSES,
    BuildPoint,
    Trial,
    elicitation_builds,
    load_trials,
    usable_trials,
)

OUT_DIR = style.FIGURES_DIR / "elicitation"
FIGURE_NAME = "elicitation_overview"
RESAMPLES = 10_000
SEED = 20260923
Z95 = 1.959964

WIDTH, HEIGHT = style.FULL_WIDTH_PX, 286
COMPACT_NAME, COMPACT_HEIGHT = "elicitation_tiers", 196  # tier and model panels only
MARGIN = {"l": 56, "r": 6, "t": 18, "b": 70}
PLOT_H = HEIGHT - MARGIN["t"] - MARGIN["b"]  # the panels' height in px, for placing text by px
FONT = style.TINY_FONT_SIZE
BAR = 0.64  # bar thickness in row units

# Soft colors, ordered light to dark from weak to strong, so the order reads even in grayscale.
# As a set they clear the all-pairs colorblind and normal-vision checks; every bar also has a
# label, which covers their low contrast on white.
TIER_COLORS = {"Weak": "#D8A444", "Medium": "#5FA389", "Strong": "#34768A"}
# Each task keeps its own color in panel (a), whatever its rank. A separate soft set, clear of the
# model colors (style.MODEL_COLORS, which mean models everywhere else) and of the tier colors; each
# bar is also labeled with its task, so color never carries identity alone. The slots were checked
# for colorblind and normal-vision separation as neighbors in today's bar order.
TASK_COLORS = {
    "customer_call_insights": "#6F8FD6",  # cornflower
    "order_to_cash": "#E47F6B",  # coral
    "slides_generator": "#3FA3CF",  # sky
    "airline_rebooking": "#8DB04A",  # olive
    "banking_support": "#C372B4",  # orchid
    "compliance_review": "#C7844F",  # clay
    "crm_insights": "#8C7CCB",  # lavender
}
ALL = "#8C8A83"  # all tasks / all models together
WHISKER = style.TEXT_SECONDARY


@dataclass(frozen=True)
class Corr:
    r: float  # NaN when undefined (under 3 points, or no spread)
    lo: float
    hi: float
    p: float
    n: int


@dataclass(frozen=True)
class Row:
    label: str
    color: str
    corr: Corr
    summary: bool = False


def pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson r. NaN for under 3 points or when either side has no spread."""
    n = len(xs)
    if n != len(ys):
        raise ValueError(f"unpaired values: {n} vs {len(ys)}")
    if n < 3:
        return math.nan
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return math.nan
    return sxy / math.sqrt(sxx * syy)


def fisher_ci(r: float, dof: int) -> tuple[float, float]:
    """95% interval for r from the Fisher z transform; `dof` is n - 3 less any means removed."""
    if math.isnan(r) or dof < 1:
        return math.nan, math.nan
    if abs(r) >= 1:
        return r, r
    z, half = math.atanh(r), Z95 / math.sqrt(dof)
    return math.tanh(z - half), math.tanh(z + half)


def permutation_p(xs: list[float], ys: list[float], groups: list[str], rng: random.Random) -> float:
    """Two-sided p for Pearson r, shuffling y only inside each group."""
    observed = abs(pearson(xs, ys))
    if math.isnan(observed):
        return math.nan
    by_group: dict[str, list[int]] = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    hits = 0
    shuffled = list(ys)
    for _ in range(RESAMPLES):
        for idx in by_group.values():
            vals = [ys[i] for i in idx]
            rng.shuffle(vals)
            for i, v in zip(idx, vals, strict=True):
                shuffled[i] = v
        r = pearson(xs, shuffled)
        if not math.isnan(r) and abs(r) >= observed - 1e-12:
            hits += 1
    # The +1s count the observed order as one of the shuffles, so p is never exactly 0.
    return (hits + 1) / (RESAMPLES + 1)


def within_task(points: list[BuildPoint], rng: random.Random) -> Corr:
    """r after subtracting each task's mean from both values. A task with one build is dropped:
    centered, it is 0 on both axes and says nothing, but it would still cost a degree of freedom."""
    by_task: dict[str, list[BuildPoint]] = {}
    for p in points:
        by_task.setdefault(p.task, []).append(p)
    by_task = {t: ps for t, ps in by_task.items() if len(ps) >= 2}
    xs, ys, groups = [], [], []
    for t, ps in by_task.items():
        mx = sum(p.elicited for p in ps) / len(ps)
        my = sum(p.accuracy for p in ps) / len(ps)
        xs += [p.elicited - mx for p in ps]
        ys += [p.accuracy - my for p in ps]
        groups += [t] * len(ps)
    r = pearson(xs, ys)
    return Corr(r, *fisher_ci(r, len(xs) - 3 - max(0, len(by_task) - 1)),
                permutation_p(xs, ys, groups, rng), len(xs))


def task_rows(builds: list[BuildPoint], rng: random.Random) -> list[Row]:
    """One row per task, highest r first (undefined last), then all tasks together."""
    rows = [Row(TASKS_SHORT[t], TASK_COLORS[t],
                within_task([b for b in builds if b.task == t], rng))
            for t in TASKS]
    rows = [r for r in rows if r.corr.n]
    rows.sort(key=lambda r: -r.corr.r if not math.isnan(r.corr.r) else math.inf)
    return rows + [Row("All tasks", ALL, within_task(builds, rng), True)]


def tier_rows(builds: list[BuildPoint], rng: random.Random) -> list[Row]:
    """One row per tier, weakest first, then all models together."""
    rows = [Row(tier, TIER_COLORS[tier], within_task([b for b in builds if b.model in ms], rng))
            for tier, ms in MODEL_TIERS.items()]
    return rows + [Row("All models", ALL, within_task(builds, rng), True)]


def model_elicitation(trials: list[Trial]) -> tuple[dict[str, float], list[str]]:
    """Each model's mean elicited share over the tasks every model has (see the module notes).
    Returns (model -> mean, the tasks used)."""
    per_build: dict[tuple[str, str, str], list[float]] = {}
    for t in trials:
        if t.status in VALID_STATUSES and t.elicited is not None:
            per_build.setdefault((t.model, t.task, t.build_id), []).append(t.elicited)
    per_cell: dict[tuple[str, str], list[float]] = {}
    for (m, task, _), vals in per_build.items():
        per_cell.setdefault((m, task), []).append(sum(vals) / len(vals))
    tasks = [t for t in TASKS if all((m, t) in per_cell for m in MODELS)]
    if not tasks:
        raise ValueError("no task has an elicitation score for every model")
    return ({m: sum(sum(per_cell[(m, t)]) / len(per_cell[(m, t)]) for t in tasks) / len(tasks)
             for m in MODELS}, tasks)


def r_range(*groups: list[Row]) -> list[float]:
    """One x range for both correlation panels: from the lowest whisker (in steps of 0.25) to
    past 1, leaving room right of the longest whisker for its value."""
    lows = [v for rows in groups for r in rows for v in (r.corr.lo, r.corr.r) if not math.isnan(v)]
    lo = min(0.0, min(lows, default=0.0))
    return [max(-1.0, math.floor(lo * 4) / 4) - 0.05, 1.12]


def _px(offset: float, plot_h: float = PLOT_H) -> float:
    """A vertical offset in px from the panels' bottom edge, as a paper coordinate."""
    return offset / plot_h


def _correlation_panel(fig: go.Figure, col: int, rows: list[Row]) -> None:
    for y, row in enumerate(rows):
        c = row.corr
        if math.isnan(c.r):
            fig.add_annotation(x=0.02, y=y, xanchor="left", showarrow=False, text="n/a",
                               font={"size": FONT, "color": style.TEXT_MUTED}, row=1, col=col)
            continue
        has_ci = not math.isnan(c.lo)
        fig.add_trace(go.Bar(
            y=[y], x=[c.r if c.r else None], orientation="h", width=BAR, showlegend=False,
            marker={"color": row.color, "line": {"width": 0}, "cornerradius": 3},
            error_x={"type": "data", "symmetric": False,
                     "array": [c.hi - c.r if has_ci else math.nan],
                     "arrayminus": [c.r - c.lo if has_ci else math.nan],
                     "color": WHISKER, "thickness": 1, "width": 2},
            hovertext=[f"<b>{row.label}</b><br>r = {c.r:.2f} [{c.lo:.2f}, {c.hi:.2f}]"
                       f"<br>p = {c.p:.3f}<br>{c.n} builds"], hoverinfo="text",
        ), row=1, col=col)
        # The value just past the whisker's end, where it cannot overlap anything.
        end = c.hi if has_ci else max(c.r, 0)
        fig.add_annotation(x=end, y=y, xanchor="left", xshift=3, showarrow=False,
                           text=f"<b>{c.r:.2f}</b>" if row.summary else f"{c.r:.2f}",
                           font={"size": FONT, "color": style.TEXT_PRIMARY}, row=1, col=col)
    n = len(rows)
    axis = fig.get_subplot(1, col)
    xname = axis.xaxis.plotly_name.replace("axis", "")
    yname = axis.yaxis.plotly_name.replace("axis", "")
    fig.add_shape(type="line", xref=f"{xname} domain", x0=0, x1=1, yref=yname, y0=n - 1.5,
                  y1=n - 1.5, line={"color": style.AXIS, "width": 1}, opacity=1)
    fig.update_yaxes(tickvals=list(range(n)), range=[n - 0.5, -0.5], row=1, col=col,
                     ticktext=[f"<b>{r.label}</b>" if r.summary else r.label for r in rows])


def _model_panel(fig: go.Figure, col: int, trials: list[Trial]) -> tuple[list, list[str]]:
    means, tasks = model_elicitation(trials)
    # Weakest tier on top, as in panel (b); best first inside a tier.
    order = [(tier, m) for tier, ms in MODEL_TIERS.items()
             for m in sorted(ms, key=lambda m: -means[m])]
    ys = list(range(len(order)))
    fig.add_trace(go.Bar(
        y=ys, x=[means[m] or None for _, m in order], orientation="h", width=0.7,
        marker={"color": [TIER_COLORS[tier] for tier, _ in order], "line": {"width": 0},
                "cornerradius": 3},
        text=[style.pct(means[m]) for _, m in order], textposition="outside", cliponaxis=False,
        textfont={"size": FONT, "color": style.TEXT_PRIMARY},
        hovertext=[f"<b>{MODELS[m]}</b> ({tier})<br>mean elicited {style.pct(means[m], 1)}"
                   for tier, m in order], hoverinfo="text", showlegend=False,
    ), row=1, col=col)
    axis = fig.get_subplot(1, col)
    xname = axis.xaxis.plotly_name.replace("axis", "")
    yname = axis.yaxis.plotly_name.replace("axis", "")
    for y in ys[1:]:
        if order[y][0] != order[y - 1][0]:  # a hairline between tiers
            fig.add_shape(type="line", xref=f"{xname} domain", x0=0, x1=1, yref=yname,
                          y0=y - 0.5, y1=y - 0.5, line={"color": style.GRID, "width": 1},
                          opacity=1)
    top = max(means.values(), default=0.0)
    fig.update_xaxes(range=[0, min(1.0, top + 0.2)], dtick=0.25, tickformat=".0%",
                     row=1, col=col)
    fig.update_yaxes(tickvals=ys, ticktext=[MODELS_SHORT[m] for _, m in order],
                     range=[len(order) - 0.5, -0.5], row=1, col=col)
    return [(MODELS[m], tier, means[m]) for tier, m in order], tasks


def _legend(fig: go.Figure, plot_h: float = PLOT_H,
            all_label: str = "All tasks / all models") -> None:
    """Two rows under the panels, drawn by hand so every entry sits exactly where it should:
    the tiers with their models, then the all-tasks gray and the whisker. Tasks need no entry:
    each bar in panel (a) is labeled."""
    def px(offset: float) -> float:
        return _px(offset, plot_h)

    def swatch(x: float, y: float, color: str) -> None:
        fig.add_shape(type="rect", xref="paper", yref="paper", x0=x, x1=x + 0.022,
                      y0=y - px(4), y1=y + px(4), fillcolor=color, line={"width": 0},
                      opacity=1)

    def text(x: float, y: float, body: str) -> None:
        fig.add_annotation(x=x, y=y, xref="paper", yref="paper", xanchor="left",
                           yanchor="middle", showarrow=False, text=body,
                           font={"size": FONT, "color": style.TEXT_PRIMARY})

    y1, y2 = -px(46), -px(61)
    muted = f"<span style='color:{style.TEXT_SECONDARY}'>"
    x = -0.09
    for tier, ms in MODEL_TIERS.items():
        swatch(x, y1, TIER_COLORS[tier])
        text(x + 0.028, y1, f"<b>{tier}</b> {muted}{', '.join(MODELS_SHORT[m] for m in ms)}</span>")
        x += {"Weak": 0.33, "Medium": 0.30}.get(tier, 0)
    swatch(-0.09, y2, ALL)
    text(-0.062, y2, all_label)
    # A whisker glyph: a line with two end caps.
    x0, x1 = 0.20, 0.24
    for shape in ({"x0": x0, "x1": x1, "y0": y2, "y1": y2},
                  {"x0": x0, "x1": x0, "y0": y2 - px(3), "y1": y2 + px(3)},
                  {"x0": x1, "x1": x1, "y0": y2 - px(3), "y1": y2 + px(3)}):
        fig.add_shape(type="line", xref="paper", yref="paper", **shape,
                      line={"color": WHISKER, "width": 1}, opacity=1)
    text(x1 + 0.008, y2, "95% confidence interval")


def build_figure(trials: list[Trial], by_task: list[Row], by_tier: list[Row], *,
                 compact: bool = False) -> tuple[go.Figure, list, list[str]]:
    """All three panels, or with `compact` only the tier and per-model panels, shorter: the
    per-task panel's intervals mostly cross 0, so the main text can do without it."""
    height = COMPACT_HEIGHT if compact else HEIGHT
    plot_h = height - MARGIN["t"] - MARGIN["b"]
    if compact:
        fig = make_subplots(rows=1, cols=2, column_widths=[0.5, 0.5], horizontal_spacing=0.17)
        _correlation_panel(fig, 1, by_tier)
        model_rows, tasks = _model_panel(fig, 2, trials)
        corr_cols, model_col = (1,), 2
    else:
        fig = make_subplots(rows=1, cols=3, column_widths=[0.34, 0.31, 0.35],
                            horizontal_spacing=0.135)
        _correlation_panel(fig, 1, by_task)
        _correlation_panel(fig, 2, by_tier)
        model_rows, tasks = _model_panel(fig, 3, trials)
        corr_cols, model_col = (1, 2), 3

    x_range = r_range(by_tier) if compact else r_range(by_task, by_tier)
    style.apply_paper_layout(fig, width=WIDTH, height=height)
    fig.update_layout(margin=MARGIN, showlegend=False, font={"size": FONT})
    tick_font = {"size": FONT - 0.5, "color": style.TEXT_SECONDARY}
    title_font = {"size": FONT, "color": style.TEXT_SECONDARY}
    fig.update_xaxes(showgrid=True, gridcolor=style.GRID, showline=False, ticks="",
                     tickfont=tick_font, title={"font": title_font, "standoff": 3})
    fig.update_yaxes(showgrid=False, zeroline=False, showline=False, ticks="",
                     tickfont={"size": FONT, "color": style.TEXT_PRIMARY})
    for col in corr_cols:
        fig.update_xaxes(range=x_range, dtick=0.5, tickformat=".1f", zeroline=True,
                         zerolinecolor=style.AXIS, zerolinewidth=1,
                         title_text="Within-task correlation r", row=1, col=col)
    fig.update_xaxes(zeroline=False, row=1, col=model_col,
                     title_text=f"Mean elicited ({len(tasks)} tasks)")

    # Panel letters only, for the caption to refer to; the caption says what each panel shows and
    # how to read it.
    for col in range(1, model_col + 1):
        x0 = fig.get_subplot(1, col).xaxis.domain[0]
        # Later panels have their labels left of the plot; put the letter over them too.
        fig.add_annotation(
            x=-0.1 if col == 1 else x0 - 0.1, y=1 + _px(3, plot_h), xref="paper", yref="paper",
            xanchor="left", yanchor="bottom", showarrow=False, text=f"<b>{'abc'[col - 1]}</b>",
            font={"size": FONT + 1, "color": style.TEXT_PRIMARY})

    _legend(fig, plot_h, "All models" if compact else "All tasks / all models")
    return fig, model_rows, tasks


def main() -> None:
    trials = load_trials()
    builds = elicitation_builds(usable_trials(trials))
    if len(builds) < 3:
        raise SystemExit(f"need at least 3 builds to correlate, got {len(builds)}")
    rng = random.Random(SEED)
    by_task, by_tier = task_rows(builds, rng), tier_rows(builds, rng)
    fig, model_rows, _ = build_figure(trials, by_task, by_tier)
    print(f"{len(builds)} builds with an accuracy")
    for header, rows in (("by task", by_task), ("by tier", by_tier)):
        print(header)
        for row in rows:
            c = row.corr
            p = "p < 0.001" if c.p < 0.001 else f"p = {c.p:.3f}"
            print(f"  {row.label:12} r = {c.r:+.2f} [{c.lo:+.2f}, {c.hi:+.2f}]  {p}  n = {c.n}")
    print("mean elicited by model")
    for name, tier, mean in model_rows:
        print(f"  {name:20} {tier:7} {style.pct(mean, 1)}")
    compact, _, _ = build_figure(trials, by_task, by_tier, compact=True)
    for figure, name in ((fig, FIGURE_NAME), (compact, COMPACT_NAME)):
        for path in style.save_figure(figure, name, OUT_DIR):
            print(path)


if __name__ == "__main__":
    main()
