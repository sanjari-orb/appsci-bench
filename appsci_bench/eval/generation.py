"""Step 1 of the evaluation: generate the artifact's outputs over the dataset.

For each (data point x repeat) unit we open a session, run it (single-turn = one query;
multi-turn = drive the artifact agent user sim up to max_turns or max_steps), capture the conversation, and
delete the session. Units run concurrently on a thread pool; each artifact query is bounded by a
timeout. A timed-out or failed query is RECORDED (the unit's `error` is set and the partial
conversation kept) rather than stalling or crashing the run -- it is scored as a failure during
scoring. Step-accounting infrastructure failures instead receive one fresh case retry. Failed
attempts are saved separately and never scored; exhausted retries leave the evaluation incomplete.

Every unit is persisted and handed to the caller's `on_record` hook the moment it completes, rather
than the whole batch being written after the last one finishes. That ordering is the difference
between a killed run having its finished cases and having none of them.

WHICH artifact a unit talks to, and which environment gets prepared for it, come from a LANE it holds
for its whole life (see `lanes.LanePool`). A task with no per-case environment state has one shared
lane and the concurrency it always had; a task that resets an environment per case has W exclusive
lanes and runs W cases at a time, one per environment. The old rule -- "env hooks are set, so run one
case at a time" -- was the W=1 case of this.

This loop is owned by the benchmark, not by task code. See docs/evaluator_system_design.md
(Evaluation System, step 1).
"""
from __future__ import annotations

import contextlib
import logging
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable

from .artifact_client import ArtifactAgentClient, ArtifactTransportError, HttpArtifactClient
from .lanes import EvalLane, LanePool, NoAvailableLanes
from .store import TranscriptStore
from .datatypes import (
    TERMINATION_COMPLETE, TERMINATION_ERROR, TERMINATION_MAX_TURNS, TERMINATION_MAX_STEPS,
    TERMINATION_USER_STOP, TERMINATION_INFRASTRUCTURE_ERROR,
    ArtifactAgentOutput, ConversationTurn, Dataset, EvalSample, EvalTypeError, GenerationRecord,
    materialize_files,
)
from .artifact_agent_user_sim import ArtifactAgentUserSimulator
from .step_budget import LaneCleanupError, LocalStepBudget, MaxStepsReached, StepBudgetUnavailable, check_budget

logger = logging.getLogger(__name__)


class GenerationIncompleteError(RuntimeError):
    """Completed records are durable; infrastructure prevented a complete evaluation."""

    def __init__(self, records, total, failures=None):
        reason = (f"infrastructure failures: {failures}" if failures else "all eval lanes retired")
        super().__init__(f"{reason}: completed {len(records)}/{total} cases")
        self.records = records


def _case_context(**fields):
    """Tag model calls made in this scope with the eval case (data_point_id, repeat_index) so the
    tracer can group user_sim/judge I/O per case. Falls back to a no-op when observability is absent."""
    try:
        from shared.observability.model_io import model_call_context
        return model_call_context(**fields)
    except Exception:  # noqa: BLE001 -- observability optional; never break generation over it
        return contextlib.nullcontext()

# Per-query (one agent turn) timeout ceiling. An agentic artifact answers a turn by running its own
# tool-calling loop (several LLM + tool round-trips), so a turn legitimately takes longer than a
# single model call; 60s was too tight and produced false timeouts. This only bounds a runaway query.
#
# RAISED 180 -> 1200 on 2026-09-18 to match BenchmarkOrchestrator.QUERY_TIMEOUT_S. These two and
# HttpArtifact.DEFAULT_TIMEOUT are the same ceiling reached by three paths, and they disagreed
# (180 / 300 / 300). The orchestrator passes its own value explicitly, so the real eval path used
# 300 and this default only governed library callers -- which meant a local `--artifact-dir` rerun
# could time out 120s EARLIER than the run it was meant to reproduce. Keep all three in step.
DEFAULT_QUERY_TIMEOUT_S = 1200.0
MAX_INFRASTRUCTURE_ATTEMPTS = 2  # one fresh retry; never retry an ordinary artifact/task failure


