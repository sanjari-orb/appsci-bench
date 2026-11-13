"""MMS@3 per model, with the optimization layer enabled.

Half the text width, to sit beside the radar. One horizontal bar per model in its own color,
best at the top, with scores shown in percent. No whisker: the spread across tasks says how uneven
a model is, not how certain its mean is, and the per-task figures show that spread directly. The
mean is over the tasks every model has a result on, so a model with a missing cell is neither
helped nor hurt by the gap; the script prints any task left out.

    python scripts/paper_plots/plot_model_average.py

writes figures/model_average.* (half width) and figures/model_average_third.* (a third).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style  # noqa: E402
from data import (  # noqa: E402
    MODELS,
    MODELS_SHORT,
    TASKS,
    Result,
    grid,
    load_results,
    mean_score,
    rank_models,
    shared_tasks,
)

FIGURE_NAME = "model_average"


def build_figure(results: list[Result], *, compact: bool = False) -> go.Figure:
    """The figure at half the text width, or at a third of it when `compact`.

    Compact drops to short model names and smaller type.
    """
    cells = grid(results, opt_enabled=True)
    tasks = shared_tasks(cells)
    models = rank_models(cells)
    means = [mean_score(cells, m, tasks) for m in models]
    left_out = [TASKS[t] for t in TASKS if t not in tasks]
    names = MODELS_SHORT if compact else MODELS
    label_size = style.TINY_FONT_SIZE if compact else style.SMALL_FONT_SIZE

    fig = go.Figure(go.Bar(
        x=means,
        y=[names[m] for m in models],
        orientation="h",
        marker={"color": [style.MODEL_COLORS[m] for m in models], "cornerradius": 3,
                "line": {"width": 0}},
        width=0.62,
        hovertext=[f"<b>{MODELS[m]}</b><br>MMS@3, mean of {len(tasks)} tasks: {style.pct(v, 1)}"
                   for m, v in zip(models, means, strict=True)],
        hoverinfo="text",
        showlegend=False,
    ))

    # The mean, in percent, in its own column right of the plot.
    for i, v in enumerate(means):
        fig.add_annotation(
            x=1, xref="paper", xanchor="left", xshift=4, y=i, yref="y", showarrow=False,
            text=f"<b>{style.pct(v)}</b>",
            font={"size": label_size + 1, "color": style.TEXT_PRIMARY},
        )

    top = max(means)
    xmax = max(0.1, math.ceil((top + 0.02) * 10) / 10)

    # No title: the caption says what this is (mean over tasks). A task left out
    # of the mean is reported when the script runs, for the caption to mention.
    if left_out:
        print(f"model average excludes {', '.join(left_out)}: not every model has a score there")
    if compact:
        style.apply_paper_layout(fig, width=style.THIRD_WIDTH_PX, height=style.RESULTS_ROW_HEIGHT_PX)
        fig.update_layout(margin={"l": 2, "r": 30, "t": 14, "b": 2})
    else:
        style.apply_paper_layout(fig, width=style.HALF_WIDTH_PX, height=style.PAIR_HEIGHT_PX)
        fig.update_layout(margin={"l": 2, "r": 34, "t": 14, "b": 4})
    fig.update_yaxes(autorange="reversed", showline=False, ticks="", ticksuffix=" ",
                     tickfont={"size": label_size})
    fig.update_xaxes(
        range=[0, xmax], dtick=0.2, tickformat=".0%",
        showgrid=True, gridcolor=style.GRID, gridwidth=1,
        showline=False, ticks="", zeroline=True, zerolinecolor=style.AXIS, zerolinewidth=1,
        tickfont={"size": label_size - 1, "color": style.TEXT_SECONDARY},
        title={"text": f"Mean MMS@3 over {len(tasks)} tasks", "standoff": 2,
               "font": {"size": label_size - 1, "color": style.TEXT_SECONDARY}},
    )
    return fig


def main() -> None:
    results = load_results()
    for name, compact in ((FIGURE_NAME, False), (f"{FIGURE_NAME}_third", True)):
        for path in style.save_figure(build_figure(results, compact=compact), name):
            print(path)


if __name__ == "__main__":
    main()
