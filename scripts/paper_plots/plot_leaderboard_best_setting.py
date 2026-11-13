"""Leaderboard: each model's MMS@3 in its best setting, the setting marked by a symbol on the bar.

The companion of the main results table (make_main_results_table.py). A model's settings are the
AppSci agent with and without its optimization layer (AppSci^opt, AppSci^no opt) and, where it was
run, its own coding harness (Claude Code for Opus 4.8 and Opus 5, Codex for GPT-5.5), with and without
its optimization guidance (CC^opt, CC^no opt; CC is Claude Code). Each bar is the setting
with the highest overall MMS@3; a tie goes to AppSci^opt, the benchmark's main setting. The whole
bar comes from that one setting, never the best setting per task, which would mix configurations
into a score no single setup reached.

Sized and styled like model_average_third (a third of the text width, model colors, best at the top,
the mean in a column right of the plot), without its std whisker: the main table dropped standard
deviations as uninformative, and this figure follows it.

    python scripts/paper_plots/plot_leaderboard_best_setting.py

writes figures/leaderboard_best_setting_third.*.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path

import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style  # noqa: E402
from data import MODELS, MODELS_SHORT, TASKS, grid, load_results  # noqa: E402
from make_main_results_table import HARNESS_NAMES, load_harness  # noqa: E402

FIGURE_NAME = "leaderboard_best_setting_third"
OPT_LABEL = "AppSci<sup>opt</sup>"
NO_OPT_LABEL = "AppSci<sup>no opt</sup>"
BAR_WIDTH = 0.62  # as in model_average
# The setting is a gray symbol just past the bar's end, named in the legend under the plot. Gray,
# so it never competes with the model color the bar carries. A shape per harness (a circle for the
# AppSci agent), filled with optimization and open without.
HARNESS_SHAPES = {"claude_code": "triangle-up", "codex": "diamond"}
# Short names, so a harness's two settings and the other harness fit on one legend row at a third
# of the text width. The caption spells out CC.
HARNESS_SHORT = {"claude_code": "CC", "codex": "Codex"}
assert set(HARNESS_SHORT) == set(HARNESS_NAMES)


def harness_setting(harness: str, opt_enabled: bool) -> str:
    return HARNESS_SHORT[harness] + ("<sup>opt</sup>" if opt_enabled else "<sup>no opt</sup>")


SETTING_SYMBOLS = {OPT_LABEL: "circle", NO_OPT_LABEL: "circle-open"}
for _h, _shape in HARNESS_SHAPES.items():
    SETTING_SYMBOLS[harness_setting(_h, True)] = _shape
    SETTING_SYMBOLS[harness_setting(_h, False)] = _shape + "-open"
SYMBOL_SIZE = 5
SYMBOL_GAP_PX = 5  # from the bar's end to the symbol's centre
# Fixed rather than automatic so the plot's size in px is known: the symbols sit a fixed px past
# each bar, and the legend band is laid out in px. l fits the longest short model name.
MARGIN = {"l": 70, "r": 30, "t": 14}  # b is set from the legend band in build_figure
AXIS_BAND_PX = 24  # under the bars: x tick labels and the axis title
LEGEND_ROW_PX = 11
LEGEND_COL_PX = 80  # the AppSci row's second entry starts this far right of the first
LEGEND_LEFT_PX = 8
LEGEND_ENTRY_GAP_PX = 10  # between two packed entries on the harness row
# Rough width of one character of the legend text, in px, to pack the harness row: plotly does not
# report rendered text widths, so this is an estimate; superscripts render at about 70% size.
LEGEND_CHAR_PX = 4.6


@dataclass(frozen=True)
class Entry:
    model: str
    setting: str  # label shown on the bar (plotly HTML)
    score: float  # mean MMS@3 over the tasks, 0-1


def setting_means() -> dict[str, list[tuple[str, float]]]:
    """model -> [(setting label, mean MMS@3 over all tasks)], AppSci^opt first."""
    results = load_results()
    on, off = grid(results, opt_enabled=True), grid(results, opt_enabled=False)
    out: dict[str, list[tuple[str, float]]] = {}
    for m in MODELS:
        for cells, label in ((on, OPT_LABEL), (off, NO_OPT_LABEL)):
            scores = [cells[(m, t)].score for t in TASKS]
            if any(s is None for s in scores):
                raise ValueError(f"{m} is missing a task with {label}")
            out.setdefault(m, []).append((label, sum(scores) / len(scores)))  # type: ignore[arg-type]
    for (m, h, opt), cells in load_harness().items():
        out[m].append((harness_setting(h, opt), sum(cells[t].score for t in TASKS) / len(TASKS)))
    return out


def leaderboard() -> list[Entry]:
    """Each model's best setting, best model first."""
    entries = []
    for m, options in setting_means().items():
        # Compared at the precision printed, so a tie that shows as a tie keeps AppSci^opt
        # (max returns the first of equal keys).
        label, score = max(options, key=lambda o: round(o[1] * 100, 1))
        entries.append(Entry(m, label, score))
    return sorted(entries, key=lambda e: -e.score)


