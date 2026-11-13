"""What AppSciBench measures: an overview diagram for the paper's introduction, in two layouts.

  list:  a one-line heading, then one tinted row per measured dimension, each with an icon, a short
         name, and a one-line description. Compact; full text width.
  wheel: a hub naming the benchmark, with the dimensions on spokes around it, each as an icon, a
         name, and a two-line description.

Both come from DIMENSIONS below; there is no data in either. Each dimension has its own color, used
for its icon badge, its spoke, and a pale tint behind it. The colors come from the categorical
palette, so they repeat hues that mean a model in the charts; this diagram shows no models, so the
two uses do not meet on one figure.

The icons are simple line drawings written here as 24x24 SVG and embedded as images, so the figure
needs no icon font and renders the same in every export format.

    python scripts/paper_plots/plot_benchmark_overview.py

writes figures/benchmark_overview_list.*, _wheel.*, and _wheel_third.* (pdf, svg, png, html).
"""

from __future__ import annotations

import base64
import math
import sys
from pathlib import Path
from typing import NamedTuple

import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style  # noqa: E402

HEADING = "AppSciBench measures"

# Icon bodies: SVG elements on a 24x24 canvas, drawn in white strokes over a colored badge.
ICONS = {
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/>'
              '<circle cx="12" cy="12" r="1.2" fill="#fff"/>',
    "chat": '<path d="M3 5h11a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2H8l-4 3v-3H3a2 2 0 0 1-1-1.7V7'
            'a2 2 0 0 1 1-2z"/><path d="M18 9h2a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-1v3l-4-3h-3"/>',
    "check_chart": '<path d="M3 20h18"/><path d="M6 20v-6"/><path d="M11 20v-9"/>'
                   '<path d="M16 20v-4"/><path d="M14 6l2.5 2.5L21 4"/>',
    "shield": '<path d="M12 3l7 3v5c0 4.5-3 8.2-7 10-4-1.8-7-5.5-7-10V6z"/>'
              '<path d="M9 12l2 2 4-4"/>',
    "stopwatch": '<circle cx="12" cy="14" r="7"/><path d="M12 14l3-3"/><path d="M10 3h4"/>'
                 '<path d="M12 3v4"/>',
}


class Dimension(NamedTuple):
    icon: str
    name: str
    line: str  # one-line description, for the list
    short: str  # two-line description (split on <br>), for the wheel
    color: str


# The names follow what the code measures: the held-out MMS@k of the delivered agent, the
# elicitation judge's score, the internal-vs-external eval comparison, the safety axes in each
# task's spec, and the per-build usage in run metrics.
DIMENSIONS = [
    Dimension("target", "Built agent quality",
              "Hidden-eval score of the agent it delivers, reliable across 3 builds",
              "Hidden-eval score of the built<br>agent, across 3 builds", style.CATEGORICAL[0]),
    Dimension("chat", "Requirement elicitation",
              "How much unstated policy it draws out of the business user",
              "Unstated policy drawn out<br>of the business user", style.CATEGORICAL[1]),
    Dimension("check_chart", "Self-evaluation fidelity",
              "Whether its own internal benchmark agrees with the hidden eval",
              "Its own internal benchmark<br>against the hidden eval", style.CATEGORICAL[6]),
    Dimension("shield", "Safety",
              "Safe-by-construction and no-fabrication checks on the built agent",
              "Safe-by-construction and<br>no-fabrication checks", style.CATEGORICAL[5]),
    Dimension("stopwatch", "Build efficiency",
              "Cost, tokens, wall-clock time, and turns per build",
              "Cost, tokens, wall-clock time,<br>and turns per build", style.CATEGORICAL[3]),
]

TINT = 0.12  # alpha of a dimension's color behind its row or node
HUB_FILL = "#262a33"  # dark slate: carries white text, softer than black


def icon_uri(name: str) -> str:
    """The icon as a base64 SVG data URI."""
    if name not in ICONS:
        raise ValueError(f"unknown icon {name!r}")
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           'stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
           f'{ICONS[name]}</svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def rounded_rect(x0: float, y0: float, x1: float, y1: float, r: float) -> str:
    """SVG path for a rectangle with corner radius r, in data (pixel) coordinates."""
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    return (f"M{x0 + r},{y0} H{x1 - r} Q{x1},{y0} {x1},{y0 + r} V{y1 - r} Q{x1},{y1} {x1 - r},{y1} "
            f"H{x0 + r} Q{x0},{y1} {x0},{y1 - r} V{y0 + r} Q{x0},{y0} {x0 + r},{y0} Z")


