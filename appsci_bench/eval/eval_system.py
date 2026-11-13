"""The evaluation system's own run loop: wire steps 1-3 into one run.

Named `eval_system` (not `orchestrator`) to avoid confusion with the separate, higher-level
`appsci_bench/orchestrator/` package (container/submission-server lifecycle for a full
build-then-eval benchmark run) -- that package's `BenchmarkOrchestrator` calls into this module's
`run_evaluation` (via `eval/evaluator.py`) for the eval half, it does not replace it.

`run_evaluation(spec, client, ...)` loads the task's dataset + objectives, generates the artifact's
outputs (step 1), scores each (data point, objective) pair (step 2), aggregates into the four views
+ final score (step 3), and returns an EvalReport. Each report gets a `run_id` that is unique per
execution (so archived reports never overwrite each other) yet prefixed with a stable config hash, so
runs of the same task/dataset/objectives stay greppable/groupable.

STEPS 1 AND 2 ARE INTERLEAVED, not sequential phases. Each case is scored and written to disk as soon
as it is generated, and the report on disk is rebuilt from the cases finished so far, flagged
`partial`. The reason is a run that does not reach the end: a 97-case banking eval was killed by its
worker timeout with 80 cases through, and because nothing was written until the last case finished,
all 80 were lost. A long eval now leaves a scored record of exactly what it got through.

`artifact_to_client` adapts whatever the build phase hands back into an ArtifactAgentClient: a client
passes through; a plain respond callable is wrapped; a legacy `appsci_bench.eval.artifact` artifact (the current
single-call `respond(ArtifactQuery)` handoff) is bridged. The legacy bridge is TEMPORARY -- it goes
away once artifacts serve the session API natively (see the design's migration note).
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import pathlib
import random
import time
from typing import Any, Callable

from .aggregate import aggregate
from .artifact_agent_user_sim import (
    PARITY_DIVERGED, TAU_BENCH_USER_SIM_MODEL, parity_status,
)
from .artifact_client import ArtifactAgentClient, InProcessArtifactClient
from .conformance import ConformanceError, check_conformance
from .dataset import load_dataset
from .generation import DEFAULT_QUERY_TIMEOUT_S, GenerationIncompleteError, generate
from .lanes import LanePool
from .objectives import load_objectives
from .scoring import DEFAULT_EVAL_TIMEOUT_S, StreamingScorer
from .workers import worker_pool
from .build_optimization import read_optimization
from .build_transcript import STATUS_UNREADABLE as TRANSCRIPT_UNREADABLE
from .build_transcript import BuildTranscript, read_run_dir, read_transcript_file
from .elicitation import score_elicitation
from .elicitation_rubric import load_rubric, validate_rubric
from .store import TranscriptStore
from .datatypes import OPTIMIZATION_STATUS_UNMEASURABLE, STATUS_NO_RUBRIC, STATUS_UNMEASURABLE, ArtifactAgentOutput, ArtifactAgentQuery, Dataset, ElicitationResult, EvalReport, EvalSample, EvalTypeError, Objective, OptimizationResult
from .artifact_agent_user_sim import ArtifactAgentUserSimulator
from shared.observability.events import EventType
from shared.observability.publisher import emit as _emit_obs

logger = logging.getLogger(__name__)


def _task_id(spec: Any) -> str:
    tid = getattr(spec, "task_id", None)
    if tid is None and isinstance(spec, dict):
        tid = spec.get("task_id")
    return tid or "task"


def _reward_objective(spec: Any) -> str | None:
    """The objective a task declares as its binary pass/fail reward, if it declares one.

    Recorded into the report (see `aggregate._reward_view`) rather than looked up later, so anything
    reading a run -- the experiment layer computing pass^k across trials, a notebook reading the S3
    mirror -- gets the pass/fail from the run itself and never has to resolve a task tree that may not
    be checked out beside it.

    Read defensively: `spec` is a TaskSpec or a plain dict here, and a task without the field is the
    normal case, not an error.
    """
    ev = getattr(spec, "evaluation_data", None)
    if ev is None and isinstance(spec, dict):
        ev = spec.get("evaluation_data")
    if not isinstance(ev, dict):
        return None
    name = ev.get("reward_objective")
    return str(name) if name else None


_run_seq = itertools.count(1)  # process-local, guarantees run_id uniqueness within a process


def _config_fingerprint(task_id: str, dataset: Dataset, objectives) -> str:
    """Internal: a STABLE hash of WHAT is being evaluated -- same (task, dataset content, objectives
    incl. paths/weights/direction) => same value. Not exposed as its own field; it's used only as the
    `run_id` prefix so runs of the same config stay greppable/groupable without a separate id.

    Covers the full identity the old run_id missed: every sample's content (not just its id) and every
    objective's name/path/positive/weight (not just the name)."""
    parts = [task_id, f"type={dataset.type}", f"repeat={dataset.repeat}", f"max_turns={dataset.max_turns}"]
    if dataset.max_steps is not None:
        parts.append(f"max_steps={dataset.max_steps}")
    for s in sorted(dataset.data, key=lambda s: s.data_point_id):
        parts.append(json.dumps(s.to_dict(), sort_keys=True, default=str))
    for o in sorted(objectives, key=lambda o: o.name):
        parts.append(f"{o.name}|{o.eval_function_path}|{o.positive}|{o.weight}")
    return "cfg_" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:12]


