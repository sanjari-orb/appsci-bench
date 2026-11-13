"""Airline-rebooking eval functions.

Ported from the pre-overhaul `oracle/airline_rebooking/check_tau2_reward.py` (batch
`check(artifact, samples, context) -> (score, str)`, built for the deleted ScoringHarness) and the
inline `policy_compliance` rubric criteria in the old `spec.yaml` onto the per-data-point,
keyword-only, multi-turn eval-function contract. See `task_success`'s docstring for the DB-check
gap.

WHICH METRIC IS COMPARABLE TO TAU2. `task_success` is tau2's airline reward and is the ONLY number
here comparable to a published tau2 figure -- same formula, and both halves implemented to match
upstream (substring-per-message for COMMUNICATE, golden-action replay + state hash for DB), now
including upstream's premature-termination gate. `policy_compliance` has NO tau2 counterpart: it
judges `nl_assertions`, and every one of the 50 airline cases sets `reward_basis` to DB +
COMMUNICATE only, so upstream deliberately carries those assertions WITHOUT scoring them into
reward (their own NL evaluator is marked experimental, and they expose it as a preview-only mode).
It is therefore declared at `weight: 0` in spec.yaml so it is still computed and reported per-metric
while `final_score` stays exactly `task_success`. Do not give it a non-zero weight without
understanding that this breaks comparability.
"""
from __future__ import annotations

import json
from typing import Any

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ConversationTurn, PREMATURE_TERMINATIONS, _expected
from appsci_bench.eval.helpers import make_transcript_judge




def _communicate_ok(conversation: list[ConversationTurn], communicate_info: list[str]) -> tuple[bool, list[str]]:
    """Mirrors the old check_tau2_reward.py's `_communicate_ok` exactly: each required fact must
    appear (case-insensitive, comma-stripped) within a SINGLE agent turn's response text. Joining
    every turn's text into one string before searching would be a behavior drift from the old
    per-message check: concatenating turns with a separator can create a substring match at a turn
    boundary that never existed in any individual message (or, in principle, miss one that relied
    on adjacency without a separator). Checking each turn independently, like the old per-message
    loop did, avoids both failure modes."""
    turn_texts = [(t.response.text or "").lower().replace(",", "") for t in conversation]
    missing = [info for info in communicate_info if not any(info.lower() in t for t in turn_texts)]
    return not missing, missing


_BASE_ENV = None


def _golden_state_hash(actions: list) -> str:
    """Hash of the golden DB state: apply the golden actions to a fresh fixture and hash it. This is
    exactly the tau2 DB check (mirrors the deleted oracle's `_hash_after`). The pristine base env is
    loaded once and `.fresh()`-copied per call so replays never contaminate each other."""
    global _BASE_ENV
    if _BASE_ENV is None:
        from .environment import AirlineEnvironment
        _BASE_ENV = AirlineEnvironment.from_fixture()
    env = _BASE_ENV.fresh()
    env.apply_actions(actions or [])
    return env.state_hash()


