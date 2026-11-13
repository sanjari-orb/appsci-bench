"""Unit tests for tasks/slides_generator/eval.py.

Deterministic checks are reproduced from the old evaluators/deterministic/*.py test cases
(true/false per check); rubric judges mock complete() at the LLM import site (matching
order_to_cash's test convention). Metrics that are not applicable to a data point assert
score is None with an explanatory reason.

Fully offline: no API calls (complete() is patched) and no Docker/LibreOffice
(render_pptx_to_pdf is patched).
"""
from __future__ import annotations

import io
import json
import pathlib
import re
from unittest.mock import patch

from pptx import Presentation

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, File
from tasks.slides_generator import eval as sg_eval


def _pptx_bytes(n_slides: int = 1) -> bytes:
    prs = Presentation()
    for _ in range(n_slides):
        prs.slides.add_slide(prs.slide_layouts[1])
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _pptx_bytes_with_text(n_slides: int = 1, *, title: str = "Title", body: str = "Body text") -> bytes:
    """Like `_pptx_bytes`, but fills the title/body placeholders so the deck extracts real content
    (`_pptx_bytes` leaves placeholders empty, which the content judges' broken-deck gate treats as
    no usable content -- see `sg_eval._deck_conversation`)."""
    prs = Presentation()
    for _ in range(n_slides):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
        for shape in slide.placeholders:
            if shape.placeholder_format.idx != 0 and shape.has_text_frame:
                shape.text_frame.text = body
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _pptx_bytes_with_duplicate_part(n_slides: int = 1) -> bytes:
    """A structurally valid deck whose zip archive has TWO entries for `ppt/slides/slide1.xml` --
    the corruption this task's build tooling can leave behind when it edits a slide part in place
    without removing the original entry (`python-pptx`/stdlib `zipfile` silently resolve this to the
    last entry written, so the deck still opens and extracts clean; real PowerPoint rejects the file
    and prompts to repair it -- see `pptx_json.has_duplicate_zip_parts`)."""
    import zipfile

    src = io.BytesIO(_pptx_bytes(n_slides))
    dst = io.BytesIO()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
            if item.filename == "ppt/slides/slide1.xml":
                zout.writestr(item, zin.read(item.filename))  # duplicate entry, same name
    return dst.getvalue()


def _turn(presentation_json: dict | None, *, pptx_bytes: bytes | None = None) -> ConversationTurn:
    files = [File(file_type="pptx", file_name="out.pptx", file_data=pptx_bytes)] if pptx_bytes else []
    resp_json = json.dumps(presentation_json) if presentation_json is not None else None
    return ConversationTurn(query=ArtifactAgentQuery(text="brief"),
                            response=ArtifactAgentOutput(json=resp_json, files=files))


def _slide(num=1, layout="content", content="hi"):
    return {"slideNumber": num, "layout": layout,
            "elements": [{"type": "text", "content": content}]}


def _expected(**fields) -> ArtifactAgentOutput:
    return ArtifactAgentOutput(json=json.dumps(fields))


# --- functional_success ------------------------------------------------------------------------

def test_functional_success_passes_with_valid_json_and_pptx():
    conv = [_turn({"presentation": {"slides": [_slide()]}}, pptx_bytes=_pptx_bytes())]
    score, meta = sg_eval.functional_success(data_point_id="p1", conversation=conv)
    assert score == 1.0 and meta["json_slide_count"] == 1


def test_functional_success_fails_with_no_slides():
    conv = [_turn({"presentation": {"slides": []}}, pptx_bytes=_pptx_bytes())]
    score, meta = sg_eval.functional_success(data_point_id="p1", conversation=conv)
    assert score == 0.0 and "slides array" in meta["reason"]


def test_functional_success_fails_with_no_pptx_file():
    conv = [_turn({"presentation": {"slides": [_slide()]}})]
    score, meta = sg_eval.functional_success(data_point_id="p1", conversation=conv)
    assert score == 0.0 and "pptx check failed" in meta["reason"]


def test_functional_success_fails_when_response_is_not_a_presentation_object():
    conv = [_turn({"something_else": True}, pptx_bytes=_pptx_bytes())]
    score, meta = sg_eval.functional_success(data_point_id="p1", conversation=conv)
    assert score == 0.0 and "not a presentation object" in meta["reason"]


def test_functional_success_fails_with_unopenable_pptx():
    conv = [_turn({"presentation": {"slides": [_slide()]}}, pptx_bytes=b"not a real pptx")]
    score, _ = sg_eval.functional_success(data_point_id="p1", conversation=conv)
    assert score == 0.0


def test_functional_success_fails_with_duplicate_zip_part():
    # Opens fine via python-pptx (which resolves the duplicate silently) but is a corrupt archive a
    # real PowerPoint would refuse -- must not pass just because python-pptx tolerates it.
    conv = [_turn({"presentation": {"slides": [_slide()]}},
                  pptx_bytes=_pptx_bytes_with_duplicate_part())]
    score, meta = sg_eval.functional_success(data_point_id="p1", conversation=conv)
    assert score == 0.0 and "duplicate part" in meta["reason"]


