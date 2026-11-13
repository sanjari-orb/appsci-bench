"""MMS@3 per task, one bar per model, with the optimization layer enabled.

Grouped bars: one group per task, easiest task first (by mean over models), and one bar per model
inside it, in the model's color and in the same best-first order as the model-average figure. Scores
are shown in percent. Error bars are the std of MMS@3 over resampled trial picks, where the results
table has one. A cell with no result is labeled n/a; an empty slot is a real score of 0.

    python scripts/paper_plots/plot_model_by_task.py

writes figures/model_by_task.{pdf,svg,png,html}.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style  # noqa: E402
from data import (  # noqa: E402
    FULL_BUILDS,
    MODELS,
    TASKS,
    Result,
    grid,
    load_results,
    rank_models,
    rank_tasks,
)

FIGURE_NAME = "model_by_task"
FEWER_BUILDS_MARK = "†"


def two_line(label: str) -> str:
    """Break a task label onto two lines: after "τ-bench:" when present, else at the first space."""
    if ": " in label:
        return label.replace(": ", ":<br>", 1)
    return label.replace(" ", "<br>", 1)


def fewer_builds(r: Result) -> bool:
    return r.k_builds is not None and r.k_builds < FULL_BUILDS


def hover_text(r: Result) -> str:
    if r.score is None:
        return f"<b>{MODELS[r.model]}</b><br>{TASKS[r.task]}: no result"
    std = "n/a" if r.score_std is None else f"{r.score_std * 100:.2f} pts"
    return (f"<b>{MODELS[r.model]}</b><br>{TASKS[r.task]}<br>MMS@3: {style.pct(r.score, 1)} ± {std}"
            f"<br>builds (k): {r.k_builds}")


def build_figure(results: list[Result]) -> go.Figure:
    cells = grid(results, opt_enabled=True)
    models = rank_models(cells)
    tasks = rank_tasks(cells)
    x = [two_line(TASKS[t]) for t in tasks]

    fig = go.Figure()
    for m in models:
        row = [cells[(m, t)] for t in tasks]
        fig.add_trace(go.Bar(
            name=MODELS[m],
            x=x,
            # A zero is left undrawn: plotly renders a zero-height bar as a sliver on the baseline,
            # which reads as a tiny score. A missing cell gets a zero-height bar only so it can
            # carry its n/a label.
            y=[0 if r.score is None else (r.score or None) for r in row],
            marker={"color": style.MODEL_COLORS[m], "cornerradius": 2, "line": {"width": 0}},
            error_y={
                "type": "data",
                # No bar for a zero spread: a flat cap at the baseline reads as a mark.
                "array": [r.score_std if r.score_std else float("nan") for r in row],
                "visible": True, "thickness": 1, "width": 1.5, "color": style.TEXT_SECONDARY,
            },
            # The dagger sits inside the bar at its base, where no error bar can cover it; n/a has
            # no bar to sit in, so it goes above the baseline.
            text=["n/a" if r.score is None else (FEWER_BUILDS_MARK if fewer_builds(r) else "")
                  for r in row],
            textposition=["outside" if r.score is None else "inside" for r in row],
            insidetextanchor="start",
            textangle=0,
            insidetextfont={"size": style.SMALL_FONT_SIZE, "color": style.SURFACE},
            outsidetextfont={"size": style.SMALL_FONT_SIZE - 1, "color": style.TEXT_MUTED},
            cliponaxis=False,
            hovertext=[hover_text(r) for r in row],
            hoverinfo="text",
        ))

    top = max((r.score + (r.score_std or 0)) for r in cells.values() if r.score is not None)
    ymax = max(0.1, math.ceil((top + 0.03) * 10) / 10)

    # No title and no footnote: the caption explains empty slots (a score of 0), n/a, the dagger
    # (fewer than FULL_BUILDS builds), and the error bars (std over trial picks).
    style.apply_paper_layout(fig, width=style.FULL_WIDTH_PX, height=290)
    fig.update_layout(
        barmode="group",
        bargap=0.12,
        bargroupgap=0.06,
        margin={"l": 4, "r": 4, "t": 44, "b": 4},
        legend={
            "orientation": "h", "x": 0, "xanchor": "left", "y": 1.0, "yanchor": "bottom",
            "font": {"size": style.SMALL_FONT_SIZE}, "traceorder": "normal",
            "entrywidth": 0.25, "entrywidthmode": "fraction",
        },
    )
    fig.update_yaxes(
        range=[0, ymax], dtick=0.1, tickformat=".0%",
        showgrid=True, gridcolor=style.GRID, gridwidth=1,
        showline=False, ticks="", zeroline=True, zerolinecolor=style.AXIS, zerolinewidth=1,
        tickfont={"size": style.SMALL_FONT_SIZE, "color": style.TEXT_SECONDARY},
        title={"text": "MMS@3", "standoff": 4,
               "font": {"size": style.SMALL_FONT_SIZE, "color": style.TEXT_SECONDARY}},
    )
    fig.update_xaxes(showline=False, ticks="", tickfont={"size": style.SMALL_FONT_SIZE})
    return fig


def main() -> None:
    fig = build_figure(load_results())
    for path in style.save_figure(fig, FIGURE_NAME):
        print(path)


if __name__ == "__main__":
    main()