# Every shape passes opacity=1: the simple_white template draws shapes at 0.3 opacity, which washes
# out the fills and badges. Badges are on layer "below" so the icon image (layer "above") draws on
# top; among "below" shapes, a later one draws over an earlier one.

def add_badge(fig: go.Figure, d: Dimension, cx: float, cy: float, r: float) -> None:
    fig.add_shape(opacity=1, type="circle", x0=cx - r, x1=cx + r, y0=cy - r, y1=cy + r,
                  fillcolor=d.color, line={"color": style.SURFACE, "width": 2}, layer="below")
    size = r * 1.25
    fig.add_layout_image(source=icon_uri(d.icon), xref="x", yref="y", x=cx, y=cy, sizex=size,
                         sizey=size, xanchor="center", yanchor="middle", sizing="contain",
                         layer="above")


def pixel_canvas(fig: go.Figure, width: int, height: int) -> go.Figure:
    """Axes in pixels, so every coordinate is a pixel position in the figure."""
    style.apply_paper_layout(fig, width=width, height=height)
    fig.update_xaxes(range=[0, width], visible=False, fixedrange=True)
    fig.update_yaxes(range=[0, height], visible=False, fixedrange=True)
    fig.update_layout(margin={"l": 0, "r": 0, "t": 0, "b": 0}, showlegend=False,
                      hovermode=False)
    return fig


def build_list() -> go.Figure:
    if not DIMENSIONS:
        raise ValueError("nothing to draw")
    width = style.FULL_WIDTH_PX
    heading_h, row_h, gap, pad, badge_r = 26, 32, 5, 2, 11
    height = heading_h + len(DIMENSIONS) * row_h + (len(DIMENSIONS) - 1) * gap + pad
    fig = go.Figure()

    fig.add_annotation(x=2, y=height - heading_h / 2, xanchor="left", showarrow=False,
                       text=f"<b>{HEADING}</b>",
                       font={"size": style.TITLE_FONT_SIZE + 1, "color": style.TEXT_PRIMARY})
    for i, d in enumerate(DIMENSIONS):
        y_top = height - heading_h - i * (row_h + gap)
        y_mid = y_top - row_h / 2
        fig.add_shape(opacity=1, type="path", layer="below",
                      path=rounded_rect(0, y_top - row_h, width, y_top, 8),
                      fillcolor=style.hex_to_rgba(d.color, TINT), line_width=0)
        # A solid stripe of the color down the row's left edge.
        fig.add_shape(opacity=1, type="path", layer="below",
                      path=rounded_rect(0, y_top - row_h, 5, y_top, 2.5),
                      fillcolor=d.color, line_width=0)
        cx = 14 + badge_r
        add_badge(fig, d, cx, y_mid, badge_r)
        text_x = cx + badge_r + 10
        fig.add_annotation(x=text_x, y=y_mid, xanchor="left", showarrow=False,
                           text=(f"<b>{d.name}</b>  <span style='color:{style.TEXT_SECONDARY};"
                                 f"font-size:{style.SMALL_FONT_SIZE}px'>{d.line}</span>"),
                           font={"size": style.SMALL_FONT_SIZE + 1, "color": style.TEXT_PRIMARY})
    return pixel_canvas(fig, width, height)


