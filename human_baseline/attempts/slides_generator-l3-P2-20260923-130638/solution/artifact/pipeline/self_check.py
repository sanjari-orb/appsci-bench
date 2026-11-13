"""Visual self-check: render the deck to images, run ONE VLM review plus geometry checks, and apply a
single round of repairs. This is the "render + single VLM pass" policy — real visual inspection with
bounded latency, balancing the VLM-judge quality bar against the "wait-for-it" speed constraint.

Rendering uses LibreOffice headless (pptx -> pdf) + PyMuPDF (pdf -> PNG). If LibreOffice is absent the
check degrades to geometry-only (text-fit vs placeholder capacity), which still catches overflow. The
concrete repair is deterministic tightening (trim text/bullets to placeholder capacity), so no extra
generation round-trip is needed and content stays grounded.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from llm import LLMClient, image_block, text_block
from models import BulletListElement, Deck, TextElement
from pipeline.template_analysis import TemplateInfo

log = logging.getLogger("slidesgen.selfcheck")


@dataclass
class CheckReport:
    rendered: bool = False
    flagged_slides: Set[int] = field(default_factory=set)
    issues: List[str] = field(default_factory=list)
    changed: bool = False


# --------------------------------------------------------------------------- rendering


def _soffice() -> Optional[str]:
    return shutil.which("soffice") or shutil.which("libreoffice")


def render_to_images(pptx_path: str, dpi: int = 110) -> List[bytes]:
    exe = _soffice()
    if not exe:
        log.info("LibreOffice not available; skipping render (geometry-only check)")
        return []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [exe, "--headless", "--convert-to", "pdf", "--outdir", tmp, pptx_path],
                check=True, capture_output=True, timeout=180,
            )
            pdfs = [f for f in os.listdir(tmp) if f.lower().endswith(".pdf")]
            if not pdfs:
                return []
            import fitz

            doc = fitz.open(os.path.join(tmp, pdfs[0]))
            imgs = [doc.load_page(p).get_pixmap(dpi=dpi).tobytes("png")
                    for p in range(doc.page_count)]
            doc.close()
            return imgs
    except Exception as exc:
        log.warning("render failed: %s", exc)
        return []


# --------------------------------------------------------------------------- geometry check


def _geometry_flags(deck: Deck, template: TemplateInfo) -> Set[int]:
    flagged: Set[int] = set()
    for sm in deck.presentation.slides:
        lo = template.layout_by_name(sm.layout)
        if not lo:
            continue
        cap = {p.idx: p for p in lo.placeholders}
        for el in sm.elements:
            p = cap.get(el.placeholder_idx) if el.placeholder_idx is not None else None
            if not p:
                continue
            if isinstance(el, TextElement):
                if len(el.content) > p.approx_chars * 1.15:
                    flagged.add(sm.slide_number)
            elif isinstance(el, BulletListElement):
                total = sum(len(i) for i in el.items)
                if len(el.items) > p.approx_lines or total > p.approx_chars * 1.15:
                    flagged.add(sm.slide_number)
    return flagged


# --------------------------------------------------------------------------- VLM review


_VLM_SYSTEM = """You are a strict presentation QA reviewer. You are shown rendered slide images.
For EACH slide, report visual defects that would make it not ready to present:
- text overflowing or spilling off the slide edge
- shapes/text overlapping or colliding
- content cut off or clearly too dense for the box
- obviously empty or broken slides
Only report real, visible problems. If a slide is fine, do not list it."""

_VLM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["problem_slides"],
    "properties": {
        "problem_slides": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["slideNumber", "issue"],
                "properties": {
                    "slideNumber": {"type": "integer"},
                    "issue": {"type": "string"},
                },
            },
        }
    },
}


def _vlm_review(llm: LLMClient, images: List[bytes]) -> Dict[int, str]:
    if not llm.available or not images:
        return {}
    content: List[Dict[str, Any]] = [text_block("Review these slides in order (slide 1 first):")]
    for i, img in enumerate(images, start=1):
        content.append(text_block(f"Slide {i}:"))
        content.append(image_block(img, "image/png"))
    try:
        result = llm.complete_json(
            system=_VLM_SYSTEM, content=content, schema=_VLM_SCHEMA,
            tool_name="report_defects", tool_description="Report visual defects per slide.",
            effort="medium", max_tokens=4000,
        )
    except Exception as exc:
        log.warning("VLM review failed: %s", exc)
        return {}
    out: Dict[int, str] = {}
    for item in result.get("problem_slides", []):
        try:
            out[int(item["slideNumber"])] = str(item.get("issue", ""))
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- repair


def _tighten_slide(sm, template: TemplateInfo) -> bool:
    lo = template.layout_by_name(sm.layout)
    if not lo:
        return False
    cap = {p.idx: p for p in lo.placeholders}
    changed = False
    for el in sm.elements:
        p = cap.get(el.placeholder_idx) if el.placeholder_idx is not None else None
        if not p:
            continue
        if isinstance(el, BulletListElement) and el.items:
            if len(el.items) > p.approx_lines:
                el.items = el.items[: max(1, p.approx_lines)]
                changed = True
            per = max(20, p.approx_chars // max(1, len(el.items)))
            new_items = [(i[: per - 1] + "…") if len(i) > per else i for i in el.items]
            if new_items != el.items:
                el.items = new_items
                changed = True
        elif isinstance(el, TextElement) and len(el.content) > p.approx_chars:
            el.content = el.content[: p.approx_chars - 1].rstrip() + "…"
            changed = True
    return changed


def run(llm: LLMClient, deck: Deck, template: TemplateInfo, pptx_path: str) -> CheckReport:
    report = CheckReport()
    images = render_to_images(pptx_path)
    report.rendered = bool(images)

    flagged = _geometry_flags(deck, template)
    vlm = _vlm_review(llm, images)
    for n, issue in vlm.items():
        flagged.add(n)
        report.issues.append(f"slide {n}: {issue}")
    report.flagged_slides = flagged

    by_number = {sm.slide_number: sm for sm in deck.presentation.slides}
    for n in flagged:
        sm = by_number.get(n)
        if sm and _tighten_slide(sm, template):
            report.changed = True

    log.info("self-check: rendered=%s flagged=%s changed=%s",
             report.rendered, sorted(flagged), report.changed)
    return report
