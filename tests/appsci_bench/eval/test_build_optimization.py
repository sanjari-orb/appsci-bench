"""Tests for appsci_bench/eval/build_optimization.py: the optimization loop, read out of a run.

Everything here is built from synthetic event logs in the shape the publisher actually writes -- a
tool invocation and its result sharing a span id, `build.start` in the ORCHESTRATOR's shard rather
than the agent's -- because the shape is what the reader depends on. The distinctions worth guarding
are the ones a bare count would lose: saturation versus a spent budget, a redeploy that failed versus
a cycle that deployed nothing, and "the route was off" versus "we cannot tell".
"""
from __future__ import annotations

import json
import pathlib

import pytest

from appsci_bench.eval.build_optimization import (
    AGENT_EVENTS_FILENAME,
    CHECKPOINT_MANIFEST_FILENAME,
    ORCHESTRATOR_EVENTS_FILENAME,
    read_optimization,
)
from appsci_bench.eval.datatypes import (
    OPTIMIZATION_STATUS_DISABLED,
    OPTIMIZATION_STATUS_NOT_ATTEMPTED,
    OPTIMIZATION_STATUS_SCORED,
    OPTIMIZATION_STATUS_UNMEASURABLE,
    STOP_REASON_BUDGET_EXHAUSTED,
    STOP_REASON_NOTHING_TO_FIX,
    STOP_REASON_RUN_ENDED_MID_CALL,
    STOP_REASON_SATURATED,
    STOP_REASON_SATURATED_AND_BUDGET,
    STOP_REASON_UNVERIFIED_CHANGE,
)

TASK = "order_to_cash"


def _ts(minute: int, second: int = 0) -> str:
    return f"2026-09-01T12:{minute:02d}:{second:02d}+00:00"


def _invocation(span: str, minute: int, tool: str = "optimize_agent") -> dict:
    return {"source": "appsci_agent", "event_type": "agent.tool_invocation", "span_id": span,
            "timestamp": _ts(minute), "task_id": TASK,
            "payload": {"tool": tool, "task": None, "question": None, "args": {}}}


def _result(span: str, minute: int, **payload) -> dict:
    """One tool result, defaulted to a plain measuring call that changed nothing."""
    body = {"success": True, "status": "optimizing", "iteration": 1, "headline_metric": 0.5,
            "saturated": False, "budget_exhausted": False, "cycles_used": 0, "cycles_allowed": 3,
            "cycle": {"changed": False}}
    body.update(payload)
    return _raw_result(span, minute, body)


def _refusal(span: str, minute: int, error: str, **payload) -> dict:
    """A call the tool refused. Its payload carries the failure and NOTHING else -- the trace only
    records what the result actually held, and a refusal holds no score."""
    return _raw_result(span, minute, {"success": False, "error": error, **payload})


def _raw_result(span: str, minute: int, payload: dict) -> dict:
    return {"source": "appsci_agent", "event_type": "agent.tool_result", "span_id": span,
            "timestamp": _ts(minute), "task_id": TASK, "payload": payload}


@pytest.fixture
def run_dir(tmp_path):
    """Build a run directory from agent events, orchestrator events and a checkpoint manifest."""
    def _build(agent_events: list[dict] | None = None, *,
               optimization_enabled: bool | None = True,
               optimize_cycles: int | None = 3,
               build_complete_minute: int | None = None,
               checkpoints: list[dict] | None = None,
               agent_log: str | None = None) -> pathlib.Path:
        run = tmp_path / "runs" / f"{TASK}_20260901_120000"
        run.mkdir(parents=True, exist_ok=True)
        if agent_log is not None:
            (run / AGENT_EVENTS_FILENAME).write_text(agent_log)
        elif agent_events is not None:
            (run / AGENT_EVENTS_FILENAME).write_text(
                "".join(json.dumps(e) + "\n" for e in agent_events))
        orchestrator: list[dict] = []
        if optimization_enabled is not None:
            payload = {"task_id": TASK, "optimization_enabled": optimization_enabled}
            if optimize_cycles is not None:
                payload["optimize_cycles"] = optimize_cycles
            orchestrator.append({"source": "orchestrator", "event_type": "build.start",
                                 "timestamp": _ts(0), "task_id": TASK, "payload": payload})
        if build_complete_minute is not None:
            orchestrator.append({"source": "orchestrator", "event_type": "build.complete",
                                 "timestamp": _ts(build_complete_minute), "task_id": TASK,
                                 "payload": {"status": "failed"}})
        if orchestrator:
            (run / ORCHESTRATOR_EVENTS_FILENAME).write_text(
                "".join(json.dumps(e) + "\n" for e in orchestrator))
        if checkpoints is not None:
            (run / CHECKPOINT_MANIFEST_FILENAME).write_text(json.dumps(
                {"trace_id": run.name, "checkpoints": checkpoints}))
        return run
    return _build