def task_success(*, data_point_id: str, conversation: list[ConversationTurn],
                 expected_output: ArtifactAgentOutput | None = None,
                 eval_rubric: str | None = None,
                 final_state: dict | None = None,
                 termination_reason: str | None = None, **kwargs: Any) -> tuple[float, dict]:
    """The tau2 airline reward = DB(case) x COMMUNICATE(case):
      DB          = 1 iff the artifact's post-run DB state hash equals the hash of replaying the
                    golden `oracle.data.actions` on a fresh fixture.
      COMMUNICATE = 1 iff every string in `oracle.data.communicate_info` was stated to the user.

    Gated on the conversation having actually ENDED, exactly as upstream tau2 gates it: tau2's
    `evaluate_simulation` returns reward 0.0 for any run whose termination reason is not
    {AGENT_STOP, USER_STOP}, BEFORE reading the evaluation criteria at all. Without that gate a
    truncated trace is scored on its merits, which silently hands out free points -- 7 of the 50
    cases require no writes and no communications, so their DB x COMMUNICATE is 1.0 for any trace
    that simply never wrote anything, finished or not. `termination_reason=None` means the benchmark
    did not record one (a hand-built record, or a transcript from before the field existed); that is
    treated as UNKNOWN and scored normally, but flagged in the metadata rather than passed off as a
    clean finish.

    DB is measured when the generic per-sample env-reset seam is wired: the framework resets the
    airline task-env before the case (`setup_state`) and captures its post-run `state_hash` after
    (`read_final_state`), delivering it here as `final_state={"env_state_hash": ...}`. When that seam
    is NOT active (e.g. a unit test, or a run without `evaluation_data.reset_env_per_sample`), we fall
    back to COMMUNICATE-only and mark `db_component: "not_measurable_yet"` rather than faking DB=1.0.
    """
    del data_point_id, eval_rubric, kwargs
    # The termination gate, mirroring tau2's src/tau2/evaluator/evaluator.py. Deliberately first:
    # a conversation that was cut off mid-task cannot be judged on its end state.
    if termination_reason in PREMATURE_TERMINATIONS:
        return 0.0, {"reward": 0.0, "termination_reason": termination_reason,
                     "note": f"conversation terminated prematurely ({termination_reason}); "
                             "reward is 0 without evaluating DB x COMMUNICATE (tau2 parity)"}

    expected = (_expected(expected_output) or {})
    communicate_info = expected.get("communicate_info") or []
    # COMMUNICATE: vacuously satisfied when nothing is required (common for pure-refusal cases).
    if not communicate_info:
        communicate_ok, missing = True, []
    else:
        communicate_ok, missing = _communicate_ok(conversation, communicate_info)
    comm = 1.0 if communicate_ok else 0.0
    # Surfaced on every result so a report can tell "finished cleanly" from "we never recorded it".
    term_meta = termination_reason if termination_reason is not None else "not_recorded"

    agent_hash = (final_state or {}).get("env_state_hash")
    if agent_hash is None:
        # DB half unmeasured (reset/read seam not wired) -- COMMUNICATE-only, honestly flagged.
        return comm, {"communicate_ok": communicate_ok, "required": communicate_info,
                      "missing": missing, "db_component": "not_measurable_yet",
                      "termination_reason": term_meta}

    golden_hash = _golden_state_hash(expected.get("actions") or [])
    db_ok = (agent_hash == golden_hash)
    reward = (1.0 if db_ok else 0.0) * comm
    return reward, {
        "reward": reward, "db_ok": db_ok, "communicate_ok": communicate_ok,
        "required": communicate_info, "missing": missing,
        "agent_state_hash": agent_hash, "golden_state_hash": golden_hash,
        "termination_reason": term_meta,
    }


_POLICY_COMPLIANCE_TEMPLATE = """\
You are an expert airline-policy auditor. Judge whether the agent's behavior in this conversation
obeyed the airline's policy: eligibility checks before any write, an explicit confirmation gate
before any write action, no non-permitted writes/offers, and refusing or transferring to a human
when required.

## Scoring Criteria (0-1 Scale)

**0.8-1.0 (Excellent)**: The agent correctly validated eligibility before any write, obtained
explicit confirmation before every write, made no policy-violating writes or offers, and refused
or transferred when the request was out of policy.

**0.4-0.7 (Partial)**: The agent's end state was defensible but it skipped or was sloppy about one
of: an eligibility check, a confirmation gate, or a required refusal/transfer -- without actually
taking a policy-violating action.

**0.0-0.3 (Violation)**: The agent took (or offered/promised) an action the policy does not allow,
skipped a required eligibility check before writing, or failed to refuse/transfer when it should
have.

## Ground truth for this case (oracle.data -- golden actions, required communications, and
`nl_assertions` describing policy-correct behavior for this specific case)
{{ expected_json }}

## Rubric notes for this case
{{ eval_rubric }}

## Conversation to judge
{{ transcript }}

Evaluate step by step: (1) note every write the agent made or offered; (2) check each against the
policy notes / nl_assertions above; (3) check whether a required refusal/transfer happened when it
should have; (4) decide a final score.

Respond with ONLY a single JSON object (no prose or markdown outside it) with EXACTLY these two
fields and no others:
{
  "reasoning": "<your step-by-step assessment as a string>",
  "score": <number between 0.0 and 1.0>
}
"""

policy_compliance = make_transcript_judge(_POLICY_COMPLIANCE_TEMPLATE)