def _run_id(config_fingerprint: str) -> str:
    """A UNIQUE-per-execution id: the config fingerprint (so runs stay groupable by what was run) plus
    a wall-clock + process-local sequence tail, so two executions -- even of the same config, even
    back-to-back -- never collide (safe for archiving reports keyed on run_id)."""
    cfg = config_fingerprint.removeprefix("cfg_")
    unique = f"{time.time_ns():x}{next(_run_seq):x}"
    return f"run_{cfg}_{unique}"


def run_evaluation(
    spec: Any,
    client: ArtifactAgentClient,
    *,
    user_sim: ArtifactAgentUserSimulator | None = None,
    workdir: str | None = None,
    package: str = "tasks",
    objectives: list[Objective] | None = None,
    query_timeout_s: float | None = DEFAULT_QUERY_TIMEOUT_S,
    eval_timeout_s: float | None = DEFAULT_EVAL_TIMEOUT_S,
    sample_seed: int | None = None,
    limit: int | None = None,
    sample_ids: list[str] | None = None,
    rerun_source: str | None = None,
    rerun_source_sha256: str | None = None,
    allow_config_change: bool = False,
    conformance: str = "auto",
    setup_state: Callable[[EvalSample], None] | None = None,
    read_final_state: Callable[[EvalSample], dict] | None = None,
    lanes: LanePool | None = None,
    build_transcript: str | pathlib.Path | BuildTranscript | None = None,
    build_run_dir: str | pathlib.Path | None = None,
) -> EvalReport:
    """Run the full evaluation for one task and return its EvalReport.

    `spec` is a TaskSpec or a dict with `task_id` + `evaluation_data` (dataset config + objectives).
    `package` is the import root for objective eval_function_paths (default the tasks/ package).
    `objectives` lets a caller pass PRE-RESOLVED objectives (each with `eval_function` set) instead
    of loading + resolving them from the spec by path -- useful for standalone scripts whose eval
    functions live in a non-importable `__main__`.
    `limit` caps the number of data points. `sample_seed` decides WHICH ones: omitted, the cap takes
    the first N in dataset order (deterministic, and what a smoke test wants); given, it takes a seeded
    random sample of N -- see :func:`subsample` for why that matters. Multi-turn datasets require `user_sim`.
    `conformance` runs the session-API probe before generation: "auto" (default) probes real
    transports but skips an in-process client (conformant by construction), "on" always probes,
    "off" never. A failed probe aborts the run with ConformanceError (no garbage scores).

    `lanes` is the pool of (task-env, artifact) stacks generation draws from -- W of them lets W cases
    run at once against W independent environments. Omitted, one is derived from `client` + the env
    hooks, which is the single-environment behaviour every caller had before lanes existed. When given,
    the conformance probe runs against EVERY lane rather than only the client passed here: the lanes
    run the same image but not the same container, and a stack that came up wrong would otherwise be
    discovered one case at a time as a run of zeros.

    `build_transcript` is the conversation the agent had with the build-time stakeholder, which the
    elicitation diagnostic scores: a run directory, a captured transcript file, or an already-read
    transcript. It is passed in, never guessed at from an ambient publisher or a run directory this
    function inferred -- a wrong guess would score one run's conversation against another's rubric,
    and reporting `unmeasurable` is the honest answer when nobody said where the conversation is.

    `build_run_dir` is the run directory of the BUILD that produced this artifact, which the
    optimization diagnostic reads: the agent's event log there says how many times its optimization
    loop ran, why it stopped, and what each cycle deployed. Separate from `build_transcript` even
    though a benchmark run passes the same directory to both -- that one also accepts a bare
    transcript file or an already-read conversation, neither of which carries an event log. Nothing
    passed means the diagnostic reports `unmeasurable`, which is not a run with no optimization.
    """
    task_id = _task_id(spec)
    dataset = load_dataset(spec)
    if limit is not None and rerun_source is None:
        dataset = Dataset(type=dataset.type,
                          data=subsample(dataset.data, limit, sample_seed),
                          max_turns=dataset.max_turns, repeat=dataset.repeat,
                          max_steps=dataset.max_steps)
    if objectives is None:
        objectives = load_objectives(spec, package=package)

    original = None
    fingerprint = _config_fingerprint(task_id, dataset, objectives)
    if sample_ids is not None or rerun_source is not None:
        if not sample_ids or not rerun_source or not workdir:
            raise ValueError("sample_ids, rerun_source and a new workdir are required together")
        from .sample_rerun import prepare
        original, dataset, fingerprint = prepare(
            pathlib.Path(rerun_source), sample_ids, dataset, objectives, task_id,
            _reward_objective(spec), pathlib.Path(workdir), rerun_source_sha256,
            allow_config_change=allow_config_change,
        )

    # Probe the clients that will actually answer cases -- every lane when a pool was supplied, else
    # the single client. A lane whose artifact deployed against the wrong environment answers the probe
    # differently from one that did not, and finding that out here costs one query instead of a
    # dataset.
    for probe_client in ([lane.client for lane in lanes.lanes] if lanes is not None else [client]):
        if _should_check_conformance(probe_client, conformance):
            report = check_conformance(probe_client)
            if not report.passed:
                raise ConformanceError(report)

    run_id = _run_id(fingerprint)
    store = TranscriptStore(workdir, run_id=run_id)
    logger.info("eval %s: transcripts -> %s", run_id, store.root)
    # Observability: emit eval-phase events into the shared trace (no-op when no publisher is
    # active, e.g. local/tests). Lets the build-side tracer show build -> eval in one timeline.
    _emit_obs(EventType.EVAL_START, {"task_id": task_id, "run_id": run_id,
                                     "dataset_type": dataset.type, "num_cases": len(dataset.data),
                                     "num_objectives": len(objectives)})
    # UNITS, not data points: generation runs one per (data point, repeat), and the progress counter
    # has to be comparable with what it is counting. The expression mirrors generation's own so
    # `cases_total` is exactly what will be attempted -- `repeat` has a schema floor of 1, so clamping
    # it here would only paper over a hand-built Dataset that generation would treat differently.
    n_units = len(dataset.data) * dataset.repeat
    reward_objective = _reward_objective(spec)
    progress = _ProgressRecorder(store, task_id=task_id, run_id=run_id, objectives=objectives,
                                 cases_total=n_units, reward_objective=reward_objective)
    # One pool for the whole eval phase. Scoring, the elicitation diagnostic and now the per-case
    # streaming all share it rather than opening a pool each, so a run's concurrency is one budget
    # instead of several that cannot see each other.
    #
    # Sized on whichever fan-out is larger, because they are not the same shape: scoring scales with the
    # dataset, the diagnostic with the rubric. Sizing on scoring alone starved the diagnostic on exactly
    # the small runs people iterate with -- a two-objective task at `--limit 1` gave two workers, so a
    # nineteen-item rubric ran in ten waves instead of two. Nothing was mis-scored, it just took several
    # times longer than it needed to.
    #
    # Opened BEFORE generation now, because scoring starts while generation is still running. The
    # sizing input moved from `len(records)` to `n_units` for the same reason; they are the same number.
    with worker_pool(max(n_units * max(1, len(objectives)), _rubric_size(spec))) as pool:
        # Scoring is interleaved with generation: each unit's objectives are enqueued the moment its
        # conversation lands, and its results are written out as soon as they are all in. A run killed
        # part-way through therefore leaves every finished case scored on disk instead of nothing --
        # see StreamingScorer's docstring for why this is not simply scoring inside the hook.
        scorer = StreamingScorer(dataset, objectives, pool=pool, eval_timeout_s=eval_timeout_s,
                                 on_unit_scored=progress.unit_scored)
        incomplete = None
        try:
            records = generate(dataset, client, user_sim=user_sim, store=store,
                               query_timeout_s=query_timeout_s,
                               setup_state=setup_state, read_final_state=read_final_state,
                               lanes=lanes, on_record=scorer.submit)
        except GenerationIncompleteError as exc:
            # Drain every completed case's scoring before reporting infrastructure exhaustion.
            # Unstarted cases and failed accounting attempts must not become invented zero scores.
            incomplete = exc
            records = exc.records
        results = scorer.drain()
        merged = original is not None and incomplete is None
        if merged:
            from .sample_rerun import merge_results, read_source
            read_source(pathlib.Path(rerun_source), sample_ids, rerun_source_sha256)
            results = merge_results(pathlib.Path(rerun_source), store.root, original,
                                    sample_ids, results)
        report = aggregate(task_id, results, objectives, run_id=run_id,
                           reward_objective=reward_objective)
        # Only a completed sample batch becomes a revised full suite. On infrastructure failure,
        # persist the attempted subset's partial report and re-raise, preserving the original trial.
        report.partial = incomplete is not None
        report.cases_scored = original.cases_scored if merged else scorer.units_scored
        report.cases_total = original.cases_total if merged else n_units
        # A condition of the measurement, not a performance note: at a width above 1 a case's
        # neighbours ran in other artifact processes against other environment copies. Recorded on the
        # report so two scores can be checked for comparability without reading a log.
        report.eval_lanes = lanes.width if lanes is not None else 1
        if lanes is not None:
            report.warnings.extend(f"{label} retired after infrastructure recovery failed: {reason}"
                                   for label, reason in lanes.retired_lanes.items())
        if incomplete is not None:
            report.warnings.append(str(incomplete))
        retried = sum(rec.generation_attempt > 1 for rec in records)
        if retried:
            report.warnings.append(f"{retried} case(s) completed after an infrastructure retry; "
                                   "failed attempts are saved in infrastructure_attempts/")
        if (rejected_warning := _rejected_files_warning(records)):
            report.warnings.append(rejected_warning)
        # Inside the pool block: the diagnostic shares it with scoring, which has finished draining by
        # now. Attached to the report AFTER aggregation, so it is structurally incapable of reaching
        # any view or the final score.
        report.elicitation = (original.elicitation if original else
                              _score_elicitation_safely(spec, build_transcript, objectives, pool))
    # Outside the pool: this one reads files and calls no model, so it needs no workers. Attached
    # after aggregation for the same structural reason as elicitation -- it cannot reach any view or
    # the final score.
    report.optimization = (original.optimization if original else
                           _read_optimization_safely(spec, build_run_dir))
    if original is not None:
        from .sample_rerun import config_provenance
        report.sample_rerun = {"source_trace_id": pathlib.Path(rerun_source).parent.name,
                               "source_run_id": original.run_id,
                               "sample_ids": list(sample_ids),
                               "source_report_sha256": rerun_source_sha256,
                               "allow_config_change": allow_config_change,
                               **config_provenance(original, dataset, sample_ids, fingerprint,
                                                   merged=merged)}
        report.warnings.append(
            "This report replaces selected samples from an earlier eval; "
            "it is a revised trial, not an independent trial." if merged else
            "Sample rerun incomplete; the original trial was not replaced.")
        if report.sample_rerun["mixed_configurations"]:
            report.warnings.append("This revised trial combines samples evaluated under different "
                                   "configurations; see sample_rerun for each sample's configuration.")
    report.store_root = str(store.root)
    _note_non_parity_user_sim(report, user_sim)
    _write_report_artifacts(report, store)
    # Per-case events were emitted live by the progress recorder as each case was scored, so only the
    # task-wide aggregate is left. Emitting the cases here as well would double every one of them.
    _emit_eval_complete(report, records)
    _emit_elicitation(report)
    _emit_optimization(report)
    if incomplete is not None:
        raise incomplete
    return report


