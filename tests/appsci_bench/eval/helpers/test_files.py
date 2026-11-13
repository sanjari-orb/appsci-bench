"""Unit tests for the file-similarity judge. No network --
`complete()` is mocked at the LLM (Level 0) import site.
"""
from __future__ import annotations

import io
from unittest.mock import patch

from docx import Document
from openpyxl import Workbook
from pptx import Presentation

from appsci_bench.common.model_config import ModelConfig
from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, File
from appsci_bench.eval.helpers.files import (
    _degrade, _estimate_tokens, _file_content, _pair_files,
    extract_docx_text, extract_pdf_text, extract_pptx_text, extract_xlsx_text,
    make_file_similarity_judge, make_source_coverage_judge, make_source_grounding_judge,
    make_visual_comparison_judge, make_visual_quality_judge,
)

_CFG = ModelConfig(model="anthropic/claude-opus-4-8", api_key="no-token")


def _pptx_bytes(bullet_texts: list[str]) -> bytes:
    prs = Presentation()
    for text in bullet_texts:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _docx_bytes(paragraphs: list[str]) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pdf_bytes(lines: list[str]) -> bytes:
    """Build a minimal one-page PDF with *lines* drawn as text, by hand -- no PDF-writing library
    is a project dependency, and adding one just for this fixture isn't worth it."""
    content = f"BT /F1 12 Tf 20 100 Td ({' '.join(lines)}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 200 200] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n")
    xref_offset = buf.tell()
    n = len(objects) + 1
    buf.write(f"xref\n0 {n}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(b"trailer\n<< /Size " + str(n).encode() + b" /Root 1 0 R >>\nstartxref\n"
              + str(xref_offset).encode() + b"\n%%EOF")
    return buf.getvalue()


# --- per-file-type extractors --------------------------------------------------------------------

def test_extract_pptx_text_walks_every_slide_no_sampling():
    data = _pptx_bytes(["Point A", "Point B", "Point C"])
    text = extract_pptx_text(data)
    assert "Point A" in text and "Point B" in text and "Point C" in text
    assert text.index("Point A") < text.index("Point B") < text.index("Point C")


def test_extract_docx_text_walks_every_paragraph():
    data = _docx_bytes(["first para", "second para", "third para"])
    text = extract_docx_text(data)
    assert "first para" in text and "second para" in text and "third para" in text


def test_extract_xlsx_text_walks_every_row_no_sampling():
    data = _xlsx_bytes([["a", "b"], [1, 2], [3, 4]])
    text = extract_xlsx_text(data)
    assert "a, b" in text and "1, 2" in text and "3, 4" in text


def test_extract_pdf_text_walks_every_page_no_sampling():
    data = _pdf_bytes(["hello from page one"])
    text = extract_pdf_text(data)
    assert "hello from page one" in text


# --- native path selection -----------------------------------------------------------------------

def test_pdf_defaults_to_text_extraction_not_native_block():
    f = File(file_type="pdf", file_name="r.pdf", file_data=b"whatever")
    blocks, how = _file_content(f, model="anthropic/claude-opus-4-8", extractors={"pdf": lambda _: "extracted"})
    assert how == "text_extraction"
    assert blocks == [{"type": "text", "text": "extracted"}]