def generate(
    dataset: Dataset,
    client: ArtifactAgentClient,
    *,
    user_sim: ArtifactAgentUserSimulator | None = None,
    store: TranscriptStore | None = None,
    query_timeout_s: float | None = DEFAULT_QUERY_TIMEOUT_S,
    max_workers: int | None = None,
    setup_state: Callable[[EvalSample], None] | None = None,
    read_final_state: Callable[[EvalSample], dict] | None = None,
    lanes: LanePool | None = None,
    on_record: Callable[[GenerationRecord], None] | None = None,
) -> list[GenerationRecord]:
    """Run every (data point, repeat) unit through the artifact and return the records.

    Records are returned in deterministic order (by data_point_id, then repeat_index). If `store`
    is given, each record is persisted as its own conversation file AS SOON AS THAT UNIT FINISHES.
    Multi-turn datasets require a `user_sim`.

    `on_record` is called once per unit, on THIS thread, immediately after the unit's conversation is
    persisted -- the seam the eval phase uses to score a case while later cases are still generating
    (see `scoring.StreamingScorer`). It must not block for long and must not submit into a pool whose
    workers this loop waits on.

    Per-case environment hooks (both optional, both receive the `EvalSample`):
      `setup_state`      -- runs BEFORE the conversation (e.g. reset a task-env to a fresh fixture).
      `read_final_state` -- runs AFTER the conversation, before the session is deleted; its returned
                            dict is stored on `GenerationRecord.final_state` and handed to eval
                            functions (e.g. a post-run env state hash for a DB-state score).
    These act on one environment, so a unit holds that environment exclusively for its whole life.
    With no `lanes` pool that means ONE environment and therefore one unit at a time, exactly as
    before. Pass `lanes` to run against W environments and W units at a time.

    `lanes` is the pool of (environment, artifact) stacks to draw from (see `lanes.LanePool`). When
    omitted, one is built from `client` + the two hooks above: a shared lane when there are no hooks
    (unchanged concurrency), an exclusive single lane when there are (unchanged serialization). Pass
    `lanes` INSTEAD of `client`/hooks, not alongside -- an explicit pool wins and the others are
    ignored.
    """
    if dataset.type == "multi_turn" and user_sim is None:
        raise EvalTypeError("multi_turn dataset requires a user_sim to drive the conversation")

    units = [(s, r) for s in dataset.data for r in range(dataset.repeat)]
    if not units:
        return []
    lane_pool = lanes if lanes is not None else _lane_pool_for(client, setup_state, read_final_state)
    if dataset.max_steps is not None:
        for lane in lane_pool.lanes:
            if isinstance(lane.client, HttpArtifactClient) and (
                lane.step_budget_factory is None or lane.cancel_query is None
            ):
                raise EvalTypeError("HTTP max_steps evaluation requires environment accounting and query cancellation")
    # The pool's own ceiling wins where it has one (an exclusive pool cannot serve more units than it
    # has environments); otherwise the historic sizing rule stands. An explicit `max_workers` is still
    # the caller's override, but never above what the pool can actually serve -- asking for 16 workers
    # on 4 lanes would leave 12 threads blocked in `acquire()` for the whole run.
    workers = max_workers or min(16, max(1, min(len(units), (os.cpu_count() or 2))))
    if lane_pool.max_workers is not None:
        workers = min(workers, lane_pool.max_workers)

    records: list[GenerationRecord] = []
    failures = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_unit, sample, r, lane_pool, user_sim, dataset, query_timeout_s): (sample, r, 1)
            for sample, r in units
        }
        # Persisting and handing off INSIDE this loop is the whole point: every unit's work is
        # recorded before the next one is collected, so a run killed part-way through still has every
        # completed case on disk. Both steps run on this thread -- the only one consuming `futures` --
        # so the store still has a single writer and needs no lock.
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for fut in done:
                sample, repeat, attempt = futures.pop(fut)
                try:
                    rec = fut.result()
                except NoAvailableLanes:
                    # No attempted conversation to score. Still collect other running cases.
                    continue
                if rec.infrastructure_error is not None:
                    _persist(store, rec)
                    key = f"{sample.data_point_id}__r{repeat}"
                    failures[key] = rec.infrastructure_error
                    if attempt < MAX_INFRASTRUCTURE_ATTEMPTS:
                        logger.warning("Retrying case %s after infrastructure failure (attempt %d): %s",
                                       key, attempt, rec.infrastructure_error)
                        retry = pool.submit(_run_unit, sample, repeat, lane_pool, user_sim, dataset,
                                            query_timeout_s, generation_attempt=attempt + 1)
                        futures[retry] = (sample, repeat, attempt + 1)
                    continue
                failures.pop(f"{sample.data_point_id}__r{repeat}", None)
                records.append(rec)
                _persist(store, rec)
                _handoff(on_record, rec)
                logger.info("generation: %d/%d units", len(records), len(units))

    records.sort(key=lambda r: (r.data_point_id, r.repeat_index))
    if len(records) != len(units):
        raise GenerationIncompleteError(records, len(units), failures)
    return records