def _rejected_files_warning(records) -> str | None:
    """A report warning naming the returned files the evaluator refused (unsupported `file_type`), or
    None when nothing was refused. Lets affected trials be found without reading worker logs."""
    rejected = [(r.get("file_name"), r.get("file_type")) for rec in records
                for turn in rec.conversation for r in turn.response.rejected_files]
    if not rejected:
        return None
    cases = sum(any(t.response.rejected_files for t in rec.conversation) for rec in records)
    return (f"{len(rejected)} returned file(s) in {cases} case(s) were rejected for an "
            "unsupported file_type and ignored: "
            + ", ".join(sorted({f"{n} ({t})" for n, t in rejected})))


class _ProgressRecorder:
    """Writes a run's progress to disk and to the trace as each case finishes scoring.

    Every call rebuilds the report from the results accumulated so far and rewrites `report.json` /
    `report.html` / `progress.json` in the store root, so the files a killed run leaves behind
    describe exactly the cases that had finished. Rebuilding rather than patching is deliberate:
    `aggregate` is the only thing that knows how the views and the weighted score are formed, and a
    second, incremental implementation of that would be free to disagree with it. Re-aggregating a few
    hundred results is microseconds against the minutes a case takes.

    The report it writes is flagged `partial` and carries `cases_scored`/`cases_total`. `final_score`
    is the weighted mean over the finished cases -- a live progress figure, not a task score.
    """

    def __init__(self, store: TranscriptStore, *, task_id: str, run_id: str,
                 objectives: list[Objective], cases_total: int, reward_objective: str | None):
        self._store = store
        self._task_id = task_id
        self._run_id = run_id
        self._objectives = objectives
        self._cases_total = cases_total
        self._reward_objective = reward_objective
        #: Counted here rather than derived from the accumulated results, so this agrees with
        #: `StreamingScorer.units_scored` (which the FINAL report uses) by construction. Deriving it
        #: from distinct ids in `all_results` would disagree in one real case: a unit that produced no
        #: results at all -- a task with an empty objective list -- was scored and must be counted.
        self._cases_scored = 0

    def unit_scored(self, rec, unit_results, all_results) -> None:
        """Called by StreamingScorer once per unit, on the generation thread."""
        self._store.save_results(rec, unit_results)
        self._cases_scored += 1
        report = aggregate(self._task_id, list(all_results), self._objectives, run_id=self._run_id,
                           reward_objective=self._reward_objective)
        report.store_root = str(self._store.root)
        report.partial = True
        report.cases_scored = self._cases_scored
        report.cases_total = self._cases_total
        _write_report_artifacts(report, self._store, log=False)
        self._store.write_json(self._store.PROGRESS, {
            "run_id": self._run_id,
            "task_id": self._task_id,
            "cases_scored": self._cases_scored,
            "cases_total": self._cases_total,
            "partial": True,
            "final_score_so_far": report.final_score,
            "last_data_point_id": rec.data_point_id,
        })
        self._emit_case(rec, report)

    def _emit_case(self, rec, report: EvalReport) -> None:
        """Emit this case's score onto the trace as it lands, so the tracer timeline advances during
        the run rather than all at once at the end."""
        row = next((r for r in report.views.get("per_data_point", [])
                    if r.get("data_point_id") == rec.data_point_id), None)
        _emit_obs(EventType.EVAL_CASE_SCORE, {
            "data_point_id": rec.data_point_id,
            "score": (row or {}).get("score"),
            "per_metric": report.views.get("matrix", {}).get(rec.data_point_id, {}),
            "error": getattr(rec, "error", None),
        })