# --- output_format_compliance --------------------------------------------------------------------

def test_output_format_compliance_passes_when_all_slides_valid():
    conv = [_turn({"slides": [_slide(1), _slide(2)]})]
    score, meta = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 1.0 and meta["slide_count"] == 2


def test_output_format_compliance_fails_on_missing_layout():
    bad_slide = {"slideNumber": 1, "layout": "", "elements": [{"type": "text", "content": "x"}]}
    conv = [_turn({"slides": [bad_slide]})]
    score, meta = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 0.0 and any("layout" in v for v in meta["violations"])


def test_output_format_compliance_fails_on_empty_element_content():
    bad_slide = {"slideNumber": 1, "layout": "content", "elements": [{"type": "text", "content": ""}]}
    conv = [_turn({"slides": [bad_slide]})]
    score, _ = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 0.0


def test_output_format_compliance_fails_on_missing_slide_number():
    bad_slide = {"slideNumber": None, "layout": "content",
                "elements": [{"type": "text", "content": "x"}]}
    conv = [_turn({"slides": [bad_slide]})]
    score, meta = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 0.0 and any("slideNumber" in v for v in meta["violations"])


def test_output_format_compliance_fails_when_slides_array_is_absent():
    conv = [_turn({"presentation": {}})]
    score, meta = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 0.0 and "slides array" in meta["reason"]


def test_output_format_compliance_counts_a_table_element_as_content():
    # A table-only slide (from pptx2json's _extract_table: type="table" + non-empty rows, no
    # content/items) carries real data and must pass -- rows is a recognized content key.
    table_slide = {"slideNumber": 1, "layout": "content",
                   "elements": [{"type": "table", "rows": [["Q1", "51000"], ["Q2", "62000"]]}]}
    conv = [_turn({"slides": [table_slide]})]
    score, _ = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 1.0


def test_output_format_compliance_fails_on_all_blank_table_rows():
    # rows present but every cell blank -> no real content, must fail (not just "rows is a list").
    blank_table = {"slideNumber": 1, "layout": "content",
                   "elements": [{"type": "table", "rows": [["", ""], ["", ""]]}]}
    conv = [_turn({"slides": [blank_table]})]
    score, meta = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 0.0 and meta["violations"]


def test_output_format_compliance_fails_on_all_blank_items():
    # Pre-existing gap tightened alongside rows: an items list of only blank strings is not content.
    blank_items = {"slideNumber": 1, "layout": "content",
                   "elements": [{"type": "bullets", "items": ["", "  "]}]}
    conv = [_turn({"slides": [blank_items]})]
    score, meta = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 0.0 and meta["violations"]


def test_output_format_compliance_passes_on_items_with_text():
    good_items = {"slideNumber": 1, "layout": "content",
                  "elements": [{"type": "bullets", "items": ["First point", "Second point"]}]}
    conv = [_turn({"slides": [good_items]})]
    score, _ = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 1.0


def test_output_format_compliance_fails_when_elements_is_missing():
    bad_slide = {"slideNumber": 1, "layout": "content"}  # no "elements" key at all
    conv = [_turn({"slides": [bad_slide]})]
    score, meta = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 0.0 and any("no elements" in v for v in meta["violations"])


def test_output_format_compliance_fails_when_elements_is_an_empty_list():
    bad_slide = {"slideNumber": 1, "layout": "content", "elements": []}
    conv = [_turn({"slides": [bad_slide]})]
    score, meta = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 0.0 and any("no elements" in v for v in meta["violations"])


def test_output_format_compliance_is_proportional_not_all_or_nothing():
    # One malformed slide in four is a materially different outcome from a wholly malformed deck;
    # the old all-or-nothing scoring collapsed both to 0.0 and threw that signal away.
    bad_slide = {"slideNumber": 4, "layout": "", "elements": [{"type": "text", "content": "x"}]}
    conv = [_turn({"slides": [_slide(1), _slide(2), _slide(3), bad_slide]})]
    score, meta = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 0.75
    assert meta["slide_count"] == 4 and meta["compliant_slide_count"] == 3
    assert len(meta["violations"]) == 1


def test_output_format_compliance_scores_zero_when_every_slide_is_malformed():
    bad = {"slideNumber": 1, "layout": "", "elements": []}
    conv = [_turn({"slides": [bad, dict(bad, slideNumber=2)]})]
    score, meta = sg_eval.output_format_compliance(data_point_id="p1", conversation=conv)
    assert score == 0.0 and meta["compliant_slide_count"] == 0


# --- constraint_satisfaction ----------------------------------------------------------------------

def test_constraint_satisfaction_passes_within_range():
    conv = [_turn({"slides": [_slide(1), _slide(2)]})]
    exp = _expected(user_constraints={"expected_slide_count_range": [1, 3]})
    score, meta = sg_eval.constraint_satisfaction(data_point_id="p1", conversation=conv, expected_output=exp)
    assert score == 1.0 and meta["slide_count"] == 2