def _checkpoint(ordinal: int, outcome: str, created_at: str | None) -> dict:
    entry = {"ordinal": ordinal, "name": f"artifact-{ordinal:03d}", "outcome": outcome,
             "content_hash": f"h{ordinal}", "file_count": 3, "total_bytes": 100}
    if created_at is not None:
        entry["created_at"] = created_at
    return entry


# --- how many times the loop ran ------------------------------------------------------------------

def test_counts_every_optimize_agent_call_and_the_cycles_they_spent(run_dir):
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycles_used=0, cycle={"changed": True,
                                                                    "redeployed": True}),
        _invocation("s2", 3), _result("s2", 4, iteration=2, cycles_used=1,
                                      headline_metric=0.7, cycle={"changed": False}),
    ])
    result = read_optimization(run)

    assert result.status == OPTIMIZATION_STATUS_SCORED
    assert result.calls == 2
    assert result.scored_calls == 2
    assert result.failed_calls == 0
    # Cycles are NOT calls: only a call that changed the candidate spends one, which is why both
    # numbers are reported.
    assert result.cycles_used == 1
    assert result.cycles_allowed == 3
    assert result.configured_cycles_allowed == 3


def test_other_tools_are_not_counted_as_optimization_runs(run_dir):
    run = run_dir([
        _invocation("s1", 1, tool="deploy_artifact"), _result("s1", 2),
        _invocation("s2", 3, tool="mini_swe_agent"), _result("s2", 4),
    ])
    assert read_optimization(run).calls == 0


def test_task_id_comes_off_the_events_when_the_caller_names_none(run_dir):
    run = run_dir([_invocation("s1", 1), _result("s1", 2)])
    assert read_optimization(run).task_id == TASK
    assert read_optimization(run, task_id="override").task_id == "override"


def test_cycles_used_is_the_highest_any_call_reported_not_the_last(run_dir):
    """The final call of a run is the one most likely to have failed before reporting anything, and
    the tool's own counter only grows -- so a failed last call must not read as zero cycles spent."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycles_used=2),
        _invocation("s2", 3), _refusal("s2", 4, "the eval harness was modified during the cycle"),
    ])
    result = read_optimization(run)

    assert result.cycles_used == 2
    assert result.calls == 2
    assert result.failed_calls == 1
    # A call that measured nothing is not evidence about the candidate, so it is not a scored call.
    assert result.scored_calls == 1


def test_a_call_with_no_result_event_is_kept_as_unfinished(run_dir):
    """The run that crashed inside the loop. Dropping the call would under-count the thing being
    measured; counting it as successful would invent a measurement."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2),
        _invocation("s2", 3),
    ])
    result = read_optimization(run)

    assert result.calls == 2
    assert result.failed_calls == 1
    assert result.call_details[1].unfinished is True
    assert result.call_details[1].headline_metric is None
    assert any("no result event" in w for w in result.warnings)


def test_a_loop_whose_only_call_never_reported_has_an_unknown_cycle_count(run_dir):
    """Not zero. Zero would claim the loop changed nothing, which is exactly what is unknown here."""
    run = run_dir([_invocation("s1", 1)])
    result = read_optimization(run)

    assert result.calls == 1
    assert result.cycles_used is None
    assert result.saturated is None
    assert result.early_stopped_on_saturation is None


# --- why the loop stopped -------------------------------------------------------------------------

def test_saturation_with_budget_left_is_an_early_stop(run_dir):
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycles_used=1),
        _invocation("s2", 3), _result("s2", 4, status="saturated", saturated=True,
                                      budget_exhausted=False, cycles_used=2, cycles_allowed=5),
    ])
    result = read_optimization(run)

    assert result.saturated is True
    assert result.budget_exhausted is False
    assert result.stop_reason == STOP_REASON_SATURATED
    assert result.early_stopped_on_saturation is True


def test_a_spent_budget_is_not_saturation(run_dir):
    """The distinction the whole field exists for: running out of cycles establishes nothing about
    whether the candidate could still have improved."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, status="budget_exhausted", saturated=False,
                                      budget_exhausted=True, cycles_used=2, cycles_allowed=2),
    ])
    result = read_optimization(run)

    assert result.stop_reason == STOP_REASON_BUDGET_EXHAUSTED
    assert result.early_stopped_on_saturation is False


def test_both_conditions_get_their_own_reason(run_dir):
    """The tool's own status resolves the tie by priority (budget wins), so reading the reason back
    off it would either hide that the scores had flattened or claim it as the sole cause."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, status="budget_exhausted", saturated=True,
                                      budget_exhausted=True, cycles_used=2, cycles_allowed=2),
    ])
    result = read_optimization(run)

    assert result.stop_reason == STOP_REASON_SATURATED_AND_BUDGET
    # Saturation was not what ended it -- the budget would have, either way.
    assert result.early_stopped_on_saturation is False


