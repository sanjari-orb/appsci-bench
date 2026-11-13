"""Generate the full deck content from the outline — the big structured call that produces the shared
DeckModel (elements mapped to template placeholders). Done in one pass over the whole deck so the
narrative stays coherent.

Grounding is enforced hard: content must come from the digest/sources; figures verbatim; nothing
invented or padded. Each element is sized against its placeholder's capacity to avoid overflow.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict, List, Optional

from llm import LLMClient, text_block
from models import (
    BulletListElement,
    Deck,
    Element,
    ImageElement,
    Metadata,
    Presentation,
    Slide,
    TableElement,
    TextElement,
)
from pipeline.extract import ExtractedImage, SourceDoc
from pipeline.plan import DeckPlan
from pipeline.template_analysis import TemplateInfo

log = logging.getLogger("slidesgen.generate")

MAX_SOURCE_CHARS = 200_000

_SYSTEM = """You are a principal presentation writer. You write the FINAL content of a deck, slide by
slide, following the given outline and using ONLY the template's layouts and placeholders.

Hard rules:
- GROUND everything in the digest/sources. Do NOT invent facts, figures, dates, names, or events.
  Copy numbers/currency/percentages/dates EXACTLY as they appear — never round or recompute (a value
  explicitly stated or trivially derivable in the sources may be used).
- COVER everything the outline/request calls for; do not drop important points and do not pad.
- Keep the deck flowing as a coherent story.
- For each slide, fill the layout's placeholders by their idx. Put each piece of content in the
  placeholder whose type fits (title text in the TITLE placeholder, body text/bullets in BODY
  placeholders, tabular figures in a TABLE placeholder).
- Respect placeholder capacity: keep text within the stated ~char/line limits so nothing overflows or
  spills off the slide. Prefer tight, presentation-ready phrasing (short bullets, not paragraphs)
  unless a running-text placeholder is intended.
- Use element types precisely: 'text' (running text -> content), 'bullet_list' (items[]),
  'table' (rows[][], first row = header), 'image' (only to place one of the AVAILABLE SOURCE IMAGES,
  referencing its id in sourceUrl; never fabricate an image).
