"""The build phase's optimization loop, read out of a finished run.

The agent's optimization route (`appsci_agent/tools/optimize_agent.py`) runs a measure-then-improve
loop over the candidate it built: each call scores the deployed artifact on the agent's own internal
eval, then hands the failing cases to a sub-agent that edits and redeploys. Six things about that
loop are worth knowing after the fact and none of them were countable before this module:

  * how many times the loop ran;
  * whether it stopped early because the scores had stopped moving (saturation), or because it ran
    out of the cycle budget -- which are different findings and must never be collapsed (a run that
    ran out of budget at cycle 2 established nothing about whether the candidate could improve);
  * how many of the redeploys the loop attempted actually produced a running artifact, and how many
    failed to build;
  * how many of the loop's own changes it then KEPT and how many it rolled back. A roll-back restores
    the previous candidate and redeploys it, which is a redeploy that is not a change -- so it leaves
    a checkpoint that the deploy counts do not explain, and counting it is what lets the two tallies
    be reconciled instead of merely noted;
  * how many DURABLE candidates the loop left behind -- checkpoints with a `deployed` outcome, which
    are the only copies of the artifacts it produced;
  * where the loop's OWN score started, how high it got and where it ended. That is the quantity the
    loop is steering, and the best and the final are kept apart because they can differ: the
    keep-or-roll-back rule compares against the previous measurement, not the best one.

READ, NOT JUDGED. Nothing here calls a model, so this costs nothing and is deterministic: every
number is counted out of records the run already wrote. That is what makes it re-runnable over runs
that finished long before the metric existed (`scripts/score_optimization.py`).

THREE SOURCES, EACH FOR WHAT IT AUTHORITATIVELY KNOWS. All three are per-run files the event store
already writes; none of them is re-derived here.

`events-appsci_agent.jsonl` is the agent's own trace. Every `optimize_agent` call appears as an
invocation event and a result event sharing one span id, and the result carries what the tool
nominated for the trace (`OptimizeAgentTool.trace_output_keys`): its status, the score, the
saturation state, the cycle counters, and the cycle record saying whether a change was made and
whether the redeploy of it succeeded. So the loop's shape and its stopping reason come from here.

`events-orchestrator.jsonl` carries `build.start`, and with it the one thing the agent's log cannot
say: whether this run had an optimization route at all. The event store is sharded by source, so that
record is in the orchestrator's log rather than the agent's -- and without it a run built without the
route (the ablation arm) is indistinguishable from an agent that had the tool and never called it.

`checkpoints_manifest.json` is the HOST's record of every deploy attempt: the orchestrator writes one
checkpoint per attempt with its outcome (`deployed` / `unhealthy` / `failed`). Whether a candidate
survived as a durable copy is only knowable from there -- the agent cannot see its own checkpoints
directory, by design.

WHY CHECKPOINTS ARE ATTRIBUTED BY TIME. A checkpoint records the deploy, not what asked for it, so
nothing in the manifest says "the optimization loop caused this one". A run's manifest also holds the
build-phase deploys that happened before the loop started. They are told apart by asking whether a
checkpoint was created while an `optimize_agent` call was in flight: a cycle's redeploy happens inside
the call that made the change, so its checkpoint lands between that call's invocation and result
events. Both clocks are UTC on the same host (the agent runs in a container on it), so the comparison
is sound -- and every checkpoint the window cannot decide on is counted as unattributed and reported,
rather than being silently assigned to one side.

The deliberate gap: a run whose optimization loop ran but whose trace is missing reports
`unmeasurable`, never zero. "The loop never ran" and "we cannot see whether it ran" are opposite
findings about the route being measured.
"""
from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime
from typing import Any

from .datatypes import (
    OPTIMIZATION_STATUS_DISABLED,
    OPTIMIZATION_STATUS_NOT_ATTEMPTED,
    OPTIMIZATION_STATUS_SCORED,
    OPTIMIZATION_STATUS_UNMEASURABLE,
    ROLLBACK_OUTCOMES,
    ROLLBACK_REVERTED,
    STOP_REASON_BUDGET_EXHAUSTED,
    STOP_REASON_NOTHING_TO_FIX,
    STOP_REASON_RUN_ENDED_MID_CALL,
    STOP_REASON_SATURATED,
    STOP_REASON_SATURATED_AND_BUDGET,
    STOP_REASON_STILL_OPTIMIZING,
    STOP_REASON_UNVERIFIED_CHANGE,
    OptimizationCall,
    OptimizationResult,
)

logger = logging.getLogger(__name__)

