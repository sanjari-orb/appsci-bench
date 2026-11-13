"""Render a `.pptx` deck to PDF via LibreOffice headless, for use by vision-based judges.

Host-only: requires the `soffice` binary (LibreOffice) on whatever machine runs eval. Not a
Python dependency -- install via `brew install --cask libreoffice` (macOS) or
`apt install libreoffice` (Linux). Never raises on a missing/failing renderer: every failure
mode degrades to `(None, reason)` so a host without LibreOffice doesn't crash an eval run --
only the objectives that need rendering return `None` for that sample.

Concurrency: `appsci_bench.eval.scoring` runs objectives (including these renders) across a
`ThreadPoolExecutor`, so multiple `soffice --headless` conversions can be in flight at once.
Plain `soffice --headless` invocations share a user profile/lock by default -- two concurrent
calls race for it and one silently produces no output PDF. Each call here gets its own
`-env:UserInstallation` profile directory (scoped to that call's temp dir, cleaned up with it) so
concurrent conversions never contend for the same profile.

No caching: every call converts. An earlier content-hash cache was removed because it could not hit
in practice -- the only callers trim a deck to its first N slides before rendering, and re-saving
through python-pptx stamps fresh timestamps into the file, so the "same" deck hashes differently on
every call. The cache paid a lock and 32 PDFs of memory to memoize decks short enough to skip the
re-save (templates) while always missing the output decks it was written for.

Every rendered PDF is recompressed by default (`_compress_pdf`) before being returned: LibreOffice
carries each slide's embedded images through at their original resolution/encoding, which is what
makes a media-heavy deck (e.g. a ~20MB branded template) blow the vision judges' token budget in
`helpers/files.py` even after trimming to 4 slides -- trimming cuts slide *count*, not per-slide
image weight. Recompression downscales/re-encodes each embedded image with Pillow; it never
changes page count or text. Best-effort: any failure (corrupt PDF, no images, Pillow error) falls
back to the uncompressed bytes rather than failing the render.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from appsci_bench.eval.datatypes import File

# Recompression targets for embedded images in a rendered PDF -- chosen to keep a slide legible to
# a vision judge (this is not a print/archival use) while cutting the base64 payload substantially.
# This is deliberately aggressive: measured against real rendered decks, most of a slide PDF's size
# is images LibreOffice re-embeds at source resolution, and pushing quality/dimension down this far
# is what actually clears the vision judges' token budget (helpers/files.py) on media-heavy decks --
# milder settings (e.g. 1600px/q70) only shave ~10% and still overflow. pypdf's `ImageFile.replace`
# re-encodes to JPEG regardless of the source format.
_MAX_IMAGE_DIMENSION_PX = 900
_JPEG_QUALITY = 35


def _compress_pdf(pdf_bytes: bytes) -> bytes:
    """Downscale/re-encode every embedded image in `pdf_bytes` to shrink the file. Best-effort:
    returns `pdf_bytes` unchanged on any failure (corrupt/unparseable PDF, no images, encoding
    error) -- compression is a size optimization, never a correctness requirement.

    Dedupes repeated images (e.g. a template's logo/background placed once on a slide master or
    layout, which LibreOffice then bakes into every page that inherits it): each source image's raw
    bytes key a cache, and the JPEG re-encode is done once per distinct image and reused for every
    occurrence -- a background repeated across N pages costs one encode instead of N."""
    try:
        from PIL import Image
        from pypdf import PdfWriter

        writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes))
        changed = False
        encoded_by_raw: dict[bytes, bytes] = {}
        for page in writer.pages:
            for img in page.images:
                try:
                    raw = img.data
                    cached = encoded_by_raw.get(raw) if raw else None
                    if cached is not None:
                        img.replace(Image.open(io.BytesIO(cached)))
                        changed = True
                        continue

                    image = img.image
                    if image is None:
                        continue
                    if image.mode not in ("RGB", "L"):
                        image = image.convert("RGB")
                    if max(image.size) > _MAX_IMAGE_DIMENSION_PX:
                        scale = _MAX_IMAGE_DIMENSION_PX / max(image.size)
                        new_size = (max(1, round(image.width * scale)),
                                    max(1, round(image.height * scale)))
                        image = image.resize(new_size, Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    image.save(buf, format="JPEG", quality=_JPEG_QUALITY)
                    encoded = buf.getvalue()
                    if raw:
                        encoded_by_raw[raw] = encoded
                    img.replace(Image.open(io.BytesIO(encoded)))
                    changed = True
                except Exception:
                    # One bad image must not sink the whole conversion -- skip it, keep the rest.
                    continue
        if not changed:
            return pdf_bytes
        # `img.replace` always allocates a fresh indirect object per occurrence, even when the
        # bytes written are identical (as they now are for every repeat of a deduped image above)
        # -- this merges those byte-identical objects back down to one so the size win actually
        # lands in the output, not just in encode time.
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
        out = io.BytesIO()
        writer.write(out)
        compressed = out.getvalue()
        # Guard against a pathological re-encode that grows the file (rare, but possible for a PDF
        # with tiny/already-optimized images plus writer overhead) -- never return something worse.
        return compressed if len(compressed) < len(pdf_bytes) else pdf_bytes
    except Exception:
        return pdf_bytes

_SOFFICE_CANDIDATES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
]


def _find_soffice() -> str | None:
    """Return the soffice executable path, or None if not found anywhere we look."""
    which = shutil.which("soffice")
    if which:
        return which
    for candidate in _SOFFICE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def _convert(data: bytes, timeout_s: float) -> tuple[bytes | None, str | None]:
    """One LibreOffice conversion. Never raises: every failure mode returns `(None, reason)`."""
    soffice = _find_soffice()
    if soffice is None:
        return None, "soffice (LibreOffice) not found on PATH or known install locations"

    with tempfile.TemporaryDirectory() as tmp_dir:
        src_path = Path(tmp_dir) / "in.pptx"
        src_path.write_bytes(data)
        profile_dir = Path(tmp_dir) / "profile"
        try:
            subprocess.run(
                [soffice, f"-env:UserInstallation=file://{profile_dir}",
                 "--headless", "--convert-to", "pdf", "--outdir", tmp_dir, str(src_path)],
                check=True, capture_output=True, timeout=timeout_s,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
            return None, f"libreoffice conversion failed: {stderr or exc}"
        except subprocess.TimeoutExpired:
            return None, f"libreoffice conversion timed out after {timeout_s}s"
        except OSError as exc:
            # soffice existed at _find_soffice() time but isn't runnable (permissions, removed
            # mid-race, corrupt install) -- degrade like every other failure mode, don't raise.
            return None, f"libreoffice conversion could not be started: {exc}"

        pdf_path = src_path.with_suffix(".pdf")
        if not pdf_path.exists():
            return None, "libreoffice did not produce a pdf output file"
        return pdf_path.read_bytes(), None


def render_pptx_to_pdf(data: bytes, *, timeout_s: float = 120.0) -> tuple[File | None, str | None]:
    """Render pptx `data` to a single PDF `File` via LibreOffice headless. Never raises: returns
    `(None, reason)` when soffice is unavailable, conversion fails, or times out. Every call runs a
    conversion -- see the module docstring on why results are not cached. The returned PDF's
    embedded images are recompressed by default (see the module docstring's `_compress_pdf`
    note)."""
    pdf_bytes, reason = _convert(data, timeout_s)
    if pdf_bytes is None:
        return None, reason
    pdf_bytes = _compress_pdf(pdf_bytes)
    return File(file_type="pdf", file_name="rendered.pdf", file_data=pdf_bytes), None
