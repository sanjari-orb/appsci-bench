"""Unit tests for tasks/airline_rebooking/eval.py.

task_success currently only computes COMMUNICATE (see its docstring for why DB is unmeasurable);
policy_compliance mocks complete() at the LLM import site, matching the other migrated tasks.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from appsci_bench.eval.datatypes import (
    TERMINATION_ERROR, TERMINATION_MAX_TURNS, TERMINATION_USER_STOP,
    ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn,
)
from tasks.airline_rebooking import eval as ar_eval


def _turn(agent_text: str) -> ConversationTurn:
    return ConversationTurn(query=ArtifactAgentQuery(text="user message"),
                            response=ArtifactAgentOutput(text=agent_text))


def _expected(**fields) -> ArtifactAgentOutput:
    return ArtifactAgentOutput(json=json.dumps(fields))


# --- task_success ----------------------------------------------------------------------------

def test_task_success_vacuously_true_when_nothing_to_communicate():
    conv = [_turn("Sorry, I can't process that cancellation.")]
    score, meta = ar_eval.task_success(data_point_id="p1", conversation=conv,
                                       expected_output=_expected(communicate_info=[]))
    assert score == 1.0 and meta["communicate_ok"] is True
    assert meta["db_component"] == "not_measurable_yet"


def test_task_success_scores_one_when_all_required_facts_stated():
    conv = [_turn("Your refund of $200 has been processed to your original payment method.")]
    exp = _expected(communicate_info=["refund", "$200"])
    score, meta = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp)
    assert score == 1.0 and meta["communicate_ok"] is True


def test_task_success_scores_zero_when_a_required_fact_is_missing():
    conv = [_turn("Your request has been processed.")]
    exp = _expected(communicate_info=["refund", "$200"])
    score, meta = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp)
    assert score == 0.0 and meta["communicate_ok"] is False
    assert set(meta["missing"]) == {"refund", "$200"}


def test_task_success_match_is_case_insensitive_and_comma_agnostic():
    conv = [_turn("your Refund of $1,200 is confirmed")]
    exp = _expected(communicate_info=["REFUND", "$1200"])
    score, _ = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp)
    assert score == 1.0


def test_task_success_does_not_credit_a_fact_split_across_a_turn_boundary():
    # Regression: an earlier version joined every turn's text into one string before searching,
    # which could spuriously match a required fact whose two halves land in adjacent turns even
    # though no single turn ever stated it -- exactly what the old per-message check (which never
    # concatenated across messages) would not have credited. "confirmation" must not be considered
    # communicated just because turn 1 ends "...confirm" and turn 2 begins "ation of your refund".
    conv = [_turn("Let me confirm"), _turn("ation of your refund is being processed")]
    exp = _expected(communicate_info=["confirmation"])
    score, meta = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp)
    assert score == 0.0 and meta["missing"] == ["confirmation"]


def test_task_success_checks_across_every_turn_not_just_the_last():
    conv = [_turn("Let me check your reservation."), _turn("Your refund has been issued.")]
    exp = _expected(communicate_info=["refund"])
    score, _ = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp)
    assert score == 1.0


# --- task_success DB component (final_state wired) -------------------------------------------

def test_task_success_db_one_when_state_hash_matches_golden_replay():
    # Refusal case: empty golden actions -> golden state == the fresh fixture. An agent that made no
    # writes leaves the env at that same hash -> DB=1, and with nothing to communicate -> reward 1.0.
    golden = ar_eval._golden_state_hash([])
    conv = [_turn("Sorry, that cancellation isn't allowed.")]
    exp = _expected(actions=[], communicate_info=[])
    score, meta = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp,
                                       final_state={"env_state_hash": golden})
    assert score == 1.0 and meta["db_ok"] is True
    assert "db_component" not in meta          # DB was actually measured now


def test_task_success_db_zero_when_state_hash_differs():
    conv = [_turn("Done.")]
    exp = _expected(actions=[], communicate_info=[])
    score, meta = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp,
                                       final_state={"env_state_hash": "not-the-golden-hash"})
    assert score == 0.0 and meta["db_ok"] is False


def test_task_success_reward_is_db_times_communicate():
    # DB matches but a required communication is missing -> product is 0.
    golden = ar_eval._golden_state_hash([])
    conv = [_turn("All set.")]
    exp = _expected(actions=[], communicate_info=["refund"])
    score, meta = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp,
                                       final_state={"env_state_hash": golden})
    assert meta["db_ok"] is True and meta["communicate_ok"] is False
    assert score == 0.0


# --- task_success premature-termination gate (tau2 parity) -----------------------------------

def test_task_success_gates_a_truncated_trace_to_zero_even_when_db_and_communicate_pass():
    # The exact hole the gate closes: a refusal case (empty golden actions, nothing to communicate)
    # where the agent simply never wrote anything scores DB x COMMUNICATE = 1.0 on its merits. If the
    # conversation was cut off by the turn ceiling, upstream tau2 scores it 0 before even reading the
    # criteria -- so we must too, or truncation hands out free points on 7 of the 50 cases.
    golden = ar_eval._golden_state_hash([])
    conv = [_turn("Sorry, that cancellation isn't allowed.")]
    exp = _expected(actions=[], communicate_info=[])
    score, meta = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp,
                                       final_state={"env_state_hash": golden},
                                       termination_reason=TERMINATION_MAX_TURNS)
    assert score == 0.0
    assert meta["termination_reason"] == TERMINATION_MAX_TURNS
    # Gated BEFORE the reward halves are computed -- neither component was evaluated.
    assert "db_ok" not in meta and "communicate_ok" not in meta


def test_task_success_gates_an_errored_trace_to_zero():
    golden = ar_eval._golden_state_hash([])
    conv = [_turn("Sorry, that cancellation isn't allowed.")]
    exp = _expected(actions=[], communicate_info=[])
    score, _ = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp,
                                    final_state={"env_state_hash": golden},
                                    termination_reason=TERMINATION_ERROR)
    assert score == 0.0


def test_task_success_scores_normally_when_the_sim_ended_the_conversation():
    golden = ar_eval._golden_state_hash([])
    conv = [_turn("Sorry, that cancellation isn't allowed.")]
    exp = _expected(actions=[], communicate_info=[])
    score, meta = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp,
                                       final_state={"env_state_hash": golden},
                                       termination_reason=TERMINATION_USER_STOP)
    assert score == 1.0 and meta["db_ok"] is True
    assert meta["termination_reason"] == TERMINATION_USER_STOP


def test_task_success_treats_an_unrecorded_termination_as_unknown_not_premature():
    # A hand-built record or a pre-gate transcript carries no termination_reason. That must score the
    # way it always did (fail OPEN, not closed) while saying so in the metadata, so a broken plumbing
    # path is visible in the report instead of silently zeroing or silently inflating every case.
    golden = ar_eval._golden_state_hash([])
    conv = [_turn("Sorry, that cancellation isn't allowed.")]
    exp = _expected(actions=[], communicate_info=[])
    score, meta = ar_eval.task_success(data_point_id="p1", conversation=conv, expected_output=exp,
                                       final_state={"env_state_hash": golden})
    assert score == 1.0
    assert meta["termination_reason"] == "not_recorded"


# --- policy_compliance (make_transcript_judge-built) ------------------------------------------

def test_policy_compliance_forwards_expected_json_and_rubric_and_transcript():
    conv = [_turn("I'm sorry, I can't process that without proper eligibility.")]
    exp = _expected(actions=[], nl_assertions=["Agent should refuse."])
    with patch("appsci_bench.eval.helpers.llm.complete",
              return_value='{"reasoning": "correctly refused", "score": 0.9}') as mock_complete:
        score, metadata = ar_eval.policy_compliance(
            data_point_id="p1", conversation=conv, expected_output=exp,
            eval_rubric="Agent should refuse to proceed with the cancellation.")
    assert score == 0.9 and metadata == {"reasoning": "correctly refused"}
    (messages, _), _ = mock_complete.call_args
    user_prompt = messages[-1]["content"]
    assert "Agent should refuse" in user_prompt
    assert "nl_assertions" in user_prompt


# --- gold agent LLM trace (for the eval viewer) -----------------------------------------------

