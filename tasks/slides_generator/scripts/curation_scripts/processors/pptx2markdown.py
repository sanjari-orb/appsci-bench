"""Convert a .pptx file to a Markdown file using liteparse."""
import logging
import sys
from pathlib import Path

from liteparse import LiteParse

from processors.base import BaseProcessor

logger = logging.getLogger(__name__)


class PptxToMarkdown(BaseProcessor):
    """Convert a ``.pptx`` file to a Markdown file using liteparse.LiteParse.

    The output file is written alongside the source with the same stem and
    a ``.md`` extension (``presentation.pptx`` → ``presentation.md``).

    Requires the ``liteparse`` package.
    """

    def execute(self, src: Path) -> Path:
        """Parse *src* (a ``.pptx``) and write a Markdown file alongside it.

        Args:
            src: Path to the input PPTX file. Must have a ``.pptx`` suffix.

        Returns:
            Path to the generated ``.md`` file.
        """
        self._require_file(src)
        self._require_ext(src, ".pptx")

        dst = src.with_suffix(".md")
        lp = LiteParse(output_format="markdown", image_mode="embed")
        result = lp.parse(src)

        text = result.text
        if result.images:
            img_dir = src.parent / "img"
            img_dir.mkdir(exist_ok=True)
            for img in result.images:
                filename = f"image_{img.id}.{img.format}"
                (img_dir / filename).write_bytes(img.bytes)
                text = text.replace(f"]({filename})", f"](img/{filename})")

        dst.write_text(text, encoding="utf-8")

        self._require_output(dst)
        return dst


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python pptx2markdown.py <file.pptx>")
        sys.exit(1)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dst = PptxToMarkdown().execute(Path(sys.argv[1]))
    logger.info("saved → %s", dst)


if __name__ == "__main__":
    main()
