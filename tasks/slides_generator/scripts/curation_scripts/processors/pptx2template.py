"""Convert a .pptx into a layout-deduplicated lorem-ipsum template."""
import contextlib
import logging
import sys
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.oxml.ns import qn

from processors.base import BaseProcessor

logger = logging.getLogger(__name__)

_LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, "
    "quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo "
    "consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse "
    "cillum dolore eu fugiat nulla pariatur."
)

_CONTENT_TYPES_TO_REMOVE = {
    MSO_SHAPE_TYPE.PICTURE,
    MSO_SHAPE_TYPE.CHART,
    MSO_SHAPE_TYPE.TABLE,
    MSO_SHAPE_TYPE.MEDIA,
    MSO_SHAPE_TYPE.OLE_CONTROL_OBJECT,
    MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT,
    MSO_SHAPE_TYPE.LINKED_OLE_OBJECT,
    MSO_SHAPE_TYPE.LINKED_PICTURE,
    MSO_SHAPE_TYPE.WEB_VIDEO,
    MSO_SHAPE_TYPE.INK,
    MSO_SHAPE_TYPE.INK_COMMENT,
}

_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _lorem_for(text: str) -> str:
    """Return lorem-ipsum filler matching the character length of *text*."""
    n = max(1, len(text))
    return (_LOREM * (n // len(_LOREM) + 2))[:n]


def _replace_texts(shape) -> None:
    """Replace all text in *shape* with length-matched lorem ipsum, preserving formatting."""
    if not shape.has_text_frame:
        return
    for para in shape.text_frame.paragraphs:
        for run in para.runs:
            run.text = _lorem_for(run.text)
        if not para.runs and para.text:
            tf_elem = para._p
            existing_r = tf_elem.findall(qn("a:r"))
            if existing_r:
                for r in existing_r:
                    t = r.find(qn("a:t"))
                    if t is not None:
                        t.text = _lorem_for(t.text or "")
            else:
                r_elem = etree.SubElement(tf_elem, qn("a:r"))
                t_elem = etree.SubElement(r_elem, qn("a:t"))
                t_elem.text = _lorem_for(para.text)


def _drop_shape_with_rels(slide_part, shape) -> None:
    """Remove *shape* from the slide, dropping all its relationship references.

    Scans the shape XML for all rId values and removes them from the slide part
    before deleting the element to prevent broken OOXML packages.
    """
    elem = shape._element
    rids: set[str] = set()
    for local in ("embed", "link", "id"):
        full = "{" + _REL_NS + "}" + local
        for node in elem.iter():
            val = node.get(full)
            if val:
                rids.add(val)
    for node in elem.iter():
        for attr_name, attr_val in node.attrib.items():
            if attr_name.endswith("}id") and attr_val.startswith("rId"):
                rids.add(attr_val)
    for rid in rids:
        with contextlib.suppress(KeyError):
            slide_part.drop_rel(rid)
    elem.getparent().remove(elem)


def _is_placeholder(shape) -> bool:
    """Return True if *shape* is a layout/master placeholder (has a <p:ph> element)."""
    return (
        shape._element.find("./" + qn("p:ph")) is not None
        or shape._element.find(".//" + qn("p:ph")) is not None
    )


def _has_any_placeholder(slide) -> bool:
    """Return True if any top-level shape on *slide* is a layout placeholder."""
    return any(_is_placeholder(shape) for shape in slide.shapes)


def _remove_non_template_shapes(slide) -> None:
    """Remove non-template shapes from *slide*, keeping template structure.

    On decks built from real PowerPoint layouts, placeholder shapes (those with
    a ``<p:ph>`` element) define the layout and are preserved; everything else
    — pictures, charts, tables, media, OLE objects, and free text/auto-shapes
    — is removed along with its slide-part relationships.

    Some decks (e.g. exported from Gamma/Canva-style tools) carry no
    placeholders at all — every shape, including body text, is a free
    text box or auto-shape. Removing "everything but placeholders" there
    would strip all content and leave an empty slide. For those slides we
    keep text-bearing auto-shapes/text boxes too, and only strip the
    non-text content types (pictures, charts, tables, media, OLE, groups
    that end up empty).
    """
    keep_text_shapes = not _has_any_placeholder(slide)

    def _remove_from(shapes_iter) -> None:
        for shape in list(shapes_iter):
            if shape.shape_type in _CONTENT_TYPES_TO_REMOVE:
                _drop_shape_with_rels(slide.part, shape)
            elif shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                _remove_from(shape.shapes)
                if not list(shape.shapes):
                    _drop_shape_with_rels(slide.part, shape)
            elif _is_placeholder(shape):
                continue
            elif keep_text_shapes and shape.has_text_frame and shape.text_frame.text.strip():
                continue
            else:
                _drop_shape_with_rels(slide.part, shape)

    _remove_from(slide.shapes)


def _slide_layout_name(slide) -> str:
    try:
        return slide.slide_layout.name
    except Exception:
        return ""


def _slide_dedup_key(slide) -> tuple[str, tuple[str, ...]]:
    """Return a dedup key covering both real layout name and remaining shape shape.

    Real PowerPoint decks have distinct, meaningful layout names, so the layout
    name alone is enough to dedup on. Placeholder-less decks (e.g. Gamma
    exports) report the same generic layout name (often ``"DEFAULT"``) for
    every slide regardless of content, so the layout name alone would collapse
    all of them into one. Folding in each remaining shape's type keeps slides
    with a visibly different shape composition distinct.
    """
    layout_name = _slide_layout_name(slide)
    shape_signature = tuple(str(shape.shape_type) for shape in slide.shapes)
    return layout_name, shape_signature


class PptxToTemplate(BaseProcessor):
    """Convert a ``.pptx`` presentation to a layout-deduplicated lorem-ipsum template.

    The processor removes non-template shapes (pictures, charts, tables, media),
    replaces remaining text with length-matched lorem ipsum, and deduplicates
    slides that share the same layout signature — retaining only the first
    occurrence. On decks with real placeholder-driven layouts, only placeholder
    shapes are kept and dedup runs on layout name alone. On placeholder-less
    decks (e.g. Gamma/Canva exports, where body text lives in free text boxes),
    text-bearing shapes are kept too, and dedup additionally factors in each
    slide's remaining shape composition, since those decks often report the
    same generic layout name for every slide.

    Output filename: ``<stem>_template.pptx`` alongside *src*.

    Requires ``python-pptx`` and ``lxml``.
    """

    def execute(self, src: Path) -> Path:
        """Anonymise and deduplicate *src*, writing the template file alongside it.

        Args:
            src: Path to the input ``.pptx`` file.

        Returns:
            Path to ``<stem>_template.pptx``.
        """
        self._require_file(src)
        self._require_ext(src, ".pptx")

        prs = Presentation(src)

        for slide in prs.slides:
            _remove_non_template_shapes(slide)
            for shape in slide.shapes:
                _replace_texts(shape)
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    for child in shape.shapes:
                        _replace_texts(child)

        seen_keys: set[tuple[str, tuple[str, ...]]] = set()
        slides_to_remove: list = []
        xml_slides = prs.slides._sldIdLst

        for slide in list(prs.slides):
            key = _slide_dedup_key(slide)
            if not list(slide.shapes) or key in seen_keys:
                slides_to_remove.append(slide._element)
            else:
                seen_keys.add(key)

        rid_to_elem: dict[str, object] = {}
        for rel in prs.slides.part.rels.values():
            with contextlib.suppress(AttributeError):
                rid_to_elem[rel.rId] = rel.target_part._element

        for slide_elem in slides_to_remove:
            for sld_id in list(xml_slides):
                rel_id = sld_id.get("{" + _REL_NS + "}id")
                if rel_id and rid_to_elem.get(rel_id) is slide_elem:
                    xml_slides.remove(sld_id)
                    break

        dst = src.with_stem(src.stem + "_template")
        prs.save(dst)

        self._require_output(dst)
        return dst


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python pptx2template.py <file.pptx>")
        sys.exit(1)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dst = PptxToTemplate().execute(Path(sys.argv[1]))
    logger.info("saved → %s", dst)


if __name__ == "__main__":
    main()