def build_wheel() -> go.Figure:
    n = len(DIMENSIONS)
    if n == 0:
        raise ValueError("nothing to draw")
    width, height = style.FULL_WIDTH_PX, 300
    cx, cy = width / 2, height / 2 + 8
    hub_r, badge_r = 50, 17
    rx, ry = 195, 105  # spoke ends lie on this ellipse, wider than tall to fit the page
    fig = go.Figure()

    nodes = []
    for i, d in enumerate(DIMENSIONS):
        # The first dimension points straight up; the rest go clockwise, evenly spaced.
        angle = math.pi / 2 - 2 * math.pi * i / n
        nodes.append((d, cx + rx * math.cos(angle), cy + ry * math.sin(angle)))

    for d, x, y in nodes:
        fig.add_shape(opacity=1, type="line", layer="below", x0=cx, y0=cy, x1=x, y1=y,
                      line={"color": d.color, "width": 2.5})
    # The hub, drawn after the spokes so it covers their inner ends.
    fig.add_shape(opacity=1, type="circle", layer="below", x0=cx - hub_r, x1=cx + hub_r,
                  y0=cy - hub_r, y1=cy + hub_r, fillcolor=HUB_FILL, line_width=0)
    fig.add_annotation(x=cx, y=cy + 8, text="<b>AppSciBench</b>", showarrow=False,
                       font={"size": style.FONT_SIZE + 1, "color": style.SURFACE})
    fig.add_annotation(x=cx, y=cy - 10, text="measures", showarrow=False,
                       font={"size": style.SMALL_FONT_SIZE, "color": "#d9d8d2"})

    for d, x, y in nodes:
        fig.add_shape(opacity=1, type="circle", layer="below", x0=x - badge_r - 6,
                      x1=x + badge_r + 6, y0=y - badge_r - 6, y1=y + badge_r + 6,
                      fillcolor=style.hex_to_rgba(d.color, TINT * 2), line_width=0)
        add_badge(fig, d, x, y, badge_r)
        label = (f"<b>{d.name}</b><br><span style='color:{style.TEXT_SECONDARY};"
                 f"font-size:{style.SMALL_FONT_SIZE - 1}px'>"
                 + d.short.replace("<br>", f"</span><br><span style='color:{style.TEXT_SECONDARY};"
                                           f"font-size:{style.SMALL_FONT_SIZE - 1}px'>")
                 + "</span>")
        if y > cy + ry * 0.9:
            # The node at the top: its text goes to the right, clear of its own spoke.
            fig.add_annotation(x=x + badge_r + 12, y=y, xanchor="left", yanchor="middle",
                               text=label, align="left", showarrow=False,
                               font={"size": style.SMALL_FONT_SIZE + 1,
                                     "color": style.TEXT_PRIMARY})
        else:
            # Every other node's text goes under its badge. Above it, the upper side nodes' text
            # would run into the top node's.
            fig.add_annotation(x=x, y=y - badge_r - 10, xanchor="center", yanchor="top",
                               text=label, align="center", showarrow=False,
                               font={"size": style.SMALL_FONT_SIZE + 1,
                                     "color": style.TEXT_PRIMARY})
    return pixel_canvas(fig, width, height)


def build_wheel_compact() -> go.Figure:
    """The wheel at a third of the text width, beside the model-average bars and the radar.

    The heading moves into a small title at the top, like the two figures beside it, so the hub
    only has to hold the name. Names only, each broken onto two lines: at this width a description
    would not fit. The top node's name sits to its right; every other name sits under its badge.
    """
    n = len(DIMENSIONS)
    if n == 0:
        raise ValueError("nothing to draw")
    width, height = style.THIRD_WIDTH_PX, style.TRIO_HEIGHT_PX
    title_h = 30
    cx, cy = width / 2, (height - title_h) / 2 + 2
    hub_r, badge_r = 22, 11
    rx, ry = 56, 82
    fig = go.Figure()

    nodes = []
    for i, d in enumerate(DIMENSIONS):
        angle = math.pi / 2 - 2 * math.pi * i / n
        nodes.append((d, cx + rx * math.cos(angle), cy + ry * math.sin(angle)))

    for d, x, y in nodes:
        fig.add_shape(opacity=1, type="line", layer="below", x0=cx, y0=cy, x1=x, y1=y,
                      line={"color": d.color, "width": 2})
    fig.add_shape(opacity=1, type="circle", layer="below", x0=cx - hub_r, x1=cx + hub_r,
                  y0=cy - hub_r, y1=cy + hub_r, fillcolor=HUB_FILL, line_width=0)
    fig.add_annotation(x=cx, y=cy, text="<b>AppSci<br>Bench</b>", showarrow=False,
                       font={"size": style.TINY_FONT_SIZE - 1, "color": style.SURFACE})

    for d, x, y in nodes:
        halo = badge_r + 4
        fig.add_shape(opacity=1, type="circle", layer="below", x0=x - halo, x1=x + halo,
                      y0=y - halo, y1=y + halo,
                      fillcolor=style.hex_to_rgba(d.color, TINT * 2), line_width=0)
        add_badge(fig, d, x, y, badge_r)
        # Two lines: "Built agent / quality", "Requirement / elicitation", "Safety".
        label = f"<b>{'<br>'.join(d.name.rsplit(' ', 1))}</b>"
        font = {"size": style.TINY_FONT_SIZE - 0.5, "color": style.TEXT_PRIMARY}
        if y > cy + ry * 0.9:
            fig.add_annotation(x=x + halo + 3, y=y, xanchor="left", yanchor="middle", text=label,
                               align="left", showarrow=False, font=font)
        else:
            fig.add_annotation(x=x, y=y - halo - 1, xanchor="center", yanchor="top", text=label,
                               align="center", showarrow=False, font=font)

    fig = pixel_canvas(fig, width, height)
    style.add_title(fig, HEADING, compact=True)
    return fig


def main() -> None:
    for name, build in (("benchmark_overview_list", build_list),
                        ("benchmark_overview_wheel", build_wheel),
                        ("benchmark_overview_wheel_third", build_wheel_compact)):
        for path in style.save_figure(build(), name):
            print(path)


if __name__ == "__main__":
    main()
