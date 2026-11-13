"""Extract per-slide content from a .pptx file and write it as JSON.

Delegates to `tasks/slides_generator/pptx_json.py` -- the shared extraction logic also used by
`eval.py`'s content judges. This file stays a thin CLI/BaseProcessor wrapper so the curation
pipeline (`main.py`) keeps working unchanged.
"""
import logging
import sys
from pathlib import Path

from processors.base import BaseProcessor

try:                                    # repo root already importable (pytest, eval)
    from tasks.slides_generator.pptx_json import write_presentation_json
except ImportError:                     # standalone CLI, cwd == curation_scripts/
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
    from tasks.slides_generator.pptx_json import write_presentation_json

logger = logging.getLogger(__name__)


class PptxToJson(BaseProcessor):
    """Extract per-slide content from a ``.pptx`` file and write it as JSON.

    Output structure:
    - ``presentation``: Top-level object with metadata and slides array
    - ``title``, ``theme``, ``aspectRatio``, ``metadata``: Presentation properties
    - Each slide contains: ``slideNumber``, ``layout``, ``elements``, ``notes``
    - Elements can be: text, bullet_list, image, table with content, items, rows, and styling

    Output: ``<stem>_json.json`` alongside *src*.

    Requires ``python-pptx``.
    """

    def execute(self, src: Path) -> Path:
        """Parse *src* and write ``<stem>_json.json`` alongside it.

        Args:
            src: Path to the input ``.pptx`` file.

        Returns:
            Path to the generated JSON file.
        """
        self._require_file(src)
        self._require_ext(src, ".pptx")
        dst = write_presentation_json(src)
        self._require_output(dst)
        return dst


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python pptx2json.py <file.pptx>")
        sys.exit(1)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dst = PptxToJson().execute(Path(sys.argv[1]))
    logger.info("saved → %s", dst)


if __name__ == "__main__":
    main()