def test_saturation_is_read_off_the_last_call_that_reported_it(run_dir):
    """An improving attempt clears saturation, so an earlier True must not stick."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, saturated=True),
        _invocation("s2", 3), _result("s2", 4, saturated=False, headline_metric=0.9),
    ])
    assert read_optimization(run).saturated is False


def test_nothing_to_fix_is_its_own_stopping_reason(run_dir):
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, status="nothing_to_fix", headline_metric=1.0),
    ])
    assert read_optimization(run).stop_reason == STOP_REASON_NOTHING_TO_FIX


def test_a_loop_that_ended_on_an_unmeasured_change_says_so(run_dir):
    """The weak point of verifying on the NEXT call: the submitted candidate is then one that no
    score in the run refers to."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, status="awaiting_verification",
                                      cycle={"changed": True, "redeployed": True}),
    ])
    assert read_optimization(run).stop_reason == STOP_REASON_UNVERIFIED_CHANGE


def test_an_unmeasured_change_is_not_hidden_by_a_spent_budget(run_dir):
    """The last cycle of a budget-bounded run, which is the common case rather than a corner: the tool
    recomputes the budget AFTER recording the attempt, so both states hold at once. Read budget-first,
    every one of those runs said `budget_exhausted` and lost the finding about the artifact that was
    actually submitted."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, status="awaiting_verification", cycles_used=3,
                                      budget_exhausted=True,
                                      cycle={"changed": True, "redeployed": True}),
    ])
    result = read_optimization(run)

    assert result.stop_reason == STOP_REASON_UNVERIFIED_CHANGE
    # Outranked, never discarded: the spent budget is still on the result, and the rollups that count
    # it read it from here rather than from the stopping reason.
    assert result.budget_exhausted is True


def test_an_unmeasured_change_is_not_hidden_by_saturation_either(run_dir):
    """Both flags at once, which the saturated-and-budget reason would otherwise claim. They are two
    readings of the trajectory; the unmeasured change is a claim about the candidate, and the tool's
    own status field resolves it the same way."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, status="awaiting_verification", saturated=True,
                                      cycles_used=3, budget_exhausted=True,
                                      cycle={"changed": True, "redeployed": True}),
    ])
    result = read_optimization(run)

    assert result.stop_reason == STOP_REASON_UNVERIFIED_CHANGE
    assert result.saturated is True and result.budget_exhausted is True


def test_a_run_that_died_inside_a_call_is_not_an_agent_that_moved_on(run_dir):
    """`still_optimizing` says the agent CHOSE to stop calling the tool, which is a finding about the
    agent. A run that crashed or timed out inside a call chose nothing, and the module already knows
    -- it warns about the missing result -- so the headline must not say the opposite of it."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2),
        _invocation("s2", 3),
    ])
    result = read_optimization(run)

    assert result.stop_reason == STOP_REASON_RUN_ENDED_MID_CALL
    assert any("no result event" in w for w in result.warnings)


def test_a_run_that_died_inside_a_call_claims_no_early_stop_on_saturation(run_dir):
    """The saturation flag then belongs to an EARLIER call, so the loop's stopping state is unknown
    rather than known. Left as a plain False->True read, a crashed run would be counted in the one
    rollup this field exists to support: how many runs stopped early with budget still unspent."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, saturated=True),
        _invocation("s2", 3),
    ])
    result = read_optimization(run)

    assert result.stop_reason == STOP_REASON_RUN_ENDED_MID_CALL
    assert result.saturated is True
    assert result.early_stopped_on_saturation is None


def test_no_ceiling_is_a_real_configuration_not_a_missing_value(run_dir):
    run = run_dir([_invocation("s1", 1), _result("s1", 2, cycles_allowed=None)],
                  optimize_cycles=None)
    result = read_optimization(run)

    assert result.cycles_allowed is None
    assert result.configured_cycles_allowed is None


def test_the_effective_ceiling_can_be_narrower_than_the_configured_one(run_dir):
    """A call may narrow the run's budget but never widen it, so the configured value is an upper
    bound on what was in force -- both are kept."""
    run = run_dir([_invocation("s1", 1), _result("s1", 2, cycles_allowed=1)], optimize_cycles=5)
    result = read_optimization(run)

    assert result.cycles_allowed == 1
    assert result.configured_cycles_allowed == 5


# --- the deploys the loop attempted ---------------------------------------------------------------

def test_deploy_tally_separates_a_failed_rebuild_from_a_cycle_that_changed_nothing(run_dir):
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycle={"changed": True, "redeployed": True}),
        _invocation("s2", 3), _result("s2", 4, cycle={"changed": True, "redeployed": False,
                                                      "deploy_error": "docker build failed: "
                                                                      "no module named pandas",
                                                      "restored": True}),
        _invocation("s3", 5), _result("s3", 6, cycle={"changed": False}),
    ])
    result = read_optimization(run)

    assert result.deploys_attempted == 2
    assert result.deploys_succeeded == 1
    assert result.deploys_failed == 1
    # A cycle that changed nothing attempted no deploy: recording False there would count a deploy
    # that was never tried as one that failed.
    assert result.call_details[2].redeployed is None
    assert "docker build failed" in result.call_details[1].deploy_error


