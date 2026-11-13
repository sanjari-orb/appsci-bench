"""Convert a hand-designed .pptx (plain shapes, no real placeholders) into a real
PowerPoint template: one named slide layout per slide design, with the variable
text/table shapes turned into genuine placeholders.

Usage:
    python design2template.py <designed.pptx> [<output.pptx>]

If <output.pptx> is omitted, writes <stem>_template.pptx alongside the input.
"""
import copy
import sys
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.opc.constants import CONTENT_TYPE as CT
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.parts.slide import SlideLayoutPart

# ---------------------------------------------------------------------------
# Per-slide placeholder plan: which shape names become which placeholder type,
# in what order (order determines the idx assigned). Shapes not listed here
# are left as-is (decorative background shapes inherited by the layout).
# ---------------------------------------------------------------------------
SLIDE_PLANS = [
    {
        "layout_name": "Cover",
        "shapes": [
            ("cover-title", "title"),
            ("byline", "body"),
            ("audience", "subTitle"),
        ],
    },
    {
        "layout_name": "Bulleted Content",
        "shapes": [
            ("title", "title"),
            ("bullet-0", "body"),
            ("bullet-1", "body"),
            ("bullet-2", "body"),
            ("bullet-3", "body"),
            ("footer-2", "ftr"),
            ("page-2", "sldNum"),
        ],
    },
    {
        "layout_name": "Section Divider",
        "shapes": [
            ("title", "title"),
            ("consequence", "body"),
            ("footer-3", "ftr"),
            ("page-3", "sldNum"),
        ],
    },
    {
        "layout_name": "Two-Phase Comparison",
        "shapes": [
            ("title", "title"),
            ("phase1", "body"),
            ("phase2", "body"),
            ("phase-0-0", "body"),
            ("phase-0-1", "body"),
            ("phase-0-2", "body"),
            ("phase-1-0", "body"),
            ("phase-1-1", "body"),
            ("phase-1-2", "body"),
            ("footer-4", "ftr"),
            ("page-4", "sldNum"),
        ],
    },
    {
        "layout_name": "Investment Table",
        "shapes": [
            ("title", "title"),
            ("payback-number", "body"),
            ("payback-label", "body"),
            ("payback-formula", "body"),
            ("investment-ledger-table", "tbl"),
            ("footer-5", "ftr"),
            ("page-5", "sldNum"),
        ],
    },
    {
        "layout_name": "The Ask",
        "shapes": [
            ("title", "title"),
            ("ask-value", "body"),
            ("ask-label", "body"),
            ("ask-bullet-0", "body"),
            ("ask-bullet-1", "body"),
            ("ask-bullet-2", "body"),
            ("footer-7", "ftr"),
            ("page-7", "sldNum"),
        ],
    },
]


def _make_placeholder(shape, ph_type, idx):
    """Inject a <p:ph> element into *shape*'s own XML, turning it into a real placeholder."""
    element = shape._element
    nv_pr = element.find(qn("p:nvSpPr")) if element.tag == qn("p:sp") else element.find(qn("p:nvGraphicFramePr"))
    nv_pr = nv_pr.find(qn("p:nvPr"))
    if nv_pr.find(qn("p:ph")) is not None:
        return
    ph = etree.SubElement(nv_pr, qn("p:ph"))
    if ph_type != "obj":
        ph.set("type", ph_type)
    ph.set("idx", str(idx))


def _add_layout_from_slide(prs, slide, layout_name, shape_plan):
    """Deep-copy *slide*'s shape tree into a brand-new named slide layout on the
    presentation's slide master, tagging the shapes in *shape_plan* as placeholders.

    Returns the new SlideLayout object.
    """
    master = prs.slide_masters[0]
    master_part = master.part
    package = master_part.package

    # Start from the master's own first layout so <p:clrMapOvr> etc. are schema-valid,
    # then replace its shape tree with a deep copy of the designed slide's shapes.
    base_layout = prs.slide_masters[0].slide_layouts[0]
    new_sldLayout_elm = copy.deepcopy(base_layout._element)
    new_sldLayout_elm.cSld.name = layout_name

    spTree = new_sldLayout_elm.cSld.spTree
    for child in list(spTree):
        tag = etree.QName(child).localname
        if tag in ("sp", "graphicFrame", "grpSp", "pic", "cxnSp"):
            spTree.remove(child)

    for shape in slide.shapes:
        spTree.append(copy.deepcopy(shape._element))

    partname = package.next_partname("/ppt/slideLayouts/slideLayout%d.xml")
    new_layout_part = SlideLayoutPart(partname, CT.PML_SLIDE_LAYOUT, package, new_sldLayout_elm)
    new_layout_part.relate_to(master_part, RT.SLIDE_MASTER)
    rId = master_part.relate_to(new_layout_part, RT.SLIDE_LAYOUT)

    sldLayoutIdLst = master._element.get_or_add_sldLayoutIdLst()
    existing_ids = [int(e.get("id")) for e in sldLayoutIdLst.sldLayoutId_lst if e.get("id")]
    next_id = max(existing_ids, default=2147483648) + 1
    new_entry = sldLayoutIdLst._add_sldLayoutId()
    new_entry.rId = rId
    new_entry.set("id", str(next_id))

    new_layout = new_layout_part.slide_layout

    by_name = {s.name: s for s in new_layout.shapes}
    for idx, (shape_name, ph_type) in enumerate(shape_plan):
        shape = by_name.get(shape_name)
        if shape is None:
            print(f"  [warn] shape {shape_name!r} not found on layout {layout_name!r}, skipping")
            continue
        _make_placeholder(shape, ph_type, idx)

    return new_layout


def _remove_shape(slide, shape):
    shape._element.getparent().remove(shape._element)


def convert(src: Path, dst: Path) -> Path:
    prs = Presentation(src)
    src_slides = list(prs.slides)
    if len(src_slides) != len(SLIDE_PLANS):
        print(
            f"[warn] expected {len(SLIDE_PLANS)} slides matching SLIDE_PLANS, "
            f"found {len(src_slides)} in {src}"
        )

    new_layouts = []
    for slide, plan in zip(src_slides, SLIDE_PLANS):
        print(f"[layout] building {plan['layout_name']!r} from slide {slide.slide_id}")
        layout = _add_layout_from_slide(prs, slide, plan["layout_name"], plan["shapes"])
        new_layouts.append(layout)

    # Drop the original designed slides -- a template ships layouts, not slides.
    # Removing the <p:sldId> entry alone leaves the slide part orphaned in the
    # package (colliding with new slides' auto-assigned partnames), so also drop
    # the underlying relationship/part via drop_rel.
    xml_slides = prs.slides._sldIdLst
    for slide_elem in [s._element for s in src_slides]:
        for sld_id in list(xml_slides):
            rel_id = sld_id.get(qn("r:id"))
            rel = prs.slides.part.rels.get(rel_id)
            if rel is not None and rel.target_part._element is slide_elem:
                xml_slides.remove(sld_id)
                prs.slides.part.drop_rel(rel_id)
                break

    # Add one representative slide per new layout, matching pptx2template.py's
    # existing convention (a template.pptx keeps one slide per distinct layout).
    for layout in new_layouts:
        prs.slides.add_slide(layout)

    prs.save(dst)
    return dst


def main():
    if len(sys.argv) < 2:
        print("usage: python design2template.py <designed.pptx> [<output.pptx>]")
        sys.exit(1)
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_stem(src.stem + "_template")
    convert(src, dst)
    print(f"saved -> {dst}")


if __name__ == "__main__":
    main()