def test_constraint_satisfaction_fails_outside_range():
    conv = [_turn({"slides": [_slide(1)]})]
    exp = _expected(user_constraints={"expected_slide_count_range": [5, 8]})
    score, _ = sg_eval.constraint_satisfaction(data_point_id="p1", conversation=conv, expected_output=exp)
    assert score == 0.0


def test_constraint_satisfaction_none_when_range_absent():
    # e.g. the 4 local slidesgen-01..04 samples, which have no hand-authored oracle at all.
    conv = [_turn({"slides": [_slide(1)]})]
    score, meta = sg_eval.constraint_satisfaction(data_point_id="p1", conversation=conv,
                                                   expected_output=_expected())
    assert score is None and meta["applicable"] is False


# --- pptx_json.extract_presentation (used by the 6 content judges' broken-deck gate) --------------

def test_extract_presentation_returns_the_presentation_envelope():
    out = sg_eval.extract_presentation(_pptx_bytes_with_text(title="My Title", body="My Body"))
    assert out is not None
    slides = out["presentation"]["slides"]
    assert out["presentation"]["title"] == "My Title"
    assert len(slides) == 1
    assert slides[0]["slideNumber"] == 1
    texts = [e.get("content") for e in slides[0]["elements"]]
    assert "My Body" in texts


def test_extract_presentation_drops_speaker_notes():
    from pptx import Presentation as _Presentation
    prs = _Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Title"
    for shape in slide.placeholders:
        if shape.placeholder_format.idx != 0 and shape.has_text_frame:
            shape.text_frame.text = "Body text"
    slide.notes_slide.notes_text_frame.text = "SECRET-NOTE"
    buf = io.BytesIO()
    prs.save(buf)

    out = sg_eval.extract_presentation(buf.getvalue(), include_notes=False)
    assert out is not None
    slide_dict = out["presentation"]["slides"][0]
    assert "notes" not in slide_dict
    assert "SECRET-NOTE" not in json.dumps(out)


def test_extract_presentation_include_notes_true_retains_the_key():
    # Pins the curation-pipeline path (extract_presentation_from_path), which needs notes kept.
    from pptx import Presentation as _Presentation
    prs = _Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Title"
    slide.notes_slide.notes_text_frame.text = "keep-me"
    buf = io.BytesIO()
    prs.save(buf)

    out = sg_eval.extract_presentation(buf.getvalue(), include_notes=True)
    assert out is not None
    assert out["presentation"]["slides"][0]["notes"] == "keep-me"


def test_extract_presentation_none_on_unopenable_bytes():
    assert sg_eval.extract_presentation(b"not a pptx at all") is None


def test_has_duplicate_zip_parts_true_on_duplicate_entry():
    assert sg_eval.has_duplicate_zip_parts(_pptx_bytes_with_duplicate_part()) is True


def test_has_duplicate_zip_parts_false_on_clean_deck():
    assert sg_eval.has_duplicate_zip_parts(_pptx_bytes()) is False


def test_has_duplicate_zip_parts_false_on_unopenable_bytes():
    # Not a zip at all -- that failure belongs to the existing open check, not this one.
    assert sg_eval.has_duplicate_zip_parts(b"not a pptx at all") is False


def test_extract_presentation_empty_slides_list_on_zero_slides():
    # Zero slides still produces a real envelope (an empty slides list) -- only an unopenable file
    # returns None. See extract_presentation's docstring.
    from pptx import Presentation as _Presentation
    buf = io.BytesIO()
    _Presentation().save(buf)
    out = sg_eval.extract_presentation(buf.getvalue())
    assert out is not None
    assert out["presentation"]["slides"] == []


def test_extract_presentation_empty_elements_when_no_slide_has_text():
    # _pptx_bytes() leaves every placeholder empty -- no element has real content -- but the deck
    # still opens, so a real envelope is returned (elements: [] on every slide), not None.
    out = sg_eval.extract_presentation(_pptx_bytes(2))
    assert out is not None
    slides = out["presentation"]["slides"]
    assert len(slides) == 2
    assert all(slide["elements"] == [] for slide in slides)


# --- _deck_conversation / _gated: content judges read the PRODUCED PPTX, not the claimed JSON -----

def test_deck_conversation_substitutes_extracted_text_for_claimed_text():
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief"),
        response=ArtifactAgentOutput(
            json=json.dumps({"slides": [_slide(content="CLAIMED-ONLY-TEXT")]}),
            text="CLAIMED-ONLY-TEXT",
            files=[File(file_type="pptx", file_name="out.pptx",
                       file_data=_pptx_bytes_with_text(body="REAL-DECK-TEXT"))]))]
    deck_conv, reason = sg_eval._deck_conversation(conv)
    assert deck_conv is not None and reason == ""
    rendered = json.dumps(deck_conv[-1].response.json)
    assert "REAL-DECK-TEXT" in rendered
    assert "CLAIMED-ONLY-TEXT" not in rendered
    # text=None is mandatory -- see _deck_conversation's docstring on why.
    assert deck_conv[-1].response.text is None
    # files preserved -- the file-existence line the transcript judge renders must survive.
    assert deck_conv[-1].response.files and deck_conv[-1].response.files[0].file_name == "out.pptx"