def test_pdf_uses_native_document_block_when_allowed_and_model_supports_it():
    f = File(file_type="pdf", file_name="r.pdf", file_data=b"%PDF-1.4 fake but doesn't matter here")
    blocks, how = _file_content(
        f, model="anthropic/claude-opus-4-8", extractors={}, allow_native_pdf=True)
    assert how == "native_document"
    assert blocks == [{"type": "file", "file": {
        "file_data": blocks[0]["file"]["file_data"], "filename": "r.pdf"}}]
    assert blocks[0]["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_pdf_skips_rather_than_guesses_when_allowed_but_model_lacks_native_support():
    f = File(file_type="pdf", file_name="r.pdf", file_data=b"whatever")
    blocks, how = _file_content(
        f, model="not-a-real-model-xyz", extractors={}, allow_native_pdf=True)
    assert blocks == [] and how == "skipped_no_native_pdf_support"


def test_image_prefers_native_image_block_when_model_supports_vision():
    f = File(file_type="png", file_name="chart.png", file_data=b"fakepngbytes")
    blocks, how = _file_content(f, model="anthropic/claude-opus-4-8", extractors={})
    assert how == "native_image"
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_pptx_uses_text_extraction_not_native():
    data = _pptx_bytes(["only slide"])
    f = File(file_type="pptx", file_name="d.pptx", file_data=data)
    from appsci_bench.eval.helpers.files import _DEFAULT_TEXT_EXTRACTORS
    blocks, how = _file_content(f, model="anthropic/claude-opus-4-8", extractors=_DEFAULT_TEXT_EXTRACTORS)
    assert how == "text_extraction"
    assert "only slide" in blocks[0]["text"]


def test_unknown_file_type_with_no_extractor_is_skipped_not_guessed():
    f = File(file_type="txt", file_name="x.txt", file_data=b"hi")
    blocks, how = _file_content(f, model="anthropic/claude-opus-4-8", extractors={})
    assert blocks == [] and how == "skipped_no_extractor"


# --- pairing ---------------------------------------------------------------------------------

def test_pair_files_matches_by_name_first():
    a = File(file_type="txt", file_name="report.txt", file_data=b"a")
    e = File(file_type="txt", file_name="report.txt", file_data=b"e")
    pairs, unmatched = _pair_files([a], [e])
    assert pairs == [("report.txt", a, e)]
    assert unmatched == []


def test_pair_files_falls_back_to_position_when_names_differ():
    a = File(file_type="txt", file_name="out.txt", file_data=b"a")
    e = File(file_type="txt", file_name="expected.txt", file_data=b"e")
    pairs, unmatched = _pair_files([a], [e])
    assert pairs == [("position_0", a, e)]
    assert unmatched == []


def test_pair_files_flags_unmatched_extra_actual_file():
    a1 = File(file_type="txt", file_name="a1.txt", file_data=b"1")
    a2 = File(file_type="txt", file_name="a2.txt", file_data=b"2")
    e1 = File(file_type="txt", file_name="e1.txt", file_data=b"1")
    pairs, unmatched = _pair_files([a1, a2], [e1])
    assert len(pairs) == 1
    assert unmatched == ["actual:a2.txt (no matching expected file)"]


def test_pair_files_flags_unmatched_extra_expected_file():
    a1 = File(file_type="txt", file_name="a1.txt", file_data=b"1")
    e1 = File(file_type="txt", file_name="e1.txt", file_data=b"1")
    e2 = File(file_type="txt", file_name="e2.txt", file_data=b"2")
    pairs, unmatched = _pair_files([a1], [e1, e2])
    assert len(pairs) == 1
    assert unmatched == ["expected:e2.txt (no matching actual file)"]


# --- size guard ------------------------------------------------------------------------------

def test_estimate_tokens_counts_text_and_native_blocks():
    blocks = [
        {"type": "text", "text": "x" * 400},
        {"type": "file", "file": {"file_data": "y" * 800, "filename": "f"}},
    ]
    assert _estimate_tokens(blocks) == (400 + 800) // 4


def test_degrade_truncates_text_but_never_native_blocks():
    native = {"type": "file", "file": {"file_data": "n" * 40, "filename": "f"}}
    text = {"type": "text", "text": "t" * 1000}
    degraded, reason = _degrade([native, text], max_content_tokens=20)  # 20*4=80 char budget
    assert degraded[0] == native  # native block untouched
    assert len(degraded[1]["text"]) < 1000
    assert "truncated" in reason


def test_degrade_reports_when_native_content_alone_exceeds_the_guard():
    native = {"type": "file", "file": {"file_data": "n" * 10_000, "filename": "f"}}
    degraded, reason = _degrade([native], max_content_tokens=10)
    assert degraded == [native]  # nothing to truncate
    assert "cannot truncate further" in reason


# --- make_file_similarity_judge: end-to-end with a stubbed judge call --------------------------

def _judge(conversation, expected_files, **kwargs):
    fn = make_file_similarity_judge(config=_CFG, **kwargs)
    return fn(
        data_point_id="p1", conversation=conversation,
        expected_output=ArtifactAgentOutput(files=expected_files))


def _turn_with_files(files: list[File]) -> ConversationTurn:
    return ConversationTurn(query=ArtifactAgentQuery(text="do the task"),
                            response=ArtifactAgentOutput(files=files))


def test_functionally_equivalent_but_differently_structured_decks_score_high():
    # 3-slide vs 4-slide deck conveying the same 3 bullet points -- the "structure differs, outcome
    # doesn't" case that motivated this level.
    actual = File(file_type="pptx", file_name="deck.pptx",
                  file_data=_pptx_bytes(["Intro", "Point A", "Point B", "Point C"]))
    expected = File(file_type="pptx", file_name="deck.pptx",
                    file_data=_pptx_bytes(["Point A", "Point B", "Point C"]))
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "same 3 points, extra intro slide", "score": 0.95}'):
        score, metadata = _judge([_turn_with_files([actual])], [expected])
    assert score == 0.95
    assert metadata["per_file"]["deck.pptx:ACTUAL"] == "text_extraction"


