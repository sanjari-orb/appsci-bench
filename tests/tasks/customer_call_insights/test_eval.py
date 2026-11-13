"""Customer-call-insights eval-function tests.

LLM-free: the item matcher goes through `appsci_bench.eval.helpers.llm.complete`, which is patched here
(the same import site the other migrated tasks patch). Nothing in this file needs credentials.

Three things these tests are specifically guarding, all of them mistakes this repo has made before:
  1. A reply that is not a JSON object must SCORE 0 with the reason recorded -- never read as "every
     label empty", which on this task's oracle would MATCH and hand out free marks.
  2. The empty cases must stay arithmetic. `test_empty_paths_never_call_a_model` fails if someone
     "simplifies" the short-circuits away and routes every comparison through the matcher, which would
     turn the fabrication signal into a prompt.
  3. The matcher's size guard must bound COST without buying LENIENCY: items trimmed away for being
     over budget still count against precision, so flooding the list can never be the cheap strategy.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn
from tasks.customer_call_insights.eval import (
    _MATCH_BUDGET_CHARS,
    _MATCH_MAX_ITEMS,
    LABEL_MEANINGS,
    NO_ORACLE,
    NO_PAYLOAD,
    SENTIMENTS,
    _as_items,
    _within_budget,
    commitments_f1,
    match_set_f1,
    next_steps_f1,
    risks_f1,
    sentiment_accuracy,
)

_LIST_METRICS = (next_steps_f1, risks_f1, commitments_f1)
_ALL_METRICS = (*_LIST_METRICS, sentiment_accuracy)

# The label each list metric reads, so a parametrized test can build a matching oracle.
_FIELD_OF = {next_steps_f1: "next_steps", risks_f1: "risks", commitments_f1: "commitments"}


def _turn(response_json: dict | None = None, *, text: str | None = None) -> ConversationTurn:
    """One exchange. `response_json` is the artifact's structured payload; `text` its prose."""
    return ConversationTurn(
        query=ArtifactAgentQuery(text="Read the attached transcript and return this call's insights."),
        response=ArtifactAgentOutput(
            text=text, json=None if response_json is None else json.dumps(response_json)),
    )


def _prose_turn(text: str) -> ConversationTurn:
    """A reply that failed the response contract: prose, empty structured slot."""
    return _turn(None, text=text)


def _expected(**fields) -> ArtifactAgentOutput:
    return ArtifactAgentOutput(json=json.dumps(fields))


def _full_oracle(**overrides) -> ArtifactAgentOutput:
    """A complete four-label oracle, overridable per test."""
    base = {"next_steps": ["send revised pricing"], "risks": ["competitor in play"],
            "commitments": ["revised pricing by Friday"], "sentiment": "NEGATIVE"}
    base.update(overrides)
    return _expected(**base)


def _matcher(pairs: list[tuple[int, int]]) -> str:
    """A matcher reply pairing (expected_index, predicted_index)."""
    return json.dumps({
        "reasoning": "aligned",
        "matched_expected_indices": [e for e, _ in pairs],
        "matched_predicted_indices": [p for _, p in pairs],
        "score": 0.0,
    })


# --- match_set_f1: the empty cases, which must never reach a model ------------------------------

def test_both_empty_scores_one_because_an_honest_empty_answer_is_correct():
    score, meta = match_set_f1([], [], label="risks")
    assert score == 1.0
    assert meta["both_empty"] is True
    assert meta["judge_called"] is False


def test_items_invented_for_an_empty_label_score_zero():
    """The fabrication case, and the reason this task exists. Gold has nothing; the artifact returned
    something; every returned item is invented."""
    score, meta = match_set_f1(["a risk nobody mentioned"], [], label="risks")
    assert score == 0.0
    assert meta["extra"] == ["a risk nobody mentioned"]
    assert meta["judge_called"] is False


def test_returning_nothing_for_a_populated_label_scores_zero():
    score, meta = match_set_f1([], ["something real"], label="next_steps")
    assert score == 0.0
    assert meta["missed"] == ["something real"]
    assert meta["judge_called"] is False


