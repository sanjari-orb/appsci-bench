"""Extract text, tables, images, and speaker notes from a .pptx content document (python-pptx).

Note: this is for .pptx files supplied as *content* (e.g. a prior deck to draw from) — not the house
template, which is handled by template_analysis.py.
"""
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


def extract_pptx(name: str, data: bytes) -> SourceDoc:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(io.BytesIO(data))
    text_parts = []
    tables = []
    images = []

    for sno, slide in enumerate(prs.slides, start=1):
        slide_lines = [f"[slide {sno}]"]
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                slide_lines.append(shape.text_frame.text.strip())
            if shape.has_table:
                rows = [[c.text.strip() for c in row.cells] for row in shape.table.rows]
                tables.append(ExtractedTable(rows=rows, caption=f"{name} slide {sno}"))
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                try:
                    img = shape.image
                    ext = (img.ext or "png").lower()
                    images.append(
                        ExtractedImage(data=img.blob, media_type=_MEDIA.get(ext, "image/png"),
                                       caption=f"{name} slide {sno}")
                    )
                except Exception:
                    pass
        # Speaker notes carry substance in many decks.
        try:
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    slide_lines.append(f"(notes) {notes}")
        except Exception:
            pass
        if len(slide_lines) > 1:
            text_parts.append("\n".join(slide_lines))

    return SourceDoc(name=name, kind="pptx", text="\n\n".join(text_parts),
                     tables=tables, images=images)