#: The agent's own event log inside a run directory (one writer, append-only). Holds the tool calls.
AGENT_EVENTS_FILENAME = "events-appsci_agent.jsonl"

#: The orchestrator's log, in the same directory. The event store is sharded by SOURCE, and
#: `build.start` -- the only record of whether this run had an optimization route at all -- is emitted
#: by the orchestrator, not the agent. Reading one log and not the other is what made an ablation arm
#: indistinguishable from an agent that simply never called the tool.
ORCHESTRATOR_EVENTS_FILENAME = "events-orchestrator.jsonl"

#: The host's per-deploy checkpoint manifest, written beside the run's `checkpoints/` directory. Named
#: here rather than imported from `appsci_bench.orchestrator.checkpoint` so this module stays readable
#: from a bare run directory without pulling in the orchestrator (and its Docker dependencies).
CHECKPOINT_MANIFEST_FILENAME = "checkpoints_manifest.json"

#: Checkpoint outcomes, as `orchestrator/checkpoint.py` records them. `deployed` is the only one that
#: means a usable candidate was captured: `unhealthy` answered its port but failed its health route,
#: and `failed` never came up at all.
OUTCOME_DEPLOYED = "deployed"
OUTCOME_UNHEALTHY = "unhealthy"
OUTCOME_FAILED = "failed"

#: The three roll-back outcomes that left the rejected code running. Derived from the vocabulary
#: rather than written out again, so a fifth outcome added to the tool cannot be silently missed by
#: one of the two lists.
_ROLLBACK_DID_NOT_TAKE = tuple(o for o in ROLLBACK_OUTCOMES if o != ROLLBACK_REVERTED)

#: The tool whose calls this module counts, and the two event types that bracket one call.
OPTIMIZE_TOOL = "optimize_agent"
_INVOCATION_EVENT = "agent.tool_invocation"
_RESULT_EVENT = "agent.tool_result"
#: Carries the build-phase configuration, including whether the optimization route existed at all.
_BUILD_START_EVENT = "build.start"
#: Emitted however the build ended -- submission, crash or timeout. Bounds a call that never
#: reported back, so a deploy from after the build cannot be attributed to the loop.
_BUILD_COMPLETE_EVENT = "build.complete"

#: Ceiling on a deploy error copied onto a call record. The tool's own error text is unbounded (it can
#: carry a Docker build log), and this result is embedded in an eval report that a browser renders.
MAX_ERROR_CHARS = 500


def read_optimization(run_dir: str | pathlib.Path, *, task_id: str = "") -> OptimizationResult:
    """Summarize one run's optimization loop from the records it left on disk.

    Args:
        run_dir: A finished run directory (``runs/<trace_id>``), holding the agent's event log and,
            for a run that went through the orchestrator, the checkpoint manifest.
        task_id: Recorded on the result so a bare `optimization.json` says what it is about. Left
            empty, it is taken from the run's own events -- which is what a standalone re-scoring
            pass over old runs wants, since nobody is there to say which task each one was.

    Returns:
        An :class:`OptimizationResult`. Never raises: every unreadable input becomes a status plus a
        warning, because a diagnostic must not be able to fail a run or a batch sweep.
    """
    try:
        return _read(pathlib.Path(run_dir), task_id=task_id)
    except Exception as e:  # noqa: BLE001 -- a diagnostic must never be the thing that fails
        logger.warning("could not read the optimization diagnostic from %s", run_dir, exc_info=True)
        return OptimizationResult(
            task_id=task_id, status=OPTIMIZATION_STATUS_UNMEASURABLE, source=str(run_dir),
            warnings=[f"the optimization diagnostic could not be read: {e!r}"],
        )