def test_empty_paths_never_call_a_model():
    """REGRESSION GUARD. The three empty cases are decided by arithmetic. If someone routes them
    through the matcher "for consistency", the fabrication signal stops being a fact about the code and
    becomes a prompt that usually works -- and this test is what says so."""
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        assert match_set_f1([], [], label="risks")[0] == 1.0
        assert match_set_f1(["invented"], [], label="risks")[0] == 0.0
        assert match_set_f1([], ["real"], label="risks")[0] == 0.0
    mock_complete.assert_not_called()


# --- match_set_f1: the matcher path ------------------------------------------------------------

def test_all_items_matched_scores_one():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_matcher([(0, 0), (1, 1)])):
        score, meta = match_set_f1(["a", "b"], ["a", "b"], label="next_steps")
    assert score == 1.0
    assert meta["precision"] == 1.0 and meta["recall"] == 1.0
    assert meta["judge_called"] is True


def test_two_of_three_matched_earns_partial_credit():
    """The whole reason this metric is a set F1 and not an all-or-nothing set compare: catching two of
    three items must score above catching none. The framework's `json_field_match` would score this 0."""
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_matcher([(0, 0), (1, 1)])):
        score, meta = match_set_f1(["a", "b"], ["a", "b", "c"], label="next_steps")
    assert score == pytest.approx(0.8)          # P=1.0, R=2/3
    assert meta["missed"] == ["c"]
    assert meta["extra"] == []


def test_an_extra_item_dilutes_precision():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_matcher([(0, 0)])):
        score, meta = match_set_f1(["a", "padding"], ["a"], label="risks")
    assert score == pytest.approx(2 / 3)        # P=0.5, R=1.0
    assert meta["extra"] == ["padding"]


def test_nothing_matched_scores_zero_even_though_both_lists_are_non_empty():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_matcher([])):
        score, meta = match_set_f1(["unrelated"], ["something else"], label="risks")
    assert score == 0.0
    assert meta["missed"] == ["something else"]
    assert meta["extra"] == ["unrelated"]


def test_a_paraphrase_the_matcher_accepts_counts_as_a_match():
    """The case a lexical ratio got wrong. Token-set Jaccard scores this pair 0.43 -- below any
    threshold that also excludes unrelated phrases -- which is why matching moved to a model."""
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_matcher([(0, 0)])):
        score, _ = match_set_f1(["loop in legal on the MSA redlines"], ["loop in legal"], label="next_steps")
    assert score == 1.0


@pytest.mark.parametrize("bad_pairs, why", [
    ({"matched_expected_indices": [0, 0], "matched_predicted_indices": [0, 1]},
     "the same expected item claimed twice"),
    ({"matched_expected_indices": [0, 1], "matched_predicted_indices": [0, 0]},
     "the same predicted item claimed twice"),
    ({"matched_expected_indices": [0, 99], "matched_predicted_indices": [0, 1]},
     "an out-of-range expected index"),
    ({"matched_expected_indices": [0, 1], "matched_predicted_indices": [0]},
     "unequal list lengths"),
])
def test_a_malformed_matcher_verdict_degrades_the_score_never_inflates_it(bad_pairs, why):
    """A sloppy verdict must not manufacture matches. Each of these would, if trusted, count one real
    item as two matches and push F1 above what the items support."""
    reply = json.dumps({"reasoning": "r", "score": 0.0, **bad_pairs})
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=reply):
        score, meta = match_set_f1(["a", "b"], ["a", "b"], label="risks")
    assert len(meta["matched"]) == 1, why
    assert score == pytest.approx(0.5)


def test_the_matcher_receives_both_item_lists_and_the_label_meaning():
    """Pins what the matcher is actually asked. If the prompt stops carrying one of the lists, matching
    silently degrades to guesswork while every count-based test above still passes."""
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_matcher([(0, 0)])) as mock_complete:
        match_set_f1(["reissue the invoice"], ["reissue the May invoice with the correct seat count"],
                     label="commitments")
    (messages, _), _ = mock_complete.call_args
    prompt = messages[-1]["content"]
    assert "reissue the invoice" in prompt                                   # RETURNED
    assert "reissue the May invoice with the correct seat count" in prompt    # EXPECTED
    assert "promises OUR side made" in prompt                                # the label's meaning
    assert "[0]" in prompt                                                   # 0-based indices, as asked for


