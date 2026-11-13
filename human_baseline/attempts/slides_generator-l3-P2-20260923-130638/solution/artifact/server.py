#!/usr/bin/env python
"""Slides Generator artifact.

Implements the artifact contract:
  GET  /health   -> 200 {"status": "ok"}
  POST /respond  -> request:  {"messages": [...], "files": [{file_type, file_name, file_data(b64)}]}
                    response: {"choices": [{"message": {"content": <summary>}}],
                               "json":  "<presentation JSON string>",
                               "files": [{file_type:"pptx", file_name:"deck.pptx", file_data:<b64>}]}

Input files (template + content docs) arrive in the top-level `files` array. The produced deck comes
back in the top-level `json` channel (the structured presentation) and the top-level `files` channel
(the editable .pptx) — the file rides in `files`, not in the prose `content`. We also mirror
deck.pptx/deck.json into /work/out. A failure returns an explanatory JSON error in `content`.

Binds 0.0.0.0:$PORT (the deployer publishes exactly that port).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import traceback

# Ensure this directory is importable when launched as `python server.py`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request  # noqa: E402

from llm import LLMClient  # noqa: E402
from pipeline import emit_json, generate, ingest, plan, render_pptx, self_check, transport, understand  # noqa: E402
from pipeline.extract import extract  # noqa: E402
from pipeline.template_analysis import analyze_template  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("slidesgen.server")

app = FastAPI()
_llm = LLMClient()

OUT_DIR = os.environ.get("SLIDES_OUT_DIR", "/work/out")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _error_content(message: str, notes=None) -> str:
    return json.dumps(
        {"error": message, "notes": notes or [], "presentation": None},
        ensure_ascii=False,
    )


def _not_readable_message(names) -> str:
    """User-facing message naming the attached file(s) we could not read or parse."""
    uniq = []
    for n in names:
        if n and n not in uniq:
            uniq.append(n)
    if len(uniq) == 1:
        return (f"The attached file '{uniq[0]}' could not be read or parsed — it may be corrupt or "
                f"not a valid file of its type. Please re-attach a readable version.")
    joined = ", ".join(f"'{n}'" for n in uniq)
    return (f"These attached files could not be read or parsed: {joined}. They may be corrupt or "
            f"not valid files of their type. Please re-attach readable versions.")


def build_deck(body: dict) -> dict:
    """Run the full pipeline and return the /respond envelope (choices + json + files channels).
    Raises on hard failure."""
    # 1. Ingest: instruction + template + content docs (+ any files whose bytes couldn't be decoded).
    ing = ingest.ingest(body)
    unreadable = list(ing.unreadable)

    # 2. The template must be present AND readable. Report unreadable/missing gracefully (not an error).
    if ing.template is None:
        if unreadable:
            raise ingest.FileNotReadableError(_not_readable_message(unreadable))
        raise ingest.FileNotReadableError(
            "No template file (.pptx) was provided in the request. A template is required.")
    with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as tf:
        tf.write(ing.template.data)
        template_path = tf.name
    try:
        template_info = analyze_template(template_path)
    except Exception as exc:  # noqa: BLE001 - a corrupt/unreadable template is a "not readable" case
        log.warning("template %r could not be parsed: %s", ing.template.name, exc)
        unreadable.append(ing.template.name)
        raise ingest.FileNotReadableError(_not_readable_message(unreadable))
    valid_layouts = [lo.name for lo in template_info.layouts]

    # 3. Extract content (multimodal). A content doc we can't parse is reported, not silently dropped.
    sources = [extract(f.name, f.data) for f in ing.content_docs]
    unreadable.extend(s.name for s in sources if s.error)
    if unreadable:
        raise ingest.FileNotReadableError(_not_readable_message(unreadable))

    # Build an image index the generator can reference.
    image_index = {}
    for s in sources:
        for k, img in enumerate(s.images, start=1):
            image_index[f"{s.name}#{k}"] = img

    if not _llm.available:
        raise RuntimeError(
            "Model unavailable (no ANTHROPIC_API_KEY). The deployer injects the key at runtime."
        )

    # 4. Understand -> 5. Plan -> 6. Generate.
    digest = understand.understand(_llm, ing.instruction, sources)
    deck_plan = plan.plan_deck(_llm, ing.instruction, template_info, digest)
    deck = generate.generate_deck(
        _llm, ing.instruction, template_info, digest, sources, deck_plan, image_index
    )

    # 7. Finalise (mirror rules) -> render.
    deck = emit_json.finalize_deck(deck, valid_layouts)
    if not deck.presentation.slides:
        raise RuntimeError("generation produced no usable slides")
    os.makedirs(OUT_DIR, exist_ok=True)
    pptx_path = os.path.join(OUT_DIR, transport.PPTX_NAME)
    render_pptx.render_deck(deck, template_path, pptx_path)

    # 8. Visual self-check (render + single VLM pass + geometry) -> repair once.
    report = self_check.run(_llm, deck, template_info, pptx_path)
    if report.changed:
        deck = emit_json.finalize_deck(deck, valid_layouts)
        render_pptx.render_deck(deck, template_path, pptx_path)

    # 9. Emit + validate JSON.
    doc = emit_json.presentation_json(deck)
    errors = emit_json.validate(doc, valid_layouts)
    if errors:
        log.warning("validation issues: %s", errors)  # log but still return a real deck

    # 10. Transport: write /work/out AND build the response envelope (json + files channels).
    return transport.build_response(deck, doc, pptx_path, OUT_DIR)


@app.post("/respond")
async def respond(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return _reply(_error_content("request body was not valid JSON"))

    try:
        return build_deck(body)          # full envelope: choices + json + files
    except ingest.FileNotReadableError as exc:
        # A file couldn't be read/parsed: reply saying so, rather than failing with an error.
        log.info("file not readable: %s", exc)
        return _reply(str(exc))
    except ingest.IngestError as exc:
        log.warning("ingest error: %s", exc)
        return _reply(_error_content(str(exc)))
    except Exception as exc:  # pragma: no cover - top-level safety net
        log.error("pipeline failure: %s\n%s", exc, traceback.format_exc())
        return _reply(_error_content(f"deck generation failed: {exc}"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