def _note_non_parity_user_sim(report: EvalReport, user_sim) -> None:
    """Record on the report when the eval-time user sim ran on a model that makes these scores
    non-comparable to tau-bench.

    Only `PARITY_DIVERGED` lands here. A known gateway alias for the pinned model is near-parity and
    is deliberately NOT recorded: it is the documented internal path (`setup_local_with_gateway.sh`
    selects it because the gateway rejects the first-party snapshot id), so warning on it would put an
    entry on every internal report and train readers to skip the field -- costing the signal exactly
    where it matters. The near-parity caveat is documented once, in evaluator_system_design.md, and
    logged at INFO by `resolve_user_sim_config`.

    `getattr` rather than an isinstance check: `ScriptedArtifactAgentUserSim` (and any other test
    double) has no model to report, and a `single_turn` run has no sim at all.
    """
    model = getattr(user_sim, "model", None)
    if model and parity_status(model) == PARITY_DIVERGED:
        msg = (f"eval-time user sim ran on {model!r}, not tau-bench's {TAU_BENCH_USER_SIM_MODEL!r} "
               f"-- these scores are NOT tau-bench-comparable")
        logger.warning(msg)
        report.warnings.append(msg)

def _emit_eval_complete(report: EvalReport, records) -> None:
    """Emit the task-wide `eval.complete`. Per-case `eval.case_score` events are emitted live by
    `_ProgressRecorder` as each case is scored -- this used to fan them all out here, at the end,
    which meant the timeline showed nothing until the run was over and showed nothing at all for a run
    that never got there."""
    _emit_obs(EventType.EVAL_COMPLETE, {
        "final_score": report.final_score,
        "per_metric": {pm["objective"]: pm["score"] for pm in report.views.get("per_metric", [])},
        "num_cases": len(report.views.get("per_data_point", [])),
        "num_errored": sum(1 for r in records if getattr(r, "error", None)),
        "partial": report.partial,
        "cases_scored": report.cases_scored,
        "cases_total": report.cases_total,
    })


