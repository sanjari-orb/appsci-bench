"""Render the DeckModel into a .pptx built FROM the template file.

Building from a copy of the template (not a fresh Presentation) is what makes the deck genuinely
on-brand: the theme, masters, and layouts come from the customer's own file, so colours/fonts/feel are
native rather than imitated. We drop the template's example slides, then add one slide per deck slide
using the layout the planner chose (reusing the template's layouts for every slide, however many).

Each element is placed into its target placeholder (by idx, with a type-based fallback). Text frames
get word-wrap + shrink-to-fit to keep content inside the box. Unfilled content placeholders are
removed so no "click to add text" prompt leaks into the render.
"""
from __future__ import annotations

import io
import logging
from typing import Optional, Set

from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.enum.text import MSO_AUTO_SIZE

from models import (
    BulletListElement,
    Deck,
    ImageElement,
    Slide as SlideModel,
    TableElement,
    TextElement,
)

log = logging.getLogger("slidesgen.render")

_CHROME = {PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.DATE}
_TITLE_TYPES = {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE}
_TEXT_TYPES = {
    PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT, PP_PLACEHOLDER.SUBTITLE,
    PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE,
}
_TABLE_TYPES = {PP_PLACEHOLDER.TABLE, PP_PLACEHOLDER.OBJECT}
_PICTURE_TYPES = {PP_PLACEHOLDER.PICTURE, PP_PLACEHOLDER.OBJECT}


def _delete_all_slides(prs) -> None:
    """Remove the template's example slides — both the sldId reference AND the underlying slide part.

    Dropping only the sldId leaves the slide parts orphaned in the package, which serialises as
    duplicate `slideN.xml` zip entries and can corrupt the file. Dropping the relationship orphans the
    part so it is not written out.
    """
    lst = prs.slides._sldIdLst
    for sld_id in list(lst):
        try:
            prs.part.drop_rel(sld_id.rId)
        except Exception:
            pass
        lst.remove(sld_id)


def _find_layout(prs, name: str):
    for lo in prs.slide_layouts:
        if lo.name == name:
            return lo
    for lo in prs.slide_layouts:
        if lo.name.lower() == (name or "").lower():
            return lo
    log.warning("layout %r not found; using first layout", name)
    return prs.slide_layouts[0]


def _enable_shrink(tf) -> None:
    tf.word_wrap = True
    try:
        tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    except Exception:
        pass


def _wanted_types(el):
    if isinstance(el, TableElement):
        return _TABLE_TYPES
    if isinstance(el, ImageElement):
        return _PICTURE_TYPES
    return _TEXT_TYPES


def _target_placeholder(slide, el, used: Set[int]):
    # 1. exact idx the generator assigned
    if el.placeholder_idx is not None and el.placeholder_idx not in used:
        for ph in slide.placeholders:
            if ph.placeholder_format.idx == el.placeholder_idx:
                return ph
    # 2. by type, first unused
    wanted = _wanted_types(el)
    for ph in slide.placeholders:
        if ph.placeholder_format.idx in used:
            continue
        if ph.placeholder_format.type in wanted:
            return ph
    return None


def _fill_text(ph, text: str) -> None:
    tf = ph.text_frame
    tf.clear()
    tf.paragraphs[0].text = text or ""
    _enable_shrink(tf)


def _fill_bullets(ph, items) -> None:
    tf = ph.text_frame
    tf.clear()
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = str(item)
    _enable_shrink(tf)


def _fill_table(slide, ph, rows) -> None:
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return
    n = len(rows)
    m = max(len(r) for r in rows)
    try:
        gframe = ph.insert_table(rows=n, cols=m)
        table = gframe.table
    except Exception:
        left, top, width, height = ph.left, ph.top, ph.width, ph.height
        _remove_ph(ph)
        gframe = slide.shapes.add_table(n, m, left, top, width, height)
        table = gframe.table
    for i, row in enumerate(rows):
        for j in range(m):
            table.cell(i, j).text = str(row[j]) if j < len(row) else ""


def _fill_image(slide, ph, el: ImageElement) -> None:
    if not el.image_bytes:
        return
    stream = io.BytesIO(el.image_bytes)
    try:
        ph.insert_picture(stream)
    except Exception:
        left, top, width, height = ph.left, ph.top, ph.width, ph.height
        _remove_ph(ph)
        slide.shapes.add_picture(stream, left, top, width=width, height=height)


def _remove_ph(ph) -> None:
    ph._element.getparent().remove(ph._element)


def _add_textbox_fallback(slide, prs, el) -> None:
    """When no placeholder fits, drop a plain textbox inside the slide bounds (kept modest so it
    doesn't collide with template chrome)."""
    from pptx.util import Emu

    left = Emu(int(prs.slide_width * 0.08))
    top = Emu(int(prs.slide_height * 0.30))
    width = Emu(int(prs.slide_width * 0.84))
    height = Emu(int(prs.slide_height * 0.55))
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    if isinstance(el, TextElement):
        tf.text = el.content
    elif isinstance(el, BulletListElement):
        for i, it in enumerate(el.items):
            (tf.paragraphs[0] if i == 0 else tf.add_paragraph()).text = str(it)
    _enable_shrink(tf)


def _apply(slide, prs, ph, el) -> None:
    if ph is None:
        _add_textbox_fallback(slide, prs, el)
        return
    if isinstance(el, TextElement):
        _fill_text(ph, el.content)
    elif isinstance(el, BulletListElement):
        _fill_bullets(ph, el.items)
    elif isinstance(el, TableElement):
        _fill_table(slide, ph, el.rows)
    elif isinstance(el, ImageElement):
        _fill_image(slide, ph, el)


def _clear_unfilled(slide, used: Set[int]) -> None:
    for ph in list(slide.placeholders):
        pf = ph.placeholder_format
        if pf.idx in used or pf.type in _CHROME:
            continue
        # Leave nothing that would render a prompt.
        _remove_ph(ph)


def render_deck(deck: Deck, template_path: str, out_path: str) -> str:
    prs = Presentation(template_path)
    _delete_all_slides(prs)

    for sm in deck.presentation.slides:
        layout = _find_layout(prs, sm.layout)
        slide = prs.slides.add_slide(layout)
        used: Set[int] = set()
        for el in sm.elements:
            ph = _target_placeholder(slide, el, used)
            # Capture idx BEFORE applying: insert_table/insert_picture transform the placeholder
            # element, after which reading placeholder_format on the stale wrapper raises.
            idx = ph.placeholder_format.idx if ph is not None else None
            _apply(slide, prs, ph, el)
            if idx is not None:
                used.add(idx)
        _clear_unfilled(slide, used)
        if sm.notes:
            try:
                slide.notes_slide.notes_text_frame.text = sm.notes
            except Exception:
                pass

    prs.save(out_path)
    log.info("rendered %d slide(s) -> %s", len(deck.presentation.slides), out_path)
    return out_path
