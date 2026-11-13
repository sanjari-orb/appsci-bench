"""Finalise the deck to the contract's hard rules, serialise the presentation JSON, and validate it.

`finalize_deck` runs BEFORE rendering so the .pptx and the JSON mirror each other (same slides, same
order): it drops empty elements/slides, snaps layout names to real template layouts, and renumbers
slides. `presentation_json` produces the exact-schema dict. `validate` re-checks every hard rule from
the output contract; the server treats any error as a failed request.
"""
from __future__ import annotations

import logging
from typing import Iterable, List

from models import Deck, Slide, element_has_content

log = logging.getLogger("slidesgen.emit")


def finalize_deck(deck: Deck, valid_layout_names: Iterable[str]) -> Deck:
    valid = set(valid_layout_names)
    kept: List[Slide] = []
    for sm in deck.presentation.slides:
        elements = [e for e in sm.elements if element_has_content(e)]
        if not elements:
            log.info("dropping empty slide (was #%s, layout=%s)", sm.slide_number, sm.layout)
            continue
        if sm.layout not in valid and valid:
            # snap to a real layout name (case-insensitive), else first available
            match = next((n for n in valid if n.lower() == (sm.layout or "").lower()), None)
            sm.layout = match or sorted(valid)[0]
        sm.elements = elements
        kept.append(sm)

    for i, sm in enumerate(kept, start=1):
        sm.slide_number = i
    deck.presentation.slides = kept
    return deck


def presentation_json(deck: Deck) -> dict:
    return deck.to_json_dict()


def validate(doc: dict, valid_layout_names: Iterable[str]) -> List[str]:
    """Return a list of contract violations ([] == valid)."""
    errors: List[str] = []
    valid = set(valid_layout_names)

    pres = doc.get("presentation")
    if not isinstance(pres, dict) or not pres:
        return ["presentation object is missing or empty"]

    for key in ("title", "theme", "aspectRatio", "metadata", "slides"):
        if key not in pres:
            errors.append(f"presentation missing '{key}'")

    md = pres.get("metadata", {})
    if not isinstance(md, dict) or any(k not in md for k in ("author", "version", "date")):
        errors.append("metadata must have author, version, date")

    slides = pres.get("slides")
    if not isinstance(slides, list) or not slides:
        errors.append("slides must be a non-empty list")
        return errors

    for i, sl in enumerate(slides, start=1):
        if not isinstance(sl, dict):
            errors.append(f"slide {i} is not an object")
            continue
        if sl.get("slideNumber") != i:
            errors.append(f"slide at position {i} has slideNumber={sl.get('slideNumber')} (order mismatch)")
        layout = sl.get("layout")
        if not layout:
            errors.append(f"slide {i} missing layout")
        elif valid and layout not in valid:
            errors.append(f"slide {i} layout {layout!r} is not a template layout")
        elements = sl.get("elements")
        if not isinstance(elements, list) or not elements:
            errors.append(f"slide {i} has no elements")
            continue
        if not any(_element_has_content(e) for e in elements):
            errors.append(f"slide {i} has no element with real content")

    return errors


def _element_has_content(e: dict) -> bool:
    if not isinstance(e, dict):
        return False
    t = e.get("type")
    if t == "text":
        return bool(str(e.get("content", "")).strip())
    if t == "bullet_list":
        return any(str(x).strip() for x in e.get("items", []))
    if t == "table":
        return any(any(str(c).strip() for c in row) for row in e.get("rows", []))
    if t == "image":
        return bool(str(e.get("sourceUrl", "")).strip())
    return False