def test_the_three_list_metrics_cover_exactly_the_three_declared_labels():
    """The metrics, the matcher's label vocabulary, and the oracle's keys must be the same three names.
    A label added to one and not the others is a silent scoring gap -- the new label would simply never
    be measured."""
    assert set(LABEL_MEANINGS) == set(_FIELD_OF.values())
    assert set(LABEL_MEANINGS) == {"next_steps", "risks", "commitments"}


# --- the three list metrics --------------------------------------------------------------------

@pytest.mark.parametrize("metric", _LIST_METRICS)
def test_each_list_metric_reads_its_own_field(metric):
    """Each metric must score its own label and ignore the other two -- a copy-paste slip that wired two
    metrics to one field would otherwise pass every other test in this file."""
    field = _FIELD_OF[metric]
    # Correct on this metric's field, wrong on both others.
    payload = {"next_steps": [], "risks": [], "commitments": [], "sentiment": "NEUTRAL"}
    payload[field] = ["the right item"]
    oracle_fields = {"next_steps": ["wrong"], "risks": ["wrong"], "commitments": ["wrong"],
                     "sentiment": "NEUTRAL"}
    oracle_fields[field] = ["the right item"]

    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_matcher([(0, 0)])):
        score, _ = metric(data_point_id="d1", conversation=[_turn(payload)],
                          expected_output=_expected(**oracle_fields))
    assert score == 1.0


@pytest.mark.parametrize("metric", _LIST_METRICS)
def test_a_correctly_empty_label_scores_one_without_a_model_call(metric):
    field = _FIELD_OF[metric]
    payload = {"next_steps": [], "risks": [], "commitments": [], "sentiment": "NEUTRAL"}
    oracle = _full_oracle(**{field: []}, **{"sentiment": "NEUTRAL"})
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = metric(data_point_id="d1", conversation=[_turn(payload)], expected_output=oracle)
    assert score == 1.0
    assert meta["both_empty"] is True
    mock_complete.assert_not_called()


@pytest.mark.parametrize("metric", _LIST_METRICS)
def test_a_label_returned_as_a_bare_string_is_a_contract_failure(metric):
    """The output contract says an empty label is `[]` and a populated one is an array. A bare string is
    neither, and must not be silently wrapped into a one-item list."""
    field = _FIELD_OF[metric]
    payload = {"next_steps": [], "risks": [], "commitments": [], "sentiment": "NEUTRAL"}
    payload[field] = "just a sentence"
    score, meta = metric(data_point_id="d1", conversation=[_turn(payload)],
                         expected_output=_full_oracle())
    assert score == 0.0
    assert "not a list" in meta["error"]


@pytest.mark.parametrize("metric", _LIST_METRICS)
def test_a_label_returned_as_null_is_a_contract_failure_not_an_empty_list(metric):
    """`null` and `[]` must stay distinguishable: `[]` is "this call had none", `null` is a shape the
    contract forbids. Reading `null` as `[]` would score full marks on every legitimately-empty label."""
    field = _FIELD_OF[metric]
    payload = {"next_steps": [], "risks": [], "commitments": [], "sentiment": "NEUTRAL"}
    payload[field] = None
    oracle = _full_oracle(**{field: []})
    score, meta = metric(data_point_id="d1", conversation=[_turn(payload)], expected_output=oracle)
    assert score == 0.0
    assert "not a list" in meta["error"]


# --- sentiment ---------------------------------------------------------------------------------

@pytest.mark.parametrize("value", sorted(SENTIMENTS))
def test_each_allowed_sentiment_matches_itself(value):
    score, _ = sentiment_accuracy(data_point_id="d1", conversation=[_turn({"sentiment": value})],
                                  expected_output=_expected(sentiment=value))
    assert score == 1.0


