"""Extract a produced/gold `.pptx` into the presentation-JSON shape the eval judges consume.

Shared by two callers: `eval.py`'s content judges (via `extract_presentation`, notes stripped -- see
that module's `_deck_conversation`) and the curation CLI (`scripts/curation_scripts/processors/
pptx2json.py`, via `extract_presentation_from_path`, notes kept -- curation output feeds
`sample_reference/gold.json`, which is authored WITH notes).

Pure `python-pptx` + stdlib only, no import-time side effects: this module is imported eagerly by
`eval.py`, which is itself imported eagerly by the objective loader.
"""
from __future__ import annotations

import io
import json
import pathlib
from typing import Any

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

_TITLE_PLACEHOLDER_TYPES = {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE}


def _has_text(value: Any) -> bool:
    """True if `value` carries real text -- a non-blank string, or a (possibly nested, e.g. table
    rows -> cells) list/tuple with at least one non-blank string somewhere inside it. Empty strings,
    empty containers, and all-blank ones (e.g. rows=[["", ""]], items=[""]) count as no content."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return any(_has_text(v) for v in value)
    return False


def _element_has_content(element: dict) -> bool:
    return any(_has_text(element.get(key)) for key in ("content", "items", "sourceUrl", "rows"))


def _get_text_style(shape) -> dict[str, Any]:
    """Extract font and style properties from shape's first paragraph."""
    style: dict[str, Any] = {}
    try:
        if not shape.has_text_frame or not shape.text_frame.paragraphs:
            return style

        para = shape.text_frame.paragraphs[0]
        if para.runs:
            run = para.runs[0]
            if run.font.size:
                style["fontSize"] = int(run.font.size.pt)
            if run.font.bold is not None:
                style["bold"] = run.font.bold
            try:
                if run.font.color.type:
                    style["color"] = _extract_color(run.font.color)
            except Exception:
                pass
    except Exception:
        pass

    return style


def _extract_color(color) -> str:
    """Extract hex color code from font color."""
    try:
        if hasattr(color, "rgb") and color.rgb:
            rgb = color.rgb
            return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
    except Exception:
        pass
    return "#000000"


def _detect_layout(slide) -> str:
    """Return the slide's REAL layout name, as authored in the deck's slide master.

    This is the same value `pptx2template.py::_slide_layout_name` reads, so a deck's extracted
    JSON and the template derived from that deck agree on what a layout is called -- an agent
    given `template.pptx` can name layouts the template actually defines.

    Falls back to inferring a generic name from shape arrangement only when the real name is
    unavailable (a slide with no layout, or a malformed part). The fallback names are NOT template
    layout names and won't match any master, so they signal "unknown" rather than pretending.
    """
    try:
        name = slide.slide_layout.name
        if name and name.strip():
            return name.strip()
    except Exception:
        pass
    return _infer_layout(slide)


def _infer_layout(slide) -> str:
    """Fallback: guess a generic layout label from shape count when the real name is missing."""
    try:
        text_shapes = [s for s in slide.shapes if s.has_text_frame]
        title_count = sum(1 for s in text_shapes if _is_title_shape(s))

        if title_count == 2:
            return "title_slide"
        elif len(text_shapes) >= 2:
            return "content_two_columns"
        elif len(text_shapes) == 1:
            return "title_only"
    except Exception:
        pass
    return "blank"


def _is_title_shape(shape) -> bool:
    """Check if shape is a title placeholder."""
    try:
        if not hasattr(shape, "placeholder_format"):
            return False
        return shape.placeholder_format.type in _TITLE_PLACEHOLDER_TYPES
    except Exception:
        return False


def _extract_title(slide, default: str) -> str:
    """Return the presentation title from *slide* (slide 1), or *default*."""
    try:
        for shape in slide.shapes:
            if _is_title_shape(shape) and shape.has_text_frame and shape.text_frame.text.strip():
                return shape.text_frame.text.strip().splitlines()[0]

        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                return shape.text_frame.text.strip().splitlines()[0]
    except Exception:
        pass
    return default


def _is_bullet_list(shape) -> bool:
    """Check if shape contains bullet-point content."""
    try:
        if not shape.has_text_frame:
            return False
        non_empty = [p for p in shape.text_frame.paragraphs if p.text.strip()]
        return len(non_empty) > 1
    except Exception:
        return False


def _extract_bullets(shape) -> list[str]:
    """Extract bullet points as list of strings."""
    bullets = []
    try:
        for para in shape.text_frame.paragraphs:
            text = para.text.strip()
            if text:
                bullets.append(text)
    except Exception:
        pass
    return bullets


