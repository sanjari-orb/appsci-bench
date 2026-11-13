"""Unit tests for the transcript judge. No network -- `complete()`
is mocked at the LLM (Level 0) import site, mirroring test_llm.py's pattern.
"""
from __future__ import annotations

from unittest.mock import patch

from appsci_bench.common.model_config import ModelConfig
from appsci_bench.eval.datatypes import (
    ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, File, extract_json_object,
)
from appsci_bench.eval.helpers.transcript import make_transcript_judge, render_conversation_for_judge

_CFG = ModelConfig(model="anthropic/claude-opus-4-8", api_key="no-token")

_SIMPLE_TEMPLATE = """\
Rubric: {{ eval_rubric }}
Expected text: {{ expected_text }}
Expected json: {{ expected_json }}
Transcript:
{{ transcript }}
"""
# Deliberately multiline: every test below exercises `_load_template`'s path-vs-literal-source
# detection with this string, which doubles as regression coverage for a real bug caught during
# development -- Path.is_file() on a long/multiline string raised OSError ("File name too long")
# rather than returning False on this platform.


def _turn(query_text: str, response_text: str, *, files: list[File] | None = None) -> ConversationTurn:
    return ConversationTurn(
        query=ArtifactAgentQuery(text=query_text),
        response=ArtifactAgentOutput(text=response_text, files=files or []),
    )


# --- render_conversation_for_judge -----------------------------------------------------------------

def test_render_includes_text_and_json():
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="what's the status?", json='{"case_id": "C1"}'),
        response=ArtifactAgentOutput(text="it's resolved", json='{"status": "closed"}'),
    )]
    rendered = render_conversation_for_judge(conv)
    assert "what's the status?" in rendered
    assert '{"case_id": "C1"}' in rendered
    assert "it's resolved" in rendered
    assert '{"status": "closed"}' in rendered


def test_render_shows_a_payload_once_when_both_slots_hold_the_same_object():
    # How the completions transport fills an output: one channel back, so `text` is the whole reply
    # and `json` is the same bytes (see HttpArtifactClient / extract_json_object). Rendering both
    # slots verbatim would put the ENTIRE reply in the judge's prompt twice -- two copies to weigh
    # where the artifact said one thing, and a prompt no longer comparable to pre-recovery runs.
    body = '{"flag": true, "fault_type": "DUPLICATE", "reason": "duplicate of INV-3350"}'
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="review it"),
        response=ArtifactAgentOutput(text=body, json=extract_json_object(body)),
    )]
    rendered = render_conversation_for_judge(conv)
    assert rendered.count(body) == 1              # the payload survives...
    assert "json: " not in rendered               # ...but not as a second labelled copy


def test_render_drops_a_structured_slot_already_embedded_in_the_text():
    # Containment, not equality: a transport that narrates AND fills the slot separately still owes
    # the judge only one copy of the object.
    payload = '{"status": "closed"}'
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="what's the status?"),
        response=ArtifactAgentOutput(text=f"resolved -- {payload} -- let me know", json=payload),
    )]
    rendered = render_conversation_for_judge(conv)
    assert rendered.count(payload) == 1


def test_render_keeps_a_structured_slot_the_text_does_not_carry():
    # The de-duplication must not swallow a real second channel: when the slots disagree, the
    # structured half is information the prose does not already give the judge.
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="review it"),
        response=ArtifactAgentOutput(text="I flagged it as a duplicate.", json='{"flag": true}'),
    )]
    rendered = render_conversation_for_judge(conv)
    assert "I flagged it as a duplicate." in rendered
    assert '{"flag": true}' in rendered


def test_render_shows_a_structured_slot_with_no_text_beside_it():
    conv = [ConversationTurn(
        query=ArtifactAgentQuery(text="review it"),
        response=ArtifactAgentOutput(json='{"flag": false}'),
    )]
    rendered = render_conversation_for_judge(conv)
    assert '{"flag": false}' in rendered