def _read(run_dir: pathlib.Path, *, task_id: str) -> OptimizationResult:
    events_path = run_dir / AGENT_EVENTS_FILENAME
    events, unreadable = _read_events(events_path, "the agent's event log")
    if unreadable is not None:
        return OptimizationResult(
            task_id=task_id, status=OPTIMIZATION_STATUS_UNMEASURABLE, source=str(events_path),
            warnings=[unreadable],
        )

    warnings: list[str] = []
    # A missing or corrupt orchestrator log is not fatal to this metric: it only carries the route's
    # configuration, and every count comes off the agent's log. It reads as "the trace does not say".
    orchestrator_events, _ = _read_events(run_dir / ORCHESTRATOR_EVENTS_FILENAME,
                                          "the orchestrator's event log")
    enabled, configured_cycles = _build_configuration(orchestrator_events)
    calls = _read_calls(events, warnings)
    if not calls and enabled is None:
        warnings.append(
            "the run's trace does not say whether the optimization route was enabled (no build.start "
            "carrying the flag) and no optimize_agent call was recorded, so a run built without the "
            "route cannot be told apart from an agent that never called it")

    result = OptimizationResult(
        task_id=task_id or _task_id(events),
        status=_status(enabled, calls),
        optimization_enabled=enabled,
        source=str(events_path),
        calls=len(calls),
        # A call only measured the candidate if it came back AND carried a score: the tool returns no
        # score when it refuses, and a refusal is not a measurement.
        scored_calls=sum(1 for c in calls if c.succeeded and c.headline_metric is not None),
        failed_calls=sum(1 for c in calls if not c.succeeded),
        call_details=calls,
        warnings=warnings,
    )
    # Read once and passed to both readers below. They ask different questions of it -- one counts
    # deploys by outcome and window, the other looks up the bytes a score was taken on -- and parsing
    # it twice would let the two disagree about what the file said.
    entries, manifest_note = _read_manifest(run_dir / CHECKPOINT_MANIFEST_FILENAME)

    _apply_loop_counters(result, calls, configured_cycles)
    _apply_deploys(result, calls)
    _apply_rollbacks(result, calls)
    _apply_internal_eval(result, calls)
    _apply_below_best(result, calls, entries)
    _apply_checkpoints(result, calls, entries, manifest_note,
                       build_end=_build_end(events, orchestrator_events))
    return result


# --- the event log --------------------------------------------------------------------------------