def _persist(store: TranscriptStore | None, rec: GenerationRecord) -> None:
    """Save one record, treating a save failure as a lost transcript rather than a lost run.

    Swallowing the exception is deliberate and is the narrow case where it is right: this call exists
    so that a run which dies early still has its finished cases, and letting a full disk or a
    read-only mount abort generation would destroy the very thing it protects -- every case, not one.
    Logged with a traceback so it is visible rather than silent."""
    if store is None:
        return
    try:
        if rec.infrastructure_error is not None:
            store.save_infrastructure_attempt(rec)
        else:
            store.save(rec)
    except Exception:                 # noqa: BLE001 -- durability is best-effort; the run is not
        logger.warning("failed to persist transcript for %s (continuing)",
                       rec.data_point_id, exc_info=True)


def _handoff(on_record: Callable[[GenerationRecord], None] | None, rec: GenerationRecord) -> None:
    """Hand one record to the caller's per-unit hook, isolating generation from its failures.

    Same reasoning as `_persist`: the hook is an observer of this loop (scoring submission, progress
    writing), so a bug in it must cost that one case's bookkeeping, never the generation of the
    remaining cases."""
    if on_record is None:
        return
    try:
        on_record(rec)
    except Exception:                 # noqa: BLE001 -- an observer must not fail the run it observes
        logger.warning("on_record hook failed for %s (continuing)",
                       rec.data_point_id, exc_info=True)


def _lane_pool_for(client, setup_state, read_final_state) -> LanePool:
    """The pool implied by the pre-lane arguments, so every caller that passes none keeps its behaviour.

    No hooks -> a SHARED lane: one client, every worker at once, the historic sizing rule in charge.
    Hooks -> a single EXCLUSIVE lane: one environment, so one unit at a time, which is exactly the
    `workers = 1` clamp this replaced.
    """
    lane = EvalLane(client=client, setup_state=setup_state, read_final_state=read_final_state)
    if setup_state is None and read_final_state is None:
        return LanePool.shared(lane)
    return LanePool.exclusive([lane])


