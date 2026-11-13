"""Step 2 of the evaluation: score each (data point, objective) pair.

For every generation record x objective we call the objective's eval function by keyword with the
contract signature (*, data_point_id, conversation, expected_output, eval_rubric, **kwargs) and turn
its return into a DataPointMetricResult. Work items run on a thread pool (a task pool, not lockstep --
a static string compare and an LLM judge have very different latency). Each call is bounded by a
timeout.

Two more keywords ride along in `**kwargs` for the eval functions that want them (both additive, so
existing eval functions ignore them): `final_state`, the post-run env state from generation's
`read_final_state` hook, and `termination_reason`, how the conversation ended. A task whose reward
is only meaningful on a conversation that actually finished reads the latter and scores a truncated
trace accordingly -- see `tasks/airline_rebooking/eval.py::task_success`.

`eval_rubric` in that signature is `sample.eval_rubric` -- a per-DATA-POINT NL note, never the
metric's own scoring criteria (see `EvalSample.eval_rubric`'s docstring in datatypes.py). Don't be
tempted to route a task-wide rubric through this per-call field; it belongs baked into the eval
function's own prompt instead.

Failure is never fatal to the run (design: Task-Specific Evaluation Function):
  * a generation-errored record is scored 0 with the generation error recorded (not re-run);
  * any eval-fn exception, timeout, non-(score, metadata) return, non-float-or-None / out-of-range
    score, or non-dict metadata -> score 0.0 with the reason in the `error` field.
  * a `None` score is NOT a failure -- it means the metric doesn't apply to this data point, and is
    excluded from aggregation entirely (aggregate.py), not scored 0.

`check_compatibility` is the best-effort pre-run flag the design promises ("flag non-compatible
data + metrics when possible before running").

TWO ENTRY POINTS, ONE DEFINITION OF THE WORK. `score_records` scores a batch of finished records --
a re-score of a saved store, the tests, the demo. `StreamingScorer` scores each unit as generation
produces it, which is what the eval phase uses so that a run killed part-way keeps the cases it
finished. Both route through `_plan_unit`, so they cannot disagree about which (record, objective)
pairs are runnable versus already-decided failures.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from typing import Any, Callable

from .datatypes import Dataset, DataPointMetricResult, GenerationRecord, Objective, materialize_files
from .workers import worker_pool

logger = logging.getLogger(__name__)

DEFAULT_EVAL_TIMEOUT_S = 60.0


def _case_context(**fields):
    """Tag model calls made in this scope with the eval case (data_point_id, repeat_index) so the
    tracer can group judge I/O per case. No-op when observability is absent."""
    try:
        from shared.observability.model_io import model_call_context
        return model_call_context(**fields)
    except Exception:  # noqa: BLE001 -- observability optional; never break scoring over it
        return contextlib.nullcontext()


def check_compatibility(dataset: Dataset, objectives: list[Objective]) -> list[str]:
    """Best-effort warnings about objective/data mismatches, emitted before the run.

    We cannot introspect what an arbitrary eval function needs, so this is heuristic: it flags
    missing / inconsistent `expected_output`, which is the common reference-based-metric pitfall.
    """
    warnings: list[str] = []
    if not objectives:
        return warnings
    n = len(dataset.data)
    with_exp = sum(1 for s in dataset.data if s.expected_output is not None)
    if n and with_exp == 0:
        warnings.append(
            "no sample has expected_output; objectives that compare against ground truth will "
            "score 0 unless their eval function ignores it")
    elif 0 < with_exp < n:
        warnings.append(
            f"expected_output is present on only {with_exp}/{n} samples (inconsistent); objectives "
            "relying on it will fail on the samples that lack it")
    return warnings


def score_records(
    records: list[GenerationRecord],
    dataset: Dataset,
    objectives: list[Objective],
    *,
    eval_timeout_s: float | None = DEFAULT_EVAL_TIMEOUT_S,
    max_workers: int | None = None,
    pool: ThreadPoolExecutor | None = None,
) -> list[DataPointMetricResult]:
    """Score every (record, objective) pair. Returns results in deterministic order
    (data_point_id, objective name, repeat_index). Never raises on per-item failure.

    `pool`: submit into an existing executor instead of opening one, and leave it running afterwards.
    This is how the eval phase gives scoring and the elicitation diagnostic ONE concurrency budget
    instead of two that cannot see each other (see `workers.py`). Left `None`, this sizes and owns its
    own pool exactly as it always has, so existing callers are unaffected. `max_workers` applies only
    to a pool this function creates -- an injected pool is already sized by its owner.

    This is the BATCH entry point, for callers holding every record already (a re-score of a saved
    store, the tests, the demo). The eval phase itself uses `StreamingScorer`, which scores each case
    as generation produces it; both share `_plan_unit` so the two cannot disagree about which
    (record, objective) pairs are runnable."""
    for w in check_compatibility(dataset, objectives):
        logger.warning("compatibility: %s", w)

    by_id = {s.data_point_id: s for s in dataset.data}
    results: list[DataPointMetricResult] = []
    work: list[tuple[GenerationRecord, Any, Objective]] = []

    for rec in records:
        sample = by_id.get(rec.data_point_id)
        immediate, runnable = _plan_unit(rec, sample, objectives)
        results.extend(immediate)
        work.extend(runnable)

    if work:
        # An injected pool is the caller's to close; only a pool we opened is ours to shut down.
        owned = worker_pool(len(work), max_workers) if pool is None else contextlib.nullcontext(pool)
        with owned as executor:
            futures = [executor.submit(_score_one, rec, sample, obj, eval_timeout_s)
                       for rec, sample, obj in work]
            for fut in as_completed(futures):
                results.append(fut.result())    # _score_one never raises
                logger.info("scoring: %d/%d", len(results), len(work))

    results.sort(key=lambda r: (r.data_point_id, r.objective, r.repeat_index))
    return results


def _plan_unit(
    rec: GenerationRecord, sample: Any, objectives: list[Objective],
) -> tuple[list[DataPointMetricResult], list[tuple[GenerationRecord, Any, Objective]]]:
    """Split one unit's objectives into results already decided and pairs that must actually run.

    The three decided-without-running cases are graded failures, not skips: a unit whose generation
    failed is scored 0 rather than re-run, and so is one the dataset or the objective list cannot
    service. Shared by the batch and streaming scorers so "what is runnable" has one definition."""
    immediate: list[DataPointMetricResult] = []
    runnable: list[tuple[GenerationRecord, Any, Objective]] = []
    for obj in objectives:
        if rec.error:                       # generation failed -> graded failure, do not re-run
            immediate.append(_failed(rec, obj, f"generation failed: {rec.error}"))
        elif sample is None:
            immediate.append(_failed(rec, obj, "no dataset sample matches this record"))
        elif obj.eval_function is None:
            immediate.append(_failed(rec, obj, "objective eval_function not resolved "
                                               "(load_objectives with resolve=True)"))
        else:
            runnable.append((rec, sample, obj))
    return immediate, runnable


class StreamingScorer:
    """Score each unit as generation produces it, so a killed run keeps its finished cases.

    Used by the eval phase as generation's `on_record` hook::

        scorer = StreamingScorer(dataset, objectives, pool=pool, on_unit_scored=cb)
        records = generate(..., on_record=scorer.submit)
        results = scorer.drain()

    WHY THIS IS NOT SIMPLY "score the record inside the hook". Blocking the hook until a case is
    scored would serialise scoring behind generation's collection loop. For a task whose generation is
    already sequential (any task with per-sample env hooks -- banking, airline) that is invisible,
    because generation of case N+1 proceeds on its worker while the hook works on case N. For a
    task generating 16 cases at once it is a real regression: generation would finish quickly and
    then scoring would crawl one case at a time. So `submit` only ENQUEUES the unit's pairs, one pool
    task each exactly as the batch scorer does, and the pool keeps the same concurrency it always had.

    WHY COMPLETION IS COLLECTED ON THE CALLER'S THREAD rather than in a done-callback. A callback
    fires on a pool worker, which would make the on-disk writes concurrent and need a lock, and its
    ordering relative to `wait()` is not something to rely on. Instead `submit` opportunistically
    flushes whatever finished since last time, and `drain` flushes the rest -- every flush therefore
    happens on the one thread that drives generation, and the store keeps its single writer. The cost
    is that a unit's results are written when the NEXT unit arrives rather than the instant its last
    objective returns, so progress can lag by one case; for a task that spends minutes per case that
    is not a meaningful lag, and `drain` closes it out.
    """

    def __init__(
        self,
        dataset: Dataset,
        objectives: list[Objective],
        *,
        pool: ThreadPoolExecutor,
        eval_timeout_s: float | None = DEFAULT_EVAL_TIMEOUT_S,
        on_unit_scored: Callable[[GenerationRecord, list[DataPointMetricResult],
                                 list[DataPointMetricResult]], None] | None = None,
    ):
        for w in check_compatibility(dataset, objectives):
            logger.warning("compatibility: %s", w)
        self._by_id = {s.data_point_id: s for s in dataset.data}
        self._objectives = objectives
        self._pool = pool
        self._eval_timeout_s = eval_timeout_s
        self._on_unit_scored = on_unit_scored
        #: unit key -> (record, immediate results, [(objective, future), ...]) awaiting flush.
        self._pending: dict[tuple[str, int], tuple[GenerationRecord,
                                                   list[DataPointMetricResult],
                                                   list[tuple[Objective, Any]]]] = {}
        self._results: list[DataPointMetricResult] = []
        self._units_scored = 0

    @property
    def units_scored(self) -> int:
        """How many units have been scored AND flushed so far."""
        return self._units_scored

    def submit(self, rec: GenerationRecord) -> None:
        """Enqueue one unit's objectives, then flush any unit that has finished in the meantime."""
        immediate, runnable = _plan_unit(rec, self._by_id.get(rec.data_point_id), self._objectives)
        futures = [(o, self._pool.submit(_score_one, r, s, o, self._eval_timeout_s))
                   for r, s, o in runnable]
        self._pending[(rec.data_point_id, rec.repeat_index)] = (rec, immediate, futures)
        self._flush_ready()

    def drain(self) -> list[DataPointMetricResult]:
        """Wait for every outstanding pair, flush all remaining units, return results in the batch
        scorer's deterministic order."""
        outstanding = [f for _, _, futures in self._pending.values() for _, f in futures]
        if outstanding:
            wait(outstanding)
        for key in list(self._pending):
            self._flush(key)
        self._results.sort(key=lambda r: (r.data_point_id, r.objective, r.repeat_index))
        return list(self._results)

    def _flush_ready(self) -> None:
        for key, (_, _, futures) in list(self._pending.items()):
            if all(f.done() for _, f in futures):
                self._flush(key)

    def _flush(self, key: tuple[str, int]) -> None:
        entry = self._pending.pop(key, None)
        if entry is None:                      # already flushed; `_flush` is idempotent by design
            return
        rec, immediate, futures = entry
        unit_results = list(immediate)
        for obj, fut in futures:
            try:
                unit_results.append(fut.result())   # _score_one never raises
            except Exception as e:                  # noqa: BLE001 -- a cancelled/broken pool task
                # Reaching here means the POOL failed, not the eval function. Recorded as a graded
                # failure for the pair it belonged to rather than dropped, so the unit's result set
                # stays the same size as its objective list.
                unit_results.append(_failed(rec, obj, repr(e)))
        unit_results.sort(key=lambda r: (r.objective, r.repeat_index))
        self._results.extend(unit_results)
        self._units_scored += 1
        logger.info("scoring: unit %s (%d scored)", rec.data_point_id, self._units_scored)
        if self._on_unit_scored is not None:
            try:
                self._on_unit_scored(rec, unit_results, self._results)
            except Exception:              # noqa: BLE001 -- bookkeeping must not fail the run
                logger.warning("on_unit_scored hook failed for %s (continuing)",
                               rec.data_point_id, exc_info=True)