def _gated_judges():
    return [
        ("prompt_satisfaction", sg_eval.prompt_satisfaction),
        ("structure_clarity", sg_eval.structure_clarity),
        ("content_narrative", sg_eval.content_narrative),
        ("content_coverage", sg_eval.content_coverage),
        ("groundedness", sg_eval.groundedness),
    ]


def test_broken_deck_scores_zero_not_none_so_weight_is_not_renormalized_away():
    # The core regression this diff exists to fix: a broken deck must be a real penalty (0.0),
    # never `None` -- `None` would renormalize the objective's weight away and RAISE the score.
    csv = File(file_type="csv", file_name="metrics.csv", file_data=b"revenue,51000")
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[csv]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[]))]  # no pptx
    for name, judge in _gated_judges():
        with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
            score, meta = judge(data_point_id="p1", conversation=conv)
        assert score == 0.0, f"{name} did not score 0.0 on a missing pptx"
        assert "pptx" in meta["reason"]
        mock_complete.assert_not_called()

    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = sg_eval.expectations_satisfaction(
            data_point_id="p1", conversation=conv, eval_rubric=_RUBRIC)
    assert score == 0.0 and "pptx" in meta["reason"]
    mock_complete.assert_not_called()


def test_broken_deck_gate_covers_only_unopenable_pptx():
    # Only an unopenable file has no JSON to hand a judge -- that stays gated to 0.0. A textless
    # (but openable) pptx now produces a real, if empty, extracted JSON and reaches the judge --
    # see test_textless_pptx_reaches_the_judge_with_empty_elements_json below.
    csv = File(file_type="csv", file_name="metrics.csv", file_data=b"revenue,51000")
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[csv]),
        response=ArtifactAgentOutput(
            json=json.dumps({"slides": [_slide()]}),
            files=[File(file_type="pptx", file_name="out.pptx", file_data=b"not a pptx at all")]))]
    for name, judge in _gated_judges():
        with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
            score, meta = judge(data_point_id="p1", conversation=conv)
        assert score == 0.0, f"{name} did not score 0.0 on an unopenable pptx"
        assert "pptx" in meta["reason"]
        mock_complete.assert_not_called()


def test_duplicate_zip_part_gates_to_zero_even_though_it_opens():
    # A deck with a duplicate part name opens and extracts clean (python-pptx/zipfile resolve the
    # duplicate to the last entry silently) but is a corrupt archive real PowerPoint rejects -- this
    # must gate to 0.0 like the unopenable case, not reach the judge just because it happens to open.
    csv = File(file_type="csv", file_name="metrics.csv", file_data=b"revenue,51000")
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[csv]),
        response=ArtifactAgentOutput(
            json=json.dumps({"slides": [_slide()]}),
            files=[File(file_type="pptx", file_name="out.pptx",
                       file_data=_pptx_bytes_with_duplicate_part())]))]
    for name, judge in _gated_judges():
        with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
            score, meta = judge(data_point_id="p1", conversation=conv)
        assert score == 0.0, f"{name} did not score 0.0 on a duplicate-part pptx"
        assert "duplicate part" in meta["reason"]
        mock_complete.assert_not_called()


def test_textless_pptx_reaches_the_judge_with_empty_elements_json():
    # A deck that opens but has no text anywhere (_pptx_bytes leaves every placeholder empty) is NOT
    # gated to 0.0: it produces a real JSON (elements: [] on every slide) and is handed to the judge,
    # which decides for itself how to score it -- see _deck_conversation's docstring.
    csv = File(file_type="csv", file_name="metrics.csv", file_data=b"revenue,51000")
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[csv]),
        response=ArtifactAgentOutput(
            json=json.dumps({"slides": [_slide()]}),
            files=[File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes(2))]))]
    for name, judge in _gated_judges():
        with patch("appsci_bench.eval.helpers.llm.complete",
                  return_value='{"reasoning": "empty deck", "score": 0.1}') as mock_complete:
            score, meta = judge(data_point_id="p1", conversation=conv)
        assert mock_complete.called, f"{name} did not reach the judge on a textless (but openable) pptx"
        assert meta["scored_from"] == "produced_pptx"


def test_gated_judges_report_scored_from_produced_pptx():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes_with_text())]
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "ok", "score": 0.6}'):
        _, meta = sg_eval.prompt_satisfaction(data_point_id="p1", conversation=conv)
    assert meta["scored_from"] == "produced_pptx"


def test_content_coverage_still_none_when_no_source_files_alongside_a_valid_deck():
    # Non-swallowing: the gate must not turn a legitimate judge-level None (no source files to
    # check against) into a 0.0 -- that would charge the agent for an input the harness didn't give.
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes())
    output = File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes_with_text())
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="build the deck", files=[template]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[output]))]
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = sg_eval.content_coverage(data_point_id="p1", conversation=conv)
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