def _swatch(fig: go.Figure, symbol: str, cx: float, cy: float, px_x, px_y) -> None:
    """One legend swatch, drawn as a layout shape so it can sit in the margin (a trace point
    outside its axis range is dropped). `cx`, `cy` are px from the plot area's bottom-left."""
    r = SYMBOL_SIZE / 2
    line = {"color": style.TEXT_SECONDARY, "width": 1}
    fill = style.SURFACE if symbol.endswith("-open") else style.TEXT_SECONDARY
    if symbol.startswith("circle"):
        fig.add_shape(type="circle", xref="paper", yref="paper", x0=px_x(cx - r), x1=px_x(cx + r),
                      y0=px_y(cy - r), y1=px_y(cy + r), fillcolor=fill, line=line, opacity=1)
        return
    corners = {
        "triangle-up": [(0, r), (r, -r * 0.8), (-r, -r * 0.8)],
        "diamond": [(0, r), (r, 0), (0, -r), (-r, 0)],
    }[symbol.removesuffix("-open")]
    path = "M " + " L ".join(f"{px_x(cx + dx)},{px_y(cy + dy)}" for dx, dy in corners) + " Z"
    fig.add_shape(type="path", xref="paper", yref="paper", path=path, fillcolor=fill, line=line,
                  opacity=1)


def _label_px(label: str) -> float:
    """Estimated rendered width of a legend label, its swatch included."""
    base, _, sup = label.partition("<sup>")
    sup = sup.removesuffix("</sup>")
    return SYMBOL_SIZE + 2 + (len(base) + 0.7 * len(sup)) * LEGEND_CHAR_PX


def _legend_rows(settings: list[str]) -> list[list[tuple[float, str]]]:
    """Legend rows as [(left px from the figure edge, setting)]: the AppSci settings on the first
    row in two fixed columns, the native harnesses packed together on the rows after it, wrapping
    only when a row would run past the figure's right edge."""
    appsci = [s for s in settings if s in (OPT_LABEL, NO_OPT_LABEL)]
    native = [s for s in settings if s not in appsci]
    rows: list[list[tuple[float, str]]] = []
    if appsci:
        rows.append([(LEGEND_LEFT_PX + j * LEGEND_COL_PX, s) for j, s in enumerate(appsci)])
    row: list[tuple[float, str]] = []
    x = LEGEND_LEFT_PX
    for s in native:
        width = _label_px(s)
        if row and x + width > style.THIRD_WIDTH_PX:
            rows.append(row)
            row, x = [], LEGEND_LEFT_PX
        row.append((x, s))
        x += width + LEGEND_ENTRY_GAP_PX
    if row:
        rows.append(row)
    return rows


