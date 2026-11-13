"""Build the /respond response envelope and (belt-and-suspenders) write the deliverables to /work/out.

The produced deck comes back the same way input files arrive — in a top-level `files` array — plus a
structured `json` channel, BOTH siblings to `choices`:

    {
      "choices": [{"message": {"role": "assistant", "content": "<short summary>"}}],
      "json":  "<JSON-encoded presentation string>",           # the structured deliverable
      "files": [{"file_type": "pptx", "file_name": "deck.pptx", "file_data": "<base64>"}]
    }

- `json` is a JSON-encoded STRING (not an object): the presentation JSON.
- `files` carries the produced .pptx as a File object (file_type / file_name / file_data-as-base64).
- `content` is only a human-readable summary; the file itself rides in the `files` channel, not in
  the prose content.

We also mirror deck.pptx / deck.json into /work/out; that's cheap and harmless.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
from typing import Any, Dict

from models import Deck, ImageElement

log = logging.getLogger("slidesgen.transport")

DEFAULT_OUT_DIR = os.environ.get("SLIDES_OUT_DIR", "/work/out")
PPTX_NAME = "deck.pptx"
JSON_NAME = "deck.json"


def _write_out_dir(deck: Deck, json_dict: Dict[str, Any], pptx_src_path: str, out_dir: str) -> bytes:
    """Write deck.pptx + deck.json (+ any referenced images) to out_dir; return the pptx bytes."""
    os.makedirs(out_dir, exist_ok=True)
    pptx_dst = os.path.join(out_dir, PPTX_NAME)
    if os.path.abspath(pptx_src_path) != os.path.abspath(pptx_dst):
        shutil.copyfile(pptx_src_path, pptx_dst)
    with open(os.path.join(out_dir, JSON_NAME), "w", encoding="utf-8") as fh:
        json.dump(json_dict, fh, ensure_ascii=False, indent=2)
    for sm in deck.presentation.slides:
        for el in sm.elements:
            if isinstance(el, ImageElement) and el.image_bytes and el.source_url:
                try:
                    with open(os.path.join(out_dir, os.path.basename(el.source_url)), "wb") as fh:
                        fh.write(el.image_bytes)
                except Exception as exc:  # pragma: no cover
                    log.warning("could not write image %s: %s", el.source_url, exc)
    with open(pptx_dst, "rb") as fh:
        return fh.read()


def build_response(deck: Deck, json_dict: Dict[str, Any], pptx_src_path: str,
                   out_dir: str = DEFAULT_OUT_DIR) -> Dict[str, Any]:
    """Return the full /respond envelope (choices + json + files) and write /work/out."""
    pptx_bytes = _write_out_dir(deck, json_dict, pptx_src_path, out_dir)

    n = len(deck.presentation.slides)
    title = deck.presentation.title or "Presentation"
    summary = f"Generated a {n}-slide deck: {title}."

    envelope: Dict[str, Any] = {
        "choices": [{"message": {"role": "assistant", "content": summary}}],
        "json": json.dumps(json_dict, ensure_ascii=False),
        "files": [{
            "file_type": "pptx",
            "file_name": PPTX_NAME,
            "file_data": base64.b64encode(pptx_bytes).decode("ascii"),
        }],
    }
    log.info("response: %d slide(s); json channel + 1 pptx file (%d bytes); mirrored to %s",
             n, len(pptx_bytes), out_dir)
    return envelope
