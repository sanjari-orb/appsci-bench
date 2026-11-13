"""Multimodal understanding: read all source docs (text + images) with Opus 4.8 once, and produce a
grounded content digest.

Why a digest: images/charts must be interpreted by the vision model, but resending every image at the
plan and generate stages would be slow and costly. So we look at the images ONCE here and fold their
meaning into a text digest. Crucially the digest is ADDITIVE — the raw extracted text (which carries
exact figures and tables) is still passed to later stages, so no figure is lost to summarisation.

Strict grounding: the digest must contain only what the sources actually say; figures are copied
verbatim, never rounded or recomputed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from llm import LLMClient, image_block, text_block
from pipeline.extract import SourceDoc

log = logging.getLogger("slidesgen.understand")

MAX_IMAGES = 20
MAX_TEXT_CHARS = 200_000

_SYSTEM = """You are a meticulous analyst preparing source material for a slide deck.
You will be given a user's deck request and a set of SOURCE DOCUMENTS (text, tables, and images).
Produce a faithful, well-organised DIGEST of the source material that a deck author can rely on.

Rules:
- Ground everything in the sources. Do NOT invent facts, figures, dates, or names.
- Copy numbers, currency amounts, percentages, and dates EXACTLY as they appear. Never round or
  recompute (a value directly derivable from the sources, like a stated total, may be quoted).
- For each image/chart, describe what it shows and extract any data points, labels, or trends.
- Note which source each fact comes from.
- Capture tables faithfully.
- Do not write slides yet; just organise the raw substance clearly and completely.
"""


def build_source_blocks(sources: List[SourceDoc], cache_last: bool = True) -> List[Dict[str, Any]]:
    """Assemble content blocks: all source text (optionally cached) then the images with captions."""
    text_parts = [s.as_text() for s in sources]
    combined = "\n\n".join(text_parts)
    if len(combined) > MAX_TEXT_CHARS:
        combined = combined[:MAX_TEXT_CHARS] + "\n\n[...source text truncated for length...]"
    blocks: List[Dict[str, Any]] = [text_block("SOURCE DOCUMENTS (text):\n\n" + combined,
                                               cache=cache_last)]

    count = 0
    for s in sources:
        for img in s.images:
            if count >= MAX_IMAGES:
                break
            blocks.append(text_block(f"Image from {img.caption or s.name}:"))
            blocks.append(image_block(img.data, img.media_type))
            count += 1
    if count:
        log.info("attached %d source image(s) to the understanding prompt", count)
    return blocks


def understand(llm: LLMClient, instruction: str, sources: List[SourceDoc]) -> str:
    if not llm.available:
        # Offline fallback: a plain concatenation still lets downstream stages run in tests.
        return "\n\n".join(s.as_text() for s in sources)

    content = [text_block(f"USER DECK REQUEST:\n{instruction}\n")]
    content += build_source_blocks(sources)
    digest = llm.complete_text(system=_SYSTEM, content=content, effort="high", max_tokens=16000)
    log.info("digest built (%d chars)", len(digest))
    return digest
