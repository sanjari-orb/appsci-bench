"""Content extraction across the five allowed input types (docx/pdf/pptx/csv/xlsx).

Each extractor returns a normalised `SourceDoc` carrying text, tables, and images so downstream stages
see one shape regardless of the source format. Images are kept as raw bytes and forwarded to the
vision model — the customer's inputs can carry meaning in charts/figures, so text-only extraction is
not enough.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List

log = logging.getLogger("slidesgen.extract")


@dataclass
class ExtractedImage:
    data: bytes
    media_type: str = "image/png"
    caption: str = ""


@dataclass
class ExtractedTable:
    rows: List[List[str]] = field(default_factory=list)
    caption: str = ""

    def to_text(self) -> str:
        return "\n".join(" | ".join(str(c) for c in row) for row in self.rows)


@dataclass
class SourceDoc:
    name: str
    kind: str                                   # docx|pdf|pptx|csv|xlsx
    text: str = ""
    tables: List[ExtractedTable] = field(default_factory=list)
    images: List[ExtractedImage] = field(default_factory=list)
    error: str = ""

    def as_text(self, max_images_note: bool = True) -> str:
        parts = [f"=== SOURCE: {self.name} ({self.kind}) ==="]
        if self.text.strip():
            parts.append(self.text.strip())
        for i, t in enumerate(self.tables):
            parts.append(f"[table {i + 1}{': ' + t.caption if t.caption else ''}]\n{t.to_text()}")
        if self.images and max_images_note:
            parts.append(f"[{len(self.images)} image(s) from this document are attached separately]")
        if self.error:
            parts.append(f"[extraction warning: {self.error}]")
        return "\n\n".join(parts)


def extract(path: str, data: bytes | None = None) -> SourceDoc:
    """Dispatch on file extension. `data` may be provided to avoid re-reading from disk."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    if data is None:
        with open(path, "rb") as fh:
            data = fh.read()
    try:
        if ext == "docx":
            from .docx_extract import extract_docx

            return extract_docx(name, data)
        if ext == "pdf":
            from .pdf_extract import extract_pdf

            return extract_pdf(name, data)
        if ext == "pptx":
            from .pptx_extract import extract_pptx

            return extract_pptx(name, data)
        if ext == "csv":
            from .csv_extract import extract_csv

            return extract_csv(name, data)
        if ext in ("xlsx", "xlsm"):
            from .xlsx_extract import extract_xlsx

            return extract_xlsx(name, data)
    except Exception as exc:  # a file we can't parse is handled upstream (reported as "not readable")
        log.warning("could not parse %s (%s): %s", name, ext, exc)
        return SourceDoc(name=name, kind=ext or "unknown", error=str(exc))
    return SourceDoc(name=name, kind=ext or "unknown", error=f"unsupported type: {ext}")
