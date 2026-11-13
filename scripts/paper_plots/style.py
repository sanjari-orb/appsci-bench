"""Shared look for every paper figure: fonts, colors, sizes, and export.

Every plot script builds a plotly figure, calls `apply_paper_layout`, then `save_figure`, so all
figures in the paper share one type size, one palette, and one set of output formats.

Sizes are in CSS pixels. Kaleido renders at 96 px per inch, so 528 px is 5.5 inches, the ICLR text
width; include the PDF at `width=\\textwidth` and the type lands at the size set here (12 px is 9 pt,
10 px is 7.5 pt).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import plotly.graph_objects as go

FULL_WIDTH_PX = 528  # 5.5 in, the ICLR text width
# Two figures side by side as subfigures at 0.49\textwidth each: same width and height, so the pair
# lines up as one row.
HALF_WIDTH_PX = 258
PAIR_HEIGHT_PX = 330
# Three side by side at 0.32\textwidth each (the overview wheel, model average, and radar). Text
# there drops to TINY_FONT_SIZE (about 6.5 pt), the least that stays readable in print.
THIRD_WIDTH_PX = 169
TRIO_HEIGHT_PX = 260
# The main-results row (radar, model average, build reliability) at a third each: shorter than
# TRIO_HEIGHT_PX, so the row takes less of the page.
RESULTS_ROW_HEIGHT_PX = 150
TINY_FONT_SIZE = 9

FONT_FAMILY = "Helvetica, Arial, sans-serif"
FONT_SIZE = 12
SMALL_FONT_SIZE = 10

SURFACE = "#ffffff"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#8a8984"
GRID = "#e9e8e4"  # hairline gridlines, one step off the surface
AXIS = "#b5b4ae"  # the zero baseline bars grow from

# Fixed categorical order. Assign by entity, never by rank, so a model keeps its color everywhere.
CATEGORICAL = [
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948",
]

TITLE_FONT_SIZE = 14
TITLE_MULTILINE_SHIFT_PX = 13  # see add_title

# Color always means MODEL, in every figure, so a reader learns it once. Slots are the categorical
# palette in its validated order (every adjacent pair clears the colorblind gate). Charts where any
# two models can sit side by side (dot plots, radar overlays) cannot clear it for 7 colors, so each
# model also has its own marker shape: identity never rests on color alone.
MODEL_COLORS: dict[str, str] = {
    "claude-opus-5": CATEGORICAL[0],
    "claude-opus-4-8": CATEGORICAL[1],
    "claude-sonnet-4-6": CATEGORICAL[2],
    "claude-haiku-4-5": CATEGORICAL[3],
    "gpt-5.5": CATEGORICAL[4],
    "gpt-oss-120b": CATEGORICAL[5],
    "kimi-k3": CATEGORICAL[6],
}
MODEL_SYMBOLS: dict[str, str] = {
    "claude-opus-5": "circle",
    "claude-opus-4-8": "diamond",
    "claude-sonnet-4-6": "square",
    "claude-haiku-4-5": "triangle-up",
    "gpt-5.5": "pentagon",
    "gpt-oss-120b": "hexagram",
    "kimi-k3": "star",
}

EXPORT_FORMATS = ("pdf", "svg", "png")
PNG_SCALE = 3  # 288 dpi at the sizes above

FIGURES_DIR = Path(__file__).resolve().parent / "figures"


def pct(value: float, digits: int = 0) -> str:
    """A 0-1 score shown as a percent: 0.453 -> "45%"."""
    return f"{value * 100:.{digits}f}%"


def apply_paper_layout(fig: go.Figure, *, width: int, height: int) -> go.Figure:
    fig.update_layout(
        width=width,
        height=height,
        template="simple_white",
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font={"family": FONT_FAMILY, "size": FONT_SIZE, "color": TEXT_PRIMARY},
        hoverlabel={"font": {"family": FONT_FAMILY, "size": FONT_SIZE}},
    )
    return fig


def add_title(fig: go.Figure, title: str, subtitle: str = "", *, compact: bool = False) -> go.Figure:
    """Left-aligned bold title with an optional gray subtitle under it, flush with the plot.

    `compact` sets both in smaller type, for figures a third of the text width.
    """
    title_size = TITLE_FONT_SIZE - 3 if compact else TITLE_FONT_SIZE
    subtitle_size = TINY_FONT_SIZE if compact else SMALL_FONT_SIZE
    # The subtitle rides in the title text as a second line rather than plotly's `title.subtitle`,
    # whose fixed gap under the title costs a line of height a paper figure cannot spare.
    # Plotly's styling does not carry across a <br>, so each subtitle line gets its own span.
    lines = [f"<b>{title}</b>"] + [
        f"<span style='font-size:{subtitle_size}px;color:{TEXT_SECONDARY}'>{line}</span>"
        for line in subtitle.split("<br>") if subtitle
    ]
    # The multi-line shift below was measured at the full sizes; it scales with the type.
    shift = round(TITLE_MULTILINE_SHIFT_PX * title_size / TITLE_FONT_SIZE)
    fig.update_layout(title={
        "text": "<br>".join(lines),
        # Pinned to the very top with a small fixed pad. Plotly shifts any multi-line title up by one
        # fixed amount whatever the line count (measured on these renders), so the pad adds it back
        # or the first line leaves the figure. Each figure's top margin must clear the whole block.
        "x": 0, "xref": "container", "xanchor": "left", "y": 1, "yref": "container", "yanchor": "top",
        "pad": {"t": 6 + (shift if len(lines) > 1 else 0), "l": 6},
        "font": {"size": title_size, "color": TEXT_PRIMARY},
    })
    return fig


def hex_to_rgba(color: str, alpha: float) -> str:
    """`#rrggbb` as an rgba() string, for translucent fills."""
    if len(color) != 7 or not color.startswith("#"):
        raise ValueError(f"expected #rrggbb, got {color!r}")
    if not 0 <= alpha <= 1:
        raise ValueError(f"alpha must be in [0, 1], got {alpha}")
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{alpha})"


@contextlib.contextmanager
def zero_margin_pdf() -> Iterator[None]:
    """Make kaleido print PDFs with no page margin, for the duration of the block.

    kaleido 1.x prints every PDF with a hard-coded 0.1 in margin on each side, on a page the size of
    the figure. The drawing is pushed right and down by about 10 px and its bottom and right edges
    are cut off. PNG and SVG are not affected. The margins live in a private helper that kaleido calls
    through its module, so this swaps that helper for one with zero margins and puts the original
    back afterwards. It fails loudly rather than exporting a clipped PDF if kaleido moves it.
    """
    try:
        from kaleido._kaleido_tab import _devtools_utils as dtools
    except ImportError as exc:
        raise RuntimeError("kaleido's PDF helper has moved; recheck zero_margin_pdf") from exc
    original = getattr(dtools, "print_pdf", None)
    if original is None or not hasattr(dtools, "_raise_error"):
        raise RuntimeError("kaleido's PDF helper has moved; recheck zero_margin_pdf")

    async def print_pdf(tab):  # same contract as kaleido's own: returns the base64 PDF data
        response = await tab.send_command("Page.printToPDF", params={
            "printBackground": True,
            "marginTop": 0, "marginBottom": 0, "marginLeft": 0, "marginRight": 0,
            "preferCSSPageSize": True,
            "pageRanges": "1",
        })
        dtools._raise_error(response)
        data = response.get("result", {}).get("data")
        if not data:
            raise RuntimeError("Chrome returned no PDF data")
        return data

    dtools.print_pdf = print_pdf
    try:
        yield
    finally:
        dtools.print_pdf = original


def save_figure(fig: go.Figure, name: str, out_dir: Path = FIGURES_DIR) -> list[Path]:
    """Write `name` in every export format plus an interactive HTML copy. Returns the paths."""
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"figure name must be a plain file stem, got {name!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for fmt in EXPORT_FORMATS:
        path = out_dir / f"{name}.{fmt}"
        if fmt == "pdf":
            with zero_margin_pdf():
                fig.write_image(path, format=fmt)
        else:
            fig.write_image(path, format=fmt, scale=PNG_SCALE if fmt == "png" else 1)
        written.append(path)
    html_path = out_dir / f"{name}.html"
    fig.write_html(html_path, include_plotlyjs="cdn", full_html=True)
    written.append(html_path)
    return written
