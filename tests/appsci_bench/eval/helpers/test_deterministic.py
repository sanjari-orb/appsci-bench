"""Unit tests for the deterministic comparison helpers."""
from __future__ import annotations

import json

import pytest

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn
from appsci_bench.eval.helpers import (
    json_field_match, make_json_field_eval_function, parse_bool, resolve_scalar, text_similarity,
)


# --- resolve_scalar ------------------------------------------------------------------------------

@pytest.mark.parametrize("predicted,expected", [
    ("$16,000", 16000),
    ("16 units", "16.0"),
    (16, 16.0000001),
    ("-5.5kg", -5.5),
    ("50%", 50),
])
def test_resolve_scalar_numeric_with_units(predicted, expected):
    matched, how = resolve_scalar(predicted, expected)
    assert matched is True and how == "numeric"


def test_resolve_scalar_numeric_out_of_tolerance():
    matched, how = resolve_scalar(10, 10.5)
    assert matched is False and how == "numeric"


@pytest.mark.parametrize("predicted,expected", [
    ("flagged", True),
    ("yes", "flag"),
    ("FALSE", "no"),
    (1, "yes"),
    (True, "1"),
])
def test_resolve_scalar_boolean_aliases(predicted, expected):
    matched, how = resolve_scalar(predicted, expected)
    assert matched is True and how == "boolean"


def test_resolve_scalar_boolean_mismatch():
    matched, how = resolve_scalar("yes", "no")
    assert matched is False and how == "boolean"


# --- parse_bool ------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False),
    ("true", True), ("FALSE", False), ("Yes", True), ("no", False),
    ("flag", True), ("flagged", True), ("unflagged", False),
    (1, True), (0, False),
])
def test_parse_bool_resolves_known_aliases(value, expected):
    assert parse_bool(value) is expected


def test_parse_bool_naive_bool_would_misread_a_stringly_typed_false():
    # The exact bug this helper exists to prevent: bool("False") is True in Python.
    assert bool("False") is True
    assert parse_bool("False") is False


@pytest.mark.parametrize("value", [2, "", "maybe", None, [], {}])
def test_parse_bool_returns_none_for_unresolvable_values(value):
    assert parse_bool(value) is None


@pytest.mark.parametrize("predicted,expected", [
    ("  Qty Mismatch  ", "qty mismatch"),
    ("DUPLICATE", "duplicate"),
])
def test_resolve_scalar_normalized_string(predicted, expected):
    matched, how = resolve_scalar(predicted, expected)
    assert matched is True and how == "normalized_string"


def test_resolve_scalar_case_sensitive_opt_out():
    matched, how = resolve_scalar("Duplicate", "duplicate", case_sensitive=True)
    assert matched is False and how == "normalized_string"


@pytest.mark.parametrize("predicted,expected", [
    ({"a": 1}, "hello"),
    ("qty mismatch", 42),
    (None, "yes"),
])
def test_resolve_scalar_incomparable_fallback(predicted, expected):
    matched, how = resolve_scalar(predicted, expected)
    assert matched is False and how == "incomparable"


# --- text_similarity -----------------------------------------------------------------------------

def test_text_similarity_token_set_ratio_is_order_insensitive():
    a = "qty mismatch, invoiced 226 vs PO 210"
    b = "PO qty 210, invoiced 226 -- quantity mismatch"
    # token_set_ratio only cares about the shared token set, so reordered phrasing still scores high.
    assert text_similarity(a, b, method="token_set_ratio") > 0.5


def test_text_similarity_sequence_ratio_is_order_sensitive():
    a = "the quick brown fox"
    b = "fox brown quick the"
    seq = text_similarity(a, b, method="sequence_ratio")
    tok = text_similarity(a, b, method="token_set_ratio")
    assert tok == 1.0          # identical token sets
    assert seq < tok           # but the sequence differs, so sequence_ratio scores lower


def test_text_similarity_identical_strings_score_one():
    assert text_similarity("same text", "same text") == 1.0
    assert text_similarity("", "") == 1.0


def test_text_similarity_disjoint_strings_score_zero():
    assert text_similarity("abc", "xyz") == 0.0


def test_text_similarity_unknown_method_raises():
    with pytest.raises(ValueError):
        text_similarity("a", "b", method="nonsense")


# --- json_field_match ------------------------------------------------------------------------------

def test_json_field_match_auto_detected_rules():
    predicted = {"flagged": "yes", "fault_type": "qty_mismatch", "amount": "$100.00"}
    expected = {"flagged": True, "fault_type": "QTY_MISMATCH", "amount": 100}
    score, meta = json_field_match(predicted, expected)
    assert score == 1.0
    assert meta["per_field"]["flagged"]["how"] == "boolean"
    assert meta["per_field"]["fault_type"]["how"] == "normalized_string"
    assert meta["per_field"]["amount"]["how"] == "numeric"


def test_json_field_match_string_field_falls_back_to_text_similarity():
    # Same bag of tokens, reordered -- fails the exact normalized-string compare but is a perfect
    # token_set_ratio match, so the auto-detected fallback should still score this field 1.0.
    predicted = {"reason": "PO qty 210, invoiced 226 -- mismatch"}
    expected = {"reason": "mismatch, invoiced 226 vs PO qty 210"}
    score, meta = json_field_match(predicted, expected)
    assert score == 1.0
    assert meta["per_field"]["reason"]["how"].startswith("text_similarity_fallback")


