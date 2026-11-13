"""MMS@3 per model when the user or the optimization layer is taken away, against the full setting.

The base is the benchmark's main setting: the builder can ask a simulated stakeholder (user) and the
optimization layer is on. For each model a gray bar marks the base score, and up to three marks in
the model's color and shape sit on it, one per other setting, always in this order left to right:
optimization off (filled mark), no user (hollow mark), and no user with optimization off (hollow
mark with a dot). An arrow runs from the base level to each mark, so an arrow pointing up means the
setting scored higher than the base, and the number above or below it is the change in points.
Models sit on the x axis, best first by the base score.

The no-user arm was run on only some models. For the rest, its two slots are labeled n/a: the
setting was not run, which is different from a score of 0.

Scores are the mean over tasks of MMS@3. A model is averaged over the tasks it has in every setting
it was run in, so every mark covers the same work as its base; the script prints any task left out.
User scores come from data/results_score_pow_k.csv, no-user scores from
data/results_nouser_score_pow_k.csv (see extract_nouser_scores.py).

    python scripts/paper_plots/plot_user_ablation.py

writes figures/user_opt_ablation/user_ablation_average.* at the full text width, like the
optimization ablation average.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path

import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style  # noqa: E402
from data import (  # noqa: E402
    MODELS,
    MODELS_SHORT,
    NOUSER_RESULTS_CSV,
    TASKS,
    Result,
    grid,
    load_results,
    rank_models,
)

OUT_DIR = style.FIGURES_DIR / "user_opt_ablation"
FIGURE_NAME = "user_ablation_average"
BASE_COLOR = style.TEXT_SECONDARY
BASE = (True, True)  # (user enabled, optimization enabled)
# The settings compared with the base, left to right on each model, with their legend labels and
# the circle style that tells them apart: solid, a hollow ring, or a light tint inside a ring. Every
# mark is a circle in the model's color. The per-model shapes other figures use are not needed:
# each model has its own column with its name under it, so identity never rests on color here.
SOLID, RING, TINT = "solid", "ring", "tint"
OTHERS: list[tuple[tuple[bool, bool], str, str]] = [
    ((True, False), "Optimization off", SOLID),
    ((False, True), "No user", RING),
    ((False, False), "No user, optimization off", TINT),
]
SPREAD = 0.34  # x distance between a model's settings; the model sits at the middle one
BAR_OVERHANG = 0.1  # how far the base bar runs past the outer marks; keeps neighbours apart
MARK_SIZE = 10
TINT_ALPHA = 0.3
STEM_ALPHA = 0.55
LABEL_SHIFT = 11  # px from a mark to its change label
LABEL_FONT_SIZE = 8  # one step under TINY: seven models leave each label about 25 px of width

Cells = dict[tuple[str, str, bool, bool], Result]  # (model, task, user, opt) -> result


def load_cells() -> tuple[Cells, list[str]]:
    """Every cell of both result tables, and every model, best first by the base score."""
    user, nouser = load_results(), load_results(NOUSER_RESULTS_CSV)
    cells: Cells = {(r.model, r.task, True, r.opt_enabled): r for r in user}
    cells.update({(r.model, r.task, False, r.opt_enabled): r for r in nouser})
    return cells, rank_models(grid(user, opt_enabled=True))


def has_setting(cells: Cells, model: str, setting: tuple[bool, bool]) -> bool:
    """Whether the model was run in this setting at all (any task scored)."""
    u, o = setting
    return any((r := cells.get((model, t, u, o))) is not None and r.score is not None
               for t in TASKS)


def complete_tasks(cells: Cells, model: str, settings: list[tuple[bool, bool]]) -> list[str]:
    """Tasks where the model has a score in every one of `settings`, in TASKS order."""
    return [t for t in TASKS
            if all((r := cells.get((model, t, u, o))) is not None and r.score is not None
                   for u, o in settings)]


def setting_mean(cells: Cells, model: str, setting: tuple[bool, bool], tasks: list[str]) -> float:
    if not tasks:
        raise ValueError(f"{model} has no task scored in every setting it was run in")
    u, o = setting
    return sum(cells[(model, t, u, o)].score for t in tasks) / len(tasks)  # type: ignore[misc]


def tint(color: str, alpha: float) -> str:
    """`color` laid at `alpha` over the white page, as an opaque color. A see-through fill would
    show the stem running through the circle."""
    h = color.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"expected a #rrggbb color, got {color!r}")
    r, g, b = (int(h[k:k + 2], 16) for k in (0, 2, 4))
    return "#" + "".join(f"{round(255 - alpha * (255 - c)):02x}" for c in (r, g, b))


def circle(color: str, fill: str) -> dict:
    """Marker settings for one setting's circle in `color`."""
    if fill == SOLID:
        return {"symbol": "circle", "size": MARK_SIZE, "color": color,
                "line": {"color": style.SURFACE, "width": 1}}
    inside = style.SURFACE if fill == RING else tint(color, TINT_ALPHA)
    return {"symbol": "circle", "size": MARK_SIZE, "color": inside,
            "line": {"color": color, "width": 2}}


@dataclass(frozen=True)
class ModelRow:
    model: str
    base: float  # mean MMS@3 in the base setting
    n_tasks: int  # tasks the means are over
    values: list[float | None]  # one per OTHERS setting; None when the model was not run in it


def summarize(cells: Cells, models: list[str]) -> list[ModelRow]:
    """Per model, the base mean and each other setting's mean, over the same tasks."""
    rows = []
    for m in models:
        run = [s for s, _, _ in OTHERS if has_setting(cells, m, s)]
        tasks = complete_tasks(cells, m, [BASE, *run])
        if len(tasks) < len(TASKS):
            print(f"{MODELS[m]} average leaves out "
                  f"{', '.join(TASKS[t] for t in TASKS if t not in tasks)}")
        rows.append(ModelRow(
            model=m, base=setting_mean(cells, m, BASE, tasks), n_tasks=len(tasks),
            values=[setting_mean(cells, m, s, tasks) if s in run else None for s, _, _ in OTHERS]))
    return rows


