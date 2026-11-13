"""Extract text, tables, and embedded images from a .docx (python-docx)."""
from __future__ import annotations

import io

from . import ExtractedImage, ExtractedTable, SourceDoc

_MEDIA = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "webp": "image/webp",
}


def extract_docx(name: str, data: bytes) -> SourceDoc:
    import docx  # python-docx

    doc = docx.Document(io.BytesIO(data))

    lines = []
    for para in doc.paragraphs:
        txt = para.text.strip()
        if not txt:
            continue
        style = (para.style.name if para.style else "") or ""
        if style.startswith("Heading") or style == "Title":
            lines.append(f"[{style}] {txt}")
        elif "List" in style:
            lines.append(f"- {txt}")
        else:
            lines.append(txt)

    tables = []
    for tbl in doc.tables:
        rows = [[cell.text.strip() for cell in row.cells] for row in tbl.rows]
        if any(any(c for c in r) for r in rows):
            tables.append(ExtractedTable(rows=rows))

    images = []
    # Embedded images live in the package's related parts.
    try:
        for rel in doc.part.rels.values():
            if "image" in rel.reltype:
                blob = rel.target_part.blob
                ext = rel.target_part.partname.ext.lower().lstrip(".")
                images.append(ExtractedImage(data=blob, media_type=_MEDIA.get(ext, "image/png")))
    except Exception:
        pass

    return SourceDoc(name=name, kind="docx", text="\n".join(lines), tables=tables, images=images)