def test_json_field_match_explicit_scalar_rule_skips_text_fallback():
    predicted = {"reason": "totally different wording"}
    expected = {"reason": "some other wording"}
    score, meta = json_field_match(predicted, expected, field_rules={"reason": "scalar"})
    assert score == 0.0
    # Explicit "scalar" stays on resolve_scalar's own normalized_string path (mismatched, but that
    # IS how two comparable strings resolve -- "incomparable" is reserved for genuinely different
    # types, see test_resolve_scalar_incomparable_fallback) and skips the text_similarity fallback.
    assert meta["per_field"]["reason"]["how"] == "normalized_string"


def test_json_field_match_explicit_text_rule():
    predicted = {"reason": "qty mismatch invoiced 226 vs PO 210"}
    expected = {"reason": "PO 210 invoiced 226 vs qty mismatch"}
    score, meta = json_field_match(predicted, expected, field_rules={"reason": "text"})
    assert score == 1.0
    assert meta["per_field"]["reason"]["how"].startswith("text_similarity=")


def test_json_field_match_set_rule_order_insensitive():
    predicted = {"tags": ["b", "a", "c"]}
    expected = {"tags": ["A", "B", "C"]}
    score, meta = json_field_match(predicted, expected)
    assert score == 1.0
    assert meta["per_field"]["tags"]["how"] == "set"


def test_json_field_match_set_rule_mismatched_length_does_not_match():
    predicted = {"tags": ["a", "b"]}
    expected = {"tags": ["a", "b", "c"]}
    score, meta = json_field_match(predicted, expected)
    assert score == 0.0
    assert meta["per_field"]["tags"] == {"matched": False, "how": "set"}


def test_json_field_match_missing_field_scores_zero_without_raising():
    predicted = {"flagged": True}
    expected = {"flagged": True, "fault_type": "DUPLICATE"}
    score, meta = json_field_match(predicted, expected)
    assert score == 0.5
    assert meta["per_field"]["fault_type"] == {"matched": False, "how": "missing_in_predicted"}


def test_json_field_match_required_fields_subset():
    predicted = {"flagged": True, "extra_field": "ignored"}
    expected = {"flagged": True, "fault_type": "DUPLICATE"}
    score, meta = json_field_match(predicted, expected, required_fields=["flagged"])
    assert score == 1.0
    assert "fault_type" not in meta["per_field"]
    assert "extra_field" not in meta["per_field"]


def test_json_field_match_no_fields_scores_full():
    score, meta = json_field_match({}, {})
    assert score == 1.0 and meta["per_field"] == {}


# --- make_json_field_eval_function -----------------------------------------------------------------

def _turn(response_json: dict | None) -> ConversationTurn:
    return ConversationTurn(
        query=ArtifactAgentQuery(text="review this invoice"),
        response=ArtifactAgentOutput(json=json.dumps(response_json) if response_json is not None else None),
    )


def test_make_json_field_eval_function_scores_match():
    fn = make_json_field_eval_function("flagged")
    conversation = [_turn({"flagged": "yes"})]
    expected_output = ArtifactAgentOutput(json=json.dumps({"flagged": True}))
    score, metadata = fn(data_point_id="p1", conversation=conversation, expected_output=expected_output)
    assert score == 1.0
    assert metadata["per_field"]["flagged"]["matched"] is True


def test_make_json_field_eval_function_respects_rule_override():
    fn = make_json_field_eval_function("reason", rule="text")
    conversation = [_turn({"reason": "qty mismatch invoiced 226 vs PO 210"})]
    expected_output = ArtifactAgentOutput(json=json.dumps({"reason": "PO 210 invoiced 226 vs qty mismatch"}))
    score, metadata = fn(data_point_id="p1", conversation=conversation, expected_output=expected_output)
    assert score == 1.0
    assert metadata["per_field"]["reason"]["how"].startswith("text_similarity=")


def test_make_json_field_eval_function_missing_expected_output():
    fn = make_json_field_eval_function("flagged")
    score, metadata = fn(data_point_id="p1", conversation=[_turn({"flagged": True})], expected_output=None)
    assert score == 0.0 and "error" in metadata


def test_make_json_field_eval_function_missing_response_json():
    fn = make_json_field_eval_function("flagged")
    expected_output = ArtifactAgentOutput(json=json.dumps({"flagged": True}))
    score, metadata = fn(data_point_id="p1", conversation=[_turn(None)], expected_output=expected_output)
    assert score == 0.0 and "error" in metadata


# --- regression: reproduce order_to_cash's old check_flag_accuracy / check_flag_fnr behavior --------
# The old evaluators/deterministic/check_flag_accuracy.py + check_flag_fnr.py hand-rolled the exact
# boolean-sniffing resolve_scalar now does generically.
# These reproduce their behavior on the same kind of sample inputs, without the old batch-mode shape.

_OLD_SAMPLES = [
    # (data_point_id, predicted_flag_value, oracle_flagged)
    ("p2p-0003", "flagged", True),        # old code's string-sniffing path
    ("p2p-0004", True, True),
    ("p2p-0005", False, False),
    ("p2p-0006", "no", True),             # false negative
]


def test_regression_flag_accuracy_matches_old_boolean_sniffing():
    correct = sum(
        1 for _, predicted, expected in _OLD_SAMPLES if resolve_scalar(predicted, expected)[0])
    accuracy = correct / len(_OLD_SAMPLES)
    assert accuracy == 3 / 4  # p2p-0006 is the one mismatch, same as the old check would find


def test_regression_flag_fnr_matches_old_logic():
    positives = [(p, e) for _, p, e in _OLD_SAMPLES if e]
    false_negatives = sum(1 for p, _ in positives if not resolve_scalar(p, True)[0])
    fnr = false_negatives / len(positives)
    score = 1.0 - fnr
    assert score == pytest.approx(2 / 3)  # 1 false negative (p2p-0006) out of 3 positives
