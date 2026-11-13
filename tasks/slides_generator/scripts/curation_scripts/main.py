"""Run the curation pipeline over a single evaluation data folder."""
import argparse
import logging
import sys
from pathlib import Path

from processors.markdown2brief import MarkdownToBrief
from processors.pdf2markdown import PdfToMarkdown
from processors.pptx2json import PptxToJson
from processors.pptx2pdf import PptxToPdf
from processors.pptx2template import PptxToTemplate

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_STEPS: list[tuple[str, str, str]] = [
    # (label, input_filename, output_filename)
    ("PPTX → PDF",      "presentation.pptx", "presentation.pdf"),
    ("PDF → Markdown",  "presentation.pdf",  "presentation.md"),
    ("MD → Brief",      "presentation.md",   "input_brief.md"),
    ("PPTX → Template", "presentation.pptx", "presentation_template.pptx"),
    ("PPTX → JSON",     "presentation.pptx", "presentation_json.json"),
]


def _processor_for(label: str) -> object:
    return {
        "PPTX → PDF":      PptxToPdf(),
        "PDF → Markdown":  PdfToMarkdown(),
        "MD → Brief":      MarkdownToBrief(),
        "PPTX → Template": PptxToTemplate(),
        "PPTX → JSON":     PptxToJson(),
    }[label]


def curate_slidesgen(slidesgen_dir: Path, *, force: bool = False) -> None:
    """Run all pipeline steps on a single *slidesgen_dir* folder."""
    logger.info("=== %s ===", slidesgen_dir.name)
    for label, input_name, output_name in _STEPS:
        src = slidesgen_dir / input_name
        dst = slidesgen_dir / output_name

        if not src.exists():
            logger.info("  [skip] %s — input not found: %s", label, input_name)
            continue
        if dst.exists() and not force:
            logger.info("  [skip] %s — output exists: %s", label, output_name)
            continue

        logger.info("  [run]  %s", label)
        try:
            _processor_for(label).execute(src)
            logger.info("  [ok]   saved → %s", output_name)
        except Exception as exc:
            logger.error("  [fail] %s: %s", label, exc)
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Curate evaluation data folders.")
    parser.add_argument(
        "path",
        type=Path,
        help="Path to a single slidesgen folder to curate.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run all steps even if output files already exist.",
    )
    args = parser.parse_args()

    if not args.path.is_dir():
        logger.error("Not a directory: %s", args.path)
        sys.exit(1)

    curate_slidesgen(args.path, force=args.force)

    logger.info("Done.")


if __name__ == "__main__":
    main()
