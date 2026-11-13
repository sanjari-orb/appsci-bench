"""Accuracy against token cost per model, with the Pareto frontier. Optimization enabled.

Two panels side by side, same x axis:
  (a) MMS@3, the mean over tasks (as in the model average);
  (b) mean task accuracy: the plain mean of trial scores per cell (the sheet's trial mean), then
      the mean over tasks.
x: build-phase tokens per build (data.mean_build_tokens: per task the mean over valid builds,
   failed ones included, then the mean over tasks), on a log scale and reversed, so fewer tokens is
   on the right and the best trade-offs sit at the top right.

A model is on the frontier when no other model scores at least as high for fewer tokens. The
frontier is drawn as a shaded region: its top edge runs straight from one frontier model to the
next, and it covers everything those models beat (more tokens and a lower score). Frontier models
get bold labels. Tokens are counts, not money; model prices differ, so a dollar version would need
a price per model.

Full text width, no title (the caption says what this is).

    python scripts/paper_plots/plot_cost_pareto.py

writes figures/cost_pareto.*.
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
    load_trials,
    mean_build_tokens,
    mean_score,
    pareto_front,
    shared_tasks,
)

FIGURE_NAME = "cost_pareto"
WIDTH, HEIGHT = style.FULL_WIDTH_PX, 240
FONT = style.SMALL_FONT_SIZE - 1
REGION = style.hex_to_rgba("#2E9C88", 0.16)  # soft teal, no model's color
REGION_EDGE = style.hex_to_rgba("#2E9C88", 0.55)
# Where each label sits relative to its marker (plotly textposition). The x axis is reversed, so
# "left" is toward more tokens. Keyed by model id so a label stays with its model.
LABEL_POSITION = {
    "claude-opus-5": "top center", "claude-opus-4-8": "top center", "gpt-5.5": "top center",
    "kimi-k3": "middle right", "claude-sonnet-4-6": "middle right",
    # Points near 0% sit on or near the region's bottom edge; their labels go under it (the y axis
    # starts a little below 0 to make room), clear of the models just above.
    "claude-haiku-4-5": "bottom center", "gpt-oss-120b": "bottom center",
}
# Per-panel exceptions, where a point sits at a different height: in (b) GPT-OSS is at ~6%, so a
# label under it would cross the region's bottom edge.
LABEL_POSITION_BY_PANEL = {2: {"gpt-oss-120b": "middle left"}}


def mean_trial_accuracy(cells: dict[tuple[str, str], Result], model: str,
                        tasks: list[str]) -> float:
    vals = [cells[(model, t)].trial_mean for t in tasks]
    if not vals or any(v is None for v in vals):
        raise ValueError(f"{model} has no trial mean on one of {tasks}")
    return sum(vals) / len(vals)  # type: ignore[arg-type]


def _panel(fig: go.Figure, col: int, points: dict[str, tuple[float, float]],
           x_edge: float) -> list[str]:
    """One panel: the frontier region, then every model. Returns the frontier models."""
    front = pareto_front(points)
    # The region: from the axis edge at the most-token frontier model's score, along the frontier
    # (straight from model to model), down to 0 at the fewest-token frontier model, back along 0.
    by_tokens_desc = sorted(front, key=lambda m: -points[m][0])
    top = points[by_tokens_desc[0]]
    fewest = points[by_tokens_desc[-1]]
    xs = [x_edge] + [points[m][0] for m in by_tokens_desc] + [fewest[0], x_edge, x_edge]
    ys = [top[1]] + [points[m][1] for m in by_tokens_desc] + [0, 0, top[1]]
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="lines", fill="toself", fillcolor=REGION,
        line={"color": REGION_EDGE, "width": 1}, name="Beaten by the Pareto frontier",
        showlegend=col == 1, hoverinfo="skip",
    ), row=1, col=col)
    for m, (x, y) in points.items():
        on_front = m in front
        fig.add_trace(go.Scatter(
            x=[x], y=[y], mode="markers+text", showlegend=False,
            marker={"symbol": style.MODEL_SYMBOLS[m], "size": 9 if on_front else 7,
                    "color": style.MODEL_COLORS[m], "line": {"color": style.SURFACE, "width": 1}},
            text=[f"<b>{MODELS_SHORT[m]}</b>" if on_front else MODELS_SHORT[m]],
            textposition=LABEL_POSITION_BY_PANEL.get(col, {}).get(
                m, LABEL_POSITION.get(m, "middle left")), cliponaxis=False,
            textfont={"size": FONT, "color": style.TEXT_PRIMARY},
            hovertext=[f"<b>{MODELS[m]}</b><br>{x / 1e6:.2f}M build tokens per build"
                       f"<br>{style.pct(y, 1)}" + ("<br>on the frontier" if on_front else "")],
            hoverinfo="text",
        ), row=1, col=col)
    return front


def build_figure(panels: list[tuple[str, dict[str, tuple[float, float]]]],
                 token_tasks: list[str]) -> go.Figure:
    all_x = [x for _, pts in panels for x, _ in pts.values()]
    if not all_x:
        raise ValueError("no model has both a score and a token count")
    if min(all_x) <= 0:
        raise ValueError("a model has 0 tokens per build; the x axis is logarithmic")
    lo, hi = math.log10(min(all_x)) - 0.14, math.log10(max(all_x)) + 0.16

    fig = make_subplots(rows=1, cols=len(panels), horizontal_spacing=0.1)
    for col, (_, pts) in enumerate(panels, start=1):
        _panel(fig, col, pts, 10 ** hi)

    style.apply_paper_layout(fig, width=WIDTH, height=HEIGHT)
    fig.update_layout(
        margin={"l": 4, "r": 6, "t": 16, "b": 4},
        legend={"x": 1, "xanchor": "right", "y": 1.0, "yanchor": "bottom", "orientation": "h",
                "font": {"size": FONT}},
    )
    axis_title = {"size": FONT, "color": style.TEXT_SECONDARY}
    tick_font = {"size": FONT, "color": style.TEXT_SECONDARY}
    ticks = [t for t in (0.5e6, 1e6, 2e6, 4e6, 8e6, 16e6) if lo <= math.log10(t) <= hi]
    # Reversed: more tokens on the left, fewer on the right.
    fig.update_xaxes(type="log", range=[hi, lo], tickvals=ticks,
                     ticktext=[f"{t / 1e6:g}M" for t in ticks], showgrid=True, gridcolor=style.GRID,
                     showline=False, ticks="", zeroline=False, tickfont=tick_font,
                     title={"text": f"Build tokens per build, mean of {len(token_tasks)} tasks"
                                    " (fewer →)", "font": axis_title, "standoff": 2})
    for col, (y_title, pts) in enumerate(panels, start=1):
        ymax = max(0.1, math.ceil((max(y for _, y in pts.values()) + 0.08) * 10) / 10)
        fig.update_yaxes(range=[-0.09, ymax], dtick=0.1 if ymax <= 0.6 else 0.2,
                         tickformat=".0%", showgrid=True, gridcolor=style.GRID, showline=False,
                         ticks="", zeroline=False, tickfont=tick_font,
                         title={"text": y_title, "font": axis_title, "standoff": 2},
                         row=1, col=col)
        # Panel letter, top left of each panel.
        fig.add_annotation(x=0, xref=f"x{'' if col == 1 else col} domain", y=1.0,
                           yref=f"y{'' if col == 1 else col} domain", xanchor="left",
                           yanchor="bottom", showarrow=False, text=f"<b>{'ab'[col - 1]}</b>",
                           font={"size": FONT + 2, "color": style.TEXT_PRIMARY})
    return fig


def main() -> None:
    cells = grid(load_results(), opt_enabled=True)
    score_tasks = shared_tasks(cells)
    tokens, token_tasks = mean_build_tokens(load_trials())
    for label, tasks in (("score", score_tasks), ("tokens", token_tasks)):
        if len(tasks) < len(TASKS):
            print(f"{label} mean leaves out {', '.join(TASKS[t] for t in TASKS if t not in tasks)}")
    panels = [
        (f"MMS@3, mean of {len(score_tasks)} tasks",
         {m: (tokens[m], mean_score(cells, m, score_tasks)) for m in MODELS}),
        (f"Mean task accuracy, mean of {len(score_tasks)} tasks",
         {m: (tokens[m], mean_trial_accuracy(cells, m, score_tasks)) for m in MODELS}),
    ]
    for title, points in panels:
        front = pareto_front(points)
        print(title)
        for m in sorted(MODELS, key=lambda m: points[m]):
            x, y = points[m]
            print(f"  {MODELS[m]:20} {x / 1e6:5.2f}M tokens  {style.pct(y, 1):>6}"
                  + ("  frontier" if m in front else ""))
    for path in style.save_figure(build_figure(panels, token_tasks), FIGURE_NAME):
        print(path)


if __name__ == "__main__":
    main()