def _failed(rec: GenerationRecord, obj: Objective, error: str) -> DataPointMetricResult:
    return DataPointMetricResult(data_point_id=rec.data_point_id, objective=obj.name,
                                 repeat_index=rec.repeat_index, score=0.0, metadata={}, error=error)


def _score_one(rec: GenerationRecord, sample, obj: Objective, timeout) -> DataPointMetricResult:
    """Call one eval function and validate its return. Always returns a result; never raises."""
    materialize_files(sample.expected_output)  # resolve file_path-sourced files before the eval fn sees them
    try:
        # All args passed by KEYWORD (the contract is keyword-only): task authors can't depend on
        # positional order, so the contract can grow / reorder additively as Parts 2/3 land.
        with _case_context(data_point_id=rec.data_point_id, repeat_index=rec.repeat_index):
            out = _call_with_timeout(
                obj.eval_function, (),
                {"data_point_id": rec.data_point_id, "conversation": rec.conversation,
                 "expected_output": sample.expected_output, "eval_rubric": sample.eval_rubric,
                 "final_state": rec.final_state,
                 "termination_reason": rec.termination_reason}, timeout)
    except Exception as e:                       # noqa: BLE001 -- eval fns are arbitrary task code
        return _failed(rec, obj, repr(e))

    score, metadata, err = _validate_return(out)
    if err is not None:
        return _failed(rec, obj, err)
    return DataPointMetricResult(data_point_id=rec.data_point_id, objective=obj.name,
                                 repeat_index=rec.repeat_index, score=score, metadata=metadata)


