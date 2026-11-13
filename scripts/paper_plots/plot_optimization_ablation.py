"""MMS@3 with the optimization layer off versus on, as dash-to-dot arrows.

For each model, a gray dash marks its score with optimization off and a dot in the model's color
marks its score with it on. A vertical arrow runs from the dash to the dot, so an arrow pointing
down means optimization lowered the score and one pointing up means it raised it. Models sit on the
x axis, best first by the optimization-on mean; nothing is drawn at a slant.

  average: one chart, the mean over tasks, with the change in points above each model.
  by task: the same chart once per task, in a grid, easiest task first, plus a panel for the mean.

A model is averaged over the tasks it has in BOTH arms, so the dash and the dot always cover the
same work; the figure names any task left out.

    python scripts/paper_plots/plot_optimization_ablation.py

writes figures/optimization/opt_ablation_average.* and figures/optimization/opt_ablation_by_task.*.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style  # noqa: E402
from data import (  # noqa: E402
    MODELS,
    MODELS_SHORT,
    TASKS,
    Result,
    grid,
    load_results,
    rank_models,
    rank_tasks,
)

OUT_DIR = style.FIGURES_DIR / "optimization"
OFF_COLOR = style.TEXT_SECONDARY
MIN_ARROW = 0.03  # changes under 3 points get no arrow: it would be only a stray arrowhead


def paired_tasks(on: dict[tuple[str, str], Result], off: dict[tuple[str, str], Result],
                 model: str) -> list[str]:
    """Tasks where the model has a score in both arms, in TASKS order."""
    return [t for t in TASKS
            if on[(model, t)].score is not None and off[(model, t)].score is not None]


def arm_mean(cells: dict[tuple[str, str], Result], model: str, tasks: list[str]) -> float:
    if not tasks:
        raise ValueError(f"{model} has no task scored in both arms")
    return sum(cells[(model, t)].score for t in tasks) / len(tasks)  # type: ignore[misc]


def add_arrows(fig: go.Figure, models: list[str], offs: list[float | None],
               ons: list[float | None], *, row: int | None = None, col: int | None = None,
               dash_size: int, dot_size: int, where: str) -> None:
    """One model per x position: dash at the off score, dot at the on score, arrow between."""
    xs = list(range(len(models)))
    kw = {} if row is None else {"row": row, "col": col}
    fig.add_trace(go.Scatter(
        x=[x for x, v in zip(xs, offs, strict=True) if v is not None],
        y=[v for v in offs if v is not None], mode="markers", name="Optimization off",
        showlegend=False,
        marker={"symbol": "line-ew", "size": dash_size, "line": {"color": OFF_COLOR, "width": 3}},
        hovertext=[f"<b>{MODELS[m]}</b><br>{where}<br>optimization off: {style.pct(v, 1)}"
                   for m, v in zip(models, offs, strict=True) if v is not None],
        hoverinfo="text",
    ), **kw)
    fig.add_trace(go.Scatter(
        x=[x for x, v in zip(xs, ons, strict=True) if v is not None],
        y=[v for v in ons if v is not None], mode="markers", name="Optimization on",
        showlegend=False,
        marker={"symbol": "circle", "size": dot_size,
                "color": [style.MODEL_COLORS[m] for m, v in zip(models, ons, strict=True)
                          if v is not None],
                "line": {"color": style.SURFACE, "width": 1}},
        hovertext=[f"<b>{MODELS[m]}</b><br>{where}<br>optimization on: {style.pct(v, 1)}"
                   for m, v in zip(models, ons, strict=True) if v is not None],
        hoverinfo="text",
    ), **kw)
    # Arrows are annotations in the panel's own axes: from the dash (tail) to the dot (head).
    xref = "x" if row is None else fig.get_subplot(row, col).xaxis.plotly_name.replace("axis", "")
    yref = "y" if row is None else fig.get_subplot(row, col).yaxis.plotly_name.replace("axis", "")
    for x, m, a, b in zip(xs, models, ons, offs, strict=True):
        if a is None or b is None or abs(a - b) < MIN_ARROW:
            continue
        fig.add_annotation(
            x=x, y=a, ax=x, ay=b, xref=xref, yref=yref, axref=xref, ayref=yref, text="",
            # Small heads and tight gaps at both ends, so a change of a few points still shows a
            # shaft and not a lone arrowhead.
            showarrow=True, arrowhead=2, arrowsize=0.8 if dot_size >= 10 else 0.6,
            arrowwidth=1.6, arrowcolor=style.MODEL_COLORS[m], standoff=dot_size / 2,
            startstandoff=1,
        )


def build_average(results: list[Result]) -> go.Figure:
    on, off = grid(results, opt_enabled=True), grid(results, opt_enabled=False)
    models = rank_models(on)
    paired = {m: paired_tasks(on, off, m) for m in models}
    mean_on = [arm_mean(on, m, paired[m]) for m in models]
    mean_off = [arm_mean(off, m, paired[m]) for m in models]

    fig = go.Figure()
    add_arrows(fig, models, mean_off, mean_on, dash_size=22, dot_size=11,
               where="mean over tasks")
    # Legend in neutral gray: a colored dot would read as one model.
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name="Optimization off",
                             marker={"symbol": "line-ew", "size": 18,
                                     "line": {"color": OFF_COLOR, "width": 3}}))
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name="Optimization on",
                             marker={"symbol": "circle", "size": 10, "color": OFF_COLOR}))
    # The change in points above the higher of each model's two marks. The sign carries the
    # direction, so the number never relies on color.
    for x, (a, b) in enumerate(zip(mean_on, mean_off, strict=True)):
        delta = round((a - b) * 100, 1)
        fig.add_annotation(x=x, y=max(a, b), yshift=14, showarrow=False,
                           text=f"<b>{delta:+.1f}</b>" if delta else "<b>0.0</b>",
                           font={"size": style.SMALL_FONT_SIZE, "color": style.TEXT_PRIMARY})

    top = max(*mean_on, *mean_off)
    ymax = max(0.1, math.ceil((top + 0.06) * 10) / 10)
    # No title: the caption explains the marks (arrow from off to on, number = change in points).
    # A task a model lacks in either arm is reported here, for the caption to mention.
    for m in models:
        if len(paired[m]) < len(TASKS):
            print(f"{MODELS[m]} average leaves out "
                  f"{', '.join(TASKS[t] for t in TASKS if t not in paired[m])}")
    style.apply_paper_layout(fig, width=style.FULL_WIDTH_PX, height=250)
    fig.update_layout(
        margin={"l": 4, "r": 4, "t": 22, "b": 4},
        legend={"orientation": "h", "x": 1, "xanchor": "right", "y": 1.0, "yanchor": "bottom",
                "font": {"size": style.SMALL_FONT_SIZE}},
    )
    fig.update_xaxes(tickvals=list(range(len(models))),
                     ticktext=[MODELS_SHORT[m] for m in models], tickangle=0,
                     range=[-0.5, len(models) - 0.5], showgrid=False, showline=False, ticks="",
                     zeroline=False, tickfont={"size": style.SMALL_FONT_SIZE})
    fig.update_yaxes(range=[-0.02, ymax], dtick=0.1, tickformat=".0%", showgrid=True,
                     gridcolor=style.GRID, zeroline=False, showline=False, ticks="",
                     tickfont={"size": style.SMALL_FONT_SIZE - 1, "color": style.TEXT_SECONDARY},
                     title={"text": "MMS@3, mean of tasks", "standoff": 4,
                            "font": {"size": style.SMALL_FONT_SIZE - 1,
                                     "color": style.TEXT_SECONDARY}})
    return fig


def build_by_task(results: list[Result]) -> go.Figure:
    on, off = grid(results, opt_enabled=True), grid(results, opt_enabled=False)
    models = rank_models(on)
    tasks = rank_tasks(on)
    cols = 4
    panels: list[str | None] = [*tasks, None]  # None is the mean over tasks
    rows = math.ceil(len(panels) / cols)
    titles = [TASKS[t] if t else "<b>Mean over tasks</b>" for t in panels]
    fig = make_subplots(rows=rows, cols=cols, shared_yaxes=True, subplot_titles=titles,
                        horizontal_spacing=0.03, vertical_spacing=0.16)

    tops = []
    for k, t in enumerate(panels):
        row, col = k // cols + 1, k % cols + 1
        if t is None:
            ons = [arm_mean(on, m, paired_tasks(on, off, m)) for m in models]
            offs = [arm_mean(off, m, paired_tasks(on, off, m)) for m in models]
            where = "mean over tasks"
        else:
            ons = [on[(m, t)].score for m in models]
            offs = [off[(m, t)].score for m in models]
            where = TASKS[t]
        tops += [v for v in (*ons, *offs) if v is not None]
        add_arrows(fig, models, offs, ons, row=row, col=col, dash_size=8, dot_size=7,
                   where=where)

    # The two marks first, in neutral gray (a colored dot would read as one model), then a legend
    # entry per model for the dot colors: the panels have no room for model names.
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name="Optimization off",
                             marker={"symbol": "line-ew", "size": 12,
                                     "line": {"color": OFF_COLOR, "width": 3}},
                             hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name="Optimization on",
                             marker={"symbol": "circle", "size": 8, "color": OFF_COLOR},
                             hoverinfo="skip"))
    for m in models:
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=MODELS_SHORT[m],
                                 marker={"symbol": "circle", "size": 8,
                                         "color": style.MODEL_COLORS[m]}, hoverinfo="skip"))

    ymax = max(0.1, math.ceil((max(tops) + 0.03) * 10) / 10)
    # No title: the caption explains the panels (MMS@3, models best first, left to right).
    style.apply_paper_layout(fig, width=style.FULL_WIDTH_PX, height=410)
    fig.update_layout(
        margin={"l": 4, "r": 4, "t": 22, "b": 70},
        # Under the grid: above it, the legend would crowd the title and the panel titles.
        legend={"orientation": "h", "x": 0, "xanchor": "left", "y": -0.05, "yanchor": "top",
                "font": {"size": style.SMALL_FONT_SIZE}, "traceorder": "normal",
                "entrywidth": 0.25, "entrywidthmode": "fraction"},
    )
    fig.update_xaxes(range=[-0.6, len(models) - 0.4], showticklabels=False, showgrid=False,
                     showline=False, ticks="", zeroline=False)
    fig.update_yaxes(range=[-0.03, ymax], dtick=0.2, tickformat=".0%", showgrid=True,
                     gridcolor=style.GRID, zeroline=False, showline=False, ticks="",
                     tickfont={"size": style.SMALL_FONT_SIZE - 1, "color": style.TEXT_SECONDARY})
    fig.update_yaxes(title={"text": "MMS@3", "standoff": 2,
                            "font": {"size": style.SMALL_FONT_SIZE - 1,
                                     "color": style.TEXT_SECONDARY}}, col=1)
    for ann in fig.layout.annotations:
        if ann.text:  # the panel titles; the arrows have no text
            ann.update(font={"size": style.SMALL_FONT_SIZE, "color": style.TEXT_PRIMARY})
    return fig


def main() -> None:
    results = load_results()
    for name, build in (("opt_ablation_average", build_average),
                        ("opt_ablation_by_task", build_by_task)):
        for path in style.save_figure(build(results), name, OUT_DIR):
            print(path)


if __name__ == "__main__":
    main()