def test_genuinely_different_content_scores_low():
    actual = File(file_type="pptx", file_name="deck.pptx", file_data=_pptx_bytes(["Unrelated content"]))
    expected = File(file_type="pptx", file_name="deck.pptx",
                    file_data=_pptx_bytes(["Point A", "Point B", "Point C"]))
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "completely different content", "score": 0.05}'):
        score, _ = _judge([_turn_with_files([actual])], [expected])
    assert score == 0.05


def test_no_files_on_either_side_errors_without_calling_the_judge():
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, metadata = _judge([_turn_with_files([])], [])
    assert score == 0.0 and "error" in metadata
    mock_complete.assert_not_called()


def test_pdf_file_content_is_extracted_text_not_raw_bytes():
    data = _pdf_bytes(["ONLY THIS EXTRACTED TEXT SHOULD APPEAR"])
    actual = File(file_type="pdf", file_name="r.pdf", file_data=data)
    expected = File(file_type="pdf", file_name="r.pdf", file_data=data)
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "identical", "score": 1.0}') as mock_complete:
        _judge([_turn_with_files([actual])], [expected])
    (messages, _), _ = mock_complete.call_args
    content = messages[-1]["content"]
    text_blocks = [b["text"] for b in content if b["type"] == "text"]
    assert any("ONLY THIS EXTRACTED TEXT SHOULD APPEAR" in t for t in text_blocks)
    file_blocks = [b for b in content if b["type"] == "file"]
    assert file_blocks == []  # PDFs go through text extraction here by default, never native blocks


def test_unmatched_file_is_flagged_not_silently_dropped():
    actual = [File(file_type="txt", file_name="a.txt", file_data=b"a"),
              File(file_type="txt", file_name="b.txt", file_data=b"b")]
    expected = [File(file_type="txt", file_name="a.txt", file_data=b"a")]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}'):
        _, metadata = _judge([_turn_with_files(actual)], expected)
    assert metadata["unmatched_files"] == ["actual:b.txt (no matching expected file)"]


def test_oversized_content_triggers_the_guard_and_flags_degraded():
    huge_text = "lorem ipsum " * 100_000  # comfortably exceeds a small max_content_tokens
    actual = File(file_type="txt", file_name="big.txt", file_data=huge_text.encode())
    expected = File(file_type="txt", file_name="big.txt", file_data=b"short")
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.5}'):
        score, metadata = _judge([_turn_with_files([actual])], [expected], max_content_tokens=100)
    assert score == 0.5
    assert "degraded" in metadata


def test_default_max_content_tokens_is_generous_not_a_default_limiter():
    # A normal-sized deck must NOT trip the guard by default -- guard, not limit.
    actual = File(file_type="pptx", file_name="d.pptx", file_data=_pptx_bytes(["a", "b", "c"]))
    expected = File(file_type="pptx", file_name="d.pptx", file_data=_pptx_bytes(["a", "b", "c"]))
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}'):
        _, metadata = _judge([_turn_with_files([actual])], [expected])
    assert "degraded" not in metadata


