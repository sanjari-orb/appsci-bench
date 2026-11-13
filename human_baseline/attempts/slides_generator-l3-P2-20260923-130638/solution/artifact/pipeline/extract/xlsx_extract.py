"""Extract sheets (as tables) and embedded images from a .xlsx (openpyxl).

Figures must survive verbatim, so cell values are stringified without reformatting. Empty trailing
rows/cols are trimmed to keep tables tight.
"""
from __future__ import annotations

import io

from . import ExtractedImage, ExtractedTable, SourceDoc

MAX_ROWS = 200
MAX_COLS = 40


def _fmt(v) -> str:
    if v is None:
        return ""
    return str(v)


def extract_xlsx(name: str, data: bytes) -> SourceDoc:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    tables = []
    images = []

    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(values_only=True):
            row = [_fmt(c) for c in r][:MAX_COLS]
            rows.append(row)
            if len(rows) >= MAX_ROWS:
                break
        # Trim fully empty trailing rows.
        while rows and not any(c.strip() for c in rows[-1]):
            rows.pop()
        if rows:
            tables.append(ExtractedTable(rows=rows, caption=f"{name} — sheet '{ws.title}'"))

        # Embedded images (charts exported as images, logos, screenshots).
        for img in getattr(ws, "_images", []):
            try:
                ref = img.ref
                blob = ref.getvalue() if hasattr(ref, "getvalue") else ref
                if isinstance(blob, bytes) and len(blob) > 2000:
                    images.append(ExtractedImage(data=blob, media_type="image/png",
                                                 caption=f"{name} sheet '{ws.title}'"))
            except Exception:
                continue

    return SourceDoc(name=name, kind="xlsx", tables=tables, images=images)