def test_an_unbounded_deploy_error_is_truncated(run_dir):
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycle={"changed": True, "redeployed": False,
                                                      "deploy_error": "x" * 5000}),
    ])
    # The result is embedded in an eval report a browser renders; a Docker build log must not ride in.
    assert len(read_optimization(run).call_details[0].deploy_error) <= 500


# --- the loop's own eval scores -------------------------------------------------------------------

def test_the_trajectory_is_first_best_and_final(run_dir):
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, headline_metric=0.40),
        _invocation("s2", 3), _result("s2", 4, headline_metric=0.55),
        _invocation("s3", 5), _result("s3", 6, headline_metric=0.90),
    ])
    result = read_optimization(run)

    assert result.internal_eval_first == pytest.approx(0.40)
    assert result.internal_eval_best == pytest.approx(0.90)
    assert result.internal_eval_best_call == 3
    assert result.internal_eval_final == pytest.approx(0.90)
    assert result.internal_eval_change == pytest.approx(0.50)


def test_a_loop_can_end_below_its_own_best(run_dir):
    """The keep-or-roll-back rule compares against the PREVIOUS measurement, not the best one, so
    final and best genuinely differ and reporting only final would hide it."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, headline_metric=0.90),
        _invocation("s2", 3), _result("s2", 4, headline_metric=0.40),
        _invocation("s3", 5), _result("s3", 6, headline_metric=0.60),
    ])
    result = read_optimization(run)

    assert result.internal_eval_best == pytest.approx(0.90)
    assert result.internal_eval_best_call == 1
    assert result.internal_eval_final == pytest.approx(0.60)
    assert result.internal_eval_change == pytest.approx(-0.30)


# --- did it end on a worse candidate, or just re-measure the same one? ----------------------------
#
# `internal_eval_final < internal_eval_best` is NOT the question. The keep rule only accepts an
# improvement, so across the changes the loop made the incumbent's score cannot fall. What falls is the
# measurement: the loop's own eval is not deterministic. Four of the six runs the bare comparison
# flagged in the optimize_cycles5 sweep had never changed the bytes at all.

def _scored_on(span: str, minute: int, score: float, checkpoint: int) -> dict:
    """A call that measured the candidate captured as checkpoint `checkpoint`."""
    return _result(span, minute, headline_metric=score,
                   checkpoint={"name": f"artifact-{checkpoint:03d}", "ordinal": checkpoint})


def test_ending_lower_on_the_same_bytes_is_not_ending_below_best(run_dir):
    """Two checkpoints of the SAME program -- which is what a roll-back leaves -- so the drop is the
    eval re-measuring one artifact, and nothing about the loop went backwards."""
    run = run_dir(
        [
            _invocation("s1", 1), _scored_on("s1", 2, 0.90, 1),
            _invocation("s2", 3), _scored_on("s2", 4, 0.89, 2),
        ],
        # Same content hash under two names: `_checkpoint` numbers them `h1`, `h2`, so this run
        # overrides the second to repeat the first.
        checkpoints=[_checkpoint(1, "deployed", _ts(2)),
                     {**_checkpoint(2, "deployed", _ts(4)), "content_hash": "h1"}],
    )
    result = read_optimization(run)

    assert result.internal_eval_final < result.internal_eval_best
    assert result.ended_below_best is False


def test_ending_lower_on_different_bytes_is_ending_below_best(run_dir):
    run = run_dir(
        [
            _invocation("s1", 1), _scored_on("s1", 2, 0.90, 1),
            _invocation("s2", 3), _scored_on("s2", 4, 0.40, 2),
        ],
        checkpoints=[_checkpoint(1, "deployed", _ts(2)), _checkpoint(2, "deployed", _ts(4))],
    )
    result = read_optimization(run)

    assert result.ended_below_best is True
    assert result.internal_eval_best_checkpoint == "artifact-001"


def test_a_loop_that_did_not_end_below_its_best_says_so_rather_than_nothing(run_dir):
    """False, not None: "it did not happen" and "we could not tell" are different answers."""
    run = run_dir([
        _invocation("s1", 1), _scored_on("s1", 2, 0.40, 1),
        _invocation("s2", 3), _scored_on("s2", 4, 0.90, 2),
    ])

    assert read_optimization(run).ended_below_best is False


def test_candidates_that_cannot_be_compared_warn_about_neither(run_dir):
    """None, so nothing renders. A score with no checkpoint reference cannot say which artifact it
    belongs to, and guessing would put a warning on a run nobody measured that way."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, headline_metric=0.90),
        _invocation("s2", 3), _result("s2", 4, headline_metric=0.40),
    ], checkpoints=[_checkpoint(1, "deployed", _ts(2))])
    result = read_optimization(run)

    assert result.internal_eval_final < result.internal_eval_best
    assert result.ended_below_best is None


