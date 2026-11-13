"""Extract a table from a .csv (stdlib csv). Figures here are often load-bearing and must be
carried through to the deck exactly, so we keep every cell as-is (no rounding/reformatting)."""
from __future__ import annotations

import csv
import io

from . import ExtractedTable, SourceDoc


def extract_csv(name: str, data: bytes) -> SourceDoc:
    text = data.decode("utf-8-sig", errors="replace")
    # Sniff the delimiter; default to comma.
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t|")
    except Exception:
        dialect = csv.excel
    rows = [list(r) for r in csv.reader(io.StringIO(text), dialect)]
    rows = [r for r in rows if any(c.strip() for c in r)]
    table = ExtractedTable(rows=rows, caption=name)
    return SourceDoc(name=name, kind="csv", tables=[table])