def slot_xs(i: int) -> list[float]:
    """x of each OTHERS setting for the model at position i; the model sits at the middle one."""
    return [i + (k - 1) * SPREAD for k in range(len(OTHERS))]


def add_change_label(fig: go.Figure, x: float, y: float, delta_pts: float, *, up: bool) -> None:
    """The change in points on the far side of a mark from the base; the sign carries the
    direction, so the number never relies on position or color."""
    delta = round(delta_pts, 1)
    fig.add_annotation(x=x, y=y, yshift=LABEL_SHIFT if up else -LABEL_SHIFT, showarrow=False,
                       text=f"<b>{delta:+.1f}</b>" if delta else "<b>0.0</b>",
                       font={"size": LABEL_FONT_SIZE, "color": style.TEXT_PRIMARY})


def add_not_run(fig: go.Figure, x: float, y: float) -> None:
    """A setting the model was not run in: said in words at the base level, never drawn."""
    fig.add_annotation(x=x, y=y, yshift=9, showarrow=False, text="n/a",
                       font={"size": style.TINY_FONT_SIZE, "color": style.TEXT_MUTED})


def style_axes(fig: go.Figure, ticktext: list[str], n_models: int) -> None:
    style.apply_paper_layout(fig, width=style.FULL_WIDTH_PX, height=290)
    fig.update_layout(
        margin={"l": 4, "r": 12, "t": 40, "b": 4},
        legend={"orientation": "h", "x": 0.5, "xanchor": "center", "y": 1.0, "yanchor": "bottom",
                "font": {"size": style.SMALL_FONT_SIZE - 1}},
    )
    fig.update_xaxes(tickvals=list(range(n_models)), ticktext=ticktext, tickangle=0,
                     range=[-0.5, n_models - 0.5], showgrid=False, showline=False, ticks="",
                     zeroline=False, tickfont={"size": style.SMALL_FONT_SIZE})


def y_title(text: str) -> dict:
    return {"text": text, "standoff": 4,
            "font": {"size": style.SMALL_FONT_SIZE - 1, "color": style.TEXT_SECONDARY}}


def build_figure(rows: list[ModelRow]) -> go.Figure:
    """Lollipop version: the base as a bar at its score, each setting a circle on a stem."""
    fig = go.Figure()
    marks = []
    for i, row in enumerate(rows):
        m, base = row.model, row.base
        marks.append(base)
        xs = slot_xs(i)
        # The base as a bar across the model's three slots, so every stem starts on it.
        fig.add_trace(go.Scatter(
            x=[xs[0] - BAR_OVERHANG, xs[-1] + BAR_OVERHANG], y=[base, base], mode="lines",
            line={"color": BASE_COLOR, "width": 2.5}, showlegend=False,
            hovertext=f"<b>{MODELS[m]}</b><br>user, optimization on: {style.pct(base, 1)}"
                      f"<br>mean over {row.n_tasks} tasks", hoverinfo="text"))
        # The base score itself, under the middle of its bar: marks above the base put their labels
        # above, and one below the base sits in an outer slot, so this spot stays free.
        fig.add_annotation(x=i, y=base, yshift=-LABEL_SHIFT, showarrow=False,
                           text=f"{base * 100:.1f}",
                           font={"size": LABEL_FONT_SIZE, "color": style.TEXT_SECONDARY})
        for x, (_, label, fill), v in zip(xs, OTHERS, row.values, strict=True):
            if v is None:
                add_not_run(fig, x, base)
                continue
            marks.append(v)
            color = style.MODEL_COLORS[m]
            # A stem from the base to the mark; drawn first so the circle sits on top of it.
            fig.add_trace(go.Scatter(
                x=[x, x], y=[base, v], mode="lines", showlegend=False, hoverinfo="skip",
                line={"color": style.hex_to_rgba(color, STEM_ALPHA), "width": 2}))
            fig.add_trace(go.Scatter(
                x=[x], y=[v], mode="markers", showlegend=False, marker=circle(color, fill),
                hovertext=f"<b>{MODELS[m]}</b><br>{label}: {style.pct(v, 1)}"
                          f"<br>change from base: {(v - base) * 100:+.1f} pts",
                hoverinfo="text"))
            add_change_label(fig, x, v, (v - base) * 100, up=v >= base)

    # Legend in neutral gray: a colored mark would read as one model. The base line, then one
    # entry per setting in its fill.
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines", name="Base: user, optimization on",
                             line={"color": BASE_COLOR, "width": 3}))
    for _, label, fill in OTHERS:
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=label,
                                 marker=circle(BASE_COLOR, fill)))

    style_axes(fig, [MODELS_SHORT[r.model] for r in rows], len(rows))
    ymax = min(1.0, math.ceil((max(marks) + 0.08) * 10) / 10)
    # Room under 0 for the base label of a model that scores 0.
    fig.update_yaxes(range=[-0.08, ymax], dtick=0.1, tickformat=".0%", showgrid=True,
                     gridcolor=style.GRID, zeroline=False, showline=False, ticks="",
                     tickfont={"size": style.SMALL_FONT_SIZE - 1, "color": style.TEXT_SECONDARY},
                     title=y_title("MMS@3, mean of tasks"))
    return fig


def main() -> None:
    cells, models = load_cells()
    rows = summarize(cells, models)
    for path in style.save_figure(build_figure(rows), FIGURE_NAME, OUT_DIR):
        print(path)


if __name__ == "__main__":
    main()