def test_eval_rubric_reaches_the_prompt():
    actual = File(file_type="txt", file_name="x.txt", file_data=b"hi")
    expected = File(file_type="txt", file_name="x.txt", file_data=b"hi")
    fn = make_file_similarity_judge(config=_CFG)
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}') as mock_complete:
        fn(data_point_id="p1", conversation=[_turn_with_files([actual])],
           expected_output=ArtifactAgentOutput(files=[expected]), eval_rubric="must match exactly")
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("must match exactly" in t for t in text_blocks)


def test_custom_extractors_override_the_defaults_for_a_task_specific_format():
    # A task with a custom binary format (file_type "csv" here, standing in for anything not
    # covered by the built-in extractors) can override/add a handler without forking files.py.
    custom_calls = []

    def _custom_csv_extractor(data: bytes) -> str:
        custom_calls.append(data)
        return "CUSTOM EXTRACTED: " + data.decode()

    actual = File(file_type="csv", file_name="d.csv", file_data=b"a,b,c")
    expected = File(file_type="csv", file_name="d.csv", file_data=b"a,b,c")
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}') as mock_complete:
        _judge([_turn_with_files([actual])], [expected], extractors={"csv": _custom_csv_extractor})
    assert len(custom_calls) == 2  # actual + expected both routed through the override
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("CUSTOM EXTRACTED: a,b,c" in t for t in text_blocks)


# --- make_source_grounding_judge: reads INPUT source files, not gold, not response-vs-expected ----

def _grounding_turn(query_files: list[File], *, response_json: str | None = None,
                    response_text: str | None = None) -> ConversationTurn:
    return ConversationTurn(
        query=ArtifactAgentQuery(text="turn the sources into an output", files=query_files),
        response=ArtifactAgentOutput(json=response_json, text=response_text))


def _grounding(conversation, **kwargs):
    fn = make_source_grounding_judge(config=_CFG, **kwargs)
    return fn(data_point_id="p1", conversation=conversation)


def test_grounding_reads_input_source_file_content_not_just_existence():
    # The core fix: the CSV numbers the output must be grounded in reach the judge as real content.
    csv = File(file_type="csv", file_name="financials.csv",
               file_data=b"Total revenue,51000\nOperating income,260")
    conv = [_grounding_turn([csv], response_json='{"slides": [{"revenue": 51000}]}')]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "51000 matches source", "score": 0.9}') as mock_complete:
        score, metadata = _grounding(conv)
    assert score == 0.9
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("Total revenue,51000" in t for t in text_blocks)   # source content is present
    assert metadata["per_file"]["SOURCE:financials.csv"] == "text_extraction"


def test_grounding_excludes_by_name_but_keeps_other_source_files():
    # exclude_name_substrings is task-supplied (default excludes nothing). A task passing
    # ("template",) drops template.pptx (styling only) while KEEPING a non-template source pptx.
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes(["THEME ONLY"]))
    prev_deck = File(file_type="pptx", file_name="previous-deck_Q1.pptx",
                     file_data=_pptx_bytes(["Prior quarter revenue 39800"]))
    conv = [_grounding_turn([template, prev_deck], response_json='{"slides": []}')]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.8}') as mock_complete:
        _, metadata = _grounding(conv, exclude_name_substrings=("template",))
    assert metadata["excluded_files"] == ["template.pptx"]
    assert "SOURCE:previous-deck_Q1.pptx" in metadata["per_file"]
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("Prior quarter revenue 39800" in t for t in text_blocks)  # kept
    assert not any("THEME ONLY" in t for t in text_blocks)               # excluded


