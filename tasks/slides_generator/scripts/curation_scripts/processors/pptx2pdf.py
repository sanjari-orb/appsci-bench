"""Convert a .pptx file to PDF using LibreOffice headless mode."""
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from processors.base import BaseProcessor

logger = logging.getLogger(__name__)

_SOFFICE_CANDIDATES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
]


def _find_soffice() -> Path:
    """Return path to the soffice executable, or raise RuntimeError if not found."""
    if which := shutil.which("soffice"):
        return Path(which)
    for candidate in _SOFFICE_CANDIDATES:
        p = Path(candidate)
        if p.exists():
            return p
    raise RuntimeError(
        "LibreOffice not found. Install it from https://www.libreoffice.org/download/ "
        "or ensure 'soffice' is on your PATH."
    )


class PptxToPdf(BaseProcessor):
    """Convert a ``.pptx`` file to PDF using LibreOffice headless mode.

    Searches for the ``soffice`` executable in this order:

    1. ``soffice`` on ``$PATH`` (handles Linux system installs, Homebrew)
    2. ``/Applications/LibreOffice.app/Contents/MacOS/soffice`` (macOS drag-install)
    3. ``/usr/bin/soffice`` and ``/usr/local/bin/soffice`` (Linux package paths)

    Output: a ``.pdf`` file written to the same directory as *src*.
    """

    def execute(self, src: Path) -> Path:
        """Invoke LibreOffice headless to convert *src* to PDF.

        Args:
            src: Path to the input ``.pptx`` file.

        Returns:
            Path to the generated ``.pdf`` file (same directory as *src*).

        Raises:
            RuntimeError: If LibreOffice is not found or conversion fails.
        """
        self._require_file(src)
        self._require_ext(src, ".pptx")

        soffice = _find_soffice()
        try:
            subprocess.run(
                [
                    str(soffice),
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(src.parent),
                    str(src),
                ],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace")
            raise RuntimeError(
                f"LibreOffice conversion failed for {src.name}: {stderr}"
            ) from exc

        dst = src.with_suffix(".pdf")
        self._require_output(dst)
        return dst


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python pptx2pdf.py <file.pptx>")
        sys.exit(1)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dst = PptxToPdf().execute(Path(sys.argv[1]))
    logger.info("saved → %s", dst)


if __name__ == "__main__":
    main()