def _run_unit(sample, repeat_index, lane_pool, user_sim, dataset, timeout, *, generation_attempt=1):
    """Run one unit end to end. Raises NoAvailableLanes only if the case cannot start.

    The lane is held across setup, the whole conversation and the final-state read, and released only
    on the way out. Both halves of that matter: releasing it before the state read would let the next
    unit reset the environment out from under this one's hash, and taking a fresh lane per turn would
    strand a multi-turn artifact's own per-conversation state on whichever stack answered turn one.
    """
    rec = GenerationRecord(data_point_id=sample.data_point_id, repeat_index=repeat_index,
                           generation_attempt=generation_attempt)
    with lane_pool.acquire() as lane:
        # Stamped before anything can fail, so a unit that dies in setup still says where it ran.
        # Only when there is more than one copy to distinguish: a single-environment run leaves this
        # None, which is what every transcript predating parallel eval has.
        if lane_pool.width > 1:
            rec.lane = lane.label
        client = lane.client
        session_id = None

        def cancel_query():
            try:
                lane.cancel_query()
            except Exception as exc:  # recovery must not replace max_steps with a scoring failure
                rec.lane_cleanup_error = str(exc)
                lane_pool.retire(lane, str(exc))
                logger.exception("Retiring %s after failed artifact recovery", lane.label)

        try:
            with _case_context(data_point_id=sample.data_point_id, repeat_index=repeat_index,
                               generation_attempt=generation_attempt):
                if lane.setup_state is not None:
                    try:
                        lane.setup_state(sample)   # per-case starting state (e.g. reset the task-env)
                    except Exception as exc:
                        if generation_attempt == 1:
                            raise
                        # A failed reset during recovery cannot turn the original infrastructure
                        # outage into a graded artifact failure. Its writes may still be in flight.
                        raise StepBudgetUnavailable(
                            f"cannot prepare a clean environment for infrastructure retry "
                            f"({type(exc).__name__})", unsafe_to_reuse_lane=True) from None
                session_id = client.create_session()
                if dataset.type == "single_turn":
                    _run_single_turn(rec, sample, client, session_id, timeout)
                else:
                    # A lane may carry its own simulator; that is how a customer who can call the
                    # environment's tools gets an executor bound to THIS lane's environment rather
                    # than to whichever one the shared simulator was built against.
                    _run_multi_turn(rec, sample, client, session_id, lane.user_sim or user_sim,
                                    dataset.max_turns, timeout, max_steps=dataset.max_steps, lane=lane,
                                    cancel_query=cancel_query if lane.cancel_query else None)
        except StepBudgetUnavailable as exc:
            rec.infrastructure_error = str(exc)
            rec.termination_reason = TERMINATION_INFRASTRUCTURE_ERROR
            if exc.unsafe_to_reuse_lane:
                lane_pool.retire(lane, str(exc))
            logger.warning("Case %s attempt %d had a step-accounting infrastructure failure: %s",
                           sample.data_point_id, generation_attempt, exc)
        except LaneCleanupError:
            raise  # infrastructure failure; never score/reuse a lane with an uncontained query
        except Exception as e:         # noqa: BLE001 -- setup/session failure is a graded unit failure
            if rec.error is None:      # don't clobber a more-specific conversation-phase error
                rec.error = repr(e)
        else:
            # Capture post-conversation env state (before delete_session), but ONLY if the conversation
            # phase itself succeeded. `_run_*_turn` records a failed turn by setting `rec.error` WITHOUT
            # raising, so guard on it here -- otherwise read_final_state runs against a broken run and,
            # if it raises, its error would overwrite the earlier, more informative failure.
            if lane.read_final_state is not None and rec.error is None:
                try:
                    rec.final_state = lane.read_final_state(sample)
                except Exception as e:  # noqa: BLE001 -- a state-read failure is itself a graded error
                    rec.error = repr(e)
        finally:
            # Any failure leaves the conversation unfinished, so record that as the termination reason
            # -- including failures the turn loop never saw. A unit whose setup, session-create, or very
            # first `user_sim.ask` raises exits via the `except` above having run no turns at all, so
            # nothing inside `_run_*_turn` ever set a reason; without this the transcript of a wholly
            # failed unit would claim "not recorded" and be indistinguishable from a pre-field run. Set
            # last (in `finally`) and only when unset, so a more specific reason always wins.
            if rec.error is not None and rec.termination_reason is None:
                rec.termination_reason = TERMINATION_ERROR
            if session_id is not None:
                try:
                    client.delete_session(session_id)
                except Exception:      # noqa: BLE001 -- cleanup must not mask the unit result
                    logger.warning("delete_session failed for %s", session_id, exc_info=True)
    return rec


def _run_single_turn(rec, sample, client, session_id, timeout):
    query = sample.single_turn_input
    if query is None:                  # guarded by the dataset loader, but stay defensive
        raise EvalTypeError(f"{sample.data_point_id!r}: single_turn sample has no single_turn_input")
    materialize_files(query)            # resolve any file_path-sourced files before the client sees them
    try:
        out = _query_with_timeout(client, session_id, query, timeout)
        rec.conversation.append(ConversationTurn(query=query, response=out))
        rec.termination_reason = TERMINATION_COMPLETE   # the one query was answered; nothing to cut off
    except Exception as e:             # noqa: BLE001 -- record a failed turn, mark the unit errored
        rec.error = repr(e)
        rec.termination_reason = TERMINATION_ERROR
        rec.conversation.append(ConversationTurn(query=query, response=ArtifactAgentOutput()))