def test_grounding_defaults_to_excluding_nothing():
    # The generic helper is task-agnostic: with no exclusion configured, every query file is a source.
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes(["THEME"]))
    conv = [_grounding_turn([template], response_json='{"slides": []}')]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.6}') as mock_complete:
        score, metadata = _grounding(conv)
    assert score == 0.6
    assert "SOURCE:template.pptx" in metadata["per_file"]
    assert "excluded_files" not in metadata
    mock_complete.assert_called_once()


def test_grounding_judges_the_response_json_as_the_output():
    csv = File(file_type="csv", file_name="src.csv", file_data=b"a,b")
    conv = [_grounding_turn([csv], response_json='{"headline": "Generated headline"}')]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.7}') as mock_complete:
        _, metadata = _grounding(conv)
    assert metadata["per_file"]["OUTPUT"] == "response_json"
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("Generated headline" in t for t in text_blocks)


def test_grounding_falls_back_to_response_text_when_no_json():
    csv = File(file_type="csv", file_name="src.csv", file_data=b"a,b")
    conv = [_grounding_turn([csv], response_text="Plain text output grounded in a,b")]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.7}') as mock_complete:
        _, metadata = _grounding(conv)
    assert metadata["per_file"]["OUTPUT"] == "response_text"
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("Plain text output grounded" in t for t in text_blocks)


def test_grounding_returns_none_when_no_source_files_after_exclusion():
    # With every query file excluded by the task's convention, there are no facts to ground against.
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes(["theme"]))
    conv = [_grounding_turn([template], response_json='{"slides": []}')]
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, metadata = _grounding(conv, exclude_name_substrings=("template",))
    assert score is None and metadata["applicable"] is False
    assert metadata["excluded_files"] == ["template.pptx"]
    mock_complete.assert_not_called()


def test_grounding_returns_none_when_no_output_was_produced():
    csv = File(file_type="csv", file_name="src.csv", file_data=b"a,b")
    conv = [_grounding_turn([csv])]  # source present, but response has no json/text
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, metadata = _grounding(conv)
    assert score is None and metadata["applicable"] is False
    mock_complete.assert_not_called()


def test_grounding_oversized_source_triggers_the_guard_and_flags_degraded():
    huge = ("row,value\n" + "lorem,123\n" * 3_000).encode()
    csv = File(file_type="csv", file_name="big.csv", file_data=huge)
    conv = [_grounding_turn([csv], response_json='{"slides": []}')]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.5}'):
        score, metadata = _grounding(conv, max_content_tokens=100)
    assert score is None


# --- metric_rubric (metric definition, system prompt) vs. eval_rubric (per-sample note, user
# prompt) -- these are two unrelated channels, same split as make_transcript_judge's fixed
# prompt_template vs. its `eval_rubric` template variable. Neither can "replace" the other because
# they were never the same channel.

def test_grounding_rubric_is_baked_into_the_system_prompt():
    csv = File(file_type="csv", file_name="financials.csv", file_data=b"revenue,51000")
    conv = [_grounding_turn([csv], response_json='{"slides": []}')]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.5}') as mock_complete:
        _grounding(conv, metric_rubric="TASK METRIC DEFINITION")
    (messages, _), _ = mock_complete.call_args
    assert "TASK METRIC DEFINITION" in messages[0]["content"]  # system message, plain string
    assert messages[0]["role"] == "system"


def test_grounding_rubric_and_eval_rubric_are_independent_and_both_reach_the_prompt():
    # metric_rubric (the metric's own definition, construction-time) and eval_rubric (a per-sample
    # NL note, call-time) land in different messages and never interact -- both must be present.
    csv = File(file_type="csv", file_name="financials.csv", file_data=b"revenue,51000")
    conv = [_grounding_turn([csv], response_json='{"slides": []}')]
    fn = make_source_grounding_judge(config=_CFG, metric_rubric="TASK METRIC DEFINITION")
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.5}') as mock_complete:
        fn(data_point_id="p1", conversation=conv, eval_rubric="sample-specific note")
    (messages, _), _ = mock_complete.call_args
    assert "TASK METRIC DEFINITION" in messages[0]["content"]           # system message
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("sample-specific note" in t for t in text_blocks)        # user message


