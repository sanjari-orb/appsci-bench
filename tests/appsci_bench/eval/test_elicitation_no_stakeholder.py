"""The elicitation diagnostic on a NO-USER run: not applicable, and specifically not a zero.

Three runs produce an empty conversation and they mean three different things:

  - the log could not be read           -> `unmeasurable`. We do not know what the agent asked.
  - the log was read and holds nothing  -> `no_exchanges`. A stakeholder was there; the agent never
                                           used them. A real, informative zero.
  - no stakeholder ever existed         -> `no_stakeholder`. The no-user ablation arm. Not a finding
                                           about the agent at all: it was handed the same facts as
                                           files, so it had nothing to elicit.

The third is what this file is about, and it is worth its own status because the natural fallback --
letting it land on `no_exchanges` -- reports a 100% MISSED rate against an agent with nothing to miss,
and drags the arm's row to 0% in exactly the results table the arm exists to appear in.
"""
from __future__ import annotations

from unittest.mock import patch

from appsci_bench.eval.build_transcript import (
    STATUS_EMPTY,
    STATUS_NO_STAKEHOLDER,
    BuildTranscript,
    no_stakeholder_transcript,
)
from appsci_bench.eval.elicitation import (
    STATUS_NO_EXCHANGES,
    STATUS_NO_STAKEHOLDER as RESULT_NO_STAKEHOLDER,
    score_elicitation,
)
from appsci_bench.eval.elicitation_rubric import ElicitationRubric, RubricItem

_FACT = "The six fault_type values, spelled exactly, and the tolerance below which nothing is flagged."


def _rubric(*item_ids: str) -> ElicitationRubric:
    return ElicitationRubric(
        task_id="demo_task",
        items=tuple(RubricItem(item_id=i, fact=_FACT, objectives=("coverage",)) for i in item_ids),
        source="tasks/demo_task/elicitation_rubric.yaml")


def test_the_constructor_produces_an_empty_transcript_that_says_why():
    transcript = no_stakeholder_transcript()
    assert transcript.status == STATUS_NO_STAKEHOLDER
    assert transcript.exchanges == ()
    assert "no-user" in transcript.source


def test_a_run_with_no_stakeholder_has_no_score():
    with patch("appsci_bench.eval.elicitation.judge_item") as judge:
        result = score_elicitation(_rubric("a", "b"), no_stakeholder_transcript())
    judge.assert_not_called()
    assert result.status == RESULT_NO_STAKEHOLDER
    # None, never 0.0. A results table averaging elicited_rate across arms must not silently count
    # this arm as "asked for nothing".
    assert (result.elicited_rate, result.asked_not_elicited_rate, result.missed_rate) == (
        None, None, None,
    )


def test_it_produces_no_per_item_verdicts():
    """Every verdict would be a claim about an agent that had nothing to miss."""
    result = score_elicitation(_rubric("a", "b"), no_stakeholder_transcript())
    assert result.items == []
    assert result.judged_items == 0
    assert result.total_items == 2  # the rubric is still known, just not applicable


def test_the_warning_says_it_is_not_a_zero():
    result = score_elicitation(_rubric("a"), no_stakeholder_transcript())
    assert any("NOT a zero" in w for w in result.warnings)
    assert any("no-user" in w for w in result.warnings)


def test_it_is_distinct_from_an_agent_that_simply_never_asked():
    """The difference this whole status exists for. Both conversations are empty."""
    silent = BuildTranscript(exchanges=(), source="runs/demo", status=STATUS_EMPTY)
    with patch("appsci_bench.eval.elicitation.judge_item"):
        never_asked = score_elicitation(_rubric("a"), silent)
        no_stakeholder = score_elicitation(_rubric("a"), no_stakeholder_transcript())

    assert never_asked.status == STATUS_NO_EXCHANGES
    assert never_asked.missed_rate == 1.0
    assert no_stakeholder.status == RESULT_NO_STAKEHOLDER
    assert no_stakeholder.missed_rate is None


def test_an_empty_rubric_does_not_mask_the_arm():
    """Reported before the no-rubric check: what a reader needs to know first about this run is that
    it had no stakeholder, not that the task happens to ship no rubric."""
    result = score_elicitation(
        ElicitationRubric(task_id="demo_task", items=(), source="x"),
        no_stakeholder_transcript(),
    )
    assert result.status == RESULT_NO_STAKEHOLDER