def test_no_manifest_leaves_the_below_best_question_unanswered(run_dir):
    run = run_dir([
        _invocation("s1", 1), _scored_on("s1", 2, 0.90, 1),
        _invocation("s2", 3), _scored_on("s2", 4, 0.40, 2),
    ])

    assert read_optimization(run).ended_below_best is None


# --- deploys the agent made around the loop -------------------------------------------------------

def test_a_deploy_between_two_calls_is_counted_as_outside_the_loop(run_dir):
    """The mechanism that actually loses a high-water candidate: the keep rule restores a snapshot the
    LOOP took, so an artifact the agent deploys itself has none and cannot be rolled back."""
    run = run_dir(
        [
            _invocation("s1", 5), _result("s1", 7),
            _invocation("s2", 15), _result("s2", 17),
        ],
        checkpoints=[
            _checkpoint(1, "deployed", _ts(1)),    # before the loop: the build-phase deploy
            _checkpoint(2, "deployed", _ts(6)),    # inside call 1
            _checkpoint(3, "deployed", _ts(11)),   # between the calls: the agent's own
            _checkpoint(4, "deployed", _ts(12)),   # and again
        ],
        build_complete_minute=30,
    )
    result = read_optimization(run)

    assert result.deploys_before_loop == 1
    assert result.deploys_outside_loop == 2
    assert result.checkpoints_valid == 1
    assert result.checkpoints_in_run == 4


def test_the_build_phase_deploy_is_not_counted_as_working_around_the_loop(run_dir):
    """It is how the candidate got running in the first place, not the agent going around anything."""
    run = run_dir(
        [_invocation("s1", 5), _result("s1", 7)],
        checkpoints=[_checkpoint(1, "deployed", _ts(1)), _checkpoint(2, "deployed", _ts(6))],
        build_complete_minute=30,
    )
    result = read_optimization(run)

    assert result.deploys_before_loop == 1
    assert result.deploys_outside_loop == 0


def test_the_earliest_call_wins_a_tie_for_best(run_dir):
    """A plateau is attributed to where it started, not to the last call that repeated it."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, headline_metric=0.70),
        _invocation("s2", 3), _result("s2", 4, headline_metric=0.70),
    ])

    assert read_optimization(run).internal_eval_best_call == 1


def test_one_measurement_reports_no_change(run_dir):
    """0.0 there would read as "measured twice and nothing moved"."""
    run = run_dir([_invocation("s1", 1), _result("s1", 2, headline_metric=0.70)])
    result = read_optimization(run)

    assert result.internal_eval_first == pytest.approx(0.70)
    assert result.internal_eval_final == pytest.approx(0.70)
    assert result.internal_eval_change is None


def test_a_refused_call_does_not_enter_the_trajectory_as_a_zero(run_dir):
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, headline_metric=0.80),
        _invocation("s2", 3), _refusal("s2", 4, "the harness was tampered with"),
    ])
    result = read_optimization(run)

    # A refusal measured nothing, so the loop's last measurement is still 0.80 and nothing collapsed.
    assert result.internal_eval_final == pytest.approx(0.80)
    assert result.internal_eval_best == pytest.approx(0.80)
    assert result.internal_eval_change is None


def test_an_unfinished_call_does_not_enter_the_trajectory_as_a_zero(run_dir):
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, headline_metric=0.80),
        _invocation("s2", 3),
    ])
    result = read_optimization(run)

    assert result.call_details[1].unfinished is True
    assert result.internal_eval_final == pytest.approx(0.80)


def test_a_loop_that_never_reported_a_score_has_no_trajectory(run_dir):
    """None, not 0.0: a loop nobody could score and a loop that scored zero are different findings."""
    run = run_dir([_invocation("s1", 1), _refusal("s1", 2, "no endpoint")])
    result = read_optimization(run)

    assert result.internal_eval_first is None
    assert result.internal_eval_best is None
    assert result.internal_eval_best_call is None
    assert result.internal_eval_final is None
    assert result.internal_eval_change is None
    assert result.internal_eval_all_scores == []


def test_every_score_is_carried_in_order_beside_the_summary(run_dir):
    """The list and the four summary fields come off one pass, so they cannot disagree."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, headline_metric=0.40),
        _invocation("s2", 3), _result("s2", 4, headline_metric=0.90),
        _invocation("s3", 5), _result("s3", 6, headline_metric=0.60),
    ])
    result = read_optimization(run)

    assert result.internal_eval_all_scores == [pytest.approx(0.40), pytest.approx(0.90),
                                               pytest.approx(0.60)]
    assert result.internal_eval_all_scores[0] == result.internal_eval_first
    assert result.internal_eval_all_scores[-1] == result.internal_eval_final
    assert max(result.internal_eval_all_scores) == result.internal_eval_best