def test_grounding_eval_rubric_alone_works_with_no_rubric_configured():
    # A task that never sets `metric_rubric=` at all (no metric-level scoring bands authored) can
    # still rely on a per-sample eval_rubric reaching the prompt -- the two channels are independent.
    csv = File(file_type="csv", file_name="financials.csv", file_data=b"revenue,51000")
    conv = [_grounding_turn([csv], response_json='{"slides": []}')]
    fn = make_source_grounding_judge(config=_CFG)  # no metric_rubric= at construction time
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.5}') as mock_complete:
        fn(data_point_id="p1", conversation=conv, eval_rubric="call-time-only note")
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("call-time-only note" in t for t in text_blocks)


def test_grounding_no_rubric_block_when_no_eval_rubric_is_set():
    csv = File(file_type="csv", file_name="financials.csv", file_data=b"revenue,51000")
    conv = [_grounding_turn([csv], response_json='{"slides": []}')]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.5}') as mock_complete:
        _grounding(conv)
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert not any("Rubric:" in t for t in text_blocks)


# --- make_source_coverage_judge: reads the same INPUT source files as grounding, but asks the
# opposite question (proportion of source covered) and DEGRADES on overflow instead of bailing.

def _coverage(conversation, **kwargs):
    fn = make_source_coverage_judge(config=_CFG, **kwargs)
    return fn(data_point_id="p1", conversation=conversation)


def _coverage_turn(query_files: list[File], *, response_json: str | None = None,
                   response_text: str | None = None) -> ConversationTurn:
    """Like `_grounding_turn`, but with blank query text -- most of the coverage-overflow tests
    below are about the SOURCE-degradation math and are sized right at the survival floor; they
    don't want the (now budget-consuming) brief in the mix. Tests that specifically cover the brief
    behavior build their own `ArtifactAgentQuery(text=...)` instead of using this helper."""
    return ConversationTurn(
        query=ArtifactAgentQuery(text=None, files=query_files),
        response=ArtifactAgentOutput(json=response_json, text=response_text))


def test_coverage_reads_input_source_file_content():
    csv = File(file_type="csv", file_name="financials.csv",
               file_data=b"Total revenue,51000\nOperating income,260")
    conv = [_grounding_turn([csv], response_json='{"slides": [{"revenue": 51000}]}')]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "revenue covered", "score": 0.6}') as mock_complete:
        score, metadata = _coverage(conv)
    assert score == 0.6
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("Total revenue,51000" in t for t in text_blocks)
    assert metadata["per_file"]["SOURCE:financials.csv"] == "text_extraction"


def test_coverage_excludes_by_name_and_returns_none_when_no_source_remains():
    template = File(file_type="pptx", file_name="template.pptx", file_data=_pptx_bytes(["THEME"]))
    conv = [_grounding_turn([template], response_json='{"slides": []}')]
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, metadata = _coverage(conv, exclude_name_substrings=("template.pptx",))
    assert score is None and metadata["applicable"] is False
    assert metadata["excluded_files"] == ["template.pptx"]
    mock_complete.assert_not_called()


def test_coverage_degrades_and_scores_on_overflow_unlike_grounding():
    # The behavioral difference from grounding: an oversized source is TRUNCATED and still scored
    # (metadata["degraded"] records it), rather than hard-bailing to None. Sized so that MOST of the
    # source still survives the truncation (>= the _MIN_SOURCE_SURVIVAL floor): the output is tiny,
    # so nearly the whole budget goes to the source.
    source_text = ("row,value\n" + "lorem,123\n" * 20).encode()
    csv = File(file_type="csv", file_name="big.csv", file_data=source_text)
    conv = [_coverage_turn([csv], response_json='{"slides": []}')]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "partial", "score": 0.4}') as mock_complete:
        score, metadata = _coverage(conv, max_content_tokens=100)  # 400-char budget, tiny output
    assert score == 0.4
    assert "degraded" in metadata
    assert "source text retained" in metadata["degraded"]
    mock_complete.assert_called_once()