def _validate_return(out: Any) -> tuple[float | None, dict, str | None]:
    """Enforce the eval-fn output contract: a (float-in-[0,1] | None, dict) pair. Returns
    (score, meta, err). `None` means the metric doesn't apply to this data point -- valid, and
    left as `None` for `aggregate.py` to exclude from every view (design: Task-Specific
    Evaluation Function)."""
    if not isinstance(out, (tuple, list)) or len(out) != 2:
        return 0.0, {}, f"eval function must return (score, metadata), got {type(out).__name__}"
    score, metadata = out
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return 0.0, {}, f"score must be a float or None, got {type(score).__name__}"
        score = float(score)
        if not (0.0 <= score <= 1.0):
            return 0.0, {}, f"score {score} is out of range [0, 1]"
    if not isinstance(metadata, dict):
        return 0.0, {}, f"metadata must be a dict, got {type(metadata).__name__}"
    return score, metadata, None


def _call_with_timeout(fn, args, kwargs, timeout):
    """Call fn(*args, **kwargs) bounded by `timeout` seconds. Timeout raises TimeoutError; the
    abandoned call keeps running in its worker but does not block us (shutdown(wait=False)).

    Runs the call inside a copy of the current context so ContextVars set by the caller (e.g. the
    per-case `model_call_context` used for observability) reach model calls made inside `fn` on the
    worker thread -- ThreadPoolExecutor does not propagate context on its own."""
    if timeout is None:
        return fn(*args, **kwargs)
    ctx = contextvars.copy_context()
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(ctx.run, fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except TimeoutError as e:
        raise TimeoutError(f"eval function timed out after {timeout}s") from e
    finally:
        pool.shutdown(wait=False)
