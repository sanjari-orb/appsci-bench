"""Slides-generator artifact.

Fulfills requirements.md from the L3 human-baseline attempt:
dual output (editable .pptx + presentation JSON) in one /respond call.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from fastapi import FastAPI, Request
from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.util import Pt

app = FastAPI()


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

@dataclass
class InFile:
    file_type: str
    file_name: str
    data: bytes


@dataclass
class Extracted:
    text_parts: list[str] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _b64_decode(raw: str | bytes) -> bytes:
    if isinstance(raw, bytes):
        return raw
    return base64.b64decode(raw)


def parse_incoming(body: dict) -> tuple[str, list[InFile]]:
    messages = body.get("messages") or []
    prompt = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            prompt = (m.get("content") or "").strip()
            break

    files: list[InFile] = []
    for item in body.get("files") or []:
        name = item.get("file_name") or item.get("filename") or "attachment"
        ftype = (item.get("file_type") or Path_ext(name)).lower().lstrip(".")
        raw = item.get("file_data") or item.get("data") or ""
        try:
            data = _b64_decode(raw) if raw else b""
        except Exception:
            data = b""
            # mark later via empty data
        files.append(InFile(file_type=ftype, file_name=name, data=data))
    return prompt, files


def Path_ext(name: str) -> str:
    if "." in name:
        return name.rsplit(".", 1)[-1]
    return "bin"


def split_template(files: list[InFile]) -> tuple[InFile | None, list[InFile]]:
    template = None
    content: list[InFile] = []
    for f in files:
        if f.file_name.lower() == "template.pptx" or (
            f.file_type == "pptx" and "template" in f.file_name.lower() and template is None
        ):
            template = f
        else:
            content.append(f)
    # Prefer exact template.pptx name
    for f in files:
        if f.file_name.lower() == "template.pptx":
            template = f
            content = [x for x in files if x is not f]
            break
    return template, content


# ---------------------------------------------------------------------------
# Extraction (fail loudly — requirements hard-no)
# ---------------------------------------------------------------------------

def extract_file(f: InFile) -> Extracted:
    out = Extracted()
    if not f.data:
        out.errors.append(f"Attachment '{f.file_name}' could not be read (empty/missing bytes).")
        return out
    try:
        if f.file_type in ("txt", "md"):
            out.text_parts.append(f.data.decode("utf-8", errors="replace"))
        elif f.file_type == "csv":
            text = f.data.decode("utf-8", errors="replace")
            out.text_parts.append(f"[CSV: {f.file_name}]\n{text}")
            reader = csv.reader(io.StringIO(text))
            rows = [list(r) for r in reader]
            if rows:
                out.tables.append(rows)
        elif f.file_type == "docx":
            from docx import Document

            doc = Document(io.BytesIO(f.data))
            paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            if not paras:
                out.warnings.append(f"Attachment '{f.file_name}' opened but had no extractable text.")
            else:
                out.text_parts.append("\n".join(paras))
            for table in doc.tables:
                rows = [[c.text.strip() for c in row.cells] for row in table.rows]
                if rows:
                    out.tables.append(rows)
        elif f.file_type == "pptx":
            # prior deck as content — extract text only; not for look
            prs = Presentation(io.BytesIO(f.data))
            chunks: list[str] = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        chunks.append(shape.text.strip())
            if chunks:
                out.text_parts.append("\n".join(chunks))
            else:
                out.warnings.append(f"Attachment '{f.file_name}' (pptx) had no extractable text.")
        elif f.file_type == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(f.data))
            pages = []
            for page in reader.pages:
                text = (page.extract_text() or "").strip()
                if text:
                    pages.append(text)
            if not pages:
                out.errors.append(
                    f"Attachment '{f.file_name}' (pdf) opened but had no extractable text; refusing to ignore it."
                )
            else:
                out.text_parts.append(f"[PDF: {f.file_name}]\n" + "\n\n".join(pages))
        elif f.file_type == "xlsx":
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(f.data), read_only=True, data_only=True)
            chunks: list[str] = []
            try:
                for sheet in wb.worksheets:
                    rows: list[list[str]] = []
                    for row in sheet.iter_rows(values_only=True):
                        cells = ["" if c is None else str(c).strip() for c in row]
                        if any(cells):
                            rows.append(cells)
                    if rows:
                        out.tables.append(rows)
                        lines = ["\t".join(r) for r in rows]
                        chunks.append(f"[XLSX: {f.file_name} / {sheet.title}]\n" + "\n".join(lines))
            finally:
                wb.close()
            if not chunks:
                out.errors.append(
                    f"Attachment '{f.file_name}' (xlsx) opened but had no extractable cells; refusing to ignore it."
                )
            else:
                out.text_parts.extend(chunks)
        else:
            out.errors.append(f"Unsupported attachment type for '{f.file_name}' ({f.file_type}).")
    except Exception as e:
        out.errors.append(f"Attachment '{f.file_name}' failed to open/parse: {type(e).__name__}: {e}")
    return out


def extract_all(content_files: list[InFile]) -> Extracted:
    merged = Extracted()
    if len(content_files) > 5:
        merged.errors.append(f"Too many content attachments ({len(content_files)}); max is 5.")
        return merged
    for f in content_files:
        part = extract_file(f)
        merged.text_parts.extend(part.text_parts)
        merged.tables.extend(part.tables)
        merged.warnings.extend(part.warnings)
        merged.errors.extend(part.errors)
    return merged


# ---------------------------------------------------------------------------
# Planning (content fidelity — do not invent)
# ---------------------------------------------------------------------------

DECK_DEFAULTS = {
    "qbr": 8,
    "status": 8,
    "pitch": 10,
    "research": 7,
    "sparse": 4,
}


def infer_slide_count(prompt: str) -> int:
    m = re.search(r"~\s*(\d+)\s*-?\s*slide", prompt, re.I)
    if m:
        return max(3, min(20, int(m.group(1))))
    m = re.search(r"(\d+)\s*-?\s*slide", prompt, re.I)
    if m:
        return max(3, min(20, int(m.group(1))))
    low = prompt.lower()
    if "pitch" in low or "investment" in low:
        return DECK_DEFAULTS["pitch"]
    if "qbr" in low or "status review" in low:
        return DECK_DEFAULTS["qbr"]
    if "research" in low or "briefing" in low:
        return DECK_DEFAULTS["research"]
    if "sparse" in low or "quick update" in low:
        return DECK_DEFAULTS["sparse"]
    return 8


def infer_title(prompt: str, source: str) -> str:
    for line in prompt.splitlines():
        line = line.strip().lstrip(">").strip()
        if 8 < len(line) < 120 and not line.lower().startswith("attachment"):
            if "need a" in line.lower() or "deck" in line.lower():
                # pull a short title from first contentful sentence
                continue
    # From source headings
    for line in source.splitlines():
        if line and len(line) < 80 and not line.startswith("["):
            return line[:80]
    return "Business Presentation"


def _bullets_from(text: str, want: int = 4) -> list[str]:
    lines = []
    for raw in text.splitlines():
        s = raw.strip().lstrip("-•*").strip()
        if len(s) < 8:
            continue
        if s.lower().startswith("phase ") or s.endswith(":"):
            lines.append(s)
        elif re.match(r"^\d+\.", s):
            lines.append(re.sub(r"^\d+\.\s*", "", s))
        else:
            # keep substantive sentences
            if len(s) > 40:
                lines.append(s if len(s) < 180 else s[:177] + "…")
        if len(lines) >= want * 3:
            break
    # de-dup preserve order
    seen = set()
    out = []
    for b in lines:
        key = b.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
        if len(out) >= want:
            break
    return out


def plan_slides(prompt: str, extracted: Extracted) -> list[dict[str, Any]]:
    """Build a slide plan using only prompt + extracted sources (no invented facts)."""
    source = "\n\n".join(extracted.text_parts)
    n = infer_slide_count(prompt)
    title = infer_title(prompt, source)
    audience = "leadership" if re.search(r"vp|board|exec|leadership|finance", prompt, re.I) else "internal team"

    # Prefer structure cues from the prompt ("Structure it as: a → b → c")
    struct = re.search(r"structure it as:\s*(.+)", prompt, re.I | re.S)
    sections: list[str] = []
    if struct:
        blob = struct.group(1).split("\n")[0]
        parts = re.split(r"\s*(?:→|->|—|-)\s*", blob)
        sections = [p.strip(" .") for p in parts if p.strip()]

    if not sections:
        sections = [
            "The problem today",
            "Cost of doing nothing",
            "Proposed approach",
            "Investment and payback",
            "Risks and mitigations",
            "The ask",
        ]

    # Map available source chunks to sections by keyword overlap
    chunks = [c.strip() for c in re.split(r"\n{2,}", source) if c.strip()]

    def pick(keywords: list[str], fallback: str) -> str:
        scored = []
        for c in chunks:
            low = c.lower()
            score = sum(1 for k in keywords if k in low)
            if score:
                scored.append((score, c))
        if scored:
            scored.sort(key=lambda x: -x[0])
            return scored[0][1]
        return fallback

    slides: list[dict[str, Any]] = []

    # Cover
    byline = "Prepared for " + audience
    slides.append(
        {
            "layout": "Cover",
            "title": title[:80],
            "byline": byline,
            "audience": audience.title(),
            "elements": [
                {"type": "text", "name": "cover-title", "content": title[:80]},
                {"type": "text", "name": "byline", "content": byline},
                {"type": "text", "name": "audience", "content": audience.title()},
            ],
        }
    )

    # Body from sections (leave room for cover + ask)
    body_slots = max(1, n - 2)
    use_sections = sections[:body_slots]

    for sec in use_sections:
        low = sec.lower()
        if "approach" in low or "phase" in low:
            phase_text = pick(["phase 1", "phase 2", "migrate", "proposed"], source[:1200])
            bullets = _bullets_from(phase_text, 6)
            p1 = [b for b in bullets if "phase 1" in b.lower()] or bullets[:3]
            p2 = [b for b in bullets if "phase 2" in b.lower()] or bullets[3:6] or bullets[:3]
            slides.append(
                {
                    "layout": "Two-Phase Comparison",
                    "title": sec[:80],
                    "phase1_title": "Phase 1",
                    "phase2_title": "Phase 2",
                    "phase1": p1[:3],
                    "phase2": p2[:3],
                    "elements": [
                        {"type": "text", "name": "title", "content": sec[:80]},
                        {"type": "bullet_list", "name": "phase1", "items": p1[:3]},
                        {"type": "bullet_list", "name": "phase2", "items": p2[:3]},
                    ],
                }
            )
        elif "invest" in low or "payback" in low or "cost" in low:
            table = extracted.tables[0] if extracted.tables else [["metric", "value"], ["(see sources)", "—"]]
            # keep header + up to 6 rows
            rows = table[:7]
            payback = ""
            for row in table:
                if any("payback" in str(c).lower() for c in row):
                    payback = " / ".join(str(c) for c in row if c)
                    break
            slides.append(
                {
                    "layout": "Investment Table",
                    "title": sec[:80],
                    "payback": payback or "See attached cost figures",
                    "rows": rows,
                    "elements": [
                        {"type": "text", "name": "title", "content": sec[:80]},
                        {"type": "text", "name": "payback-number", "content": payback.split("/")[0].strip() if payback else "—"},
                        {"type": "text", "name": "payback-label", "content": "Payback / key metric"},
                        {"type": "table", "name": "metrics", "rows": rows},
                    ],
                }
            )
        elif "ask" in low:
            ask_src = pick(["ask", "approval", "budget", "why now"], source[-800:] if len(source) > 800 else source)
            items = _bullets_from(ask_src, 3) or _bullets_from(source, 3)
            slides.append(
                {
                    "layout": "The Ask",
                    "title": sec[:80],
                    "ask_value": items[0][:80] if items else "Approve the proposal",
                    "ask_label": "Ask",
                    "bullets": items[:3],
                    "elements": [
                        {"type": "text", "name": "title", "content": sec[:80]},
                        {"type": "text", "name": "ask-value", "content": items[0][:80] if items else "Approve the proposal"},
                        {"type": "bullet_list", "name": "ask-bullets", "items": items[:3]},
                    ],
                }
            )
        elif "risk" in low:
            txt = pick(["risk", "mitigation", "rollback"], source)
            items = _bullets_from(txt, 4)
            slides.append(
                {
                    "layout": "Bulleted Content",
                    "title": sec[:80],
                    "bullets": items,
                    "elements": [
                        {"type": "text", "name": "title", "content": sec[:80]},
                        {"type": "bullet_list", "name": "bullets", "items": items},
                    ],
                }
            )
        else:
            txt = pick(low.split(), source)
            items = _bullets_from(txt, 4)
            if not items:
                items = _bullets_from(source, 4)
            # Prefer section divider for short "problem" framing when thin
            if len(items) <= 1:
                slides.append(
                    {
                        "layout": "Section Divider",
                        "title": sec[:80],
                        "consequence": (items[0] if items else "Details from attached sources.")[:200],
                        "elements": [
                            {"type": "text", "name": "title", "content": sec[:80]},
                            {
                                "type": "text",
                                "name": "consequence",
                                "content": (items[0] if items else "Details from attached sources.")[:200],
                            },
                        ],
                    }
                )
            else:
                slides.append(
                    {
                        "layout": "Bulleted Content",
                        "title": sec[:80],
                        "bullets": items,
                        "elements": [
                            {"type": "text", "name": "title", "content": sec[:80]},
                            {"type": "bullet_list", "name": "bullets", "items": items},
                        ],
                    }
                )

    # Ensure closing ask if missing and we have room
    if not any(s.get("layout") == "The Ask" for s in slides) and len(slides) < n:
        items = _bullets_from(pick(["why now", "approval", "ask"], source), 3)
        slides.append(
            {
                "layout": "The Ask",
                "title": "The ask",
                "ask_value": items[0][:80] if items else "Approve next steps",
                "ask_label": "Ask",
                "bullets": items[:3] or ["Approve based on attached brief and figures"],
                "elements": [
                    {"type": "text", "name": "title", "content": "The ask"},
                    {"type": "text", "name": "ask-value", "content": items[0][:80] if items else "Approve next steps"},
                    {
                        "type": "bullet_list",
                        "name": "ask-bullets",
                        "items": items[:3] or ["Approve based on attached brief and figures"],
                    },
                ],
            }
        )

    # Trim/pad to target n without inventing content — trim only
    if len(slides) > n:
        # keep cover + last ask-ish + middle
        ask = [s for s in slides if s.get("layout") == "The Ask"]
        body = [s for s in slides[1:] if s.get("layout") != "The Ask"]
        keep = [slides[0]] + body[: max(0, n - 1 - len(ask))] + ask[:1]
        slides = keep[:n]

    if not source.strip():
        # Sparse notes only — expand carefully from prompt alone, flag gap
        slides = [
            {
                "layout": "Cover",
                "title": (prompt[:60] or "Draft deck"),
                "byline": "Draft from prompt only — sources missing",
                "audience": audience.title(),
                "elements": [
                    {"type": "text", "name": "cover-title", "content": (prompt[:60] or "Draft deck")},
                    {"type": "text", "name": "byline", "content": "Draft from prompt only"},
                    {"type": "text", "name": "audience", "content": audience.title()},
                ],
            },
            {
                "layout": "Bulleted Content",
                "title": "What we know from the brief",
                "bullets": _bullets_from(prompt, 4) or [prompt[:180] or "No source material provided"],
                "elements": [
                    {"type": "text", "name": "title", "content": "What we know from the brief"},
                    {
                        "type": "bullet_list",
                        "name": "bullets",
                        "items": _bullets_from(prompt, 4) or [prompt[:180] or "No source material provided"],
                    },
                ],
            },
        ]

    return slides


# ---------------------------------------------------------------------------
# Render pptx from template layouts
# ---------------------------------------------------------------------------

def _set_placeholder_text(slide, idx: int, text: str) -> None:
    for shape in slide.placeholders:
        if shape.placeholder_format.idx == idx:
            shape.text = text
            return
    # fallback by first empty body
    for shape in slide.placeholders:
        if shape.has_text_frame and not shape.text_frame.text.strip():
            shape.text = text
            return


def _fill_named(slide, mapping: dict[str, str]) -> None:
    for shape in slide.shapes:
        name = getattr(shape, "name", "") or ""
        key = name.lower()
        for k, v in mapping.items():
            if k.lower() == key or k.lower() in key.lower():
                if shape.has_text_frame:
                    shape.text = v


def _clear_slides(prs: Presentation) -> None:
    """Remove every slide part from the template package (list-only remove leaves orphans)."""
    while len(prs.slides._sldIdLst):  # noqa: SLF001
        r_id = prs.slides._sldIdLst[0].rId
        prs.part.drop_rel(r_id)
        del prs.slides._sldIdLst[0]


def render_pptx(template_bytes: bytes, slides_plan: list[dict[str, Any]]) -> bytes:
    prs = Presentation(io.BytesIO(template_bytes))
    _clear_slides(prs)
    layout_by_name = {l.name: l for l in prs.slide_layouts}

    for i, plan in enumerate(slides_plan, start=1):
        lname = plan["layout"]
        layout = layout_by_name.get(lname) or prs.slide_layouts[min(i - 1, len(prs.slide_layouts) - 1)]
        slide = prs.slides.add_slide(layout)

        if lname == "Cover":
            _fill_named(
                slide,
                {
                    "cover-title": plan.get("title", ""),
                    "byline": plan.get("byline", ""),
                    "audience": plan.get("audience", ""),
                },
            )
            _set_placeholder_text(slide, 0, plan.get("title", ""))
            _set_placeholder_text(slide, 1, plan.get("byline", ""))
            _set_placeholder_text(slide, 2, plan.get("audience", ""))
        elif lname == "Bulleted Content":
            _set_placeholder_text(slide, 0, plan.get("title", ""))
            bullets = plan.get("bullets") or []
            for bi, text in enumerate(bullets[:4]):
                _set_placeholder_text(slide, bi + 1, text)
        elif lname == "Section Divider":
            _set_placeholder_text(slide, 0, plan.get("title", ""))
            _set_placeholder_text(slide, 1, plan.get("consequence", ""))
        elif lname == "Two-Phase Comparison":
            _set_placeholder_text(slide, 0, plan.get("title", ""))
            _set_placeholder_text(slide, 1, plan.get("phase1_title", "Phase 1"))
            _set_placeholder_text(slide, 2, plan.get("phase2_title", "Phase 2"))
            for bi, text in enumerate((plan.get("phase1") or [])[:3]):
                _set_placeholder_text(slide, 3 + bi, text)
            for bi, text in enumerate((plan.get("phase2") or [])[:3]):
                _set_placeholder_text(slide, 6 + bi, text)
        elif lname == "Investment Table":
            _set_placeholder_text(slide, 0, plan.get("title", ""))
            pay = plan.get("payback") or ""
            _set_placeholder_text(slide, 1, pay.split("/")[0].strip()[:40] if pay else "—")
            _set_placeholder_text(slide, 2, "Key figure from sources")
            _set_placeholder_text(slide, 3, pay[:120] if pay else "")
            # table placeholder
            rows = plan.get("rows") or [["metric", "value"]]
            for shape in slide.placeholders:
                if shape.placeholder_format.type == PP_PLACEHOLDER.TABLE:
                    # replace by adding a table shape is complex; put CSV-like text in a body if needed
                    break
            # Also dump a compact text summary into footer-ish body if present
            summary = " | ".join(",".join(r[:3]) for r in rows[:4])
            _fill_named(slide, {"payback-formula": summary[:200]})
        elif lname == "The Ask":
            _set_placeholder_text(slide, 0, plan.get("title", "The ask"))
            _set_placeholder_text(slide, 1, plan.get("ask_value", "")[:80])
            _set_placeholder_text(slide, 2, plan.get("ask_label", "Ask"))
            for bi, text in enumerate((plan.get("bullets") or [])[:3]):
                _set_placeholder_text(slide, 3 + bi, text)
        else:
            _set_placeholder_text(slide, 0, plan.get("title", f"Slide {i}"))

        # page numbers if present
        for shape in slide.placeholders:
            if shape.placeholder_format.type == PP_PLACEHOLDER.SLIDE_NUMBER:
                shape.text = str(i)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def to_presentation_json(slides_plan: list[dict[str, Any]], title: str) -> dict[str, Any]:
    slides = []
    for i, plan in enumerate(slides_plan, start=1):
        elements = plan.get("elements") or []
        # ensure at least one contentful element
        if not elements:
            elements = [{"type": "text", "name": "title", "content": plan.get("title", f"Slide {i}")}]
        slides.append(
            {
                "slideNumber": i,
                "layout": plan.get("layout", "Bulleted Content"),
                "elements": elements,
                "notes": plan.get("notes", ""),
            }
        )
    return {
        "presentation": {
            "title": title,
            "theme": "template",
            "aspectRatio": "16:9",
            "metadata": {
                "author": "slides-generator",
                "version": "1.0",
                "date": date.today().isoformat(),
            },
            "slides": slides,
        }
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/respond")
async def respond(request: Request) -> dict:
    body = await request.json()
    prompt, files = parse_incoming(body)
    warnings: list[str] = []

    template, content_files = split_template(files)
    if template is None or not template.data:
        return {
            "choices": [{"message": {"role": "assistant", "content": "ERROR: template.pptx is required and must be readable."}}],
            "json": json.dumps({"error": "missing_template", "presentation": None}),
            "files": [],
        }

    # Validate template opens
    try:
        Presentation(io.BytesIO(template.data))
    except Exception as e:
        return {
            "choices": [{"message": {"role": "assistant", "content": f"ERROR: template.pptx is not a valid PowerPoint: {e}"}}],
            "json": json.dumps({"error": "bad_template", "presentation": None}),
            "files": [],
        }

    extracted = extract_all(content_files)
    if extracted.errors:
        # Hard fail on unreadable attachments (requirements)
        msg = "ERROR: attachment failure(s): " + "; ".join(extracted.errors)
        return {
            "choices": [{"message": {"role": "assistant", "content": msg}}],
            "json": json.dumps({"error": "attachment_failure", "details": extracted.errors, "presentation": None}),
            "files": [],
        }
    warnings.extend(extracted.warnings)
    if not extracted.text_parts and not prompt:
        return {
            "choices": [{"message": {"role": "assistant", "content": "ERROR: empty request — no prompt and no readable content."}}],
            "json": json.dumps({"error": "empty_input", "presentation": None}),
            "files": [],
        }
    if not extracted.text_parts:
        warnings.append("GAP: no grounding documents produced text; expanding carefully from the prompt only — do not treat as fully sourced.")

    slides_plan = plan_slides(prompt, extracted)
    title = slides_plan[0].get("title") if slides_plan else "Presentation"
    try:
        pptx_bytes = render_pptx(template.data, slides_plan)
        # verify opens
        Presentation(io.BytesIO(pptx_bytes))
    except Exception as e:
        return {
            "choices": [{"message": {"role": "assistant", "content": f"ERROR: failed to render editable pptx: {e}"}}],
            "json": json.dumps({"error": "render_failure", "presentation": None}),
            "files": [],
        }

    presentation = to_presentation_json(slides_plan, title=str(title))
    if not presentation["presentation"]["slides"]:
        return {
            "choices": [{"message": {"role": "assistant", "content": "ERROR: empty presentation object."}}],
            "json": json.dumps({"error": "empty_presentation", "presentation": None}),
            "files": [],
        }

    status = f"Generated {len(slides_plan)} slides (editable pptx + structured JSON)."
    if warnings:
        status += " WARNINGS: " + " | ".join(warnings)

    return {
        "choices": [{"message": {"role": "assistant", "content": status}}],
        "json": json.dumps(presentation),
        "files": [
            {
                "file_type": "pptx",
                "file_name": "deck.pptx",
                "file_data": base64.b64encode(pptx_bytes).decode("ascii"),
            }
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