def test_coverage_bails_to_none_when_source_is_gutted_below_the_survival_floor():
    # Near-cap case: the source is far larger than the budget, so only a sliver survives truncation.
    # A coverage score against source the judge barely saw would misrepresent what was measured, so
    # bail to None (with the surviving fraction recorded) rather than report a misleading number.
    huge = ("row,value\n" + "lorem,123\n" * 3_000).encode()
    csv = File(file_type="csv", file_name="big.csv", file_data=huge)
    conv = [_coverage_turn([csv], response_json='{"slides": []}')]
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, metadata = _coverage(conv, max_content_tokens=100)
    assert score is None and metadata["applicable"] is False
    assert "source text survives" in metadata["reason"]
    assert metadata["source_survival"] < 0.5
    mock_complete.assert_not_called()


def test_coverage_bails_to_none_when_the_output_alone_exceeds_the_budget():
    # Degrade truncates SOURCE only, never the OUTPUT (truncating the deck would manufacture the
    # very omission coverage measures). When the OUTPUT alone can't fit, there's nothing safe to
    # degrade -> bail to None instead of scoring a truncated deck.
    small_source = File(file_type="csv", file_name="metrics.csv", file_data=b"revenue,51000")
    huge_output = '{"slides": ["' + "lorem ipsum " * 3_000 + '"]}'
    conv = [_coverage_turn([small_source], response_json=huge_output)]
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, metadata = _coverage(conv, max_content_tokens=100)
    assert score is None and metadata["applicable"] is False
    assert "output" in metadata["reason"] and "exceeds" in metadata["reason"]
    mock_complete.assert_not_called()


# --- coverage reads the brief (query.text) to scope what counts as material -----------------------
# content_coverage is the only source judge that sends the REQUEST: it needs the brief to tell a
# source fact the user scoped out apart from one that was silently dropped. groundedness does not
# get this (a fabricated figure is ungrounded regardless of what was asked for).

def test_coverage_sends_the_brief_as_a_labelled_request_block():
    csv = File(file_type="csv", file_name="financials.csv", file_data=b"revenue,51000\nheadcount,900")
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="Focus only on revenue; skip headcount entirely.", files=[csv]),
        response=ArtifactAgentOutput(json='{"slides": [{"revenue": 51000}]}'))]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "revenue covered, headcount out of scope", "score": 0.9}'
               ) as mock_complete:
        score, _ = _coverage(conv)
    assert score == 0.9
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("USER REQUEST" in t and "Focus only on revenue" in t for t in text_blocks)


def test_grounding_never_sends_the_brief():
    # Grounding's fabrication check doesn't depend on what was asked, so it must not get the
    # include_query_text treatment coverage gets from the same shared builder.
    csv = File(file_type="csv", file_name="financials.csv", file_data=b"revenue,51000")
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="A brief that must never reach the grounding judge.", files=[csv]),
        response=ArtifactAgentOutput(json='{"slides": []}'))]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.5}') as mock_complete:
        _grounding(conv)
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert not any("USER REQUEST" in t for t in text_blocks)
    assert not any("must never reach" in t for t in text_blocks)


def test_coverage_omits_the_request_block_when_query_text_is_blank():
    csv = File(file_type="csv", file_name="financials.csv", file_data=b"revenue,51000")
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="   ", files=[csv]),
        response=ArtifactAgentOutput(json='{"slides": []}'))]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.5}') as mock_complete:
        _coverage(conv)
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert not any("USER REQUEST" in t for t in text_blocks)


def test_coverage_brief_survives_intact_when_source_is_degraded_on_overflow():
    # The brief sits in the never-truncated tail alongside OUTPUT (see _make_source_judge), so a
    # huge source that gets proportionally truncated must never drag the brief down with it -- this
    # is the scenario the fix targets: a large source, scoped by a brief that must stay legible.
    huge_source = ("row,value\n" + "lorem,123\n" * 30).encode()
    csv = File(file_type="csv", file_name="big.csv", file_data=huge_source)
    brief = "Focus only on the summary row; ignore the rest of this large dataset."
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text=brief, files=[csv]),
        response=ArtifactAgentOutput(json='{"slides": []}'))]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "in-scope content covered", "score": 0.8}') as mock_complete:
        score, metadata = _coverage(conv, max_content_tokens=200)
    assert score == 0.8
    assert "degraded" in metadata
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any(brief in t for t in text_blocks)  # brief is verbatim, not truncated


