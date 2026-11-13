"""Unit tests for tasks/order_to_cash/eval.py.

Each test reproduces a true/false case from the old batch checks' logic against the new
per-data-point contract. No network -- reason_quality (the one LLM-backed metric left) mocks
`complete()` at the LLM (Level 0) import site, mirroring appsci_bench/eval/helpers's own test
convention. fault_classification needs no mock: it compares the fault type the artifact declared,
with no judge involved.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn
from tasks.order_to_cash import eval as o2c_eval


def _turn(response_json: dict) -> ConversationTurn:
    return ConversationTurn(query=ArtifactAgentQuery(text="review this invoice"),
                            response=ArtifactAgentOutput(json=json.dumps(response_json)))


def _prose_turn(text: str) -> ConversationTurn:
    """A reply that did NOT meet the response contract: readable text, no structured payload.

    This is what the transport produces for anything that isn't a bare JSON object (see
    `appsci_bench.eval.datatypes.extract_json_object`) -- the `json` slot is left empty rather than
    guessing which object in a narrated reply was the answer."""
    return ConversationTurn(query=ArtifactAgentQuery(text="review this invoice"),
                            response=ArtifactAgentOutput(text=text, json=None))


def _expected(**fields) -> ArtifactAgentOutput:
    return ArtifactAgentOutput(json=json.dumps(fields))


# --- flag_accuracy --------------------------------------------------------------------------

def test_flag_accuracy_scores_one_when_flag_matches_oracle():
    score, meta = o2c_eval.flag_accuracy(
        data_point_id="p1", conversation=[_turn({"flagged": True})],
        expected_output=_expected(flagged=True))
    assert score == 1.0 and meta["predicted"] is True and meta["expected"] is True


def test_flag_accuracy_scores_zero_when_flag_mismatches_oracle():
    score, _ = o2c_eval.flag_accuracy(
        data_point_id="p1", conversation=[_turn({"flagged": False})],
        expected_output=_expected(flagged=True))
    assert score == 0.0


def test_flag_accuracy_correctly_agrees_on_a_clean_invoice():
    score, _ = o2c_eval.flag_accuracy(
        data_point_id="p1", conversation=[_turn({"flagged": False})],
        expected_output=_expected(flagged=False))
    assert score == 1.0


def test_flag_accuracy_does_not_misread_a_stringly_typed_false_as_flagged():
    # Regression: a naive bool(value) is True for ANY non-empty string, including "False" --
    # an artifact that emits {"flagged": "False"} (a string, not a real JSON boolean) must still
    # be read as NOT flagged, matching an oracle that also says not flagged.
    score, meta = o2c_eval.flag_accuracy(
        data_point_id="p1", conversation=[_turn({"flagged": "False"})],
        expected_output=_expected(flagged=False))
    assert score == 1.0 and meta["predicted"] is False


# --- flag_recall --------------------------------------------------------------------------------

def test_flag_recall_scores_one_when_a_real_fault_is_caught():
    score, meta = o2c_eval.flag_recall(
        data_point_id="p1", conversation=[_turn({"flagged": True})],
        expected_output=_expected(flagged=True))
    assert score == 1.0 and meta["applicable"] is True


def test_flag_recall_scores_zero_on_a_false_negative():
    score, meta = o2c_eval.flag_recall(
        data_point_id="p1", conversation=[_turn({"flagged": False})],
        expected_output=_expected(flagged=True))
    assert score == 0.0 and meta["applicable"] is True


def test_flag_recall_is_none_when_not_applicable():
    # oracle says this invoice is clean -- flag_recall (missed-fault rate) doesn't apply here;
    # flag_accuracy is the objective that penalizes a wrong decision on this sample instead.
    score, meta = o2c_eval.flag_recall(
        data_point_id="p1", conversation=[_turn({"flagged": True})],  # even a false positive
        expected_output=_expected(flagged=False))
    assert score is None and meta["applicable"] is False


def test_flag_recall_reads_a_stringly_typed_oracle_flag_correctly():
    # Regression: the oracle-side {"flagged": "false"} must gate flag_recall to "not applicable",
    # not be misread as truthy and treated as a sample that should have been flagged.
    score, meta = o2c_eval.flag_recall(
        data_point_id="p1", conversation=[_turn({"flagged": True})],
        expected_output=_expected(flagged="false"))
    assert score is None and meta["applicable"] is False


# --- fault_classification --------------------------------------------------------------------

def test_fault_classification_not_applicable_when_oracle_says_clean():
    score, meta = o2c_eval.fault_classification(
        data_point_id="p1", conversation=[_turn({"flagged": False})],
        expected_output=_expected(flagged=False))
    assert score is None and meta["applicable"] is False


def test_fault_classification_not_applicable_on_a_missed_positive():
    # a false negative -- flag_recall already penalizes this; fault_classification doesn't pile on.
    # Contrast with the no-payload case below, which must NOT be excluded this way: that artifact
    # claimed nothing, this one answered in the contract and said "clean".
    score, meta = o2c_eval.fault_classification(
        data_point_id="p1", conversation=[_turn({"flagged": False})],
        expected_output=_expected(flagged=True, fault_type="PRICE_DELTA"))
    assert score is None and meta["applicable"] is False


def test_fault_classification_scores_zero_when_no_fault_type_is_declared():
    # A flagged invoice with no fault_type at all: the artifact skipped a field the response
    # contract requires, so the metric says 0 rather than inferring a type from the reason prose.
    score, meta = o2c_eval.fault_classification(
        data_point_id="p1",
        conversation=[_turn({"flagged": True, "reason": "unit price 148.14 != PO 137.08"})],
        expected_output=_expected(flagged=True, fault_type="PRICE_DELTA"))
    assert score == 0.0
    assert meta["applicable"] is True and meta["predicted_fault_type"] is None
    assert "no fault_type" in meta["error"]


def test_fault_classification_scores_zero_when_the_declared_fault_type_is_blank():
    score, meta = o2c_eval.fault_classification(
        data_point_id="p1", conversation=[_turn({"flagged": True, "fault_type": "   "})],
        expected_output=_expected(flagged=True, fault_type="PRICE_DELTA"))
    assert score == 0.0 and meta["predicted_fault_type"] is None


def test_fault_classification_scores_one_on_a_correct_true_positive():
    score, meta = o2c_eval.fault_classification(
        data_point_id="p1", conversation=[_turn({"flagged": True, "fault_type": "PRICE_DELTA"})],
        expected_output=_expected(flagged=True, fault_type="PRICE_DELTA"))
    assert score == 1.0 and meta["predicted_fault_type"] == "PRICE_DELTA"


def test_fault_classification_accepts_a_differently_cased_or_padded_declaration():
    # The contract asks for the value verbatim, but casing/whitespace is not what this metric is
    # measuring -- resolve_scalar normalizes both sides, same as flag_accuracy.
    score, meta = o2c_eval.fault_classification(
        data_point_id="p1", conversation=[_turn({"flagged": True, "fault_type": " price_delta "})],
        expected_output=_expected(flagged=True, fault_type="PRICE_DELTA"))
    assert score == 1.0 and meta["predicted_fault_type"] == "PRICE_DELTA"


def test_fault_classification_scores_zero_on_a_wrong_true_positive():
    score, meta = o2c_eval.fault_classification(
        data_point_id="p1", conversation=[_turn({"flagged": True, "fault_type": "QTY_MISMATCH"})],
        expected_output=_expected(flagged=True, fault_type="PRICE_DELTA"))
    assert score == 0.0 and meta["predicted_fault_type"] == "QTY_MISMATCH"


def test_fault_classification_scores_zero_on_a_non_canonical_fault_type():
    # Nothing stops an artifact inventing a value; a type outside FAULT_TYPES is a contract
    # violation, reported with what it actually said rather than collapsed to a placeholder.
    score, meta = o2c_eval.fault_classification(
        data_point_id="p1", conversation=[_turn({"flagged": True, "fault_type": "SHIPPING_DELAY"})],
        expected_output=_expected(flagged=True, fault_type="PRICE_DELTA"))
    assert score == 0.0
    assert meta["predicted_fault_type"] == "SHIPPING_DELAY"
    assert "not one of" in meta["error"]


def test_fault_classification_does_not_call_an_llm():
    # This metric is a mechanical comparison over a closed set -- no judge, so no judge variance
    # and no cost. A regression back to an LLM judge would trip this.
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, _ = o2c_eval.fault_classification(
            data_point_id="p1", conversation=[_turn({"flagged": True, "fault_type": "DUPLICATE"})],
            expected_output=_expected(flagged=True, fault_type="DUPLICATE"))
    assert score == 1.0
    mock_complete.assert_not_called()


def test_fault_classification_scores_zero_when_expected_fault_type_is_absent():
    # expected_output has no "fault_type" field at all -- a dataset-authoring gap. No declared type
    # can equal None, so this is 0; the metadata shows the oracle side is what's missing.
    score, meta = o2c_eval.fault_classification(
        data_point_id="p1", conversation=[_turn({"flagged": True, "fault_type": "PRICE_DELTA"})],
        expected_output=_expected(flagged=True))
    assert score == 0.0 and meta["expected_fault_type"] is None


# --- reason_quality (make_transcript_judge-built) --------------------------------------------

def test_reason_quality_forwards_oracle_and_transcript_to_the_judge():
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "good", "score": 0.9}') as mock_complete:
        score, metadata = o2c_eval.reason_quality(
            data_point_id="p1",
            conversation=[_turn({"flagged": True, "reason": "dup of INV-1"})],
            expected_output=_expected(flagged=True, fault_type="DUPLICATE", duplicate_of="INV-1"))
    assert score == 0.9 and metadata == {"reasoning": "good"}
    (messages, _), _ = mock_complete.call_args
    user_prompt = messages[-1]["content"]
    assert "DUPLICATE" in user_prompt and "dup of INV-1" in user_prompt


# --- the response contract: a reply with no structured payload ---------------------------------
#
# The contract (a JSON object carrying flag / fault_type / reason) is stated to the agent during
# BUILD, and the transport fills the structured slot only when the whole reply IS that object. So an
# empty slot means the artifact answered in some other shape. Every metric must report that as a
# failure ON THIS DATA POINT -- never as a `false` flag, and never as "not applicable".
#
# Both readings were live bugs. Treating the absent flag as `false` gave a non-compliant artifact a
# free 1.0 on every clean invoice (19 of 60 here -- a 0.32 floor for answering in the wrong shape) and
# recorded false negatives on the flagged ones. Letting fault_classification gate on the artifact's own
# flag made the failure return None, so it dropped out of aggregation and the objective scored as if
# the data point did not exist.

_NARRATED = ('I looked up the invoice row:\n'
             '{"invoice_number": "INV-3046-DUP", "amount": 134568.0}\n'
             'It duplicates INV-3046, so it must be flagged.')


def test_flag_accuracy_fails_loudly_when_no_payload_was_returned():
    score, meta = o2c_eval.flag_accuracy(
        data_point_id="p1", conversation=[_prose_turn(_NARRATED)],
        expected_output=_expected(flagged=True, fault_type="DUPLICATE"))
    assert score == 0.0
    assert meta["error"] == o2c_eval.NO_PAYLOAD
    assert "predicted" not in meta          # no flag was claimed; don't invent one


def test_flag_accuracy_does_not_award_a_clean_invoice_to_a_non_compliant_reply():
    # The regression that matters most: an absent flag read as "not flagged" used to MATCH the oracle
    # on every clean sample, so answering in the wrong shape scored the not-flagged base rate.
    score, meta = o2c_eval.flag_accuracy(
        data_point_id="p1", conversation=[_prose_turn("This invoice looks fine to me.")],
        expected_output=_expected(flagged=False, fault_type=None))
    assert score == 0.0 and meta["error"] == o2c_eval.NO_PAYLOAD


def test_flag_recall_scores_a_missing_payload_as_a_miss_on_a_flagged_invoice():
    score, meta = o2c_eval.flag_recall(
        data_point_id="p1", conversation=[_prose_turn(_NARRATED)],
        expected_output=_expected(flagged=True, fault_type="DUPLICATE"))
    assert score == 0.0
    assert meta["applicable"] is True and meta["error"] == o2c_eval.NO_PAYLOAD


def test_flag_recall_stays_not_applicable_on_a_clean_invoice_regardless_of_the_reply():
    # Applicability is a property of the ORACLE, not of the response: false-negative rate is undefined
    # for an invoice that should not be flagged. flag_accuracy is what reports the contract failure.
    score, meta = o2c_eval.flag_recall(
        data_point_id="p1", conversation=[_prose_turn("Looks fine.")],
        expected_output=_expected(flagged=False, fault_type=None))
    assert score is None and meta["applicable"] is False


def test_fault_classification_scores_zero_not_none_when_no_payload_was_returned():
    # The silent-drop regression: gating on the artifact's flag made this return None, so a contract
    # failure was excluded from aggregation instead of scored.
    score, meta = o2c_eval.fault_classification(
        data_point_id="p1", conversation=[_prose_turn(_NARRATED)],
        expected_output=_expected(flagged=True, fault_type="DUPLICATE"))
    assert score == 0.0
    assert meta["applicable"] is True and meta["error"] == o2c_eval.NO_PAYLOAD
    assert meta["expected_fault_type"] == "DUPLICATE"


def test_an_empty_conversation_is_treated_as_no_payload():
    score, meta = o2c_eval.flag_accuracy(
        data_point_id="p1", conversation=[], expected_output=_expected(flagged=True))
    assert score == 0.0 and meta["error"] == o2c_eval.NO_PAYLOAD


# --- a payload that is an object but not an ANSWER ----------------------------------------------
#
# The regression this block exists for: the guards used to ask only whether the payload was ABSENT, so
# a bare object that merely lacked the flag field fell through to `.get(FLAG_KEY, False)` and read as a
# confident "not flagged". On a clean oracle that MATCHED -- 1.0 for a reply that answered nothing, on
# all 19 clean invoices (a 0.32 floor); on a flagged oracle it recorded a false negative the artifact
# never committed, with fault_classification returning None and dropping out of aggregation. Same
# silent coercion as the empty slot, one door over.

_CLEAN_ORACLE = dict(flagged=False, fault_type=None)
_FLAGGED_ORACLE = dict(flagged=True, fault_type="DUPLICATE")

# Objects that are NOT an answer, and the reason each fails.
_NOT_AN_ANSWER = [
    ({"flag": True, "fault_type": "DUPLICATE"}, "no 'flagged' field"),   # some other key name
    ({"fault_type": None, "reason": "matches PO"}, "no 'flagged' field"),  # fields, but not the flag
    ({}, "no 'flagged' field"),                                          # empty object
    ({"invoice_number": "INV-1", "amount": 12.0}, "no 'flagged' field"),  # a DB row
    ({"flagged": None}, "not a boolean"),                                # present but null
    ({"flagged": "maybe"}, "not a boolean"),                             # present but a shrug
]


@pytest.mark.parametrize("payload,reason", _NOT_AN_ANSWER)
def test_flag_accuracy_never_awards_a_clean_invoice_to_a_non_answer(payload, reason):
    # The headline regression: every one of these used to score 1.0 against a clean oracle.
    score, meta = o2c_eval.flag_accuracy(
        data_point_id="p1", conversation=[_turn(payload)], expected_output=_expected(**_CLEAN_ORACLE))
    assert score == 0.0
    assert reason in meta["error"]
    assert "predicted" not in meta          # nothing was claimed; don't record an invented flag


@pytest.mark.parametrize("payload,reason", _NOT_AN_ANSWER)
def test_flag_recall_does_not_invent_a_miss_silently_for_a_non_answer(payload, reason):
    score, meta = o2c_eval.flag_recall(
        data_point_id="p1", conversation=[_turn(payload)], expected_output=_expected(**_FLAGGED_ORACLE))
    assert score == 0.0 and meta["applicable"] is True and reason in meta["error"]


@pytest.mark.parametrize("payload,reason", _NOT_AN_ANSWER)
def test_fault_classification_scores_a_non_answer_rather_than_excluding_it(payload, reason):
    score, meta = o2c_eval.fault_classification(
        data_point_id="p1", conversation=[_turn(payload)], expected_output=_expected(**_FLAGGED_ORACLE))
    assert score == 0.0, "a contract failure must be scored, never dropped from aggregation"
    assert meta["applicable"] is True and reason in meta["error"]


def test_a_boolean_resolvable_flag_is_still_accepted():
    # The rejection above is for genuinely non-boolean values only. The alias table's forms are a
    # stylistic choice by the artifact, not a contract failure, and must keep scoring.
    for raw in (True, "true", "True", "yes", 1):
        conv = [_turn({"flagged": raw, "fault_type": "DUPLICATE"})]
        assert o2c_eval.flag_accuracy(data_point_id="p1", conversation=conv,
                                      expected_output=_expected(**_FLAGGED_ORACLE))[0] == 1.0
    for raw in (False, "false", "False", "no", 0):
        conv = [_turn({"flagged": raw})]
        assert o2c_eval.flag_accuracy(data_point_id="p1", conversation=conv,
                                      expected_output=_expected(**_CLEAN_ORACLE))[0] == 1.0


# --- the oracle side of the same hole -----------------------------------------------------------

def test_a_sample_with_no_oracle_flag_is_excluded_not_scored():
    # Mirror image: an absent expected_output used to collapse to `{}` and read as "clean", turning a
    # dataset gap into a scoreable expectation. It isn't the artifact's fault, so exclude with a reason
    # rather than score against a default. Every current sample carries the field; this guards the next.
    conv = [_turn({"flagged": True, "fault_type": "DUPLICATE"})]
    for fn in (o2c_eval.flag_accuracy, o2c_eval.flag_recall, o2c_eval.fault_classification):
        score, meta = fn(data_point_id="p1", conversation=conv, expected_output=None)
        assert score is None, f"{fn.__name__} scored a sample with no oracle"
        assert meta["error"] == o2c_eval.NO_ORACLE_FLAG
