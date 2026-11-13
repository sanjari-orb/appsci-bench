"""Plan the deck: decide the deck type, the slide count, and an ordered outline of slides — each
mapped to one of the template's real layouts — that tells a coherent story and covers everything the
request asks for.

Slide-count policy (customer): honor an explicit count in the request; otherwise default by deck type
— QBR/status review ~8, product pitch 8-12, research briefing 6-8, sparse status update 3-5. The
template does NOT cap length; extra slides reuse the template's layouts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List

from llm import LLMClient, text_block
from pipeline.template_analysis import TemplateInfo

log = logging.getLogger("slidesgen.plan")


@dataclass
class PlanSlide:
    role: str
    layout: str
    brief: str


@dataclass
class DeckPlan:
    title: str
    deck_type: str
    slides: List[PlanSlide]


_SYSTEM = """You are a principal presentation designer. You plan a deck OUTLINE from a user's request
and a prepared digest of source material, using ONLY the layouts available in the provided template.

Follow these rules:
- The deck must SAY WHAT THE USER ASKED FOR: cover every point/section they requested, in a sensible
  narrative order that flows like a story (not just slides in the right order).
- Ground everything in the digest/sources. Do NOT invent content. Do NOT pad with material the user
  did not provide.
- Slide count: if the user states an explicit number (e.g. "~7 slides"), match it. Otherwise choose a
  sensible length for the deck type: QBR/status review ~8, product pitch 8-12, research briefing 6-8,
  sparse status update 3-5.
- If the user specifies a structure/section order, follow it.
- Every slide must use one of the template layout NAMES exactly as listed. Choose the layout whose
  placeholders best fit that slide's content (e.g. a comparison layout for phase-by-phase content, a
  table layout for figures).
- Give each slide a short 'role' (its narrative job) and a 'brief' describing exactly what content it
  should carry, referencing the specific facts/figures from the digest it should use.
"""

_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "deckType", "slides"],
    "properties": {
        "title": {"type": "string", "description": "The deck title."},
        "deckType": {
            "type": "string",
            "description": "One of: qbr, status, pitch, briefing, other.",
        },
        "slides": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["role", "layout", "brief"],
                "properties": {
                    "role": {"type": "string"},
                    "layout": {"type": "string", "description": "Exact template layout name."},
                    "brief": {"type": "string"},
                },
            },
        },
    },
}


def plan_deck(
    llm: LLMClient,
    instruction: str,
    template: TemplateInfo,
    digest: str,
) -> DeckPlan:
    if not llm.available:
        raise RuntimeError("planning requires the model; LLM client unavailable")

    content = [
        text_block(f"USER DECK REQUEST:\n{instruction}\n"),
        text_block("TEMPLATE LAYOUT CATALOG:\n" + template.catalog_for_prompt() + "\n"),
        text_block("SOURCE DIGEST:\n" + digest, cache=True),
    ]
    result = llm.complete_json(
        system=_SYSTEM,
        content=content,
        schema=_SCHEMA,
        tool_name="emit_outline",
        tool_description="Return the deck outline.",
        effort="xhigh",
        max_tokens=8000,
    )

    valid_names = {lo.name for lo in template.layouts}
    slides = []
    for s in result.get("slides", []):
        layout = s.get("layout", "")
        if layout not in valid_names:
            layout = _closest_layout(layout, template) or (template.layouts[0].name)
        slides.append(PlanSlide(role=s.get("role", ""), layout=layout, brief=s.get("brief", "")))

    plan = DeckPlan(
        title=result.get("title", "Presentation"),
        deck_type=result.get("deckType", "other"),
        slides=slides,
    )
    log.info("plan: %d slides, type=%s, title=%r", len(plan.slides), plan.deck_type, plan.title)
    return plan


def _closest_layout(name: str, template: TemplateInfo) -> str | None:
    low = (name or "").lower()
    for lo in template.layouts:
        if lo.name.lower() == low:
            return lo.name
    for lo in template.layouts:
        if low and (low in lo.name.lower() or lo.name.lower() in low):
            return lo.name
    return None