def test_coverage_rubric_is_baked_into_the_system_prompt():
    csv = File(file_type="csv", file_name="financials.csv", file_data=b"revenue,51000")
    conv = [_grounding_turn([csv], response_json='{"slides": []}')]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.5}') as mock_complete:
        _coverage(conv, metric_rubric="COVERAGE METRIC DEFINITION")
    (messages, _), _ = mock_complete.call_args
    assert "COVERAGE METRIC DEFINITION" in messages[0]["content"]
    assert messages[0]["role"] == "system"


# --- make_visual_comparison_judge / make_visual_quality_judge: judge already-rendered documents
# (e.g. PDFs from helpers/render.py), not the raw source/output files above. Neither has the
# `eval_function` (*, data_point_id, conversation, ...) contract -- the caller renders and passes
# in the File(s) to compare/score.

def _pdf(name: str, data: bytes = b"%PDF-1.4 fake") -> File:
    return File(file_type="pdf", file_name=name, file_data=data)


def test_visual_comparison_sends_reference_and_actual_as_native_pdf_blocks():
    fn = make_visual_comparison_judge(system_prompt="compare visually", config=_CFG)
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "similar theme", "score": 0.8}') as mock_complete:
        score, _ = fn(_pdf("reference.pdf"), _pdf("actual.pdf"), None)
    assert score == 0.8
    (messages, _), _ = mock_complete.call_args
    content = messages[-1]["content"]
    text_blocks = [b["text"] for b in content if b["type"] == "text"]
    assert any("REFERENCE" in t for t in text_blocks)
    assert any("ACTUAL" in t for t in text_blocks)
    file_blocks = [b for b in content if b["type"] == "file"]
    assert len(file_blocks) == 2


def test_visual_comparison_eval_rubric_reaches_the_prompt():
    fn = make_visual_comparison_judge(system_prompt="compare visually", config=_CFG)
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 0.5}') as mock_complete:
        fn(_pdf("reference.pdf"), _pdf("actual.pdf"), "must match the brand colors")
    (messages, _), _ = mock_complete.call_args
    text_blocks = [b["text"] for b in messages[-1]["content"] if b["type"] == "text"]
    assert any("must match the brand colors" in t for t in text_blocks)


def test_visual_comparison_bails_to_none_when_model_lacks_pdf_support():
    fn = make_visual_comparison_judge(
        system_prompt="compare visually",
        config=ModelConfig(model="not-a-real-model-xyz", api_key="no-token"))
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = fn(_pdf("reference.pdf"), _pdf("actual.pdf"), None)
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


def test_visual_comparison_bails_to_none_on_overflow_rather_than_degrading():
    huge = _pdf("reference.pdf", data=b"%PDF-1.4 " + b"x" * 10_000)
    fn = make_visual_comparison_judge(system_prompt="compare visually", config=_CFG, max_content_tokens=10)
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = fn(huge, _pdf("actual.pdf"), None)
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


def test_visual_quality_sends_one_rendered_document_as_a_native_pdf_block():
    fn = make_visual_quality_judge(system_prompt="judge visual quality", config=_CFG)
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "clean design", "score": 0.7}') as mock_complete:
        score, _ = fn(_pdf("rendered.pdf"), None)
    assert score == 0.7
    (messages, _), _ = mock_complete.call_args
    file_blocks = [b for b in messages[-1]["content"] if b["type"] == "file"]
    assert len(file_blocks) == 1


def test_visual_quality_bails_to_none_when_model_lacks_pdf_support():
    fn = make_visual_quality_judge(
        system_prompt="judge visual quality",
        config=ModelConfig(model="not-a-real-model-xyz", api_key="no-token"))
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = fn(_pdf("rendered.pdf"), None)
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()