def _read_events(path: pathlib.Path, label: str) -> tuple[list[dict[str, Any]], str | None]:
    """Every event in one of the run's logs, or a reason that log cannot be trusted.

    A single corrupt line makes the whole log unusable for this metric rather than a partial one: the
    numbers here are counts, and a count taken over the half of a log that happened to parse reads
    exactly like a complete one. That is the same call `build_transcript.read_run_dir` makes, for the
    same reason.

    `label` names the log in the returned message. Both shards come through here -- the agent's and the
    orchestrator's -- and the two are read for different things, so a message that named one of them
    for both would send a reader to the wrong file. Only the agent's reason is surfaced today (a
    missing orchestrator log is not fatal and its reason is discarded), which is exactly why the
    wrong name would have gone unnoticed until someone started showing it.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [], f"{label} could not be read ({path}): {e}"

    events: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return [], (f"{label} at {path} is corrupt at line {lineno}; the "
                        f"optimization loop cannot be counted from a partial log")
        if isinstance(event, dict):
            events.append(event)
    return events, None


def _task_id(events: list[dict[str, Any]]) -> str:
    """The task the run was for, off its events. Empty when no event carries one."""
    for event in events:
        value = event.get("task_id")
        if isinstance(value, str) and value:
            return value
    return ""


def _build_configuration(events: list[dict[str, Any]]) -> tuple[bool | None, int | None]:
    """Whether the optimization route existed on this run, and the cycle ceiling it was given.

    Both come off `build.start`, which the orchestrator emits once the agent container is up. `None`
    for either means the run's trace does not say -- an older run, or one that never started a build
    -- and that is reported as unknown rather than defaulted: `optimization_enabled` defaults to True
    in the agent, so guessing True here would label an ablation arm as an optimization run.
    """
    for event in events:
        if event.get("event_type") != _BUILD_START_EVENT:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        enabled = payload.get("optimization_enabled")
        cycles = payload.get("optimize_cycles")
        return (
            enabled if isinstance(enabled, bool) else None,
            cycles if _is_count(cycles) else None,
        )
    return None, None


def _read_calls(events: list[dict[str, Any]], warnings: list[str]) -> list[OptimizationCall]:
    """One record per `optimize_agent` call, in the order the run made them.

    The two halves of a call are joined by SPAN ID, not by file order: a call runs a whole eval and
    then a sub-agent, and everything they emit -- model calls, the sub-agent's own spans -- lands
    between the invocation and the result. Order alone would also not identify the result at all,
    since a tool result's payload does not name its tool; the span is what says which call it closes.

    A call with no result event is kept and marked unfinished. That is the run that crashed or timed
    out inside the loop, and dropping it would under-count the thing this metric is about.
    """
    results_by_span = {
        event.get("span_id"): event
        for event in events
        if event.get("event_type") == _RESULT_EVENT and event.get("span_id")
    }

    calls: list[OptimizationCall] = []
    for event in events:
        if event.get("event_type") != _INVOCATION_EVENT:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("tool") != OPTIMIZE_TOOL:
            continue
        span = event.get("span_id")
        result = results_by_span.get(span) if span else None
        if result is None:
            warnings.append(
                f"optimize_agent call {len(calls) + 1} has no result event -- the run ended inside "
                f"it, so its score, its stopping state and any deploy it made are unknown")
        calls.append(_call_record(
            ordinal=len(calls) + 1,
            started_at=_timestamp(event),
            result=result,
        ))
    return calls


def _call_record(*, ordinal: int, started_at: str | None,
                 result: dict[str, Any] | None) -> OptimizationCall:
    """One call, as its result event recorded it.

    An absent result leaves every measured field `None` and `succeeded=False`: the call was made, and
    nothing about how it went is known. `changed` is False in that case rather than None -- a change
    is only counted where the trace says one was made, so an unknown cannot inflate the deploy tally.
    """
    payload = result.get("payload") if isinstance(result, dict) else None
    if not isinstance(payload, dict):
        return OptimizationCall(ordinal=ordinal, started_at=started_at, succeeded=False,
                                unfinished=True)

    cycle = payload.get("cycle")
    cycle = cycle if isinstance(cycle, dict) else {}
    changed = cycle.get("changed") is True
    kept, rollback = _verdict(cycle.get("verified_previous"))
    return OptimizationCall(
        ordinal=ordinal,
        iteration=payload.get("iteration") if _is_count(payload.get("iteration")) else None,
        status=str(payload["status"]) if isinstance(payload.get("status"), str) else None,
        succeeded=payload.get("success") is True,
        headline_metric=_number(payload.get("headline_metric")),
        saturated=payload.get("saturated") if isinstance(payload.get("saturated"), bool) else None,
        budget_exhausted=(payload.get("budget_exhausted")
                          if isinstance(payload.get("budget_exhausted"), bool) else None),
        cycles_used=payload.get("cycles_used") if _is_count(payload.get("cycles_used")) else None,
        cycles_allowed=(payload.get("cycles_allowed")
                        if _is_count(payload.get("cycles_allowed")) else None),
        changed=changed,
        # Only meaningful when a change was made: a cycle that changed nothing attempts no deploy, and
        # recording False there would count a deploy that was never tried as one that failed.
        redeployed=(cycle.get("redeployed") is True) if changed else None,
        deploy_error=_text(cycle.get("deploy_error")),
        previous_change_kept=kept,
        rollback=rollback,
        checkpoint=_checkpoint_name(payload.get("checkpoint")),
        error=_text(payload.get("error")),
        started_at=started_at,
        ended_at=_timestamp(result),
        unfinished=False,
    )


def _status(enabled: bool | None, calls: list[OptimizationCall]) -> str:
    """Which of the four statuses this run is in.

    `disabled` is read off the trace, never inferred from an empty call list: the ablation arm and an
    agent that simply never called the tool are different runs, and only the first one is evidence
    about the route rather than about the agent's behaviour inside it.

    Which is also why a run with no calls whose trace does not say is `unmeasurable`, not
    `not_attempted`: on a run that predates `build.start` carrying the flag, those two readings are
    indistinguishable, and the one that gets picked by default would become a statistic.
    """
    if calls:
        return OPTIMIZATION_STATUS_SCORED
    if enabled is False:
        return OPTIMIZATION_STATUS_DISABLED
    if enabled is True:
        return OPTIMIZATION_STATUS_NOT_ATTEMPTED
    return OPTIMIZATION_STATUS_UNMEASURABLE


# --- the four metrics -----------------------------------------------------------------------------

def _apply_loop_counters(result: OptimizationResult, calls: list[OptimizationCall],
                         configured_cycles: int | None) -> None:
    """The loop's length, its ceiling, and why it stopped.

    `cycles_used` is taken as the MAXIMUM any call reported, not the last one's: the tool counts
    cycles out of its own persisted history, so the figure only grows -- but the final call of a run
    is also the one most likely to have failed before reporting anything.

    The saturation and budget flags come from the LAST call that reported them, because those are
    states of the trajectory as it then stood, and both can legitimately change from call to call (an
    improving attempt clears saturation). The ceiling prefers what the tool itself reported over the
    configured one: the call parameter may narrow the run's budget, so the configured value is an
    upper bound on the ceiling actually in force, not the ceiling.
    """
    # None rather than 0 when no call reported one: a run whose every call died before reporting has
    # an unknown cycle count, and 0 would claim the loop changed nothing.
    used = [c.cycles_used for c in calls if c.cycles_used is not None]
    result.cycles_used = max(used) if used else None

    allowed = [c.cycles_allowed for c in calls if c.cycles_allowed is not None]
    result.cycles_allowed = allowed[-1] if allowed else configured_cycles
    result.configured_cycles_allowed = configured_cycles

    saturated = [c.saturated for c in calls if c.saturated is not None]
    result.saturated = saturated[-1] if saturated else None
    exhausted = [c.budget_exhausted for c in calls if c.budget_exhausted is not None]
    result.budget_exhausted = exhausted[-1] if exhausted else None

    if not calls:
        return
    result.stop_reason = _stop_reason(result, calls[-1])
    # The question actually asked of this metric: did the loop stop because the scores flattened
    # while it still had budget left to spend? A saturated loop that had also spent its budget cannot
    # answer it -- the budget would have stopped it anyway -- so that is not counted as an early stop.
    # Neither can a run that died inside a call: the flags just read off belong to an EARLIER call,
    # and the loop never reached a stopping decision, so the answer is unknown rather than no. Left as
    # False, a crashed run would have been counted in the rollup line that says how many runs stopped
    # early on saturation -- the one claim this field exists to support.
    if result.saturated is None or result.stop_reason == STOP_REASON_RUN_ENDED_MID_CALL:
        result.early_stopped_on_saturation = None
    else:
        result.early_stopped_on_saturation = bool(
            result.saturated and result.budget_exhausted is not True
        )


def _stop_reason(result: OptimizationResult, last: OptimizationCall) -> str:
    """The one-word reason the loop ended, from the state of its final call.

    TWO CONDITIONS OUTRANK THE FLAGS, and the order is the finding rather than a detail.

    A run that died inside a call comes first, because the loop did not stop -- the run did. The
    saturation and budget flags on `result` are then the state as of an EARLIER call, the last one
    that reported back, so every reason below would describe the trajectory as it stood before the
    call that killed the run, and `still_optimizing` would describe it as "the agent moved on".

    An unverified change comes next, and it outranks saturation and a spent budget for the same reason
    the tool itself does it that way: `optimize_agent._result` resolves its own `status` field with
    `awaiting_verification` ahead of `budget_exhausted` ahead of `saturated`, and it recomputes both
    of those flags AFTER recording the attempt -- so the last cycle of a budget-bounded run routinely
    reports an unverified change and a spent budget at once. Checking the flags first read those runs
    as `budget_exhausted` and dropped the finding this diagnostic is best placed to make. It is also a
    different KIND of claim: saturation and budget are two readings of the trajectory, while this one
    is about the candidate -- nothing measured what was submitted. Being outranked here hides nothing,
    because `saturated`, `budget_exhausted` and `early_stopped_on_saturation` stay on the result and
    are rendered beside this reason wherever it is shown.

    Saturation and a spent budget are still reported together when both held, rather than one of them
    being picked: the tool's status field resolves that tie by priority too (`budget_exhausted` wins),
    and reading it back as the reason would turn "the scores had also flattened" into evidence that
    was never claimed -- or hide it.
    """
    if last.unfinished:
        return STOP_REASON_RUN_ENDED_MID_CALL
    # The loop's last call left a change nobody measured. The second half of the test is for a trace
    # written before the tool nominated `status` for it: a cycle that changed the candidate and got it
    # redeployed is an unverified change whatever the payload calls itself.
    if last.status == "awaiting_verification" or (last.changed and last.redeployed):
        return STOP_REASON_UNVERIFIED_CHANGE
    if result.saturated and result.budget_exhausted:
        return STOP_REASON_SATURATED_AND_BUDGET
    if result.saturated:
        return STOP_REASON_SATURATED
    if result.budget_exhausted:
        return STOP_REASON_BUDGET_EXHAUSTED
    if last.status == "nothing_to_fix":
        return STOP_REASON_NOTHING_TO_FIX
    return STOP_REASON_STILL_OPTIMIZING


def _apply_deploys(result: OptimizationResult, calls: list[OptimizationCall]) -> None:
    """How many of the loop's redeploys produced a running artifact, and how many would not build.

    Counted over the cycles that CHANGED the candidate, which are the only ones that deploy. A failed
    deploy here is the tool's own verdict on its redeploy of an edited candidate -- it restores the
    previous source afterwards, so the failure costs the change, not the run.

    These are the deploys the optimization loop itself asked for. A deploy the agent made by hand
    between two cycles is not one of them and is deliberately not counted here; the checkpoint tally
    below is what covers every durable candidate.
    """
    attempted = [c for c in calls if c.changed]
    result.deploys_attempted = len(attempted)
    result.deploys_succeeded = sum(1 for c in attempted if c.redeployed is True)
    result.deploys_failed = sum(1 for c in attempted if c.redeployed is False)


def _apply_rollbacks(result: OptimizationResult, calls: list[OptimizationCall]) -> None:
    """How many of the loop's own changes survived being measured, and what the rejections cost.

    A change is judged by the NEXT call, so these count verdicts rather than calls: the first call has
    nothing pending to resolve, and so does any call after one that changed nothing.

    WHY THIS EXISTS, AND WHAT IT RECONCILES. `_apply_deploys` counts the cycles that CHANGED the
    candidate, because those are the deploys the cycle budget is spent on. But a rejected change is
    redeployed too, in the other direction: `optimize_agent._resolve_pending` restores the snapshot and
    redeploys the previous candidate. That restore writes a checkpoint like any other deploy, so a run
    with four changes and one roll-back leaves five checkpoints, and until this was counted the two
    tallies could only be reconciled in prose. They now close arithmetically:

        checkpoints_valid == deploys_succeeded + rollback_redeploys

    on a run whose every checkpoint came from the loop. `deploys_attempted` is deliberately NOT widened
    to include restores. It is the count that pairs with `cycles_used`, and a restore spends no cycle
    and produces no new candidate -- folding it in would change what an existing column means and make
    the deploy tally stop answering the question it was added for.

    `rollbacks_failed` is kept apart from `rollback_redeploys` because only one of them is bookkeeping.
    A roll-back that could not restore or could not redeploy leaves the code the loop DECIDED AGAINST
    running, and that is a fact about the artifact the run went on to submit.
    """
    verdicts = [c for c in calls if c.previous_change_kept is not None]
    result.changes_kept = sum(1 for c in verdicts if c.previous_change_kept is True)
    rolled_back = [c for c in verdicts if c.previous_change_kept is False]
    result.changes_rolled_back = len(rolled_back)
    result.rollback_redeploys = sum(1 for c in rolled_back if c.rollback == ROLLBACK_REVERTED)
    result.rollbacks_failed = sum(1 for c in rolled_back if c.rollback in _ROLLBACK_DID_NOT_TAKE)


def _apply_internal_eval(result: OptimizationResult, calls: list[OptimizationCall]) -> None:
    """Where the loop's own score started, how high it got, and where it ended.

    This is the agent's INTERNAL eval -- its own generated cases under its own metric names -- which is
    the quantity the loop is actually steering. It is reported here and never compared to the task
    score by this module: the two score different things, so any arithmetic between them belongs to a
    reader who has been told that, not to a field.

    BEST IS KEPT SEPARATELY FROM FINAL because they genuinely differ. The tool's keep-or-roll-back
    rule compares a new measurement against the IMMEDIATELY PRECEDING one, not against the best the
    loop has ever seen (`optimize_agent._incumbent_record`), so a loop can accept a series of small
    gains off a low point and finish below its own high-water mark. Reporting only the final score
    would hide that, and it is the finding.

    Only calls that actually measured count -- in the summary fields and in the full list alike. A
    refused call and a call whose result never arrived both have no score, and letting either in as a
    zero would invent a collapse that did not happen.
    """
    scores = [(c.ordinal, c.headline_metric) for c in calls if c.headline_metric is not None]
    if not scores:
        return

    # The whole trajectory, off the SAME list the summary fields come from, so the two can never
    # disagree about what was measured.
    result.internal_eval_all_scores = [s for _, s in scores]
    result.internal_eval_first = scores[0][1]
    result.internal_eval_final = scores[-1][1]
    best = max(s for _, s in scores)
    result.internal_eval_best = best
    # The EARLIEST call holding the best score, so a plateau is attributed to where it started rather
    # than to the last call that happened to repeat it.
    result.internal_eval_best_call = next(o for o, s in scores if s == best)
    if len(scores) > 1:
        result.internal_eval_change = scores[-1][1] - scores[0][1]


def _apply_below_best(result: OptimizationResult, calls: list[OptimizationCall],
                      entries: list[dict[str, Any]]) -> None:
    """Did the loop really end holding a worse candidate, or did it just re-measure the same one?

    WHY THIS IS NOT `final < best`. That comparison was the first thing this diagnostic reported and
    it over-fires, because two of its inputs move for different reasons. The keep-or-roll-back rule
    only ever accepts an improvement, so across the changes THE LOOP MADE the incumbent's score cannot
    fall -- and it does not. What falls is the measurement: the loop's own eval is not deterministic,
    so scoring the identical program twice returns two numbers. Over the 39-build `optimize_cycles5`
    sweep, six runs finished below their own best and four of them had never changed the bytes at all,
    finishing 0.001 to 0.016 down on the same content hash. A warning that is wrong two times out of
    three is one a reader learns to skip.

    So the question asked here is whether the best-scoring call and the last measuring call scored
    DIFFERENT CANDIDATES, decided on the candidate's content hash rather than on its checkpoint name:
    the same bytes are legitimately captured under several names when the loop restores them, and
    comparing names would call a restore a regression.

    Three-valued, and the unknown is the point. A score whose checkpoint reference is missing, or a
    manifest that could not be read, means the candidates cannot be compared -- so neither "it ended
    worse" nor "it did not" is claimed, and nothing warns.
    """
    scored = [c for c in calls if c.headline_metric is not None]
    if not scored or result.internal_eval_best is None or result.internal_eval_final is None:
        return
    if result.internal_eval_final >= result.internal_eval_best:
        result.ended_below_best = False
        return

    by_name = {str(e.get("name")): str(e.get("content_hash") or "")
               for e in entries if e.get("name")}
    best = next(c for c in scored if c.headline_metric == result.internal_eval_best)
    result.internal_eval_best_checkpoint = best.checkpoint
    final = scored[-1]
    best_hash, final_hash = by_name.get(best.checkpoint or ""), by_name.get(final.checkpoint or "")
    if not best_hash or not final_hash:
        return
    result.ended_below_best = best_hash != final_hash


def _build_end(agent_events: list[dict[str, Any]],
               orchestrator_events: list[dict[str, Any]]) -> datetime | None:
    """When the build phase ended, for bounding a call that never reported back.

    `build.complete` is the bound to want: the orchestrator emits it however the build ended --
    submission, crash or timeout -- and a deploy after it is not the loop's. Today only the agent's
    own deploy route writes checkpoints (the handover redeploy and the eval-only path deliberately do
    not), so there is usually nothing after the build to mis-attribute; the bound is what keeps that
    true if another route starts checkpointing, rather than leaving an unfinished call's window open
    to the end of the run.

    Falls back to the last event either log recorded, which is the closest thing to "the run's end"
    available when the build never got to report one.
    """
    for event in orchestrator_events:
        if event.get("event_type") == _BUILD_COMPLETE_EVENT:
            end = _parse_time(_timestamp(event))
            if end is not None:
                return end
    seen = [t for t in (_parse_time(_timestamp(e))
                        for e in (*agent_events, *orchestrator_events)) if t is not None]
    return max(seen) if seen else None


def _apply_checkpoints(result: OptimizationResult, calls: list[OptimizationCall],
                       entries: list[dict[str, Any]], manifest_note: str | None, *,
                       build_end: datetime | None) -> None:
    """Count the durable candidates the loop left behind, by outcome.

    The counts stay `None` when there is nothing to count them from -- no manifest, or no call to
    attribute a checkpoint to -- and `checkpoint_note` says which. A zero here would claim the loop
    produced no durable candidate, which is a finding; "this run keeps no checkpoints" is not.
    """
    if manifest_note is not None:
        result.checkpoint_note = manifest_note
        return
    if not calls:
        result.checkpoint_note = ("no optimize_agent call ran, so none of this run's checkpoints came "
                                  "from optimization")
        return

    windows = _call_windows(calls, build_end)
    if not windows:
        result.checkpoint_note = ("the optimize_agent calls carry no usable timestamps, so this run's "
                                  "checkpoints cannot be attributed to them")
        return

    counts = {OUTCOME_DEPLOYED: 0, OUTCOME_UNHEALTHY: 0, OUTCOME_FAILED: 0}
    other = unattributed = 0
    # A deploy no call window covers came from the agent rather than from the loop, and WHEN it
    # happened is what tells the two apart. Before the first call is the build-phase deploy, which is
    # how the candidate got running in the first place. After that, it is the agent deploying around
    # a loop that is in play -- see `deploys_outside_loop` on the result for why that matters.
    loop_start = min(start for start, _end in windows)
    before = outside = 0
    for entry in entries:
        created = _parse_time(entry.get("created_at"))
        if created is None:
            unattributed += 1
            continue
        if not any(start <= created <= end for start, end in windows):
            if created < loop_start:
                before += 1
            else:
                outside += 1
            continue
        outcome = entry.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
        else:
            other += 1

    result.checkpoints_valid = counts[OUTCOME_DEPLOYED]
    result.checkpoints_unhealthy = counts[OUTCOME_UNHEALTHY]
    result.checkpoints_failed = counts[OUTCOME_FAILED]
    result.checkpoints_in_run = len(entries)
    result.deploys_before_loop = before
    result.deploys_outside_loop = outside
    if other:
        result.warnings.append(
            f"{other} checkpoint(s) created during optimization carry an outcome this diagnostic "
            f"does not know, and are in none of the three counts")
    if unattributed:
        result.warnings.append(
            f"{unattributed} of this run's {len(entries)} checkpoint(s) have no readable creation "
            f"time, so they could not be attributed to the optimization loop either way")


def _read_manifest(path: pathlib.Path) -> tuple[list[dict[str, Any]], str | None]:
    """The checkpoint manifest's entries, or a reason there are none to read."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], ("this run has no checkpoint manifest -- it kept no per-deploy copies (a local "
                    "run, or one that predates checkpointing)")
    except OSError as e:
        return [], f"the checkpoint manifest could not be read ({path}): {e}"
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as e:
        return [], f"the checkpoint manifest at {path} is not readable JSON: {e}"
    entries = manifest.get("checkpoints") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        return [], f"the checkpoint manifest at {path} holds no checkpoint list"
    return [e for e in entries if isinstance(e, dict)], None