def test_expectations_satisfaction_none_with_no_rubric_even_when_deck_is_broken():
    # Pins the order in expectations_satisfaction: "no rubric declared" is checked BEFORE the
    # broken-deck gate, since it means not-applicable-to-this-sample regardless of deck health.
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief"),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[]))]  # no pptx
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = sg_eval.expectations_satisfaction(data_point_id="p1", conversation=conv)
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()



# --- rubric via make_transcript_judge -------------------------------------------------------------

def test_prompt_satisfaction_scores_via_the_judge():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes_with_text())]
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "followed the brief", "score": 0.7}'):
        score, metadata = sg_eval.prompt_satisfaction(data_point_id="p1", conversation=conv)
    assert score == 0.7 and metadata == {"reasoning": "followed the brief", "scored_from": "produced_pptx"}


def test_content_narrative_scores_via_the_judge():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes_with_text())]
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "clear arc", "score": 0.65}'):
        score, _ = sg_eval.content_narrative(data_point_id="p1", conversation=conv)
    assert score == 0.65


def test_structure_clarity_scores_via_the_judge():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes_with_text())]
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "coherent", "score": 0.8}'):
        score, _ = sg_eval.structure_clarity(data_point_id="p1", conversation=conv)
    assert score == 0.8


# --- content_coverage via make_source_coverage_judge (reads INPUT source file content) ------------

def test_content_coverage_reads_input_source_file_content():
    # content_coverage is a source-reading judge (like groundedness): it must SEE the query's
    # attached source bytes to judge what proportion of source facts the deck covers. Attach a
    # source CSV to the query and confirm its content reaches the judge call.
    csv = File(file_type="csv", file_name="metrics.csv",
               file_data=b"Total revenue,51000\nOperating income,260")
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="build the deck", files=[csv]),
        response=ArtifactAgentOutput(
            json=json.dumps({"slides": [_slide()]}),
            files=[File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes_with_text())]))]
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "covers revenue", "score": 0.6}') as mock_complete:
        score, _ = sg_eval.content_coverage(data_point_id="p1", conversation=conv)
    assert score == 0.6
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("Total revenue,51000" in t for t in text_blocks)


def test_content_coverage_is_none_when_only_a_styling_template_is_attached():
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes())
    output = File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes_with_text())
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="build the deck", files=[template]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[output]))]
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = sg_eval.content_coverage(data_point_id="p1", conversation=conv)
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


# --- groundedness via make_source_grounding_judge (reads INPUT source file content) ---------------

def test_groundedness_reads_input_source_file_content():
    # Unlike the transcript judges, groundedness must SEE the query's attached source bytes -- the
    # CSV numbers the deck is checked against. Attach a source CSV to the query and confirm its
    # content reaches the judge call (not just "[a file was attached]").
    csv = File(file_type="csv", file_name="financials.csv",
               file_data=b"Total revenue,51000\nOperating income,260")
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="build the deck", files=[csv]),
        response=ArtifactAgentOutput(
            json=json.dumps({"slides": [_slide()]}),
            files=[File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes_with_text())]))]
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "51000 matches source", "score": 0.9}') as mock_complete:
        score, _ = sg_eval.groundedness(data_point_id="p1", conversation=conv)
    assert score == 0.9
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("Total revenue,51000" in t for t in text_blocks)


def test_groundedness_is_none_when_only_a_styling_template_is_attached():
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes())
    output = File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes_with_text())
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="build the deck", files=[template]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[output]))]
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = sg_eval.groundedness(data_point_id="p1", conversation=conv)
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


# --- expectations_satisfaction (the ONLY eval_rubric consumer) -----------------------------------

_RUBRIC = "1. Figures are exact, no rounding.\n2. No invented churn probability."


def test_expectations_satisfaction_scores_when_a_rubric_is_declared():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes_with_text())]
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "1 met, 2 met -> 2/2", "score": 1.0}'):
        score, metadata = sg_eval.expectations_satisfaction(
            data_point_id="p1", conversation=conv, eval_rubric=_RUBRIC)
    assert score == 1.0
    assert metadata == {"reasoning": "1 met, 2 met -> 2/2", "scored_from": "produced_pptx"}


def test_expectations_satisfaction_passes_the_rubric_text_through_to_the_judge():
    # The whole point of this metric: the sample's expectations must actually REACH the judge.
    # The rest of the suite never asserted prompt content, which is how the transcript templates
    # came to silently ignore this variable in the first place.
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes_with_text())]
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "ok", "score": 0.5}') as mock_complete:
        sg_eval.expectations_satisfaction(
            data_point_id="p1", conversation=conv, eval_rubric=_RUBRIC)
    prompt = json.dumps(mock_complete.call_args[0][0])
    assert "No invented churn probability" in prompt


def test_expectations_satisfaction_none_when_no_rubric_declared():
    conv = [_turn({"slides": [_slide()]})]
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = sg_eval.expectations_satisfaction(data_point_id="p1", conversation=conv)
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


def test_expectations_satisfaction_none_when_rubric_is_blank():
    # A whitespace-only rubric is "nothing declared", not "an empty checklist the deck failed".
    conv = [_turn({"slides": [_slide()]})]
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = sg_eval.expectations_satisfaction(
            data_point_id="p1", conversation=conv, eval_rubric="   \n  ")
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


