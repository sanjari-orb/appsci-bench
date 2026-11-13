"""How reliably each model builds a usable artifact: one square per build, as a waffle.

From the per-trial run log (data/elicitation_trials.csv, optimization enabled). Each valid build
(data.build_outcomes) is one square, colored by how it ended:
  usable       the artifact was handed over and the eval scored it;
  broken       an artifact was handed over but could not be used (failed the API conformance
               check, or crashed on start);
  no artifact  the builder never handed one over (did not submit, or ran out of time, turns, or
               context first).
Each model has a 2-row block of 21 slots (11 + 10), one per task x build it was meant to have.
Squares are sorted usable first, so the failures gather at the bottom right of each block, and
within an outcome the builds given a longer limit (the dotted ones) come last. A slot
with no valid build (the run was invalid, an infrastructure failure, and is left out) is an empty
outline. The share of valid builds that failed is printed beside each block.

A build listed in data/extended_build_limits.csv ran with a longer build limit than the task's other
builds (a rerun of a build-limit timeout at 4 h instead of 3 h). Its square carries a small dot, so
the extra time shows where its outcome is.

A third of the text width and as tall as the model average and radar, to sit in their row. Models
are in the same order as the model average (best MMS@3 first), so rows line up across the row.

    python scripts/paper_plots/plot_build_reliability.py

writes figures/build_reliability_third.*.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style  # noqa: E402
from data import (  # noqa: E402
    BROKEN,
    FULL_BUILDS,
    MODELS,
    MODELS_SHORT,
    NO_ARTIFACT,
    TASKS,
    USABLE,
    BuildOutcome,
    build_outcomes,
    grid,
    load_results,
    load_trials,
    rank_models,
)

FIGURE_NAME = "build_reliability_third"
SLOTS = len(TASKS) * FULL_BUILDS  # one slot per task x build
ROWS = 2  # a block is ROWS tall and COLS wide; the last row is short when SLOTS is odd
COLS = -(-SLOTS // ROWS)
BLOCK_GAP = 1.1  # empty rows between two models' blocks, in square units
MARGIN = {"l": 70, "r": 24, "t": 14, "b": 30}  # l: room for the model names, fixed so
# the plot width is known and the squares can be made square
FONT = style.TINY_FONT_SIZE

# Outcome colors: teal, amber, brick. They clear the all-pairs colorblind and normal-vision checks
# as a set; the legend names each, which covers amber's low contrast on white.
OUTCOMES = (
    (USABLE, "Usable", "#2E9C88"),
    (BROKEN, "Broken artifact", "#E0A030"),
    (NO_ARTIFACT, "No artifact", "#C65A48"),
)
EMPTY_LINE = "#CFCDC6"
EXTENDED_CSV = Path(__file__).resolve().parent / "data" / "extended_build_limits.csv"
EXTENDED_DOT = "#FFFFFF"  # on the square's own color; outlined in the legend, where it sits on white
EXTENDED_LABEL = "Needed more time"  # the limits themselves are in the hover text


def load_extended(path: Path = EXTENDED_CSV) -> dict[str, tuple[int, int]]:
    """build_id -> (its build limit, the task's standard limit), in seconds."""
    out: dict[str, tuple[int, int]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row_no, row in enumerate(csv.DictReader(f), start=2):
            build = (row.get("build_id") or "").strip()
            try:
                limit, standard = int(row["build_limit_s"]), int(row["standard_limit_s"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path.name} row {row_no}: limits must be whole seconds") from exc
            if not build or build in out or not 0 < standard < limit:
                raise ValueError(f"{path.name} row {row_no}: needs a new build_id and "
                                 f"0 < standard_limit_s < build_limit_s")
            out[build] = (limit, standard)
    return out


def _legend(fig: go.Figure, entries: list[tuple[str, str | None]]) -> None:
    """Two columns of swatch + name in the bottom margin, under the grid. A None color is the
    empty outline of a slot with no valid run; "dot" is the longer-build-limit dot."""
    plot_w = style.THIRD_WIDTH_PX - MARGIN["l"] - MARGIN["r"]
    plot_h = style.RESULTS_ROW_HEIGHT_PX - MARGIN["t"] - MARGIN["b"]
    # Paper coordinates are relative to the plot area; convert from px measured on the figure.
    def px_x(px: float) -> float:
        return (px - MARGIN["l"]) / plot_w

    def px_y(px_below_plot: float) -> float:
        return -px_below_plot / plot_h

    for i, (name, color) in enumerate(entries):
        x0, y = 4 + (i % 2) * 84, 10 + (i // 2) * 12
        if color == "dot":
            fig.add_shape(type="circle", xref="paper", yref="paper", x0=px_x(x0 + 2),
                          x1=px_x(x0 + 5), y0=px_y(y + 1.5), y1=px_y(y - 1.5), opacity=1,
                          fillcolor=EXTENDED_DOT,
                          line={"color": style.TEXT_SECONDARY, "width": 0.75})
        else:
            fig.add_shape(type="rect", xref="paper", yref="paper", x0=px_x(x0), x1=px_x(x0 + 7),
                          y0=px_y(y + 3.5), y1=px_y(y - 3.5), opacity=1,
                          fillcolor=color or style.SURFACE,
                          line={"color": EMPTY_LINE, "width": 1} if color is None else {"width": 0})
        fig.add_annotation(x=px_x(x0 + 10), y=px_y(y), xref="paper", yref="paper",
                           xanchor="left", yanchor="middle", showarrow=False, text=name,
                           font={"size": FONT - 1, "color": style.TEXT_PRIMARY})


def build_figure(outcomes: list[BuildOutcome], models: list[str],
                 extended: dict[str, tuple[int, int]] | None = None) -> tuple[go.Figure, dict]:
    extended = extended or {}
    by_model: dict[str, list[BuildOutcome]] = {m: [] for m in models}
    for o in outcomes:
        if o.model in by_model:
            by_model[o.model].append(o)
    rank = {key: i for i, (key, _, _) in enumerate(OUTCOMES)}
    summary = {}

    fig = go.Figure()
    xs: dict[str, list[float]] = {key: [] for key, _, _ in OUTCOMES}
    ys: dict[str, list[float]] = {key: [] for key, _, _ in OUTCOMES}
    hover: dict[str, list[str]] = {key: [] for key, _, _ in OUTCOMES}
    empty_x, empty_y = [], []
    dot_x, dot_y = [], []
    centers = []
    for k, m in enumerate(models):
        # Within an outcome, builds given a longer limit go last, so their dots gather at the end
        # of that outcome's run the way the failures gather at the end of the block.
        builds = sorted(by_model[m], key=lambda o: (rank[o.outcome], o.build_id in extended,
                                                     list(TASKS).index(o.task)))
        if len(builds) > SLOTS:
            raise ValueError(f"{m} has {len(builds)} valid builds, more than {SLOTS} slots")
        top = k * (ROWS + BLOCK_GAP)
        centers.append(top + (ROWS - 1) / 2)
        for i in range(SLOTS):
            x, y = i % COLS, top + i // COLS
            if i >= len(builds):
                empty_x.append(x)
                empty_y.append(y)
                continue
            o = builds[i]
            xs[o.outcome].append(x)
            ys[o.outcome].append(y)
            limit = extended.get(o.build_id)
            if limit:
                dot_x.append(x)
                dot_y.append(y)
            hover[o.outcome].append(f"<b>{MODELS[m]}</b><br>{TASKS[o.task]}<br>{o.outcome}"
                                    + (f"<br>{o.error}" if o.error else "")
                                    + (f"<br>{limit[0] / 3600:g} h build limit (others "
                                       f"{limit[1] / 3600:g} h)" if limit else ""))
        failed = sum(o.outcome != USABLE for o in builds)
        summary[m] = (failed, len(builds))

    # Square size in px: the panel's height split over all rows of squares and gaps.
    plot_h = style.RESULTS_ROW_HEIGHT_PX - MARGIN["t"] - MARGIN["b"]
    y_units = len(models) * ROWS + (len(models) - 1) * BLOCK_GAP
    unit_px = plot_h / (y_units + 0.4)
    size = unit_px * 0.84
    for key, name, color in OUTCOMES:
        fig.add_trace(go.Scatter(
            x=xs[key], y=ys[key], mode="markers", name=name,
            marker={"symbol": "square", "size": size, "color": color, "line": {"width": 0}},
            hovertext=hover[key], hoverinfo="text",
        ))
    fig.add_trace(go.Scatter(
        x=dot_x, y=dot_y, mode="markers", name="Longer build limit", hoverinfo="skip",
        marker={"symbol": "circle", "size": size * 0.32, "color": EXTENDED_DOT,
                "line": {"width": 0}},
    ))
    fig.add_trace(go.Scatter(
        x=empty_x, y=empty_y, mode="markers", name="No valid run", showlegend=bool(empty_x),
        marker={"symbol": "square-open", "size": size - 1, "color": EMPTY_LINE,
                "line": {"width": 1}},
        hoverinfo="skip",
    ))
    # The share of valid builds that failed, right of each block, under a column header so the
    # number is never read as a score.
    # Too wide for the number column alone, so it ends at the figure's right edge and runs back over
    # the top of the squares, where there is nothing else.
    fig.add_annotation(x=1, xref="paper", xanchor="right", xshift=MARGIN["r"] - 2, y=-0.7,
                       yanchor="bottom", showarrow=False, text="% failed build attempts",
                       font={"size": FONT - 1, "color": style.TEXT_SECONDARY})
    for m, c in zip(models, centers, strict=True):
        failed, n = summary[m]
        fig.add_annotation(x=COLS - 0.5, y=c, xanchor="left", xshift=3, showarrow=False,
                           text=f"<b>{style.pct(failed / n)}</b>" if n else "n/a",
                           font={"size": FONT, "color": style.TEXT_PRIMARY})

    _legend(fig, [(name, color) for _, name, color in OUTCOMES]
            + ([("No valid run", None)] if empty_x else [])
            + ([(EXTENDED_LABEL, "dot")] if dot_x else []))
    style.apply_paper_layout(fig, width=style.THIRD_WIDTH_PX, height=style.RESULTS_ROW_HEIGHT_PX)
    # No title: the caption says what the squares are.
    fig.update_layout(
        margin=MARGIN,
        # The legend is drawn by hand (_legend): plotly stacks a horizontal legend at this width,
        # and its own legend would grow the margin and squash the grid.
        showlegend=False,
    )
    # Keep squares square: the x range spans as many units as the plot is wide in unit_px.
    plot_w = style.THIRD_WIDTH_PX - MARGIN["l"] - MARGIN["r"]
    fig.update_xaxes(range=[-0.6, -0.6 + plot_w / unit_px], visible=False)
    fig.update_yaxes(range=[y_units - 0.3, -0.7], showgrid=False, zeroline=False, showline=False,
                     automargin=False,
                     ticks="", tickvals=centers, ticktext=[MODELS_SHORT[m] for m in models],
                     ticksuffix=" ", tickfont={"size": FONT, "color": style.TEXT_PRIMARY})
    return fig, summary


def main() -> None:
    models = rank_models(grid(load_results(), opt_enabled=True))
    outcomes = build_outcomes(load_trials())
    extended = load_extended()
    unmatched = sorted(set(extended) - {o.build_id for o in outcomes})
    if unmatched:
        print(f"  note: extended-limit builds not in this figure: {unmatched}", file=sys.stderr)
    fig, summary = build_figure(outcomes, models, extended)
    for m in models:
        failed, n = summary[m]
        print(f"  {MODELS[m]:20} {failed:2} of {n:2} valid builds failed"
              f" ({style.pct(failed / n, 1) if n else 'n/a'})")
    for path in style.save_figure(fig, FIGURE_NAME):
        print(path)


if __name__ == "__main__":
    main()
