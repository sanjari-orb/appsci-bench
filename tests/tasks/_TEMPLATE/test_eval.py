"""Unit tests for tasks/_TEMPLATE/eval.py's two sample eval functions.

`_TEMPLATE` is copy-paste starter code, not a real task -- it's excluded from
`all_task_ids()`/the generic cross-task contract test on purpose (its spec.yaml's
`eval_function_path`s are literal placeholders). These tests cover the two functions directly so
the template's own glue code (not just the helpers it calls) is exercised somewhere.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn
from tasks._TEMPLATE import eval as template_eval


def _turn(response_json: dict | None) -> ConversationTurn:
    """One exchange. `None` means the artifact returned no structured payload -- the shape the
    transport produces for a reply that isn't a bare JSON object."""
    return ConversationTurn(
        query=ArtifactAgentQuery(text="do the thing"),
        response=ArtifactAgentOutput(
            json=json.dumps(response_json) if response_json is not None else None))


def _expected(**fields) -> ArtifactAgentOutput:
    return ArtifactAgentOutput(json=json.dumps(fields))


# --- field_match -----------------------------------------------------------------------------

def test_field_match_scores_one_when_all_fields_match():
    score, meta = template_eval.field_match(
        data_point_id="p1", conversation=[_turn({"field_1": "yes", "field_2": 42})],
        expected_output=_expected(field_1="yes", field_2=42))
    assert score == 1.0
    assert meta["per_field"]["field_1"]["matched"] and meta["per_field"]["field_2"]["matched"]


def test_field_match_scores_partial_when_one_field_mismatches():
    score, meta = template_eval.field_match(
        data_point_id="p1", conversation=[_turn({"field_1": "no", "field_2": 42})],
        expected_output=_expected(field_1="yes", field_2=42))
    assert score == 0.5
    assert not meta["per_field"]["field_1"]["matched"]
    assert meta["per_field"]["field_2"]["matched"]


def test_field_match_handles_missing_response_json():
    score, _ = template_eval.field_match(
        data_point_id="p1", conversation=[], expected_output=_expected(field_1="yes"))
    assert score == 0.0


def test_field_match_excludes_a_sample_with_no_expected_output():
    # A missing oracle is a DATASET gap, not an artifact failure: exclude the sample (None) with the
    # reason recorded. It must never score, and in particular must never score 1.0 -- which is what
    # this did before, because an absent oracle collapsed to `{}` and `json_field_match` reads zero
    # fields to compare as vacuously satisfied. That is the right answer for a pure comparison and the
    # wrong one for a scorer, so the guard belongs here, in the caller.
    score, meta = template_eval.field_match(
        data_point_id="p1", conversation=[_turn({"field_1": "yes"})], expected_output=None)
    assert score is None
    assert "no expected_output" in meta["error"]


def test_field_match_fails_loudly_when_the_artifact_returned_no_json_object():
    # The mirror guard. An unparseable reply used to collapse to `{}` and be compared field by field,
    # so a reply that said NOTHING was read as a reply that said "absent" for every field -- which
    # against the right oracle matches. Score 0 with the reason instead.
    score, meta = template_eval.field_match(
        data_point_id="p1", conversation=[_turn(None)],
        expected_output=ArtifactAgentOutput(json=json.dumps({"field_1": "yes"})))
    assert score == 0.0
    assert "no JSON object" in meta["error"]


# --- response_quality --------------------------------------------------------------------------

def test_response_quality_matches_the_keyword_only_contract_and_scores():
    conv = [_turn({"field_1": "yes"})]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "meets the rubric", "score": 0.9}'):
        score, meta = template_eval.response_quality(
            data_point_id="p1", conversation=conv, eval_rubric="respond helpfully")
    assert score == 0.9
    assert meta == {"reasoning": "meets the rubric"}


def test_response_quality_prompt_includes_rubric_and_transcript():
    conv = [_turn({"field_1": "yes"})]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}') as mock_complete:
        template_eval.response_quality(
            data_point_id="p1", conversation=conv, eval_rubric="the specific rubric text")
    user_prompt = mock_complete.call_args[0][0][-1]["content"]
    assert "the specific rubric text" in user_prompt
    assert "do the thing" in user_prompt