def _rubric_size(spec) -> int:
    """How many items the diagnostic will fan out, for pool sizing only.

    Zero on any problem: a rubric that will not load is a status for the diagnostic to report, not a
    reason to missize the pool or to fail the run -- the scorer's own guard handles it a moment later and
    produces the warning. Deliberately reads the rubric a second time rather than threading it through
    the pool block; it is one small file, and the alternative is carrying a load-failure through two
    call sites to save a few microseconds.
    """
    try:
        rubric = load_rubric(spec)
    except Exception:  # noqa: BLE001 -- pool sizing must not be able to fail an eval run
        return 0
    return len(rubric.items) if rubric else 0


def _score_elicitation_safely(spec, build_transcript, objectives, pool) -> ElicitationResult:
    """Load the task's rubric, read the conversation, and score it. Cannot fail the eval run.

    `score_elicitation` already swallows its own failures, so the guard here is for everything before
    it: a rubric that will not load, a transcript path that will not read. Those are real problems and
    they belong on the report as a status plus a warning -- but a run whose artifact scored fine must
    not be thrown away because its diagnostic could not be computed.

    A rubric that loads but names an objective the task does not declare is reported and scored anyway. A
    mistyped objective name is worth seeing; suppressing the whole diagnostic over it would hide both
    the typo and the score.
    """
    try:
        rubric = load_rubric(spec)
        transcript = _read_build_transcript(build_transcript)
        # Rubric references describe the task's metrics, including diagnostics disabled for this run.
        # Keep caller-supplied objectives valid too, and never import disabled functions here.
        ev = spec.get("evaluation_data") if isinstance(spec, dict) else getattr(spec, "evaluation_data", None)
        declared = (ev or {}).get("objectives") or []
        problems = validate_rubric(rubric, [*declared, *objectives]) if rubric else []
        for problem in problems:
            logger.warning("elicitation rubric: %s", problem)
        result = score_elicitation(rubric, transcript, pool=pool, task_id=_task_id(spec))
        result.warnings = problems + result.warnings
        return result
    except Exception as e:  # noqa: BLE001 -- a diagnostic must never cost a scored run
        logger.warning("elicitation diagnostic skipped", exc_info=True)
        return ElicitationResult(task_id=_task_id(spec), status=STATUS_UNMEASURABLE,
                                 warnings=[f"the elicitation diagnostic could not run: {e!r}"])