def _run_multi_turn(rec, sample, client, session_id, user_sim, max_turns, timeout,
                    *, max_steps=None, lane=None, cancel_query=None):
    mti = sample.multi_turn_input
    if mti is None:
        raise EvalTypeError(f"{sample.data_point_id!r}: multi_turn sample has no multi_turn_input")
    materialize_files(mti)              # resolve any file_path-sourced files before the sim sees them
    sim_session_id = user_sim.create_session(mti)
    budget = None
    accounting_failed = False
    try:
        if max_steps is not None:
            factory = lane.step_budget_factory if lane is not None else None
            budget = factory(max_steps) if factory else LocalStepBudget(max_steps)
            _run_simulation(rec, client, session_id, user_sim, sim_session_id, timeout, budget,
                            cancel_query)
            return
        query = user_sim.ask(sim_session_id, rec.conversation)   # conversation=[] -> opening turn
        for _ in range(max_turns):
            if query is None:              # sim signalled stop (criteria met)
                break
            try:
                out = _query_with_timeout(client, session_id, query, timeout)
            except Exception as e:         # noqa: BLE001 -- record failed turn, stop this unit
                rec.error = repr(e)
                rec.termination_reason = TERMINATION_ERROR
                rec.conversation.append(ConversationTurn(query=query, response=ArtifactAgentOutput()))
                break
            rec.conversation.append(ConversationTurn(query=query, response=out))
            query = user_sim.ask(sim_session_id, rec.conversation)
        # Why the loop ended, recorded for the eval functions. `query is None` = the sim ended the
        # conversation itself; still-set = the max_turns ceiling bound first, so the conversation is
        # TRUNCATED, not finished. Testing `query` rather than tracking a flag also gets the last
        # iteration right: a sim that stops on the final permitted turn is a real stop, not a
        # ceiling hit. The error branch above has already set its own reason, so don't overwrite it.
        if rec.termination_reason is None:
            rec.termination_reason = (
                TERMINATION_USER_STOP if query is None else TERMINATION_MAX_TURNS)
    except StepBudgetUnavailable:
        accounting_failed = True
        raise
    finally:
        try:
            if budget is not None and not accounting_failed:
                rec.simulation_steps = budget.snapshot()
        finally:
            user_sim.delete_session(sim_session_id)


def _run_simulation(rec, client, session_id, user_sim, sim_session_id, timeout, budget, cancel_query):
    """tau2-style loop: test the shared ceiling after each speaker/environment step.

    The stop message itself consumes a step; reaching the ceiling on it is still max_steps,
    matching upstream's post-step termination check. There is no additional exchange limit.
    """
    try:
        while True:
            check_budget(budget)
            query = user_sim.ask(sim_session_id, rec.conversation, step_budget=budget)
            budget.consume()  # customer text or stop message (tools are counted inside ask)
            check_budget(budget)
            if query is None:
                rec.termination_reason = TERMINATION_USER_STOP
                return
            try:
                out = _query_with_timeout(client, session_id, query, timeout,
                                          budget=budget, cancel_query=cancel_query)
            except MaxStepsReached:
                rec.conversation.append(ConversationTurn(query=query, response=ArtifactAgentOutput()))
                raise
            except (LaneCleanupError, StepBudgetUnavailable):
                raise
            except Exception as exc:
                rec.conversation.append(ConversationTurn(query=query, response=ArtifactAgentOutput()))
                # The artifact may surface the environment's cutoff as HTTP 500 or another error
                # before the poll runs. It is still max_steps, not an artifact failure.
                check_budget(budget)
                rec.error = repr(exc)
                rec.termination_reason = TERMINATION_ERROR
                return
            rec.conversation.append(ConversationTurn(query=query, response=out))
            # A tool may have exhausted the budget while the HTTP response was in flight.
            check_budget(budget)
            budget.consume()  # artifact's final text message
            check_budget(budget)
    except MaxStepsReached:
        rec.termination_reason = TERMINATION_MAX_STEPS


def _query_with_timeout(client, session_id, query, timeout, *, budget=None, cancel_query=None):
    """Call query_session, bounded by `timeout` seconds. A timeout raises TimeoutError; the
    optional cancel callback stops the artifact before a step-limited lane is reused. Legacy
    clients without cancellation leave the abandoned thread running (shutdown(wait=False))."""
    if timeout is None and budget is None:
        return client.query_session(session_id, query)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(client.query_session, session_id, query)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        if budget is None:
            return future.result(timeout=timeout)
        while True:
            check_budget(budget)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            try:
                return future.result(timeout=1.0 if remaining is None else min(1.0, remaining))
            except TimeoutError:
                if future.done():
                    raise  # the artifact itself raised TimeoutError
    except TimeoutError as e:
        raise TimeoutError(f"query_session timed out after {timeout}s") from e
    finally:
        try:
            unfinished = not future.done()
            if not unfinished:
                # A local socket timeout completes the future while /respond can still be running
                # on the server. It requires the same containment as an abandoned worker thread.
                unfinished = isinstance(future.exception(), (ArtifactTransportError, TimeoutError))
            if unfinished and cancel_query is not None:
                cancel_query()  # also contain queries abandoned on timeout or accounting failure
        finally:
            pool.shutdown(wait=False)