@pytest.mark.parametrize("returned", ["negative", "  NEGATIVE  ", "Negative"])
def test_sentiment_matching_ignores_case_and_whitespace(returned):
    score, _ = sentiment_accuracy(data_point_id="d1", conversation=[_turn({"sentiment": returned})],
                                  expected_output=_expected(sentiment="NEGATIVE"))
    assert score == 1.0


def test_a_wrong_sentiment_scores_zero():
    score, meta = sentiment_accuracy(data_point_id="d1", conversation=[_turn({"sentiment": "POSITIVE"})],
                                     expected_output=_expected(sentiment="NEGATIVE"))
    assert score == 0.0
    assert meta["predicted"] == "POSITIVE" and meta["expected"] == "NEGATIVE"


@pytest.mark.parametrize("invented", ["AT_RISK", "MIXED", "CHURN_RISK", "neutral-ish"])
def test_a_sentiment_outside_the_closed_set_scores_zero(invented):
    """`AT_RISK` is the one worth naming: it is the plausible guess this task deliberately excludes, and
    returning it means the artifact folded the risks label into the sentiment field."""
    score, meta = sentiment_accuracy(data_point_id="d1", conversation=[_turn({"sentiment": invented})],
                                     expected_output=_expected(sentiment="NEGATIVE"))
    assert score == 0.0
    assert "is not one of" in meta["error"]


def test_at_risk_is_not_an_allowed_sentiment():
    """Belt and braces on the decision itself: if someone adds AT_RISK to SENTIMENTS, the metric above
    stops catching the conflation it exists to catch."""
    assert "AT_RISK" not in SENTIMENTS
    assert SENTIMENTS == {"POSITIVE", "NEUTRAL", "NEGATIVE"}


@pytest.mark.parametrize("missing", [{}, {"sentiment": None}, {"sentiment": "   "}])
def test_a_missing_or_blank_sentiment_scores_zero(missing):
    score, meta = sentiment_accuracy(data_point_id="d1", conversation=[_turn(missing)],
                                     expected_output=_expected(sentiment="NEUTRAL"))
    assert score == 0.0
    assert "error" in meta


def test_sentiment_accuracy_does_not_call_an_llm():
    """REGRESSION GUARD: sentiment is a closed set, so it is a mechanical comparison. A future change
    that routes it through a judge would add cost and variance for no accuracy."""
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        sentiment_accuracy(data_point_id="d1", conversation=[_turn({"sentiment": "NEUTRAL"})],
                           expected_output=_expected(sentiment="NEUTRAL"))
    mock_complete.assert_not_called()


# --- regression: a reply with no structured payload ---------------------------------------------

_NARRATED = ("Looking at this call, the customer raised some concerns about the integration timeline "
             "and there are a few follow-ups on both sides.")


@pytest.mark.parametrize("metric", _ALL_METRICS)
def test_a_prose_reply_scores_zero_with_the_contract_failure_recorded(metric):
    """A reply the CRM cannot parse scores 0 -- it is not "every label empty". Reading an absent label
    as empty would MATCH the oracle on every legitimately-quiet call and hand a non-compliant artifact
    free marks on exactly the samples this task cares most about."""
    score, meta = metric(data_point_id="d1", conversation=[_prose_turn(_NARRATED)],
                         expected_output=_full_oracle(next_steps=[], risks=[], commitments=[]))
    assert score == 0.0
    assert meta["error"] == NO_PAYLOAD


@pytest.mark.parametrize("metric", _ALL_METRICS)
def test_an_empty_conversation_scores_zero(metric):
    score, meta = metric(data_point_id="d1", conversation=[], expected_output=_full_oracle())
    assert score == 0.0
    assert meta["error"] == NO_PAYLOAD


@pytest.mark.parametrize("metric", _LIST_METRICS)
def test_a_prose_reply_does_not_claim_a_prediction(metric):
    """Nothing was claimed, so the metadata must not report a predicted value -- don't invent one."""
    score, meta = metric(data_point_id="d1", conversation=[_prose_turn(_NARRATED)],
                         expected_output=_full_oracle())
    assert score == 0.0
    assert "predicted" not in meta


# --- regression: an object that is not an ANSWER ------------------------------------------------