def build_figure(entries: list[Entry]) -> go.Figure:
    label_size = style.TINY_FONT_SIZE
    unknown = {e.setting for e in entries} - set(SETTING_SYMBOLS)
    if unknown:
        raise ValueError(f"no symbol for setting(s) {sorted(unknown)}")
    settings = [s for s in SETTING_SYMBOLS if any(e.setting == s for e in entries)]
    legend_rows = _legend_rows(settings)
    legend_h = len(legend_rows) * LEGEND_ROW_PX
    margin = {**MARGIN, "b": AXIS_BAND_PX + legend_h + 2}
    plot_w = style.THIRD_WIDTH_PX - margin["l"] - margin["r"]
    plot_h = style.RESULTS_ROW_HEIGHT_PX - margin["t"] - margin["b"]

    # Room past the longest bar for its symbol, so the symbol stays inside the axis range.
    top = max(e.score for e in entries)
    xmax = max(0.1, math.ceil((top + 0.02) * 10) / 10)
    gap = SYMBOL_GAP_PX / plot_w * xmax  # the symbol's offset past the bar, in data units
    if top + 2 * gap > xmax:
        xmax = top + 2 * gap
        gap = SYMBOL_GAP_PX / plot_w * xmax

    fig = go.Figure(go.Bar(
        x=[e.score for e in entries],
        y=[MODELS_SHORT[e.model] for e in entries],
        orientation="h",
        marker={"color": [style.MODEL_COLORS[e.model] for e in entries], "cornerradius": 3,
                "line": {"width": 0}},
        width=BAR_WIDTH,
        hovertext=[f"<b>{MODELS[e.model]}</b><br>{e.setting}: {style.pct(e.score, 1)}"
                   for e in entries],
        hoverinfo="text",
        showlegend=False,
    ))
    for s in settings:
        rows = [e for e in entries if e.setting == s]
        fig.add_trace(go.Scatter(
            x=[e.score + gap for e in rows], y=[MODELS_SHORT[e.model] for e in rows],
            mode="markers", cliponaxis=False, hoverinfo="skip", showlegend=False,
            marker={"symbol": SETTING_SYMBOLS[s], "size": SYMBOL_SIZE,
                    "color": style.TEXT_SECONDARY,
                    "line": {"width": 1, "color": style.TEXT_SECONDARY}},
        ))

    # Legend band in the bottom margin, under the axis title: two columns placed across the whole
    # figure width. Positions are px, converted to paper coordinates (relative to the plot area).
    def px_x(px_from_plot_left: float) -> float:
        return px_from_plot_left / plot_w

    def px_y(px_from_plot_bottom: float) -> float:
        return px_from_plot_bottom / plot_h

    for row, placed in enumerate(legend_rows):
        cy = -(AXIS_BAND_PX + (row + 0.5) * LEGEND_ROW_PX)
        for left, s in placed:
            cx = left - margin["l"]
            _swatch(fig, SETTING_SYMBOLS[s], cx, cy, px_x, px_y)
            fig.add_annotation(x=px_x(cx + SYMBOL_SIZE), y=px_y(cy), xref="paper", yref="paper",
                               xanchor="left", yanchor="middle", showarrow=False, text=s,
                               font={"size": label_size - 1, "color": style.TEXT_PRIMARY})

    # The mean in its own column right of the plot, as in model_average_third.
    for i, e in enumerate(entries):
        fig.add_annotation(
            x=1, xref="paper", xanchor="left", xshift=4, y=i, yref="y", showarrow=False,
            text=f"<b>{style.pct(e.score)}</b>",
            font={"size": label_size + 1, "color": style.TEXT_PRIMARY})
    fig.add_annotation(
        x=1, xref="paper", xanchor="left", xshift=4, y=-0.5, yref="y", yanchor="bottom",
        showarrow=False, text="mean", font={"size": label_size - 2, "color": style.TEXT_MUTED})

    # No title: the caption says what this is.
    style.apply_paper_layout(fig, width=style.THIRD_WIDTH_PX, height=style.RESULTS_ROW_HEIGHT_PX)
    fig.update_layout(margin=margin)
    # Every model named: left automatic, plotly thins the labels once the legend band grows.
    names = [MODELS_SHORT[e.model] for e in entries]
    fig.update_yaxes(autorange="reversed", showline=False, ticks="", ticksuffix=" ",
                     tickmode="array", tickvals=names, ticktext=names,
                     automargin=False, tickfont={"size": label_size})
    fig.update_xaxes(
        range=[0, xmax], tickvals=[v / 10 for v in range(0, int(xmax * 10) + 1, 2)],
        tickformat=".0%", showgrid=True, gridcolor=style.GRID, gridwidth=1,
        showline=False, ticks="", zeroline=True, zerolinecolor=style.AXIS, zerolinewidth=1,
        automargin=False,
        tickfont={"size": label_size - 1, "color": style.TEXT_SECONDARY},
        title={"text": f"Mean MMS@3 over {len(TASKS)} tasks", "standoff": 2,
               "font": {"size": label_size - 1, "color": style.TEXT_SECONDARY}},
    )
    return fig


def main() -> None:
    entries = leaderboard()
    for e in entries:
        print(f"{MODELS[e.model]:18} {e.setting.replace('<sup>', '^').replace('</sup>', ''):16} "
              f"{e.score * 100:.1f}%", file=sys.stderr)
    for path in style.save_figure(build_figure(entries), FIGURE_NAME):
        print(path)


if __name__ == "__main__":
    main()