# --- architectural guards ------------------------------------------------------------------------
# eval_rubric is the one input that VARIES per data point, so it is routed into exactly one
# dedicated objective. A metric that reads it would silently change definition sample to sample and
# stop being comparable across samples. These tests pin that decision down.

def test_only_the_expectations_prompt_references_eval_rubric():
    templates = {name: value for name, value in vars(sg_eval).items()
                 if name.endswith(("_TEMPLATE", "_RUBRIC", "_SYSTEM_PROMPT")) and isinstance(value, str)}
    assert templates, "no prompt templates found -- did they get renamed?"
    offenders = [name for name, text in templates.items()
                 if "eval_rubric" in text and name != "_EXPECTATIONS_SATISFACTION_TEMPLATE"]
    assert not offenders, f"only _EXPECTATIONS_SATISFACTION_TEMPLATE may read eval_rubric: {offenders}"


_LEAK_MARKER = "DO-NOT-LEAK-THIS-RUBRIC-TEXT"


def _assert_rubric_not_in_prompt(mock_complete):
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert not any(_LEAK_MARKER in t for t in text_blocks), \
        "eval_rubric reached the judge prompt at runtime -- only expectations_satisfaction may read it"


def test_content_coverage_does_not_forward_eval_rubric_at_runtime():
    # The static-template guard above can't catch a leak introduced by forwarding the eval_rubric
    # PARAMETER at call time rather than templating it into a prompt string -- confirm the runtime
    # path directly.
    csv = File(file_type="csv", file_name="metrics.csv", file_data=b"Total revenue,51000")
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="build the deck", files=[csv]),
        response=ArtifactAgentOutput(
            json=json.dumps({"slides": [_slide()]}),
            files=[File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes_with_text())]))]
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "ok", "score": 0.6}') as mock_complete:
        sg_eval.content_coverage(data_point_id="p1", conversation=conv, eval_rubric=_LEAK_MARKER)
    _assert_rubric_not_in_prompt(mock_complete)


def test_groundedness_does_not_forward_eval_rubric_at_runtime():
    csv = File(file_type="csv", file_name="financials.csv", file_data=b"Total revenue,51000")
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="build the deck", files=[csv]),
        response=ArtifactAgentOutput(
            json=json.dumps({"slides": [_slide()]}),
            files=[File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes_with_text())]))]
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "ok", "score": 0.9}') as mock_complete:
        sg_eval.groundedness(data_point_id="p1", conversation=conv, eval_rubric=_LEAK_MARKER)
    _assert_rubric_not_in_prompt(mock_complete)


def test_template_adherence_does_not_forward_eval_rubric_at_runtime():
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes())
    output = File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes())
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[template]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[output]))]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(_pdf_file(), None)), \
         patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "ok", "score": 0.85}') as mock_complete:
        sg_eval.template_adherence(data_point_id="p1", conversation=conv, eval_rubric=_LEAK_MARKER)
    _assert_rubric_not_in_prompt(mock_complete)


def test_layout_quality_does_not_forward_eval_rubric_at_runtime():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes())]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(_pdf_file(), None)), \
         patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "ok", "score": 0.75}') as mock_complete:
        sg_eval.layout_quality(data_point_id="p1", conversation=conv, eval_rubric=_LEAK_MARKER)
    _assert_rubric_not_in_prompt(mock_complete)



def test_expectations_prompt_does_reference_eval_rubric():
    assert "{{ eval_rubric }}" in sg_eval._EXPECTATIONS_SATISFACTION_TEMPLATE


def test_removed_objectives_are_gone():
    # figure_relevance never produced a score; layout_color_quality was folded into layout_quality.
    # outcome_coverage was retargeted and renamed to prompt_satisfaction.
    for gone in ("figure_relevance", "layout_color_quality", "outcome_coverage"):
        assert not hasattr(sg_eval, gone), f"{gone} should have been removed"


def test_rubric_bands_do_not_overlap():
    # A judge asked to score exactly 0.8 must have exactly one valid band.
    for name, text in vars(sg_eval).items():
        if name.endswith(("_TEMPLATE", "_RUBRIC", "_SYSTEM_PROMPT")) and isinstance(text, str):
            for stale in ("0.6-0.8", "0.4-0.6", "0.2-0.4", "0.0-0.2"):
                assert stale not in text, f"{name} still has the overlapping band {stale}"


# --- _first_n_slides -------------------------------------------------------------------------------

def test_first_n_slides_trims_a_longer_deck():
    trimmed, reason, degraded = sg_eval._first_n_slides(_pptx_bytes(6), n=4)
    assert reason is None
    prs = Presentation(io.BytesIO(trimmed))
    assert len(prs.slides) == 4
    # The trim must be reportable: a whole-deck score computed from part of the deck is never silent.
    assert degraded == "judged the first 4 of 6 slides"


def test_first_n_slides_is_a_noop_on_a_shorter_deck():
    original = _pptx_bytes(2)
    # No degrade reason: nothing was dropped, so nothing to report.
    assert sg_eval._first_n_slides(original, n=4) == (original, None, None)