_NOT_AN_ANSWER = [
    ({}, "an empty object"),
    ({"nextSteps": ["a"], "sentiment": "NEUTRAL"}, "camelCase key names"),
    ({"next_step": ["a"], "sentiment": "NEUTRAL"}, "a singular key name"),
    ({"call_id": "CALL-7731", "turns": 12}, "a transcript echo rather than a reading"),
    ({"insights": {"next_steps": ["a"]}, "sentiment": "NEUTRAL"}, "the labels nested a level down"),
]


@pytest.mark.parametrize("payload, why", _NOT_AN_ANSWER)
@pytest.mark.parametrize("metric", _LIST_METRICS)
def test_an_object_that_is_not_an_answer_scores_zero(metric, payload, why):
    """A present-but-wrong object must not fall through to a comparison where an absent field reads as
    "empty" and matches an empty oracle. This is the o2c bug one door over: silent coercion of something
    the artifact never said."""
    oracle = _full_oracle(next_steps=[], risks=[], commitments=[])
    score, _ = metric(data_point_id="d1", conversation=[_turn(payload)], expected_output=oracle)
    assert score == 0.0, f"{why} should not score as a correct empty answer"


def test_a_non_object_payload_is_a_contract_failure():
    """`extract_json_object` only fills the slot for an object, but a hand-built transcript could carry
    a list -- treat it as no payload rather than crashing on `.get`."""
    turn = ConversationTurn(query=ArtifactAgentQuery(text="q"),
                            response=ArtifactAgentOutput(json=json.dumps(["not", "an", "object"])))
    for metric in _ALL_METRICS:
        score, meta = metric(data_point_id="d1", conversation=[turn], expected_output=_full_oracle())
        assert score == 0.0
        assert meta["error"] == NO_PAYLOAD


def test_unparseable_json_is_a_contract_failure():
    turn = ConversationTurn(query=ArtifactAgentQuery(text="q"),
                            response=ArtifactAgentOutput(json="{not valid json"))
    for metric in _ALL_METRICS:
        score, meta = metric(data_point_id="d1", conversation=[turn], expected_output=_full_oracle())
        assert score == 0.0
        assert meta["error"] == NO_PAYLOAD


# --- oracle-side mirror: a dataset gap is excluded, not scored ----------------------------------

@pytest.mark.parametrize("metric", _ALL_METRICS)
def test_a_sample_with_no_oracle_is_excluded_not_scored(metric):
    """An absent oracle is OUR gap, not the artifact's fault: score None (excluded from aggregation)
    rather than measuring the artifact against a default."""
    payload = {"next_steps": [], "risks": [], "commitments": [], "sentiment": "NEUTRAL"}
    score, meta = metric(data_point_id="d1", conversation=[_turn(payload)], expected_output=None)
    assert score is None
    assert meta["error"] == NO_ORACLE
    assert meta["scoreable"] is False


@pytest.mark.parametrize("metric", _LIST_METRICS)
def test_an_oracle_label_that_is_not_a_list_is_excluded(metric):
    field = _FIELD_OF[metric]
    oracle = _full_oracle(**{field: "a bare string"})
    payload = {"next_steps": [], "risks": [], "commitments": [], "sentiment": "NEUTRAL"}
    score, meta = metric(data_point_id="d1", conversation=[_turn(payload)], expected_output=oracle)
    assert score is None
    assert "not a list" in meta["error"]


def test_an_oracle_with_no_sentiment_is_excluded():
    score, meta = sentiment_accuracy(data_point_id="d1", conversation=[_turn({"sentiment": "NEUTRAL"})],
                                     expected_output=_expected(next_steps=[]))
    assert score is None
    assert meta["scoreable"] is False


# --- element coercion: what counts as an item the artifact actually said -------------------------

def test_a_null_inside_a_list_is_dropped_not_stringified():
    """REGRESSION. `str(None)` is `"None"`, which is non-blank -- so stripping-and-keeping turned a
    `null` element into an item literally named "None". That invents content the artifact never
    claimed: it gets counted against precision, or offered to the matcher as something to align."""
    assert _as_items(["   ", "", None]) == []
    assert _as_items(["a", None, "b"]) == ["a", "b"]
    assert _as_items(["a", {}, []]) == ["a"]        # junk stringifications dropped too