def _read_optimization_safely(spec, build_run_dir) -> OptimizationResult:
    """Summarize the build's optimization loop. Cannot fail the eval run.

    `read_optimization` swallows its own failures, so this guard covers only the case it cannot: no
    directory was passed. That is reported as unmeasurable rather than as a loop that never ran --
    an eval-only rerun that nobody handed a build run dir has no way to see the loop, and saying "0
    optimization runs" there would be a claim about the agent rather than about the input.
    """
    if build_run_dir is None:
        return OptimizationResult(
            task_id=_task_id(spec), status=OPTIMIZATION_STATUS_UNMEASURABLE,
            warnings=["no build run directory was provided, so the build's optimization loop could "
                      "not be read (this is NOT a run without optimization)"],
        )
    return read_optimization(build_run_dir, task_id=_task_id(spec))


def _read_build_transcript(source) -> BuildTranscript:
    """Resolve the caller's `build_transcript` into a transcript.

    Nothing passed is `unreadable`, which the scorer turns into `unmeasurable` -- deliberately not the
    same as an empty conversation. "Nobody told us where the conversation is" and "the agent never had
    one" are different findings and the statuses keep them apart.
    """
    if source is None:
        return BuildTranscript(exchanges=(), source="(no build transcript was provided)",
                               status=TRANSCRIPT_UNREADABLE)
    if isinstance(source, BuildTranscript):
        return source
    path = pathlib.Path(source)
    return read_run_dir(path) if path.is_dir() else read_transcript_file(path)


def _emit_elicitation(report: EvalReport) -> None:
    """Emit the one-per-run elicitation summary (no-op without a publisher).

    Always emitted, rubric or not: a timeline that shows nothing for a task without a rubric leaves the
    reader unable to tell "not measured" from "the emitter is broken". Summary only -- the per-item
    verdicts and their quoted evidence are on the report, which the tracer links to, and putting fifty
    of them on the timeline would bury everything else.

    Best-effort like the rest of the observability path: a failed emit must not fail a scored run.
    """
    el = report.elicitation
    try:
        _emit_obs(EventType.EVAL_ELICITATION, {
            "status": el.status if el else STATUS_NO_RUBRIC,
            "elicited_rate": el.elicited_rate if el else None,
            "asked_not_elicited_rate": el.asked_not_elicited_rate if el else None,
            "missed_rate": el.missed_rate if el else None,
            # The denominator of all three rates above. It travels with them because a rate over three
            # judged items is not the same claim as a rate over fifty.
            "judged_items": el.judged_items if el else 0,
            "total_items": el.total_items if el else 0,
            "error_items": el.error_items if el else 0,
            "num_exchanges": el.num_exchanges if el else 0,
        })
    except Exception:  # noqa: BLE001 -- observability must never break a scored run
        logger.warning("failed to emit the elicitation event", exc_info=True)


