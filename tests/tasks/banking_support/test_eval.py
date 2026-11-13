"""Unit tests for tasks/banking_support/eval.py -- the five tau2-banking eval objectives.

Creds-free. Deterministic axes (task_success DB/ACTION) run the REAL BankingEnvironment via
`_golden_state_hash` against real sample oracles; the LLM-judge axes stub the transport
(`appsci_bench.eval.helpers.llm.complete`) at the import site -- the same convention as
tests/tasks/airline_rebooking/test_eval.py and tests/tasks/order_to_cash/test_eval_safety.py -- so
the real `llm_judge_call` parsing (JudgeVerdict schema + JudgeParseError) is exercised, not mocked
away.

The load-bearing property under test is the FAIL-SAFE contract: a metric that genuinely cannot be
assessed (basis component not delivered by the env seam, no KB docs, no artifact output, unparseable
verdict) returns score=None -- EXCLUDED from aggregation -- and NEVER a fake pass. This matters
acutely for banking: `communicate_info` is empty in every case, so a COMMUNICATE-only fallback would
be a vacuous 1.0, which task_success must refuse to emit.
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

import pytest

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn
from tasks.banking_support import eval as bk
from tasks.banking_support import eval_safety as bk_safety

_EVAL_DATA = pathlib.Path(__file__).resolve().parents[3] / "tasks" / "banking_support" / "eval_data"


# --- helpers ----------------------------------------------------------------------------------

def _turn(agent_text: str) -> ConversationTurn:
    return ConversationTurn(query=ArtifactAgentQuery(text="customer message"),
                            response=ArtifactAgentOutput(text=agent_text))


def _expected(**fields) -> ArtifactAgentOutput:
    """A synthetic oracle: fields become expected_output.json (a JSON string), as on disk."""
    return ArtifactAgentOutput(json=json.dumps(fields))


def _oracle(dpid: str) -> dict:
    """The parsed oracle dict for a real committed sample."""
    raw = json.loads((_EVAL_DATA / dpid / "sample.json").read_text())
    return json.loads(raw["expected_output"]["json"])


def _expected_of(dpid: str) -> ArtifactAgentOutput:
    raw = json.loads((_EVAL_DATA / dpid / "sample.json").read_text())
    return ArtifactAgentOutput(json=raw["expected_output"]["json"])


def _verdict(score, reasoning: str = "stub") -> str:
    return '{"reasoning": "%s", "score": %s}' % (reasoning, score)


# --- task_success: DB basis (real env replay) -------------------------------------------------

def test_db_reward_one_when_hash_matches_golden_replay():
    # tau2_banking_1: DB basis, initial_state=null, one apply_for_credit_card write. An artifact
    # that drove the DB to the golden state produces the golden hash -> DB=1, and with nothing to
    # communicate -> reward 1.0.
    o = _oracle("tau2_banking_1")
    assert o["reward_basis"] == ["DB"] and o["initial_state"] is None
    golden = bk._golden_state_hash(o["actions"], o["initial_state"])
    score, meta = bk.task_success(
        data_point_id="tau2_banking_1", conversation=[_turn("The Gold Rewards Card fits best.")],
        expected_output=_expected_of("tau2_banking_1"), final_state={"env_state_hash": golden})
    assert score == 1.0 and meta["db_ok"] is True and meta["communicate_ok"] is True


def test_db_reward_zero_when_hash_differs():
    o = _oracle("tau2_banking_1")
    score, meta = bk.task_success(
        data_point_id="tau2_banking_1", conversation=[_turn("Applied.")],
        expected_output=_expected_of("tau2_banking_1"), final_state={"env_state_hash": "not-golden"})
    assert score == 0.0 and meta["db_ok"] is False


def test_db_none_when_no_state_hash_delivered():
    # Fail-SAFE: reset seam off (no env_state_hash) -> None, NOT COMMUNICATE-only (which is a vacuous
    # 1.0 for banking's empty communicate_info).
    score, meta = bk.task_success(
        data_point_id="tau2_banking_1", conversation=[_turn("Applied.")],
        expected_output=_expected_of("tau2_banking_1"), final_state=None)
    assert score is None and meta["measurable"] is False
    assert any("env_state_hash" in r for r in meta["reasons"])


def test_db_none_when_initial_state_present_but_not_seeded():
    # A case with a non-null initial_state is not measurable until the live env is seeded (signalled
    # by seeded_initial_state) -> honest None, never a false 0.0 for correct behavior.
    exp = _expected(reward_basis=["DB"], actions=[], communicate_info=[],
                    initial_state={"agent_data": {"users": {"data": {}}}})
    score, meta = bk.task_success(data_point_id="x", conversation=[_turn("Done.")],
                                  expected_output=exp, final_state={"env_state_hash": "abc"})
    assert score is None and meta["measurable"] is False
    assert any("seeded" in r for r in meta["reasons"])


def test_db_measured_against_seeded_golden_when_seeded_flag_present():
    # tau2_banking_100: DB basis WITH a non-null initial_state. Once the seam signals it seeded the
    # live env, the comparison opens AND it is the SEEDED golden that is compared.
    o = _oracle("tau2_banking_100")
    assert o["reward_basis"] == ["DB"] and o["initial_state"]
    seeded_golden = bk._golden_state_hash(o["actions"], o["initial_state"])
    # The live env must also report the same required-reads allowlist the golden replay used (see
    # test_required_reads_allowlist_must_match_on_both_sides for why an asymmetric one is refused).
    applied = bk._required_reads(o["actions"])
    score, meta = bk.task_success(
        data_point_id="tau2_banking_100", conversation=[_turn("Handled.")],
        expected_output=_expected_of("tau2_banking_100"),
        final_state={"env_state_hash": seeded_golden, "seeded_initial_state": True,
                     "read_log_allowlist": applied})
    assert score == 1.0 and meta["db_ok"] is True
    # Same hash but WITHOUT the seeded flag -> gate stays closed -> None.
    score2, meta2 = bk.task_success(
        data_point_id="tau2_banking_100", conversation=[_turn("Handled.")],
        expected_output=_expected_of("tau2_banking_100"),
        final_state={"env_state_hash": seeded_golden, "read_log_allowlist": applied})
    assert score2 is None and meta2["measurable"] is False


def test_reward_is_basis_times_communicate():
    # DB matches but a required communication is missing -> product is 0 (guards the x COMMUNICATE).
    golden = bk._golden_state_hash([], None)
    exp = _expected(reward_basis=["DB"], actions=[], communicate_info=["wire transfer"])
    score, meta = bk.task_success(data_point_id="x", conversation=[_turn("All set.")],
                                  expected_output=exp, final_state={"env_state_hash": golden})
    assert meta["db_ok"] is True and meta["communicate_ok"] is False and score == 0.0


def test_reward_basis_defaults_to_db():
    golden = bk._golden_state_hash([], None)
    exp = _expected(actions=[], communicate_info=[])   # no reward_basis key
    score, meta = bk.task_success(data_point_id="x", conversation=[_turn("No changes needed.")],
                                  expected_output=exp, final_state={"env_state_hash": golden})
    assert score == 1.0 and meta["reward_basis"] == ["DB"]


# --- task_success: ACTION basis (trace match) -------------------------------------------------

def test_action_reward_one_matches_trace_ignoring_uncompared_args():
    # tau2_banking_12: transfer_to_human_agents with compare_args=["reason"] -- the free-text summary
    # must NOT be compared. A trace with the right reason but a totally different summary still wins.
    o = _oracle("tau2_banking_12")
    assert o["reward_basis"] == ["ACTION"] and o["actions"][0]["compare_args"] == ["reason"]
    trace = [{"name": "transfer_to_human_agents",
              "arguments": {"reason": o["actions"][0]["arguments"]["reason"],
                            "summary": "an entirely different summary"}}]
    score, meta = bk.task_success(
        data_point_id="tau2_banking_12", conversation=[_turn("Let me transfer you to a human.")],
        expected_output=_expected_of("tau2_banking_12"), final_state={"agent_actions": trace})
    assert score == 1.0 and meta["action_ok"] is True


def test_action_reward_zero_when_compared_arg_differs():
    o = _oracle("tau2_banking_12")
    trace = [{"name": "transfer_to_human_agents", "arguments": {"reason": "a_different_reason"}}]
    score, meta = bk.task_success(
        data_point_id="tau2_banking_12", conversation=[_turn("Transferring.")],
        expected_output=_expected_of("tau2_banking_12"), final_state={"agent_actions": trace})
    assert score == 0.0 and meta["action_ok"] is False


def test_action_none_when_no_trace_captured():
    # Fail-SAFE: ACTION basis but no agent action-trace delivered -> None (not a fake pass).
    score, meta = bk.task_success(
        data_point_id="tau2_banking_12", conversation=[_turn("Transferring.")],
        expected_output=_expected_of("tau2_banking_12"), final_state={"env_state_hash": "irrelevant"})
    assert score is None and meta["measurable"] is False
    assert any("action" in r for r in meta["reasons"])


# --- task_success: the termination gate -------------------------------------------------------

def test_a_truncated_conversation_scores_zero_even_when_the_hash_matches():
    # The gate's whole point: a case whose golden writes landed but which then talked past the turn
    # ceiling has NOT finished, so it cannot bank the pass. Same inputs as the DB=1 test above --
    # only termination_reason differs.
    o = _oracle("tau2_banking_1")
    golden = bk._golden_state_hash(o["actions"], o["initial_state"])
    score, meta = bk.task_success(
        data_point_id="tau2_banking_1", conversation=[_turn("The Gold Rewards Card fits best.")],
        expected_output=_expected_of("tau2_banking_1"), final_state={"env_state_hash": golden},
        termination_reason="max_turns")
    assert score == 0.0
    assert meta["termination_reason"] == "max_turns" and "prematurely" in meta["note"]


def test_a_premature_end_is_zero_not_none_so_it_counts_against_pass_k():
    # The distinction this function uniquely has to keep: None = "could not measure" (excluded from
    # aggregation and pass^k); 0.0 = "measurably did not finish" (belongs in the denominator).
    # Returning None here would drop truncated cases instead of counting them, inverting the gate.
    for reason in ("max_turns", "max_steps", "error"):
        score, meta = bk.task_success(
            data_point_id="tau2_banking_1", conversation=[],
            expected_output=_expected_of("tau2_banking_1"), final_state=None,
            termination_reason=reason)
        assert score == 0.0, f"{reason} must be a scored failure, not an exclusion"
        assert meta["measurable"] is True


def test_the_gate_runs_before_measurability_so_an_unmeasured_truncation_is_still_zero():
    # Ordering matters: with no final_state this case would otherwise return None. A conversation cut
    # off mid-task is a failure regardless of whether the env seam delivered anything.
    score, _ = bk.task_success(
        data_point_id="tau2_banking_1", conversation=[], final_state=None,
        expected_output=_expected_of("tau2_banking_1"), termination_reason="error")
    assert score == 0.0


def test_a_clean_stop_is_scored_normally_and_recorded():
    o = _oracle("tau2_banking_1")
    golden = bk._golden_state_hash(o["actions"], o["initial_state"])
    score, meta = bk.task_success(
        data_point_id="tau2_banking_1", conversation=[_turn("The Gold Rewards Card fits best.")],
        expected_output=_expected_of("tau2_banking_1"), final_state={"env_state_hash": golden},
        termination_reason="user_stop")
    assert score == 1.0 and meta["termination_reason"] == "user_stop"


def test_an_unrecorded_termination_is_scored_but_flagged_rather_than_passed_off_as_clean():
    # A hand-built record or a pre-field transcript: treated as UNKNOWN, scored normally, and said so.
    o = _oracle("tau2_banking_1")
    golden = bk._golden_state_hash(o["actions"], o["initial_state"])
    score, meta = bk.task_success(
        data_point_id="tau2_banking_1", conversation=[_turn("The Gold Rewards Card fits best.")],
        expected_output=_expected_of("tau2_banking_1"), final_state={"env_state_hash": golden})
    assert score == 1.0 and meta["termination_reason"] == "not_recorded"


# --- stop criteria carried per case ------------------------------------------------------------

def test_every_case_declares_its_own_stop_criteria_naming_all_three_end_conditions():
    # Absent this field a case silently inherits the framework default, which covers only "goal
    # satisfied" -- leaving transfer and scenario-exhausted unrepresented in a domain full of both.
    seen = set()
    for d in sorted(_EVAL_DATA.iterdir()):
        if not (d / "sample.json").is_file():
            continue
        crit = json.loads((d / "sample.json").read_text())["multi_turn_input"]["user_stop_criteria"]
        seen.add(crit)
        low = crit.lower()
        assert "goal" in low, f"{d.name}: no goal-satisfied condition"
        assert "transferring you to a human" in low, f"{d.name}: no transfer condition"
        assert "instructions do not give you" in low, f"{d.name}: no scenario-exhausted condition"
    assert len(seen) == 1, "one shared criterion expected; per-case variation rides in user_task"


def test_the_criterion_makes_the_scripted_per_case_endings_operative():
    # 43 cases script their ending with a literal marker inside user_task. The sim signals through a
    # structured field, so without an explicit clause those markers are inert noise in its prompt.
    crit = next(iter({
        json.loads((d / "sample.json").read_text())["multi_turn_input"]["user_stop_criteria"]
        for d in _EVAL_DATA.iterdir() if (d / "sample.json").is_file()
    }))
    assert "###STOP###" in crit and "###OUT-OF-SCOPE###" in crit
    assert "override" in crit.lower(), "per-case instructions must outrank the global conditions"
    assert "never type the marker" in crit.lower(), "a marker is an instruction, not words to say"


def test_the_criterion_does_not_let_the_customers_own_escalation_end_the_conversation():
    # 19 cases hand the CUSTOMER a transfer-request tool and in only 3 is using it correct. Treating
    # the customer's own escalation as an ending would corrupt all 19.
    crit = next(iter({
        json.loads((d / "sample.json").read_text())["multi_turn_input"]["user_stop_criteria"]
        for d in _EVAL_DATA.iterdir() if (d / "sample.json").is_file()
    }))
    assert "yourself is an action you take, not a reason to stop" in crit.lower()


def test_the_criterion_omits_airlines_non_upstream_refusal_exit():
    # Upstream's goal-satisfied condition has no "agent refused and you have no argument left" exit.
    # Refuse-then-escalate is banking's most common shape, so importing it would license stopping
    # early in exactly the cases most sensitive to it.
    crit = next(iter({
        json.loads((d / "sample.json").read_text())["multi_turn_input"]["user_stop_criteria"]
        for d in _EVAL_DATA.iterdir() if (d / "sample.json").is_file()
    }))
    assert "no further argument" not in crit.lower()


# --- groundedness (corpus-backed judge; stub the transport) -----------------------------------

def test_groundedness_passes_judge_score_and_feeds_required_docs_as_source():
    exp = _expected_of("tau2_banking_1")           # 4 real credit-card docs resolve from the corpus
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_verdict(0.85, "all claims grounded")) as mock_complete:
        score, meta = bk.groundedness(
            data_point_id="tau2_banking_1",
            conversation=[_turn("The Gold Rewards Card has no annual fee and 2% cash back.")],
            expected_output=exp)
    assert score == 0.85 and meta["reasoning"] == "all claims grounded"
    assert "doc_credit_cards_gold_rewards_card_001" in meta["grounded_against"]
    (messages, _), _ = mock_complete.call_args
    user_prompt = messages[-1]["content"]
    assert "doc_credit_cards_gold_rewards_card_001" in user_prompt   # a required doc is in SOURCE
    assert "AGENT STATEMENTS" in user_prompt and "no annual fee" in user_prompt


def test_groundedness_none_when_no_artifact_output():
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = bk.groundedness(data_point_id="tau2_banking_1", conversation=[_turn("")],
                                      expected_output=_expected_of("tau2_banking_1"))
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


def test_groundedness_none_when_case_lists_no_required_documents():
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = bk.groundedness(data_point_id="x", conversation=[_turn("hi")],
                                      expected_output=_expected(required_documents=[]))
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


def test_groundedness_none_when_required_docs_missing_from_corpus():
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = bk.groundedness(
            data_point_id="x", conversation=[_turn("some advice")],
            expected_output=_expected(required_documents=["doc_that_does_not_exist_999"]))
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


def test_groundedness_none_when_verdict_unparseable():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value="not json at all"):
        score, meta = bk.groundedness(
            data_point_id="tau2_banking_1", conversation=[_turn("advice")],
            expected_output=_expected_of("tau2_banking_1"))
    assert score is None and meta["applicable"] is False and "JudgeParseError" in meta["reason"]


# --- no_fabrication (standalone safety judge; stub the transport) -----------------------------

def test_no_fabrication_scores_one_on_clean_output():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_verdict(1.0, "no invented facts")):
        score, meta = bk.no_fabrication(
            data_point_id="x", conversation=[_turn("I don't have that detail; let me transfer you.")])
    assert score == 1.0 and meta["reasoning"] == "no invented facts"


def test_no_fabrication_scores_zero_when_judge_finds_fabrication():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_verdict(0.0, "invented an APR")):
        score, _ = bk.no_fabrication(data_point_id="x", conversation=[_turn("Your APR is exactly 9.99%.")])
    assert score == 0.0


def test_no_fabrication_none_when_no_output():
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = bk.no_fabrication(data_point_id="x", conversation=[_turn("")])
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


def test_no_fabrication_none_when_verdict_unparseable():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value="garbage"):
        score, meta = bk.no_fabrication(data_point_id="x", conversation=[_turn("something")])
    assert score is None and "JudgeParseError" in meta["reason"]


# --- retrieval_recall: a REAL weighted metric with two measurement paths ----------------------
# ENV-MEASURED (preferred): exact set-overlap of the env-logged retrieved doc ids vs the case's
# required_documents -- no judge. TRANSCRIPT PROXY (fallback): a coverage judge over the answer,
# until the eval-side seam delivers final_state["retrieved_documents"]. Fail-SAFE None when neither
# path can be measured. (LLM-judge paths stub the transport, as elsewhere in this file.)

def test_retrieval_recall_env_measured_full_overlap():
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = bk.retrieval_recall(
            data_point_id="x", conversation=[_turn("answer")],
            expected_output=_expected(required_documents=["d1", "d2"]),
            final_state={"retrieved_documents": ["d1", "d2", "d_extra"]})
    assert score == 1.0
    assert meta["measured"] == "env" and meta["hit"] == ["d1", "d2"] and meta["retrieved_count"] == 3
    mock_complete.assert_not_called()          # the env path never invokes the judge


def test_retrieval_recall_env_measured_partial_overlap():
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = bk.retrieval_recall(
            data_point_id="x", conversation=[_turn("answer")],
            expected_output=_expected(required_documents=["d1", "d2", "d3", "d4"]),
            final_state={"retrieved_documents": ["d1", "d3"]})
    assert score == 0.5 and meta["measured"] == "env"
    assert meta["hit"] == ["d1", "d3"] and meta["required"] == ["d1", "d2", "d3", "d4"]
    mock_complete.assert_not_called()


def test_retrieval_recall_env_measured_empty_is_zero_not_none():
    # A delivered-but-empty retrieved set is an AUTHORITATIVE 0.0 (the artifact retrieved none of the
    # required docs) -- never None, and never a fallthrough to the proxy.
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = bk.retrieval_recall(
            data_point_id="x", conversation=[_turn("answer")],
            expected_output=_expected(required_documents=["d1", "d2"]),
            final_state={"retrieved_documents": []})
    assert score == 0.0 and meta["measured"] == "env"
    mock_complete.assert_not_called()


def test_retrieval_recall_falls_back_to_proxy_when_env_absent():
    # final_state present but WITHOUT retrieved_documents => the env seam hasn't delivered => proxy.
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_verdict(0.4, "partial coverage")):
        score, meta = bk.retrieval_recall(
            data_point_id="tau2_banking_1",
            conversation=[_turn("The Gold Rewards Card gives 2% cash back with no annual fee.")],
            expected_output=_expected_of("tau2_banking_1"),
            final_state={"env_state_hash": "abc"})
    assert score == 0.4 and meta["measured"] == "proxy"


def test_retrieval_recall_passes_judge_score():
    # No final_state at all => proxy path (the working-today default until the seam lands).
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_verdict(0.7, "covered key facts")):
        score, meta = bk.retrieval_recall(
            data_point_id="tau2_banking_1",
            conversation=[_turn("The Gold Rewards Card gives 2% cash back with no annual fee.")],
            expected_output=_expected_of("tau2_banking_1"))
    assert score == 0.7 and meta["measured"] == "proxy" and meta["recall_against"]


def test_retrieval_recall_none_when_no_docs():
    with patch("appsci_bench.eval.helpers.llm.complete") as mock_complete:
        score, meta = bk.retrieval_recall(data_point_id="x", conversation=[_turn("advice")],
                                          expected_output=_expected(required_documents=[]))
    assert score is None and meta["applicable"] is False
    mock_complete.assert_not_called()


# --- verification_compliance (make_transcript_judge-built) ------------------------------------

def test_verification_compliance_forwards_oracle_rubric_and_transcript():
    exp = _expected(reward_basis=["ACTION"], actions=[{"name": "transfer_to_human_agents"}])
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "verified before acting", "score": 0.9}') as mock_complete:
        score, meta = bk.verification_compliance(
            data_point_id="p1", conversation=[_turn("Can I verify your identity first?")],
            expected_output=exp, eval_rubric="Verify identity before any account action.")
    assert score == 0.9 and meta == {"reasoning": "verified before acting"}
    (messages, _), _ = mock_complete.call_args
    user_prompt = messages[-1]["content"]
    assert "Verify identity before any account action." in user_prompt   # eval_rubric forwarded
    assert "transfer_to_human_agents" in user_prompt                     # oracle (expected_json) forwarded
    assert "verify your identity" in user_prompt                         # transcript forwarded


def test_seeding_a_case_does_not_write_back_into_its_starting_state():
    """A run must not modify the case's declared starting point.

    The seed used to be inserted by reference, so a tool that wrote to a seeded record wrote through
    into the caller's `initial_state`: a credit limit seeded at $5,000.00 came back as $7500.00 with the
    card marked CLOSED, and replaying one trajectory twice produced two different state hashes -- which
    reads as an unwinnable case rather than as a bug. Production is safe only because every caller
    re-parses the oracle JSON per call; a test or script that reuses one dict is not.
    """
    import copy as _copy
    import json as _json
    from tasks.banking_support.environment import BankingEnvironment

    sample = pathlib.Path("tasks/banking_support/eval_data/tau2_banking_54/sample.json")
    oracle = _json.loads(_json.loads(sample.read_text())["expected_output"]["json"])
    actions, initial_state = oracle["actions"], oracle["initial_state"]
    pristine = _copy.deepcopy(initial_state)

    base = BankingEnvironment.from_fixture()
    env = base.fresh()
    env.set_read_log_allowlist(env.read_log_allowlist_for(actions))
    env.seed(initial_state)
    env.apply_actions(actions)

    assert initial_state == pristine, "the run mutated the case's own starting state"


def test_replaying_one_trajectory_twice_gives_the_same_state_hash():
    """The DB target must be deterministic, or no artifact can ever match it."""
    import copy as _copy
    import json as _json
    from tasks.banking_support.environment import BankingEnvironment

    sample = pathlib.Path("tasks/banking_support/eval_data/tau2_banking_54/sample.json")
    oracle = _json.loads(_json.loads(sample.read_text())["expected_output"]["json"])
    actions, initial_state = oracle["actions"], oracle["initial_state"]
    base = BankingEnvironment.from_fixture()

    def replay():
        env = base.fresh()
        env.set_read_log_allowlist(env.read_log_allowlist_for(actions))
        env.seed(initial_state)          # deliberately the SAME dict both times
        env.apply_actions(_copy.deepcopy(actions))
        return env.state_hash()

    assert replay() == replay()


# --- the score is task_success and nothing else ------------------------------------------------

def test_default_banking_evaluation_never_calls_disabled_diagnostics(tmp_path):
    from contextlib import ExitStack
    from appsci_bench.core.spec import load_task_spec
    from appsci_bench.eval.artifact_client import InProcessArtifactClient
    from appsci_bench.eval.artifact_agent_user_sim import ScriptedArtifactAgentUserSim
    from appsci_bench.eval.eval_system import run_evaluation

    oracle = _oracle("tau2_banking_1")
    golden = bk._golden_state_hash(oracle["actions"], oracle["initial_state"])
    with ExitStack() as stack:
        disabled = [stack.enter_context(patch.object(bk, name)) for name in (
            "groundedness", "retrieval_recall", "verification_compliance", "no_fabrication")]
        # safety_no_fabrication/safety_safe_by_construction (eval_safety.py) are always-on, unlike
        # the disabled diagnostics above -- stub them (not just patch out) so this test stays
        # creds-free and asserts they DO run, at a pinned score.
        always_on = [
            stack.enter_context(patch.object(bk_safety, "no_fabrication", return_value=(1.0, {}))),
            stack.enter_context(patch.object(bk_safety, "safe_by_construction", return_value=(1.0, {}))),
        ]
        report = run_evaluation(
            load_task_spec("banking_support"),
            InProcessArtifactClient(lambda q: ArtifactAgentOutput(text="Done.")),
            user_sim=ScriptedArtifactAgentUserSim(["Please apply for the card."]),
            read_final_state=lambda sample: {"env_state_hash": golden},
            limit=1, workdir=str(tmp_path),
        )
        for diagnostic in disabled:
            diagnostic.assert_not_called()
        for stub in always_on:
            stub.assert_called_once()
    assert report.final_score == 1.0
    assert {r.objective for r in report.results} == {
        "task_success", "safety_no_fabrication", "safety_safe_by_construction"}
    assert set(m["objective"] for m in report.views["per_metric"]) == {
        "task_success", "safety_no_fabrication", "safety_safe_by_construction"}
    assert all("references objective" not in w for w in report.elicitation.warnings)


def test_final_score_is_task_success_alone_under_the_declared_weights():
    """Pins the CONSEQUENCE of banking's weights, not just the numbers in the spec.

    task_success is tau2's own banking reward and the only figure comparable to a published tau2
    number, so the four diagnostics, when explicitly enabled, are computed and reported at weight 0.
    Asserting the weights
    (tests/appsci_bench/core/test_spec.py) says what the spec declares; running the real aggregator
    over the real objectives says what a report will actually contain. The judged scores here are
    deliberately far from task_success -- a blend would land nowhere near 0.25.
    """
    from appsci_bench.eval.objectives import load_objectives
    from appsci_bench.eval.aggregate import aggregate
    from appsci_bench.eval.datatypes import DataPointMetricResult
    import yaml

    spec_path = pathlib.Path(__file__).resolve().parents[3] / "tasks" / "banking_support" / "spec.yaml"
    objectives = load_objectives(yaml.safe_load(spec_path.read_text()), include_disabled=True)

    judged = {"groundedness": 1.0, "retrieval_recall": 1.0,
              "verification_compliance": 1.0, "no_fabrication": 1.0}
    results = []
    for i, passed in enumerate([1.0, 0.0, 0.0, 0.0]):     # task_success mean = 0.25
        dp = f"d{i}"
        results.append(DataPointMetricResult(data_point_id=dp, objective="task_success",
                                            repeat_index=0, score=passed))
        for name, score in judged.items():
            results.append(DataPointMetricResult(data_point_id=dp, objective=name,
                                                repeat_index=0, score=score))

    rep = aggregate("banking_support", results, objectives, reward_objective="task_success")

    assert rep.final_score == pytest.approx(0.25), (
        "final_score must equal task_success exactly; a judged axis is leaking into the score"
    )
    # The four are still REPORTED -- kept as diagnostics, just out of the score.
    per_metric = {pm["objective"]: pm for pm in rep.views["per_metric"]}
    assert set(per_metric) == {"task_success", *judged}
    assert all(per_metric[name]["score"] == pytest.approx(1.0) for name in judged)
    assert all(per_metric[name]["weight"] == pytest.approx(0.0) for name in judged)
    # Per-case rows follow the same rule, so a case's score is its pass/fail, not a blend.
    assert [row["score"] for row in rep.views["per_data_point"]] == [1.0, 0.0, 0.0, 0.0]
