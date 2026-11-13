"""Every model's task profile on one radar.

Half the text width, to sit beside the model-average bars at the same height. One spoke per task,
in the easiest-first task order, and one closed shape per model in its color and
marker shape, with a light fill. Models are drawn best first, so the larger shapes go down first and
a smaller one is never buried under a bigger fill. A model that scores 0 on every task is a point at
the center; the legend still lists it, and says so. The legend uses short model names; the mean per
model is in the bar chart that sits beside this one.

    python scripts/paper_plots/plot_model_radar.py

writes figures/model_radar.* (half width) and figures/model_radar_third.* (a third).
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
    TASKS_SHORT,
    Result,
    grid,
    load_results,
    rank_models,
    rank_tasks,
)

FIGURE_NAME = "model_radar"
FILL_ALPHA = 0.10
# Compact: plotly's legend rows are too tall for the short results row, so the legend is drawn by
# hand in a band under the radar: two columns, a short line with the marker, then the name.
COMPACT_MARGIN = {"l": 4, "r": 4, "t": 12, "b": 2}
LEGEND_ROW_PX = 10
LEGEND_COL_PX = 84
RING = "#d9d8d2"  # rings and spokes: visible behind the shapes, still recessive


def closed(values: list) -> list:
    """Repeat the first point at the end so the polygon closes."""
    if not values:
        raise ValueError("a radar needs at least one spoke")
    return [*values, values[0]]


def _compact_legend(fig: go.Figure, models: list[str], plot_w: float, plot_h: float) -> float:
    """Draw the legend at the bottom of the plot area and return its height in px.

    The swatches are traces on a hidden cartesian axis laid over the plot area in px (0 at the
    bottom left), so they carry the same marker symbols as the radar.
    """
    rows = math.ceil(len(models) / 2)
    height = rows * LEGEND_ROW_PX
    fig.update_layout(
        xaxis={"domain": [0, 1], "range": [0, plot_w], "visible": False, "fixedrange": True},
        yaxis={"domain": [0, 1], "range": [0, plot_h], "visible": False, "fixedrange": True},
    )
    for i, m in enumerate(models):
        x0 = (plot_w - 2 * LEGEND_COL_PX) / 2 + (i % 2) * LEGEND_COL_PX + 2
        y = height - (i // 2 + 0.5) * LEGEND_ROW_PX
        color = style.MODEL_COLORS[m]
        fig.add_trace(go.Scatter(x=[x0, x0 + 12], y=[y, y], mode="lines", xaxis="x", yaxis="y",
                                 line={"color": color, "width": 1.3}, hoverinfo="skip",
                                 showlegend=False))
        fig.add_trace(go.Scatter(x=[x0 + 6], y=[y], mode="markers", xaxis="x", yaxis="y",
                                 marker={"size": 4.5, "color": color,
                                         "symbol": style.MODEL_SYMBOLS[m],
                                         "line": {"width": 0.6, "color": style.SURFACE}},
                                 hoverinfo="skip", showlegend=False))
        fig.add_annotation(x=x0 + 15, y=y, xref="x", yref="y", xanchor="left", yanchor="middle",
                           showarrow=False, text=MODELS_SHORT[m],
                           font={"size": style.TINY_FONT_SIZE - 1, "color": style.TEXT_PRIMARY})
    return height


def build_figure(results: list[Result], *, compact: bool = False) -> go.Figure:
    """The figure at half the text width, or at a third of it when `compact`.

    Compact uses smaller type, marks, and legend, drops the "(all 0)" tags (the model-average bars
    beside it show those zeros), and leaves the rings unlabeled; the subtitle gives their step.
    """
    cells = grid(results, opt_enabled=True)
    models = rank_models(cells)
    tasks = rank_tasks(cells)
    theta = closed([TASKS_SHORT[t] for t in tasks])
    small = style.TINY_FONT_SIZE if compact else style.SMALL_FONT_SIZE

    fig = go.Figure()
    for m in models:
        vals = [cells[(m, t)].score for t in tasks]
        color = style.MODEL_COLORS[m]
        all_zero = all(not v for v in vals)
        fig.add_trace(go.Scatterpolar(
            # A missing cell draws at 0 on its spoke; the hover says it has no result.
            r=closed([v or 0 for v in vals]),
            theta=theta,
            mode="lines+markers",
            fill="toself",
            fillcolor=style.hex_to_rgba(color, FILL_ALPHA),
            line={"color": color, "width": 1.3 if compact else 1.8},
            marker={"size": 4 if compact else 6, "color": color,
                    "symbol": style.MODEL_SYMBOLS[m],
                    "line": {"width": 0.6 if compact else 0.8, "color": style.SURFACE}},
            # Short names keep the legend to two columns; the means are in the bar chart beside
            # this one.
            name=MODELS_SHORT[m] + (" (all 0)" if all_zero and not compact else ""),
            hovertext=closed([f"<b>{MODELS[m]}</b><br>{TASKS[t]}: "
                              + ("no result" if v is None else style.pct(v, 1))
                              for t, v in zip(tasks, vals, strict=True)]),
            hoverinfo="text",
        ))

    top = max((r.score or 0) for r in cells.values())
    rmax = max(0.1, math.ceil((top + 0.02) * 10) / 10)
    # Radius labels run halfway between the first two spokes, clear of both. The first spoke is
    # at 90 degrees and they go clockwise.
    tick_angle = 90 - 180 / len(tasks)
    rings = [round(0.2 * i, 1) for i in range(1, int(rmax / 0.2 + 1e-9) + 1)]  # no label at 0
    if compact:
        style.apply_paper_layout(fig, width=style.THIRD_WIDTH_PX, height=style.RESULTS_ROW_HEIGHT_PX)
        # No title: the caption says what this is and that rings are every 20%.
        margin = COMPACT_MARGIN
        plot_w = style.THIRD_WIDTH_PX - margin["l"] - margin["r"]
        plot_h = style.RESULTS_ROW_HEIGHT_PX - margin["t"] - margin["b"]
        legend_h = _compact_legend(fig, models, plot_w, plot_h)
        # Room under the radar for the lower spoke labels, then the legend.
        domain = {"x": [0.1, 0.9], "y": [(legend_h + 12) / plot_h, 0.97]}
    else:
        style.apply_paper_layout(fig, width=style.HALF_WIDTH_PX, height=style.PAIR_HEIGHT_PX)
        margin = {"l": 6, "r": 6, "t": 16, "b": 2}
        domain = {"x": [0.12, 0.88], "y": [0.4, 0.97]}
    fig.update_layout(
        margin=margin,
        polar={
            # The radar takes the top of the panel; the legend fills the band under it.
            "domain": domain,
            "bgcolor": style.SURFACE,
            # Compact labels only the outermost ring, which sets the scale; the inner labels would
            # crowd the small radar, and the caption gives their step.
            "radialaxis": {"range": [0, rmax], "tickvals": rings,
                           "ticktext": [style.pct(v) if not compact or v == rings[-1] else ""
                                        for v in rings],
                           "showgrid": True, "gridcolor": RING, "showline": False, "ticks": "",
                           "angle": tick_angle, "tickangle": tick_angle,
                           "tickfont": {"size": style.SMALL_FONT_SIZE - 3,
                                        "color": style.TEXT_MUTED}},
            "angularaxis": {"showgrid": True, "gridcolor": RING, "linecolor": style.AXIS,
                            "ticks": "", "direction": "clockwise", "rotation": 90,
                            "tickfont": {"size": small - 1, "color": style.TEXT_SECONDARY}},
        },
        showlegend=not compact,
        legend={"orientation": "h", "x": 0.5, "xanchor": "center", "y": 0.0, "yanchor": "bottom",
                "font": {"size": small - 1}, "traceorder": "normal",
                "entrywidth": 0.5, "entrywidthmode": "fraction", "itemwidth": 30,
                "tracegroupgap": 0},
    )
    return fig


def main() -> None:
    results = load_results()
    for name, compact in ((FIGURE_NAME, False), (f"{FIGURE_NAME}_third", True)):
        for path in style.save_figure(build_figure(results, compact=compact), name):
            print(path)


if __name__ == "__main__":
    main()
