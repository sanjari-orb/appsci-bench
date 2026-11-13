"""Extract text and images from a .pdf (PyMuPDF / fitz).

We pull selectable text and embedded raster images, and also render each page to a PNG. The page
renders let the vision model read charts/figures/scanned tables that are not recoverable as text or
image objects. To keep prompts bounded we cap how many page renders we emit.
"""
from __future__ import annotations

import io

from . import ExtractedImage, SourceDoc

MAX_PAGE_RENDERS = 12
MIN_EMBEDDED_IMG_BYTES = 3000   # skip tiny logos/bullets


def extract_pdf(name: str, data: bytes) -> SourceDoc:
    import fitz  # PyMuPDF

    doc = fitz.open(stream=data, filetype="pdf")
    text_parts = []
    images = []

    for pno in range(doc.page_count):
        page = doc.load_page(pno)
        txt = page.get_text("text").strip()
        if txt:
            text_parts.append(f"[page {pno + 1}]\n{txt}")

        # Embedded raster images on the page.
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                base = doc.extract_image(xref)
                blob = base.get("image")
                if blob and len(blob) >= MIN_EMBEDDED_IMG_BYTES:
                    ext = base.get("ext", "png")
                    mt = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                    images.append(ExtractedImage(data=blob, media_type=mt,
                                                 caption=f"{name} p{pno + 1}"))
            except Exception:
                continue

    # Page renders (capped) so charts/figures are always visible to the VLM.
    n_render = min(doc.page_count, MAX_PAGE_RENDERS)
    for pno in range(n_render):
        try:
            page = doc.load_page(pno)
            pix = page.get_pixmap(dpi=110)
            png = pix.tobytes("png")
            images.append(ExtractedImage(data=png, media_type="image/png",
                                         caption=f"{name} page {pno + 1} (render)"))
        except Exception:
            continue

    doc.close()
    return SourceDoc(name=name, kind="pdf", text="\n\n".join(text_parts), images=images)
