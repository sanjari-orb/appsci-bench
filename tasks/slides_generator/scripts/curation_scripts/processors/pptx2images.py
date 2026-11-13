"""Convert a .pptx file to per-slide PNG images optimised for LLM consumption."""
import logging
import sys
from pathlib import Path

from processors.base import BaseProcessor
from processors.pptx2pdf import PptxToPdf

try:
    from pdf2image import convert_from_path as _convert_from_path

    _PDF2IMAGE_AVAILABLE = True
except ImportError:
    _PDF2IMAGE_AVAILABLE = False

logger = logging.getLogger(__name__)

# 150 DPI ≈ 1024 px wide on a 16:9 slide — enough for LLM vision, low token cost.
_DPI = 150


class PptxToImages(BaseProcessor):
    """Render each slide of a ``.pptx`` file to a PNG image.

    Pipeline: pptx → pdf (LibreOffice) → per-slide PNG (pdf2image/poppler).

    Output directory: ``<src_stem>_slides/`` created next to *src*.
    Files: ``slide_001.png``, ``slide_002.png``, …

    PNG at 150 DPI avoids JPEG artefacts that degrade LLM vision/OCR accuracy.

    Requires:
        pip install pdf2image
        # macOS:  brew install poppler
        # Linux:  apt-get install poppler-utils
    """

    def execute(self, src: Path) -> Path:
        """Convert *src* to slide images; return the output directory.

        Args:
            src: Path to the input ``.pptx`` file.

        Returns:
            Path to the directory containing ``slide_NNN.png`` files.

        Raises:
            FileNotFoundError: *src* does not exist.
            ValueError: *src* is not a ``.pptx`` file.
            RuntimeError: pdf2image/poppler not installed, or conversion fails.
        """
        self._require_file(src)
        self._require_ext(src, ".pptx")

        if not _PDF2IMAGE_AVAILABLE:
            raise RuntimeError(
                "pdf2image not installed. Run: pip install pdf2image\n"
                "Also install poppler: brew install poppler  (macOS) "
                "or apt-get install poppler-utils  (Linux)"
            )

        pdf_path = PptxToPdf().execute(src)

        out_dir = src.parent / f"{src.stem}_slides"
        out_dir.mkdir(exist_ok=True)

        images = _convert_from_path(pdf_path, dpi=_DPI)
        if not images:
            raise RuntimeError(f"pdf2image returned no pages for {pdf_path}")

        paths = []
        for i, img in enumerate(images, start=1):
            out_path = out_dir / f"slide_{i:03d}.png"
            img.save(out_path, format="PNG", optimize=False)
            paths.append(out_path)
            logger.debug("saved slide %d → %s", i, out_path)

        logger.info("exported %d slides → %s", len(paths), out_dir)
        return out_dir


def main() -> None:
    """CLI entry point: python pptx2images.py <file.pptx>"""
    if len(sys.argv) < 2:
        print("usage: python pptx2images.py <file.pptx>")
        sys.exit(1)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out_dir = PptxToImages().execute(Path(sys.argv[1]))
    logger.info("done → %s", out_dir)


if __name__ == "__main__":
    main()