def _extract_element(shape) -> dict[str, Any] | None:
    """Convert shape to JSON element object."""
    try:
        element: dict[str, Any] = {
            "type": "text",
            "name": shape.name,
        }

        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            element["type"] = "image"
            element["sourceUrl"] = ""
            element["altText"] = shape.name or ""
            return element

        if shape.has_table:
            element["type"] = "table"
            element["rows"] = [
                [cell.text for cell in row.cells] for row in shape.table.rows
            ]
            return element

        if shape.has_text_frame:
            text = "\n".join(para.text for para in shape.text_frame.paragraphs if para.text)
            if not text.strip():
                return None

            if _is_bullet_list(shape) and len(_extract_bullets(shape)) > 1:
                element["type"] = "bullet_list"
                element["items"] = _extract_bullets(shape)
            else:
                element["content"] = text

            if shape.text_frame.paragraphs:
                element["style"] = _get_text_style(shape)

            return element

        return None
    except Exception:
        return None


def _extract_slide(slide, slide_number: int) -> dict[str, Any]:
    """Return a dict representation of *slide*."""
    elements = []
    for shape in slide.shapes:
        element = _extract_element(shape)
        if element:
            elements.append(element)

    notes = ""
    try:
        if slide.notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text or ""
    except Exception:
        pass

    return {
        "slideNumber": slide_number,
        "layout": _detect_layout(slide),
        "elements": elements,
        "notes": notes,
    }


def _build_presentation_dict(prs: Presentation, *, default_title: str) -> dict:
    slides = [_extract_slide(slide, i + 1) for i, slide in enumerate(prs.slides)]
    title = _extract_title(prs.slides[0], default_title) if prs.slides else default_title
    return {
        "presentation": {
            "title": title,
            "theme": "default",
            "aspectRatio": "16:9",
            "metadata": {
                "author": "Unknown",
                "version": "1.0",
                "date": "",
            },
            "slides": slides,
        }
    }


def extract_presentation(
    pptx_bytes: bytes, *, include_notes: bool = False, default_title: str = "presentation",
) -> dict | None:
    """Extract the produced deck into the presentation-JSON shape judges consume.

    Returns the SAME {"presentation": {...}} envelope PptxToJson writes and the artifact
    self-reports, so no downstream shape switch is needed. Returns None ONLY when python-pptx
    cannot open the bytes at all -- there is no JSON to produce in that case. A deck that opens but
    has zero slides, or slides with no text anywhere (`_element_has_content` false on every
    element), still returns the envelope: an empty `slides: []` or all-empty `elements: []` is a
    real, if degenerate, extraction result, and it is up to each judge to decide how to score that
    content -- not this function's job to pre-empt with a `None`.

    `include_notes=False` (the default, used when judging a produced deck) strips each slide's
    "notes" key entirely rather than blanking it -- content judges must not see speaker notes.
    """
    try:
        prs = Presentation(io.BytesIO(pptx_bytes))
    except Exception:
        return None

    presentation = _build_presentation_dict(prs, default_title=default_title)
    slides = presentation["presentation"]["slides"]

    if not include_notes:
        for slide in slides:
            slide.pop("notes", None)

    return presentation


def has_duplicate_zip_parts(pptx_bytes: bytes) -> bool:
    """True if the archive has a part name written more than once.

    `python-pptx` (via stdlib `zipfile`) silently resolves a duplicate name to the last entry
    written, so a `.pptx` with e.g. two `ppt/slides/slide3.xml` entries -- one shadowing a stale
    template part left behind by an in-place archive edit -- opens and extracts cleanly, with no
    trace of the orphaned entry in the extracted JSON. Real PowerPoint's stricter OOXML parser
    rejects the same file and prompts to repair it. Returns False (not duplicate) if the bytes
    aren't even a valid zip -- that failure is caught by the existing open check instead.
    """
    import zipfile

    try:
        names = zipfile.ZipFile(io.BytesIO(pptx_bytes)).namelist()
    except Exception:
        return False
    return len(names) != len(set(names))


def extract_presentation_from_path(src: pathlib.Path) -> dict:
    """Read *src*, return the full envelope WITH notes (curation-pipeline behavior).

    Raises rather than returning None -- a curation run over a broken gold deck must fail loudly."""
    prs = Presentation(src)
    return _build_presentation_dict(prs, default_title=src.stem)


def write_presentation_json(src: pathlib.Path) -> pathlib.Path:
    """Curation-CLI entry point: read *src*, write `<stem>_json.json` alongside it, return that path."""
    presentation = extract_presentation_from_path(src)
    dst = src.with_name(src.stem + "_json.json")
    dst.write_text(json.dumps(presentation, ensure_ascii=False, indent=2), encoding="utf-8")
    return dst