- Every slide must have at least one element carrying real content.
- Optionally add concise speaker 'notes'.
"""

_ELEMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "name", "placeholderIdx"],
    "properties": {
        "type": {"type": "string", "enum": ["text", "bullet_list", "table", "image"]},
        "name": {"type": "string", "description": "Descriptive label, e.g. 'Headline', 'Metrics'."},
        "placeholderIdx": {"type": "integer", "description": "Target placeholder idx in the layout."},
        "content": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
        "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        "sourceUrl": {"type": "string", "description": "For image: id of an available source image."},
        "altText": {"type": "string"},
    },
}

_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "slides"],
    "properties": {
        "title": {"type": "string"},
        "slides": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["layout", "elements"],
                "properties": {
                    "layout": {"type": "string"},
                    "notes": {"type": "string"},
                    "elements": {"type": "array", "items": _ELEMENT_SCHEMA},
                },
            },
        },
    },
}


def _source_text(sources: List[SourceDoc]) -> str:
    combined = "\n\n".join(s.as_text(max_images_note=False) for s in sources)
    if len(combined) > MAX_SOURCE_CHARS:
        combined = combined[:MAX_SOURCE_CHARS] + "\n\n[...truncated...]"
    return combined


def _outline_text(plan: DeckPlan) -> str:
    lines = [f"Deck title: {plan.title}", f"Deck type: {plan.deck_type}", "Slides:"]
    for i, s in enumerate(plan.slides, start=1):
        lines.append(f"  {i}. [{s.layout}] ({s.role}) — {s.brief}")
    return "\n".join(lines)


def _images_catalog(image_index: Dict[str, ExtractedImage]) -> str:
    if not image_index:
        return "AVAILABLE SOURCE IMAGES: none."
    lines = ["AVAILABLE SOURCE IMAGES (reference by id in an image element's sourceUrl):"]
    for iid, img in image_index.items():
        lines.append(f'  - id="{iid}" — {img.caption or "image"}')
    return "\n".join(lines)


def generate_deck(
    llm: LLMClient,
    instruction: str,
    template: TemplateInfo,
    digest: str,
    sources: List[SourceDoc],
    plan: DeckPlan,
    image_index: Optional[Dict[str, ExtractedImage]] = None,
) -> Deck:
    if not llm.available:
        raise RuntimeError("generation requires the model; LLM client unavailable")
    image_index = image_index or {}

    content = [
        text_block(f"USER DECK REQUEST:\n{instruction}\n"),
        text_block("TEMPLATE LAYOUT CATALOG:\n" + template.catalog_for_prompt() + "\n"),
        text_block(_images_catalog(image_index) + "\n"),
        text_block("APPROVED OUTLINE (follow this):\n" + _outline_text(plan) + "\n"),
        text_block("SOURCE DIGEST:\n" + digest + "\n"),
        text_block("RAW SOURCE TEXT (authoritative for exact figures/tables):\n"
                   + _source_text(sources), cache=True),
    ]
    result = llm.complete_json(
        system=_SYSTEM,
        content=content,
        schema=_SCHEMA,
        tool_name="emit_deck",
        tool_description="Return the full deck content.",
        effort="high",
        max_tokens=32000,
    )
    return _to_deck(result, template, plan, image_index)


def _to_deck(
    result: Dict[str, Any],
    template: TemplateInfo,
    plan: DeckPlan,
    image_index: Dict[str, ExtractedImage],
) -> Deck:
    valid_names = {lo.name for lo in template.layouts}
    slides: List[Slide] = []
    img_counter = 0

    for i, s in enumerate(result.get("slides", []), start=1):
        layout = s.get("layout", "")
        if layout not in valid_names:
            # snap to the planned layout for this position if possible
            layout = plan.slides[i - 1].layout if i - 1 < len(plan.slides) else next(iter(valid_names))
        elements: List[Element] = []
        for e in s.get("elements", []):
            etype = e.get("type")
            name = e.get("name", etype or "element")
            pidx = e.get("placeholderIdx")
            if etype == "text":
                elements.append(TextElement(name=name, content=e.get("content", ""),
                                             placeholder_idx=pidx))
            elif etype == "bullet_list":
                items = [str(x) for x in e.get("items", []) if str(x).strip()]
                elements.append(BulletListElement(name=name, items=items, placeholder_idx=pidx))
            elif etype == "table":
                rows = [[str(c) for c in row] for row in e.get("rows", [])]
                elements.append(TableElement(name=name, rows=rows, placeholder_idx=pidx))
            elif etype == "image":
                iid = e.get("sourceUrl", "")
                src = image_index.get(iid)
                if not src:
                    log.info("dropping image element with unknown source id %r", iid)
                    continue
                img_counter += 1
                ext = "png" if "png" in src.media_type else ("jpg" if "jpe" in src.media_type else "png")
                elements.append(
                    ImageElement(
                        name=name,
                        source_url=f"image{img_counter}.{ext}",
                        alt_text=e.get("altText", src.caption or ""),
                        image_bytes=src.data,
                        media_type=src.media_type,
                        placeholder_idx=pidx,
                    )
                )
        slides.append(Slide(slide_number=i, layout=layout, elements=elements,
                            notes=(s.get("notes") or None)))

    presentation = Presentation(
        title=result.get("title") or plan.title or "Presentation",
        theme=template.theme_name,
        aspect_ratio=template.aspect_ratio,
        metadata=Metadata(
            author="Slides Generator",
            version="1.0",
            date=_dt.date.today().isoformat(),
        ),
        slides=slides,
    )
    return Deck(presentation=presentation)