def _emit_optimization(report: EvalReport) -> None:
    """Emit the one-per-run optimization summary (no-op without a publisher).

    Always emitted, loop or no loop: the timeline has to be able to say "the optimization route was
    off on this run" rather than showing nothing, which is indistinguishable from a broken emitter.
    Summary only -- the per-call table is on the report.

    Best-effort like the rest of the observability path: a failed emit must not fail a scored run.
    """
    opt = report.optimization
    try:
        _emit_obs(EventType.EVAL_OPTIMIZATION, {
            "status": opt.status if opt else OPTIMIZATION_STATUS_UNMEASURABLE,
            "optimization_enabled": opt.optimization_enabled if opt else None,
            "calls": opt.calls if opt else 0,
            "cycles_used": opt.cycles_used if opt else None,
            "cycles_allowed": opt.cycles_allowed if opt else None,
            "saturated": opt.saturated if opt else None,
            "early_stopped_on_saturation": opt.early_stopped_on_saturation if opt else None,
            "stop_reason": opt.stop_reason if opt else None,
            "deploys_succeeded": opt.deploys_succeeded if opt else 0,
            "deploys_failed": opt.deploys_failed if opt else 0,
            # How many of its own changes the loop kept, and how many it undid. A roll-back redeploys
            # the previous candidate, so it leaves a checkpoint that `deploys_succeeded` does not
            # explain -- carried here so the timeline's two tallies can be reconciled without the
            # report.
            "changes_rolled_back": opt.changes_rolled_back if opt else 0,
            "rollback_redeploys": opt.rollback_redeploys if opt else 0,
            "rollbacks_failed": opt.rollbacks_failed if opt else 0,
            # The loop's OWN eval, not this run's score: where it ended and how far it moved. Carried
            # so the timeline can say whether the loop got anywhere, not only how long it ran.
            "internal_eval_final": opt.internal_eval_final if opt else None,
            "internal_eval_change": opt.internal_eval_change if opt else None,
            "checkpoints_valid": opt.checkpoints_valid if opt else None,
        })
    except Exception:  # noqa: BLE001 -- observability must never break a scored run
        logger.warning("failed to emit the optimization event", exc_info=True)


def _write_report_artifacts(report: EvalReport, store: TranscriptStore, *,
                            log: bool = True) -> None:
    """Persist report.json (regenerable) + a self-contained report.html into the run's store root.
    Best-effort: a rendering/IO error is logged and never fails the eval run.

    Called once per scored case while a run is in flight and once more at the end, so the files on
    disk always describe the cases finished so far. Writes go through the store's atomic helpers --
    rewriting these in place would mean a kill mid-write leaves a truncated report where the previous
    complete one used to be, and both the tracer and the experiment layer parse them on a live run.
    `log=False` on the per-case path keeps one line per case out of the run log; the final write still
    announces where the report is.

    Re-rendering per case is affordable and was measured rather than assumed: at banking's shape (90
    multi-turn conversations, 5 objectives) one render is ~25ms and ~3MB, so a 97-case run spends about
    two seconds of its four hours here. If a future task makes that untrue, throttle the HTML -- not
    report.json, which is what every reader actually parses."""
    try:
        store.write_json("report.json", report.to_dict())
        if report.elicitation is not None:
            # Also on its own, so a consumer that wants only the diagnostic -- a sweep comparing
            # elicitation across runs, say -- does not have to parse a whole report to reach it.
            store.write_json("elicitation.json", report.elicitation.to_dict())
        if report.optimization is not None:
            # Same reason: a sweep asking how often the optimization loop saturated reads one small
            # file per run instead of parsing every report.
            store.write_json("optimization.json", report.optimization.to_dict())
        from .report_html import render_html
        store.write_text("report.html", render_html(report, store))
        if log:
            logger.info("eval %s: report -> %s", report.run_id, store.root / "report.html")
    except Exception:  # noqa: BLE001 -- reporting must not break the run
        logger.warning("failed to write report artifacts to %s", store.root, exc_info=True)