def test_a_non_list_value_is_still_a_contract_failure_not_an_empty_list():
    """The other half of the same boundary: dropping bad ELEMENTS must not start forgiving a bad
    SHAPE. `[]` is a correct answer; a bare string is the artifact answering in the wrong shape."""
    assert _as_items("a bare string") is None
    assert _as_items(None) is None
    assert _as_items(7) is None
    assert _as_items([]) == []                       # genuinely empty, a legitimate answer


@pytest.mark.parametrize("metric", _LIST_METRICS)
def test_a_label_of_only_blanks_and_nulls_reads_as_an_honest_empty_answer(metric):
    """End to end: `["  ", null]` against an empty oracle is the both-empty case (1.0), decided
    without a model call -- not a fabrication, because nothing was actually claimed."""
    field = _FIELD_OF[metric]
    payload = {"next_steps": [], "risks": [], "commitments": [], "sentiment": "NEUTRAL"}
    payload[field] = ["   ", "", None]
    oracle = _full_oracle(next_steps=[], risks=[], commitments=[], sentiment="NEUTRAL")
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = metric(data_point_id="d1", conversation=[_turn(payload)],
                             expected_output=oracle)
    assert score == 1.0
    assert meta["both_empty"] is True
    mock_complete.assert_not_called()


# --- the matcher token guard --------------------------------------------------------------------
#
# The predicted list is artifact-controlled and unbounded. These pin the guard's contract: honest
# output is never trimmed, and a flooded list is bounded WITHOUT being let off -- the dropped items
# still count against precision. Getting that backwards would make flooding the cheaper strategy.

def test_an_honest_sized_list_is_never_trimmed():
    gold = ["send revised pricing deck", "loop in legal", "schedule a QBR"]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_matcher([(0, 0), (1, 1), (2, 2)])) as mock_complete:
        score, meta = match_set_f1(gold, gold, label="next_steps")
    assert score == 1.0
    assert "degraded" not in meta          # the guard did not fire
    (messages, _), _ = mock_complete.call_args
    for item in gold:
        assert item in messages[-1]["content"]


def test_a_flooded_list_is_bounded_but_still_loses_precision():
    """The guard bounds what the MODEL reads; it must not bound what the artifact is SCORED on."""
    gold = ["send revised pricing deck", "loop in legal", "schedule a QBR"]
    flood = gold + [f"fabricated step {i} with a wordy description" for i in range(500)]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_matcher([(0, 0), (1, 1), (2, 2)])) as mock_complete:
        score, meta = match_set_f1(flood, gold, label="next_steps")

    (messages, _), _ = mock_complete.call_args
    assert len(messages[-1]["content"]) < 4 * _MATCH_BUDGET_CHARS   # prompt is bounded

    assert "degraded" in meta                     # and the trim is disclosed, never silent
    assert meta["recall"] == 1.0                  # all three real items were found
    assert meta["precision"] < 0.01               # but 500 fabrications cost precision
    assert len(meta["extra"]) == 500               # every fabrication counted, incl. untrimmed ones
    assert score < 0.05


def test_trimming_keeps_the_prefix_so_reported_indices_stay_meaningful():
    """`missed`/`extra` enumerate the FULL lists while matched indices come from the TRIMMED ones.
    That is only sound because the guard keeps a prefix -- pin it, or the two would silently desync."""
    items = [f"item {i}" for i in range(200)]
    kept, dropped = _within_budget(items)
    assert kept == items[:len(kept)]
    assert dropped == len(items) - len(kept)
    assert len(kept) <= _MATCH_MAX_ITEMS


def test_the_guard_also_bounds_a_short_list_of_enormous_items():
    """The count cap alone is not enough: ten 5k-character items blow the budget at ten items."""
    kept, dropped = _within_budget(["x" * 5000 for _ in range(10)])
    assert dropped > 0
    assert sum(len(k) for k in kept) <= _MATCH_BUDGET_CHARS