def test_render_never_includes_file_data_but_notes_file_existence():
    big_secret_bytes = b"%PDF-1.4 SOME BINARY CONTENT THAT MUST NEVER LEAK"
    conv = [_turn("generate a report", "here you go",
                  files=[File(file_type="pdf", file_name="q3_report.pdf", file_data=big_secret_bytes)])]
    rendered = render_conversation_for_judge(conv)
    assert big_secret_bytes.decode() not in rendered
    assert "q3_report.pdf" in rendered and "pdf" in rendered


def test_render_multiple_turns_in_order():
    conv = [_turn("first question", "first answer"), _turn("second question", "second answer")]
    rendered = render_conversation_for_judge(conv)
    assert rendered.index("first question") < rendered.index("second question")
    assert "Turn 1" in rendered and "Turn 2" in rendered


def test_render_empty_turn_does_not_crash():
    conv = [ConversationTurn(query=ArtifactAgentQuery(), response=ArtifactAgentOutput())]
    rendered = render_conversation_for_judge(conv)
    assert "(empty)" in rendered


# --- make_transcript_judge ---------------------------------------------------------------------

def test_eval_function_matches_the_keyword_only_contract_and_scores():
    fn = make_transcript_judge(_SIMPLE_TEMPLATE, config=_CFG)
    conv = [_turn("can you help", "yes, done")]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "good", "score": 0.8}'):
        score, metadata = fn(
            data_point_id="p1", conversation=conv,
            expected_output=ArtifactAgentOutput(text="done"), eval_rubric="be helpful")
    assert score == 0.8
    assert metadata == {"reasoning": "good"}


def test_prompt_includes_rubric_and_expected_output_and_transcript():
    fn = make_transcript_judge(_SIMPLE_TEMPLATE, config=_CFG)
    conv = [_turn("can you help", "yes, done")]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}') as mock_complete:
        fn(data_point_id="p1", conversation=conv,
           expected_output=ArtifactAgentOutput(text="the expected thing", json='{"k": "v"}'),
           eval_rubric="the rubric text")
    (messages, _), _ = mock_complete.call_args
    user_prompt = messages[-1]["content"]
    assert "the rubric text" in user_prompt
    assert "the expected thing" in user_prompt
    assert '{"k": "v"}' in user_prompt
    assert "can you help" in user_prompt and "yes, done" in user_prompt


def test_file_attached_to_a_turn_never_reaches_the_prompt():
    fn = make_transcript_judge(_SIMPLE_TEMPLATE, config=_CFG)
    secret = b"BINARY_FILE_BYTES_MUST_NOT_LEAK"
    conv = [_turn("make a deck", "here it is",
                  files=[File(file_type="pptx", file_name="deck.pptx", file_data=secret)])]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}') as mock_complete:
        fn(data_point_id="p1", conversation=conv)
    (messages, _), _ = mock_complete.call_args
    user_prompt = messages[-1]["content"]
    assert secret.decode() not in user_prompt
    assert "deck.pptx" in user_prompt


def test_missing_expected_output_and_rubric_render_as_empty_not_crash():
    fn = make_transcript_judge(_SIMPLE_TEMPLATE, config=_CFG)
    conv = [_turn("hi", "hello")]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}'):
        score, _ = fn(data_point_id="p1", conversation=conv, expected_output=None, eval_rubric=None)
    assert score == 1.0


def test_custom_output_schema_extra_fields_land_in_metadata():
    from pydantic import BaseModel

    class _Verdict(BaseModel):
        model_config = {"extra": "forbid"}
        reasoning: str
        score: float
        strengths: list[str]

    fn = make_transcript_judge(_SIMPLE_TEMPLATE, output_schema=_Verdict, config=_CFG)
    conv = [_turn("hi", "hello")]
    raw = '{"reasoning": "solid", "score": 0.6, "strengths": ["clear", "concise"]}'
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=raw):
        score, metadata = fn(data_point_id="p1", conversation=conv)
    assert score == 0.6
    assert metadata == {"reasoning": "solid", "strengths": ["clear", "concise"]}