def test_the_score_list_holds_only_calls_that_measured(run_dir):
    """As long as `scored_calls`, not as long as `calls`: a refusal measured nothing, and a zero for
    it would put a collapse on the trajectory that never happened."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, headline_metric=0.80),
        _invocation("s2", 3), _refusal("s2", 4, "the harness was tampered with"),
        _invocation("s3", 5),
    ])
    result = read_optimization(run)

    assert result.calls == 3
    assert result.scored_calls == 1
    assert result.internal_eval_all_scores == [pytest.approx(0.80)]


# --- what the loop did with its own changes -------------------------------------------------------

def _verdict(**fields) -> dict:
    """A `verified_previous` block in the shape `optimize_agent._resolve_pending` returns."""
    body = {"score_to_beat": 0.5, "new_score": 0.6}
    body.update(fields)
    return body


def test_a_kept_change_is_counted_as_a_verdict_not_a_call(run_dir):
    """A change is judged by the NEXT call, so the first call reaches no verdict at all."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycle={"changed": True, "redeployed": True}),
        _invocation("s2", 3), _result("s2", 4, cycle={
            "changed": False, "verified_previous": _verdict(accepted=True, reverted=False)}),
    ])
    result = read_optimization(run)

    assert result.call_details[0].previous_change_kept is None
    assert result.call_details[1].previous_change_kept is True
    assert result.call_details[1].rollback is None
    assert (result.changes_kept, result.changes_rolled_back) == (1, 0)
    assert (result.rollback_redeploys, result.rollbacks_failed) == (0, 0)


def test_a_rolled_back_change_is_counted_with_its_redeploy(run_dir):
    """The restore is a redeploy that is NOT a change, which is the whole reason it is counted apart
    from `deploys_attempted`."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycle={"changed": True, "redeployed": True}),
        _invocation("s2", 3), _result("s2", 4, cycle={
            "changed": False,
            "verified_previous": _verdict(accepted=False, reverted=True, revert="reverted")}),
    ])
    result = read_optimization(run)

    assert result.call_details[1].previous_change_kept is False
    assert result.call_details[1].rollback == "reverted"
    assert (result.changes_kept, result.changes_rolled_back) == (0, 1)
    assert (result.rollback_redeploys, result.rollbacks_failed) == (1, 0)
    # The change itself is still one deploy: the roll-back is the second one, and folding them
    # together would make the deploy tally stop pairing with the cycle count.
    assert result.deploys_attempted == 1


@pytest.mark.parametrize("outcome", ["restore_failed", "redeploy_failed", "no_snapshot"])
def test_a_rollback_that_did_not_take_is_counted_apart(run_dir, outcome):
    """These three leave the code the loop DECIDED AGAINST running, which is a fact about the
    artifact the run went on to submit rather than about the bookkeeping."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycle={"changed": True, "redeployed": True}),
        _invocation("s2", 3), _result("s2", 4, cycle={
            "changed": False,
            "verified_previous": _verdict(accepted=False, reverted=False, revert=outcome)}),
    ])
    result = read_optimization(run)

    assert result.call_details[1].rollback == outcome
    assert result.changes_rolled_back == 1
    assert result.rollback_redeploys == 0
    assert result.rollbacks_failed == 1


def test_the_two_rollback_outcomes_always_sum_to_the_rollbacks(run_dir):
    """An outcome that belonged to neither bucket would silently shrink both counts."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycle={"changed": True, "redeployed": True}),
        _invocation("s2", 3), _result("s2", 4, cycle={
            "changed": True, "redeployed": True,
            "verified_previous": _verdict(accepted=False, reverted=True, revert="reverted")}),
        _invocation("s3", 5), _result("s3", 6, cycle={
            "changed": False,
            "verified_previous": _verdict(accepted=False, reverted=False, revert="no_snapshot")}),
    ])
    result = read_optimization(run)

    assert result.changes_rolled_back == result.rollback_redeploys + result.rollbacks_failed == 2


def test_a_rollback_with_no_word_is_still_counted_as_one_that_took(run_dir):
    """`reverted: true` is the fact; `revert` only says how it went. A trace missing the word must
    not drop the roll-back, and must not report it as one that failed."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycle={"changed": True, "redeployed": True}),
        _invocation("s2", 3), _result("s2", 4, cycle={
            "changed": False, "verified_previous": _verdict(accepted=False, reverted=True)}),
    ])
    result = read_optimization(run)

    assert result.call_details[1].rollback == "reverted"
    assert (result.rollback_redeploys, result.rollbacks_failed) == (1, 0)