def test_first_n_slides_reports_a_reason_on_an_unreadable_deck():
    # Must NOT fall back to the untrimmed bytes: rendering an untrimmed deck would send every page
    # of an arbitrarily long deck to the judge and score slides these objectives declare out of scope.
    trimmed, reason, degraded = sg_eval._first_n_slides(b"not a pptx at all", n=4)
    assert trimmed is None
    assert "could not trim" in reason
    # A failed trim scores nothing at all, so there is no degraded score to annotate.
    assert degraded is None


# --- _dedupe_zip -------------------------------------------------------------------------------
# Regression coverage for a real failure: some decks carry a latent python-pptx package quirk
# (an orphaned notes-slide relationship) that only surfaces as an actual duplicate zip member once
# `_first_n_slides` removes slides and re-saves -- python-pptx serializes the same part name twice,
# and LibreOffice then silently produces no output PDF for it (no error, exit 0). The quirk itself
# needs a specific deck shape to trigger and isn't practically fabricable via python-pptx's public
# API in a unit test, so this tests `_dedupe_zip` directly rather than trying to reproduce the quirk.

def _zip_bytes(names_and_contents: list[tuple[str, bytes]]) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in names_and_contents:
            z.writestr(name, content)
    return buf.getvalue()


def test_dedupe_zip_drops_earlier_duplicates_keeping_the_last():
    import zipfile

    data = _zip_bytes([
        ("ppt/slides/slide1.xml", b"first"),
        ("ppt/slides/slide2.xml", b"dropped"),
        ("ppt/slides/slide2.xml", b"kept"),
    ])
    out = sg_eval._dedupe_zip(data)
    z = zipfile.ZipFile(io.BytesIO(out))
    names = z.namelist()
    assert len(names) == len(set(names)), "output must have no duplicate member names"
    assert z.read("ppt/slides/slide2.xml") == b"kept"


def test_dedupe_zip_is_a_noop_on_a_zip_with_no_duplicates():
    import zipfile

    data = _zip_bytes([("a.xml", b"1"), ("b.xml", b"2")])
    out = sg_eval._dedupe_zip(data)
    z = zipfile.ZipFile(io.BytesIO(out))
    assert set(z.namelist()) == {"a.xml", "b.xml"}
    assert z.read("a.xml") == b"1"
    assert z.read("b.xml") == b"2"


# --- template_adherence (vision judge over rendered PDFs) -----------------------------------------

def _pdf_file(name: str = "rendered.pdf") -> File:
    return File(file_type="pdf", file_name=name, file_data=b"%PDF-1.4 fake")


def test_template_adherence_scores_via_the_judge():
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes())
    output = File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes())
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[template]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[output]))]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(_pdf_file(), None)), \
         patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "matches theme", "score": 0.85}'):
        score, _ = sg_eval.template_adherence(data_point_id="p1", conversation=conv)
    assert score == 0.85


def test_template_adherence_none_when_no_template_attached():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes())]
    score, meta = sg_eval.template_adherence(data_point_id="p1", conversation=conv)
    assert score is None and meta["applicable"] is False
    assert "missing" in meta["reason"]


def test_template_adherence_none_when_rendering_fails():
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes())
    output = File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes())
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[template]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[output]))]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(None, "soffice not found")):
        score, meta = sg_eval.template_adherence(data_point_id="p1", conversation=conv)
    assert score is None and meta["applicable"] is False
    assert "rendering failed" in meta["reason"]


def test_template_adherence_records_the_trim_under_degraded():
    # A 10-slide deck judged on 4 slides must say so: a whole-deck score computed from a fraction of
    # the deck is exactly what helpers/files.py's "never silent" rule exists to prevent.
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes(4))
    output = File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes(10))
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[template]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[output]))]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(_pdf_file(), None)), \
         patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "matches theme", "score": 0.85}'):
        score, meta = sg_eval.template_adherence(data_point_id="p1", conversation=conv)
    assert score == 0.85
    # Only the output was over the cap; the 4-slide template passed through untouched.
    assert meta["degraded"] == "output: judged the first 4 of 10 slides"


def test_template_adherence_records_both_decks_when_both_are_trimmed():
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes(6))
    output = File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes(10))
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[template]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[output]))]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(_pdf_file(), None)), \
         patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "matches theme", "score": 0.85}'):
        _, meta = sg_eval.template_adherence(data_point_id="p1", conversation=conv)
    assert meta["degraded"] == ("template: judged the first 4 of 6 slides; "
                                "output: judged the first 4 of 10 slides")


def test_template_adherence_reports_no_degrade_when_nothing_is_trimmed():
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes(4))
    output = File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes(3))
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[template]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[output]))]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(_pdf_file(), None)), \
         patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "matches theme", "score": 0.85}'):
        _, meta = sg_eval.template_adherence(data_point_id="p1", conversation=conv)
    assert "degraded" not in meta