def test_prompt_template_accepts_a_filesystem_path(tmp_path):
    template_file = tmp_path / "rubric.jinja2"
    template_file.write_text("Rubric-from-file: {{ eval_rubric }}\nTranscript:\n{{ transcript }}")
    fn = make_transcript_judge(str(template_file), config=_CFG)
    conv = [_turn("hi", "hello")]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}') as mock_complete:
        fn(data_point_id="p1", conversation=conv, eval_rubric="loaded from disk")
    (messages, _), _ = mock_complete.call_args
    assert "Rubric-from-file: loaded from disk" in messages[-1]["content"]


def test_short_single_line_template_that_is_not_a_real_path_is_treated_as_literal_source():
    # Path-shaped (single line, short) but doesn't exist on disk -- must fall through to being
    # rendered as the literal template text, not raise or silently produce an empty prompt.
    fn = make_transcript_judge("Rubric: {{ eval_rubric }} | {{ transcript }}", config=_CFG)
    conv = [_turn("hi", "hello")]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}') as mock_complete:
        fn(data_point_id="p1", conversation=conv, eval_rubric="be nice")
    (messages, _), _ = mock_complete.call_args
    assert "Rubric: be nice |" in messages[-1]["content"]


# --- prebuilt judges/builtin.py -----------------------------------------------------------------

def test_builtin_judges_are_ready_eval_functions():
    from appsci_bench.eval.helpers.judges import (
        conversation_quality_judge, process_compliance_judge, task_resolution_judge,
    )
    conv = [_turn("can you cancel my flight", "I've cancelled reservation ABC123")]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "fine", "score": 0.9}'):
        for judge in (conversation_quality_judge, task_resolution_judge, process_compliance_judge):
            score, metadata = judge(data_point_id="p1", conversation=conv, eval_rubric="be correct")
            assert score == 0.9 and metadata["reasoning"] == "fine"


def test_conversation_quality_judge_prompt_covers_communication_not_outcome():
    from appsci_bench.eval.helpers.judges import conversation_quality_judge
    conv = [_turn("can you cancel my flight", "I've cancelled reservation ABC123")]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "fine", "score": 0.9}') as mock_complete:
        conversation_quality_judge(data_point_id="p1", conversation=conv, eval_rubric="be correct")
    user_prompt = mock_complete.call_args[0][0][-1]["content"]
    assert "QUALITY of an AI agent's conversation" in user_prompt
    assert "be correct" in user_prompt
    assert "cancelled reservation ABC123" in user_prompt


def test_task_resolution_judge_prompt_includes_expected_outcome_fields():
    from appsci_bench.eval.helpers.judges import task_resolution_judge
    conv = [_turn("can you cancel my flight", "I've cancelled reservation ABC123")]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "fine", "score": 0.9}') as mock_complete:
        task_resolution_judge(
            data_point_id="p1", conversation=conv, eval_rubric="be correct",
            expected_output=ArtifactAgentOutput(text="reservation cancelled", json='{"status": "cancelled"}'))
    user_prompt = mock_complete.call_args[0][0][-1]["content"]
    assert "actually RESOLVED the user's" in user_prompt
    assert "reservation cancelled" in user_prompt
    assert '{"status": "cancelled"}' in user_prompt


def test_process_compliance_judge_prompt_frames_rubric_as_required_process():
    from appsci_bench.eval.helpers.judges import process_compliance_judge
    conv = [_turn("can you cancel my flight", "I've cancelled reservation ABC123")]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "fine", "score": 0.9}') as mock_complete:
        process_compliance_judge(
            data_point_id="p1", conversation=conv,
            eval_rubric="verify identity before cancelling")
    user_prompt = mock_complete.call_args[0][0][-1]["content"]
    assert "FOLLOWED THE REQUIRED PROCESS" in user_prompt
    assert "Required Process / Policy" in user_prompt
    assert "verify identity before cancelling" in user_prompt
