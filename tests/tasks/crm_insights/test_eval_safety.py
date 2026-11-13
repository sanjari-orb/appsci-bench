"""Unit tests for tasks/crm_insights/eval_safety.py -- the two artifact-safety objectives.

Safety is an LLM judgment, so both objectives call the shared `llm_judge_call`; we stub its
transport (`appsci_bench.eval.helpers.llm.complete`) at the import site, matching the convention in
tests/tasks/order_to_cash/test_eval_safety.py. The point of these tests is the fail-SAFE contract: a
violation scores 0.0, a clean artifact scores 1.0, and anything the judge can't assess (no artifact
reply / unparseable verdict) returns None -- EXCLUDED from aggregation, never a fabricated 1.0.

Regression: `_conversation_view` used to set `has_content = True` off the QUESTION alone (`q or text
or js`), so a turn with a question but no artifact reply still got judged as if it had answered --
fail-open, not fail-SAFE. Fixed to require `text or js` (the artifact's own reply).
"""
from __future__ import annotations

from unittest.mock import patch

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn
from tasks.crm_insights import eval_safety as safety


def _turn(query: str = "how many cases were transferred last quarter?", text: str = "", json: str = "") -> ConversationTurn:
    return ConversationTurn(query=ArtifactAgentQuery(text=query),
                            response=ArtifactAgentOutput(text=text, json=json))


def _verdict(score: float, reasoning: str = "stub") -> str:
    return '{"reasoning": "%s", "score": %s}' % (reasoning, score)


# --- no_fabrication ------------------------------------------------------------------------------

def test_no_fabrication_scores_one_on_a_clean_artifact():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_verdict(1.0, "derivation checks out")):
        score, meta = safety.no_fabrication(
            data_point_id="p1",
            conversation=[_turn(text="OR, based on cases 5001/5002/5003 in Q3.")])
    assert score == 1.0 and meta["ran"] is True and meta["axis"] == "safety_no_fabrication"
    assert meta["reasoning"] == "derivation checks out"


def test_no_fabrication_scores_zero_when_the_judge_finds_a_fabrication():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_verdict(0.0, "no derivation path")):
        score, meta = safety.no_fabrication(
            data_point_id="p1", conversation=[_turn(text="It's OR.")])
    assert score == 0.0 and meta["ran"] is True


def test_no_fabrication_returns_none_when_there_is_no_artifact_to_judge():
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = safety.no_fabrication(data_point_id="p1", conversation=[])
    assert score is None and meta["ran"] is False
    mock_complete.assert_not_called()


def test_no_fabrication_returns_none_when_the_question_has_no_artifact_reply():
    # fail-SAFE regression: a turn with a question but an EMPTY reply must not count as "content" --
    # it must not be judged as if it had answered, and must never fabricate a "safe" 1.0.
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = safety.no_fabrication(
            data_point_id="p1",
            conversation=[_turn(query="how many cases were transferred last quarter?", text="", json="")])
    assert score is None and meta["ran"] is False
    mock_complete.assert_not_called()


def test_no_fabrication_returns_none_when_the_judge_verdict_is_unparseable():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value="not json at all"):
        score, meta = safety.no_fabrication(
            data_point_id="p1", conversation=[_turn(text="some artifact output")])
    assert score is None and meta["ran"] is False and "JudgeParseError" in meta["reason"]


# --- safe_by_construction ------------------------------------------------------------------------

def test_safe_by_construction_scores_one_when_safeguards_are_intact():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_verdict(1.0, "ambiguity flagged")):
        score, meta = safety.safe_by_construction(
            data_point_id="p1",
            conversation=[_turn(text="Assuming a calendar-quarter window (not stated in the schema): OR.")])
    assert score == 1.0 and meta["ran"] is True and meta["axis"] == "safety_safe_by_construction"


def test_safe_by_construction_scores_zero_when_a_safeguard_is_dropped():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_verdict(0.0, "silently picked a window")):
        score, meta = safety.safe_by_construction(
            data_point_id="p1", conversation=[_turn(text="OR.")])
    assert score == 0.0 and meta["ran"] is True


def test_safe_by_construction_returns_none_on_empty_conversation():
    score, meta = safety.safe_by_construction(data_point_id="p1", conversation=[])
    assert score is None and meta["ran"] is False


def test_safe_by_construction_returns_none_when_the_question_has_no_artifact_reply():
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = safety.safe_by_construction(
            data_point_id="p1",
            conversation=[_turn(query="what's the most similar prior issue?", text="", json="")])
    assert score is None and meta["ran"] is False
    mock_complete.assert_not_called()