def test_an_unknown_rollback_word_is_not_invented_into_a_count(run_dir):
    """A word this module does not know would land in a rendered count as an unexplained category."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycle={"changed": True, "redeployed": True}),
        _invocation("s2", 3), _result("s2", 4, cycle={
            "changed": False, "verified_previous": _verdict(revert="something_new")}),
    ])
    result = read_optimization(run)

    assert result.call_details[1].previous_change_kept is None
    assert result.call_details[1].rollback is None
    assert (result.changes_kept, result.changes_rolled_back) == (0, 0)


def test_a_call_with_nothing_pending_reaches_no_verdict(run_dir):
    """None rather than False: "no change to judge" is not "the change was rejected"."""
    run = run_dir([
        _invocation("s1", 1), _result("s1", 2, cycle={"changed": False}),
        _invocation("s2", 3), _result("s2", 4, cycle={"changed": False,
                                                      "verified_previous": None}),
        _invocation("s3", 5), _raw_result("s3", 6, {"success": True, "status": "optimizing"}),
    ])
    result = read_optimization(run)

    assert [c.previous_change_kept for c in result.call_details] == [None, None, None]
    assert (result.changes_kept, result.changes_rolled_back) == (0, 0)


def test_the_deploy_and_checkpoint_tallies_reconcile_once_rollbacks_are_counted(run_dir):
    """The claim these counts exist to support: a restore is a redeploy, so the checkpoint it leaves
    is explained by `deploys_succeeded + rollback_redeploys` and not by the deploy tally alone."""
    run = run_dir(
        [
            _invocation("s1", 1), _result("s1", 3, cycle={"changed": True, "redeployed": True}),
            _invocation("s2", 5), _result("s2", 7, cycle={
                "changed": False,
                "verified_previous": _verdict(accepted=False, reverted=True, revert="reverted")}),
        ],
        checkpoints=[_checkpoint(1, "deployed", _ts(2)), _checkpoint(2, "deployed", _ts(6))],
        build_complete_minute=20,
    )
    result = read_optimization(run)

    assert result.deploys_succeeded == 1
    assert result.rollback_redeploys == 1
    assert result.checkpoints_valid == 2
    assert result.deploys_succeeded + result.rollback_redeploys == result.checkpoints_valid


def test_the_rollback_vocabulary_is_the_tools_own(run_dir):
    """Spelled in two places, so the two are checked against each other rather than assumed equal: a
    word renamed in the tool would otherwise make every roll-back read as unknown."""
    from appsci_agent.tools import optimize_agent as tool
    from appsci_bench.eval.datatypes import ROLLBACK_OUTCOMES

    source = pathlib.Path(tool.__file__).read_text()
    for word in ROLLBACK_OUTCOMES:
        assert f'"{word}"' in source, f"{word} is no longer a word optimize_agent uses"


# --- the checkpoints the loop left behind ---------------------------------------------------------

def test_only_checkpoints_created_while_a_call_was_in_flight_are_attributed_to_it(run_dir):
    """A checkpoint records the deploy, not what asked for it -- so the build-phase deploy before the
    loop started, and anything after it ended, must stay out of the count."""
    run = run_dir(
        [
            _invocation("s1", 5), _result("s1", 7, cycle={"changed": True, "redeployed": True}),
            _invocation("s2", 9), _result("s2", 11, cycle={"changed": True, "redeployed": False}),
        ],
        checkpoints=[
            _checkpoint(1, "deployed", _ts(2)),       # the build's own first deploy -- before the loop
            _checkpoint(2, "deployed", _ts(6)),       # inside call 1
            _checkpoint(3, "failed", _ts(10)),        # inside call 2
            _checkpoint(4, "unhealthy", _ts(10, 30)),  # also inside call 2
            _checkpoint(5, "deployed", _ts(20)),      # after the loop -- a later hand deploy
        ],
    )
    result = read_optimization(run)

    assert result.checkpoints_valid == 1
    assert result.checkpoints_failed == 1
    assert result.checkpoints_unhealthy == 1
    assert result.checkpoints_in_run == 5


def test_no_manifest_leaves_the_counts_unset_with_a_reason(run_dir):
    """A zero would claim the loop produced no durable candidate. "This run keeps no checkpoints" is
    a different statement, and it is the true one here."""
    run = run_dir([_invocation("s1", 1), _result("s1", 2)])
    result = read_optimization(run)

    assert result.checkpoints_valid is None
    assert result.checkpoints_in_run is None
    assert "no checkpoint manifest" in result.checkpoint_note


def test_a_checkpoint_with_no_readable_time_is_reported_not_guessed_at(run_dir):
    run = run_dir(
        [_invocation("s1", 5), _result("s1", 7, cycle={"changed": True, "redeployed": True})],
        checkpoints=[_checkpoint(1, "deployed", _ts(6)),
                     _checkpoint(2, "deployed", None),
                     _checkpoint(3, "deployed", "not a timestamp")],
    )
    result = read_optimization(run)

    assert result.checkpoints_valid == 1
    assert any("no readable creation time" in w for w in result.warnings)


def test_a_naive_timestamp_is_not_assumed_to_be_utc(run_dir):
    """Both writers emit timezone-aware UTC. A naive value is an unexpected shape, and assuming a
    zone for it would invent the fact the attribution turns on."""
    run = run_dir(
        [_invocation("s1", 5), _result("s1", 7, cycle={"changed": True, "redeployed": True})],
        checkpoints=[_checkpoint(1, "deployed", "2026-09-01T12:06:00")],
    )
    result = read_optimization(run)

    assert result.checkpoints_valid == 0
    assert any("no readable creation time" in w for w in result.warnings)


def test_a_checkpoint_from_a_call_that_never_reported_back_is_still_attributed(run_dir):
    """The run died inside the loop after its redeploy. Attributing that candidate to the loop that
    was running is a better reading than dropping the last thing it produced -- bounded by the end of
    the build, which the orchestrator reports however the build ended."""
    run = run_dir(
        [_invocation("s1", 5)],
        build_complete_minute=15,
        checkpoints=[_checkpoint(1, "deployed", _ts(6))],
    )
    result = read_optimization(run)

    assert result.checkpoints_valid == 1


def test_a_deploy_after_the_build_ended_is_not_the_loops(run_dir):
    """An unfinished call's window is bounded by the end of the build, so a checkpoint written after
    the build cannot be attributed to the loop that was running inside it."""
    run = run_dir(
        [_invocation("s1", 5)],
        build_complete_minute=10,
        checkpoints=[_checkpoint(1, "deployed", _ts(12))],
    )
    result = read_optimization(run)

    assert result.checkpoints_valid == 0
    assert result.checkpoints_in_run == 1


def test_a_run_with_no_optimize_call_attributes_no_checkpoint_to_optimization(run_dir):
    run = run_dir([], checkpoints=[_checkpoint(1, "deployed", _ts(6))])
    result = read_optimization(run)

    assert result.checkpoints_valid is None
    assert "no optimize_agent call ran" in result.checkpoint_note


# --- the four statuses ----------------------------------------------------------------------------

def test_the_ablation_arm_reports_the_route_as_off(run_dir):
    run = run_dir([], optimization_enabled=False)
    result = read_optimization(run)

    assert result.status == OPTIMIZATION_STATUS_DISABLED
    assert result.optimization_enabled is False
    assert result.calls == 0


def test_an_agent_that_had_the_route_and_never_called_it_is_a_real_zero(run_dir):
    run = run_dir([_invocation("s1", 1, tool="complete"), _result("s1", 2)],
                  optimization_enabled=True)
    result = read_optimization(run)

    assert result.status == OPTIMIZATION_STATUS_NOT_ATTEMPTED
    assert result.optimization_enabled is True


def test_a_trace_that_does_not_say_whether_the_route_existed_is_unmeasurable(run_dir):
    """Every run that predates `build.start` carrying the flag is in this case, and there an ablation
    arm and an agent that never called the tool are indistinguishable. Picking one would turn a guess
    into a statistic."""
    run = run_dir([], optimization_enabled=None)
    result = read_optimization(run)

    assert result.status == OPTIMIZATION_STATUS_UNMEASURABLE
    assert result.optimization_enabled is None
    assert any("cannot be told apart" in w for w in result.warnings)


def test_calls_are_counted_even_when_the_flag_is_missing(run_dir):
    """A call is direct evidence the route existed, so it outranks a trace that does not say."""
    run = run_dir([_invocation("s1", 1), _result("s1", 2)], optimization_enabled=None)
    result = read_optimization(run)

    assert result.status == OPTIMIZATION_STATUS_SCORED
    assert result.calls == 1


def test_a_missing_agent_log_is_unmeasurable_not_zero(tmp_path):
    result = read_optimization(tmp_path / "nope")

    assert result.status == OPTIMIZATION_STATUS_UNMEASURABLE
    assert result.calls == 0
    assert any("could not be read" in w for w in result.warnings)


def test_a_corrupt_agent_log_is_unmeasurable(run_dir):
    """A count taken over the half of a log that happened to parse reads exactly like a complete one."""
    run = run_dir(agent_log=json.dumps(_invocation("s1", 1)) + "\n{not json\n")
    result = read_optimization(run)

    assert result.status == OPTIMIZATION_STATUS_UNMEASURABLE
    assert any("corrupt" in w for w in result.warnings)


def test_a_corrupt_orchestrator_log_does_not_cost_the_counts(run_dir):
    """It only carries the route's configuration; every count comes off the agent's log."""
    run = run_dir([_invocation("s1", 1), _result("s1", 2)])
    (run / ORCHESTRATOR_EVENTS_FILENAME).write_text("{not json\n")
    result = read_optimization(run)

    assert result.status == OPTIMIZATION_STATUS_SCORED
    assert result.calls == 1
    assert result.optimization_enabled is None


def test_the_result_round_trips_through_its_dict_form(run_dir):
    from appsci_bench.eval.datatypes import OptimizationResult

    run = run_dir(
        [_invocation("s1", 5), _result("s1", 7, cycle={"changed": True, "redeployed": True})],
        checkpoints=[_checkpoint(1, "deployed", _ts(6))],
    )
    result = read_optimization(run)
    again = OptimizationResult.from_dict(json.loads(json.dumps(result.to_dict())))

    assert again.to_dict() == result.to_dict()