def _call_windows(calls: list[OptimizationCall],
                  build_end: datetime | None) -> list[tuple[datetime, datetime]]:
    """The intervals during which an `optimize_agent` call was in flight.

    A call that never reported back has no end of its own, so the end of the BUILD is used: a deploy
    it managed to make happened somewhere in there. Generous on purpose in that one case --
    attributing a checkpoint to the loop that was running when the run died is a better reading than
    dropping the last candidate the loop produced -- but bounded, because the handover's own redeploy
    writes a checkpoint too and must not be handed to the loop.
    """
    windows = []
    for call in calls:
        start = _parse_time(call.started_at)
        if start is None:
            continue
        end = _parse_time(call.ended_at) or build_end
        if end is None or end < start:
            continue
        windows.append((start, end))
    return windows


# --- small readers --------------------------------------------------------------------------------

def _timestamp(event: dict[str, Any] | None) -> str | None:
    if not isinstance(event, dict):
        return None
    value = event.get("timestamp")
    return value if isinstance(value, str) else None


def _parse_time(value: Any) -> datetime | None:
    """An event or manifest timestamp as a datetime, or None when it cannot be read.

    Both writers emit timezone-aware UTC ISO-8601, so a naive value is an unexpected shape rather
    than a local time to be assumed -- it is dropped, and the caller counts it as unattributed. A
    mixed comparison would raise; assuming UTC would invent a fact.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_count(value: Any) -> bool:
    """A real integer, not a bool. `0` is a meaningful value everywhere here (a budget of zero
    forbids any attempt), so it must survive the check."""
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    """A short, bounded string. Unbounded tool text (a Docker build log in a deploy error) would be
    embedded into the eval report and rendered in a browser."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


