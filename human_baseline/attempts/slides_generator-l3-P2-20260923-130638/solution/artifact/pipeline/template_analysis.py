"""Analyse the house template.pptx: aspect ratio, theme, and the layout catalog.

The template is the LOOK: we never read its placeholder sample text as content. What we extract here
is the *structure* the deck must be built into — the named layouts and their placeholders — plus theme
metadata for the JSON `theme` field. The planner picks layouts from this catalog and the generator
sizes text against each placeholder's geometry to avoid overflow.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pptx import Presentation
from pptx.util import Emu

log = logging.getLogger("slidesgen.template")


@dataclass
class PlaceholderInfo:
    idx: int
    ph_type: str          # e.g. TITLE, BODY, TABLE, PICTURE, FOOTER, SLIDE_NUMBER
    name: str
    width_in: float
    height_in: float
    # Rough capacity hints so the generator can keep text inside the box.
    approx_chars: int     # characters that fit before overflow risk
    approx_lines: int     # lines that fit


@dataclass
class LayoutInfo:
    index: int
    name: str
    placeholders: List[PlaceholderInfo] = field(default_factory=list)

    def content_placeholders(self) -> List[PlaceholderInfo]:
        """Placeholders that hold real content (exclude footer/page-number/date chrome)."""
        chrome = {"FOOTER", "SLIDE_NUMBER", "DATE"}
        return [p for p in self.placeholders if p.ph_type not in chrome]


@dataclass
class TemplateInfo:
    aspect_ratio: str
    theme_name: str
    slide_width_emu: int
    slide_height_emu: int
    layouts: List[LayoutInfo]

    def layout_by_name(self, name: str) -> Optional[LayoutInfo]:
        for lo in self.layouts:
            if lo.name == name:
                return lo
        # Case-insensitive fallback.
        for lo in self.layouts:
            if lo.name.lower() == (name or "").lower():
                return lo
        return None

    def catalog_for_prompt(self) -> str:
        """Human-readable layout catalog handed to the planner/generator."""
        lines = [f"Aspect ratio: {self.aspect_ratio}", "Available layouts (use these names exactly):"]
        for lo in self.layouts:
            lines.append(f"- \"{lo.name}\":")
            for p in lo.content_placeholders():
                lines.append(
                    f"    * placeholder idx={p.idx} type={p.ph_type} name=\"{p.name}\" "
                    f"(~{p.approx_chars} chars / ~{p.approx_lines} lines)"
                )
        return "\n".join(lines)


def _aspect_ratio(width_emu: int, height_emu: int) -> str:
    if not height_emu:
        return "16:9"
    ratio = width_emu / height_emu
    known = {16 / 9: "16:9", 4 / 3: "4:3", 16 / 10: "16:10"}
    for value, label in known.items():
        if abs(ratio - value) < 0.03:
            return label
    from math import gcd

    g = gcd(width_emu, height_emu) or 1
    return f"{width_emu // g}:{height_emu // g}"


def _capacity(width_in: float, height_in: float, ph_type: str) -> tuple[int, int]:
    """Very rough text capacity for a box, to steer the generator away from overflow."""
    # Assume ~18pt body text: ~0.25in per line, ~0.10in per character column.
    if ph_type == "TITLE":
        font_in, char_in = 0.5, 0.28
    else:
        font_in, char_in = 0.28, 0.11
    lines = max(1, int(height_in / font_in))
    chars_per_line = max(4, int(width_in / char_in))
    return chars_per_line * lines, lines


_RT_THEME = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"


def _theme_name(prs) -> str:
    """Read the theme's name from the first master's theme part; fall back to 'default'."""
    try:
        from lxml import etree

        theme_part = prs.slide_masters[0].part.part_related_by(_RT_THEME)
        name = etree.fromstring(theme_part.blob).get("name")
        return name or "default"
    except Exception:
        return "default"


def analyze_template(path: str) -> TemplateInfo:
    prs = Presentation(path)
    w, h = int(prs.slide_width), int(prs.slide_height)

    theme_name = _theme_name(prs)

    layouts: List[LayoutInfo] = []
    for i, layout in enumerate(prs.slide_layouts):
        info = LayoutInfo(index=i, name=layout.name)
        for ph in layout.placeholders:
            try:
                pf = ph.placeholder_format
                ph_type = str(pf.type).split()[0] if pf.type is not None else "BODY"
                width_in = Emu(ph.width).inches if ph.width else 4.0
                height_in = Emu(ph.height).inches if ph.height else 1.0
                approx_chars, approx_lines = _capacity(width_in, height_in, ph_type)
                info.placeholders.append(
                    PlaceholderInfo(
                        idx=pf.idx,
                        ph_type=ph_type,
                        name=ph.name,
                        width_in=round(width_in, 2),
                        height_in=round(height_in, 2),
                        approx_chars=approx_chars,
                        approx_lines=approx_lines,
                    )
                )
            except Exception as exc:  # pragma: no cover - odd placeholder
                log.debug("skipping placeholder in layout %s: %s", layout.name, exc)
        layouts.append(info)

    return TemplateInfo(
        aspect_ratio=_aspect_ratio(w, h),
        theme_name=theme_name,
        slide_width_emu=w,
        slide_height_emu=h,
        layouts=layouts,
    )