def test_template_adherence_appends_to_a_degrade_the_judge_already_set():
    # The judge can degrade its own payload; a score degraded twice must report both causes.
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes(4))
    output = File(file_type="pptx", file_name="out.pptx", file_data=_pptx_bytes(10))
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[template]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[output]))]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(_pdf_file(), None)), \
         patch("tasks.slides_generator.eval._template_adherence_judge",
              return_value=(0.85, {"degraded": "payload truncated"})):
        _, meta = sg_eval.template_adherence(data_point_id="p1", conversation=conv)
    assert meta["degraded"] == "payload truncated; output: judged the first 4 of 10 slides"


# --- layout_quality (vision judge over a rendered PDF) -------------------------------------------

def test_layout_quality_scores_via_the_judge():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes())]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(_pdf_file(), None)), \
         patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "clean layout", "score": 0.75}'):
        score, _ = sg_eval.layout_quality(data_point_id="p1", conversation=conv)
    assert score == 0.75


def test_layout_quality_none_when_no_output_pptx():
    conv = [_turn({"slides": [_slide()]})]
    score, meta = sg_eval.layout_quality(data_point_id="p1", conversation=conv)
    assert score is None and meta["applicable"] is False
    assert "no output" in meta["reason"]


def test_layout_quality_none_when_rendering_fails():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes())]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(None, "soffice not found")):
        score, meta = sg_eval.layout_quality(data_point_id="p1", conversation=conv)
    assert score is None and meta["applicable"] is False
    assert "rendering failed" in meta["reason"]


def test_layout_quality_records_the_trim_under_degraded():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes(10))]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(_pdf_file(), None)), \
         patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "clean layout", "score": 0.75}'):
        score, meta = sg_eval.layout_quality(data_point_id="p1", conversation=conv)
    assert score == 0.75
    assert meta["degraded"] == "judged the first 4 of 10 slides"


def test_layout_quality_reports_no_degrade_when_nothing_is_trimmed():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes(3))]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(_pdf_file(), None)), \
         patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "clean layout", "score": 0.75}'):
        _, meta = sg_eval.layout_quality(data_point_id="p1", conversation=conv)
    assert "degraded" not in meta


def test_layout_quality_appends_to_a_degrade_the_judge_already_set():
    conv = [_turn({"slides": [_slide()]}, pptx_bytes=_pptx_bytes(10))]
    with patch("tasks.slides_generator.eval.render_pptx_to_pdf",
              return_value=(_pdf_file(), None)), \
         patch("tasks.slides_generator.eval._layout_quality_judge",
              return_value=(0.75, {"degraded": "payload truncated"})):
        _, meta = sg_eval.layout_quality(data_point_id="p1", conversation=conv)
    assert meta["degraded"] == "payload truncated; judged the first 4 of 10 slides"


def test_vision_judges_are_none_not_zero_when_the_deck_cannot_be_trimmed():
    # An unopenable deck must bail to None (an infra/input problem) rather than raise out of the eval
    # function, which scoring.py charges to the agent as a hard 0.0.
    corrupt = File(file_type="pptx", file_name="out.pptx", file_data=b"not a pptx at all")
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes())
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="brief", files=[template]),
        response=ArtifactAgentOutput(json=json.dumps({"slides": [_slide()]}), files=[corrupt]))]

    score, meta = sg_eval.layout_quality(data_point_id="p1", conversation=conv)
    assert score is None and meta["applicable"] is False
    assert "trim failed" in meta["reason"]

    score, meta = sg_eval.template_adherence(data_point_id="p1", conversation=conv)
    assert score is None and meta["applicable"] is False
    assert "trim failed" in meta["reason"]



# --- shipped eval_data samples -------------------------------------------------------------------

def _sample_paths():
    root = pathlib.Path(__file__).resolve().parents[3] / "tasks/slides_generator/eval_data"
    return sorted(root.glob("product-slidesgen-*/sample.json"))


def test_every_shipped_sample_declares_expectations():
    # expectations_satisfaction carries weight 2.0 and returns None without a rubric, so a sample
    # shipped without one silently drops the heaviest objective for that data point.
    paths = _sample_paths()
    assert paths, "no eval_data samples found"
    missing = [p.parent.name for p in paths
               if not (json.loads(p.read_text()).get("eval_rubric") or "").strip()]
    assert not missing, f"samples with no eval_rubric: {missing}"


def test_shipped_expectations_stay_non_visual():
    # expectations_satisfaction is a TRANSCRIPT judge -- it sees the JSON presentation object and
    # never a render. A rubric line about color, font, or palette asks it to judge something it
    # structurally cannot see; those expectations belong to template_adherence.
    visual = re.compile(r"#[0-9A-Fa-f]{6}|\bLato\b|\bfonts?\b|\bcolou?rs?\b|\bpalette\b|\bteal\b",
                        re.IGNORECASE)
    offenders = {}
    for p in _sample_paths():
        hits = visual.findall(json.loads(p.read_text()).get("eval_rubric") or "")
        if hits:
            offenders[p.parent.name] = sorted(set(hits))
    assert not offenders, f"visual expectations leaked into eval_rubric: {offenders}"