def _checkpoint_name(value: Any) -> str | None:
    return value.get("name") if isinstance(value, dict) and isinstance(value.get("name"), str) else None


def _verdict(value: Any) -> tuple[bool | None, str | None]:
    """How the previous call's change was judged: `(kept, rollback)`.

    `(None, None)` when there was no verdict to reach -- the first call of a loop, and any call after
    one that changed nothing. That is not the same as a rejection, so neither field is defaulted to a
    value that would be counted as one.

    `rollback` is only read when the change was NOT accepted, and only when the tool's own word for
    the outcome is one this module knows. An unrecognised word is dropped rather than passed through:
    it would land in a rendered count as an unexplained category, and the roll-back still gets counted
    through `kept=False`.
    """
    if not isinstance(value, dict):
        return None, None
    if value.get("accepted") is True:
        return True, None
    if value.get("reverted") is True:
        # The one case the tool spells twice. `revert` is absent on some traces, so the boolean is
        # what decides that a roll-back happened; the word only says how it went.
        outcome = value.get("revert")
        return False, outcome if outcome in ROLLBACK_OUTCOMES else ROLLBACK_REVERTED
    outcome = value.get("revert")
    if outcome in ROLLBACK_OUTCOMES:
        return False, outcome
    # A verdict dict that says neither accepted nor reverted and carries no word this module knows.
    # It happened, and what it decided is unknown -- so it is not counted as either.
    return None, None
