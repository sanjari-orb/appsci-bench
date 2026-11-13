"""Convert a parsed-presentation Markdown file to a prose input brief via Claude."""
import logging
import sys
from pathlib import Path

import anthropic

from processors.base import BaseProcessor

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a technical writer reconstructing a source document from a parsed presentation.

Your job is to produce a manuscript that could plausibly have been the source material
the authors used before converting their ideas into slides. The manuscript should:

- Be written in flowing prose, not bullet points
- Be meant to be read, not presented
- Length should be at max a 5 pager: 1000-1500 words.
- Preserve all ideas, findings, and conclusions from the original
- Use a natural essay structure (not mirroring the slide order slavishly)
- Include only images that appear to come from the source document
  (speaker photos, externally sourced figures) — not images that are clearly
  presentation diagrams, template graphics, or decorative slide elements
- Reference images using markdown syntax: ![](filename) as done in the input document
- Omit slide headers, section dividers, and presentation artefacts
  (e.g. conference branding, "thank you" slides, repeated boilerplate)
- Add all the extra information that is expected in a manuscript but is missing from the input document.
Return only the manuscript content in markdown format. No preamble, no explanation.
"""


class MarkdownToBrief(BaseProcessor):
    """Convert a parsed-presentation Markdown file to a prose input brief via Claude.

    Input: a ``.md`` file produced by :class:`PdfToMarkdown` containing
    slide-extracted text.
    Output: ``input_brief.md`` written to the same directory as *src*.

    Requires the ``anthropic`` package and the ``ANTHROPIC_API_KEY``
    environment variable (or gateway credentials).
    """

    MODEL = "claude-haiku-4-5"
    MAX_TOKENS = 4096*4

    def execute(self, src: Path) -> Path:
        """Call Claude to rewrite *src* as a prose input brief.

        Args:
            src: Path to the input Markdown file (``.md``).

        Returns:
            Path to ``input_brief.md`` in the same directory as *src*.
        """
        self._require_file(src)
        self._require_ext(src, ".md")

        presentation_text = src.read_text(encoding="utf-8")
        client = anthropic.Anthropic()
        with client.messages.stream(
            model=self.MODEL,
            max_tokens=self.MAX_TOKENS,
            timeout=120,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": presentation_text}],
        ) as stream:
            brief_text = stream.get_final_text()
        dst = src.parent / "input_brief.md"
        dst.write_text(brief_text, encoding="utf-8")

        self._require_output(dst)
        return dst


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python markdown2brief.py <presentation.md>")
        sys.exit(1)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dst = MarkdownToBrief().execute(Path(sys.argv[1]))
    logger.info("saved → %s", dst)


if __name__ == "__main__":
    main()