def subsample(data: list, limit: int, seed: int | None) -> list:
    """The first `limit` data points, or a seeded random `limit` of them when `seed` is given.

    WHY A RANDOM SAMPLE IS AN OPTION AT ALL. Taking the first N is not a neutral way to take fewer: eval
    data is authored and stored in id order, and that order carries structure. Measured on
    `order_to_cash`, whose 60 points split 20/40:

        clean invoices        25% of the first 20   vs  35% of the rest
        DUPLICATE faults      10%                   vs  2.5%
        OFF_CONTRACT_ITEM     absent entirely       vs  present

    So a first-20 run over-represents one fault fourfold and never exercises another at all. The skewed
    absolute level is the lesser problem; the real one is that an artifact which improved at the omitted
    category cannot be seen to have improved. A biased-but-fixed sample can make a real gain invisible.

    A SEED, NOT AN UNSEEDED SHUFFLE, because every artifact being compared must meet the SAME cases. Two
    captures scored on two random subsets are not two points on one curve, so the caller passes a seed it
    holds fixed across a series and records alongside the score.

    Sorted by data-point id before sampling, so the answer depends only on the seed and the set. For the
    per-directory dataset layout this is belt-and-braces -- that loader already sorts, and rejects a
    sample whose id does not match its directory name -- but it is what makes the guarantee hold for an
    inline `data` block too, which is authored in whatever order someone wrote it.
    """
    # The unseeded path keeps DATASET order, exactly as the caller-facing docs promise ("the first N in
    # dataset order"). Sorting it would quietly redefine which cases a `--limit` smoke test scores.
    if seed is None:
        return data[:limit]
    ordered = sorted(data, key=lambda d: getattr(d, "data_point_id", "") or "")
    # Sorted before the size check as well as after, so the seeded path's documented id ordering is
    # unconditional: raising a limit past the dataset size must not change the case order underneath it.
    if limit >= len(ordered):
        return ordered
    chosen = random.Random(seed).sample(ordered, limit)
    # Returned in id order rather than draw order: the transcript store and every report read more
    # easily when cases appear in a stable order, and the SET is what the seed decides.
    return sorted(chosen, key=lambda d: getattr(d, "data_point_id", "") or "")


def _should_check_conformance(client: ArtifactAgentClient, mode: str) -> bool:
    if mode == "off":
        return False
    if mode == "on":
        return True
    # "auto": an in-process client is treated as conformant by construction (we built it) -> skip.
    # TODO: this keys off the concrete type, but artifact_to_client also wraps the LOSSY legacy
    # bridge in an InProcessArtifactClient (see _legacy_respond) -- so a foreign artifact routed
    # through that bridge is also skipped under "auto". A benchmark RUN never hits this: production
    # always builds an HttpArtifactClient directly (orchestrator/run.py), which IS an
    # ArtifactAgentClient and returns from artifact_to_client's first branch, never reaching the
    # bridge. The bridge's real callers are tests and gold/slides_generator/eval_gold.py, whose
    # artifacts we author ourselves. Revisit when a real caller starts passing a foreign
    # `.respond`-only artifact through this path.
    return not isinstance(client, InProcessArtifactClient)


def artifact_to_client(artifact: Any) -> ArtifactAgentClient:
    """Adapt a built artifact into an ArtifactAgentClient.

    - already an ArtifactAgentClient -> returned as-is;
    - a legacy `appsci_bench.eval.artifact` artifact (has `respond`) -> bridged (TEMPORARY, see module docstring);
    - a plain callable `fn(ArtifactAgentQuery) -> ArtifactAgentOutput|dict|str` -> wrapped.
    """
    if isinstance(artifact, ArtifactAgentClient):
        return artifact
    if callable(getattr(artifact, "respond", None)):
        # NOTE: wrapping in InProcessArtifactClient means "auto" conformance SKIPS this lossy bridge
        # (see _should_check_conformance TODO). No benchmark run reaches it -- production always
        # builds an HttpArtifactClient directly and never a `.respond`-only artifact -- so its
        # callers are tests and gold/slides_generator/eval_gold.py, whose artifacts we author.
        return InProcessArtifactClient(lambda q: _legacy_respond(artifact, q))
    if callable(artifact):
        return InProcessArtifactClient(artifact)
    raise EvalTypeError(f"cannot adapt {type(artifact).__name__} to an ArtifactAgentClient")


def _legacy_respond(artifact: Any, query: ArtifactAgentQuery) -> ArtifactAgentOutput:
    """Bridge one query through a legacy `appsci_bench.eval.artifact` artifact. Lossy by nature (the legacy
    ArtifactQuery has no text/json split) -- a stopgap until artifacts serve sessions. Input files DO
    cross the bridge, but the return path does not: unlike HttpArtifactClient, it never reads
    `ArtifactResponse.json`/`.files` off the response and never calls `materialize_files`, so a
    produced deck would be dropped. Not currently exercised in production (see artifact_to_client)."""
    from appsci_bench.eval.artifact import ArtifactQuery, ArtifactResponse  # local: legacy, to be removed
    resp = artifact.respond(ArtifactQuery(intended_task=query.text or query.json or "",
                                         files=list(query.files)))
    trace = resp.trace if isinstance(resp, ArtifactResponse) else None
    out = resp.output if isinstance(resp, ArtifactResponse) else resp
    if isinstance(out, ArtifactAgentOutput):
        output = out
    elif isinstance(out, dict) and any(k in out for k in ("text", "json", "files")):
        output = ArtifactAgentOutput.from_dict(out)
    elif isinstance(out, str):
        output = ArtifactAgentOutput(text=out)
    else:
        output = ArtifactAgentOutput(json=json.dumps(out, default=str))
    if trace and output.trace is None:
        output.trace = trace
    return output
