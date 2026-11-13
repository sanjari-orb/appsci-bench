# eval -- evaluator system

This evaluator replaced the old two-track (A.2/A.8) scoring stack. It scores a produced
**artifact agent** over a task-defined **dataset** using **objectives** that bind to
**per-data-point eval functions**, then aggregates to a final score.

- Design: [`docs/evaluator_system_design.md`](../../docs/evaluator_system_design.md)

## Four-part split (status)

| Part | What | Status |
|------|------|--------|
| 1. Orchestration | task contact + generate -> score -> aggregate run loop, session-API client, shared types | feature-complete |
| 2. Eval-function helpers | mechanical comparisons + LLM-judge mechanism + prebuilt metric judges for task designers | feature-complete |
| 3. Artifact agent user sim | multi-turn driver for `multi_turn` datasets (separate from the build-time sim) | feature-complete and wired into the orchestrator's eval phase (`artifact_agent_user_sim.py`) -- exercised end to end by `airline_rebooking`, the one migrated task with a `multi_turn` dataset |
| 4. Task migration | each task's `spec.yaml`/dataset/evaluator code onto `objectives`/`EvalSample` | `order_to_cash`, `slides_generator`, `airline_rebooking`, `customer_call_insights`, and `banking_support` have real datasets and objectives (`customer_call_insights` on a 50-sample held-out set); the other 6 tasks got the spec-shape rewrite only and have no dataset/`eval.py` yet |

This table reflects current capability, not a rollout checklist.

## Module map (Part 1)

Done:
- `datatypes.py` -- shared protocol dataclasses: `ArtifactAgentQuery`/`ArtifactAgentOutput` (+`sim_view`),
  `File`, `ConversationTurn`, `MultiTurnInput`, `EvalSample`, `Dataset`, `Objective`, result types.
- `dataset.py` -- load + validate a task's `Dataset` (`parse_dataset` / `load_dataset`).
- `objectives.py` -- load `Objective`s + eager fail-fast `eval_function_path` resolution.
- `artifact_client.py` -- session-API client: `ArtifactAgentClient` protocol + `InProcessArtifactClient` (tests) + `Mcp`/`Http` stubs (deferred transports).
- `artifact_agent_user_sim.py` -- `ArtifactAgentUserSimulator` protocol + the real `ArtifactAgentUserSim`
  (session-based, LLM-backed) + `ScriptedArtifactAgentUserSim` stub (eval-time sim, Part 3 -- separate
  from the build-time appsci-agent-user-sim). Constructed by the orchestrator's eval phase for a
  `multi_turn` dataset and passed to `run_evaluation`. Runs on **tau-bench's user-simulator
  model** (`TAU_BENCH_USER_SIM_MODEL` = tau2-bench's `DEFAULT_LLM_USER`), read from its own
  single `ARTIFACT_USERSIM_MODEL` var -- NOT `JUDGE_MODEL`/`MODEL_NAME` that the judges below
  and the build-time sim share. See [the design doc's Model section](../../docs/evaluator_system_design.md#model).
- `generation.py` -- step 1: per-(data point, repeat) session loop (single + multi turn), threadpool, per-query timeouts, failures recorded not raised. Each unit is persisted and handed to an `on_record` hook AS IT COMPLETES, not after the batch -- see **Partial progress** below.
- `store.py` -- transcript "database": one JSON per conversation and one per unit's scores (`results/`); produced files dumped + referenced by path; a `progress.json` counter. Every write is atomic (temp file + `os.replace`), because readers parse these files while a run is still writing them. Its root is recorded on the report (`store_root`) and shown in the rendered output, so a run's transcripts are findable; defaults to a run-id-tagged temp dir when no `workdir` is given -- but the orchestrator passes `runs/<trace_id>/eval`, so a real run writes where the tracer reads and the S3 mirror ships.
- `scoring.py` -- step 2: per-(data point, objective) eval-function harness; task-pool, per-call timeouts, failures -> score 0 + error; `check_compatibility` pre-run flag. Two entry points over one definition of "which pairs are runnable" (`_plan_unit`): `score_records` scores a batch of finished records (a re-score, the tests, the demo), and `StreamingScorer` scores each unit as generation produces it -- what the eval phase uses.
- `aggregate.py` -- step 3: positive=False inversion (errors forced to 0), repeat-averaging, the four views + weighted final score, deterministic ordering.
- `report.py` -- render an `EvalReport` as text (`render_text`) or JSON (`render_json`).
- `eval_system.py` -- `run_evaluation(spec, client, ...)` wiring steps 1-3; stamps each report with a `run_id` that is unique per execution (so archived reports never collide) yet prefixed with a stable config hash (task+dataset+objectives), so runs of the same config stay groupable. `artifact_to_client` adapter (incl. a temporary legacy `eval.artifact` bridge). Named `eval_system`, not `orchestrator`, to avoid confusion with the separate top-level `appsci_bench/orchestrator/` package (container/submission-server lifecycle for a full benchmark run, which calls into this module for the eval half via `eval/evaluator.py`).
- `contract.py` -- loads the session-API contract from a **versioned registry** (`schema/contracts/session_api.v<N>.json`, validated by `session_api.schema.json`). `session_api_contract(version=None)` returns the latest (published to the agent); pass a version to load an older contract. `available_versions()` / `latest_version()` enumerate the registry. Adding a version = adding a file. The reserved (currently-empty) `create_session` `context` arg lets per-case starting state land additively later.
- `conformance.py` -- `check_conformance(client)` probe (create->query->delete; create is probed with an empty `context` to enforce acceptance now) + `ConformanceError`.

Part 1 is feature-complete. Run the end-to-end demo (a manual check in `scripts/`) with
`python scripts/evaluator_system_e2e.py` -- it prints each report as text + JSON and writes them
(plus transcripts) to a fresh gitignored `scripts/_out/<run>/` directory.

## Partial progress: a killed eval keeps the cases it finished

**Steps 1 and 2 are interleaved, not sequential phases.** A case is generated, scored against every
objective, written to disk, and emitted to the trace before the run moves on. The report in the store
root is rebuilt from the cases finished so far and flagged `partial`.

This exists because of a specific loss. A 97-case `banking_support` eval was SIGKILLed by its worker
timeout with 80 cases generated, and **nothing survived**: the transcript store was written only after
the last unit completed, scoring had not begun (it was a second phase), and the store root was a temp
dir the orchestrator copied out at the end of a path that never ran. 4.3M tokens and four hours
produced zero recorded cases. A full banking eval takes ~4h on its own, so a run that does not reach
the end is a normal event, not an exotic one.

What a run leaves on disk, as it goes, under `runs/<trace_id>/eval/`:

| | |
|---|---|
| `conversations/<unit>.json` | the turns, `final_state` and `termination_reason` for one unit |
| `results/<unit>.json` | that unit's score per objective -- one file per unit, never rewritten |
| `report.json` / `report.html` | rebuilt after every scored case; `partial: true` until the run ends |
| `progress.json` | `cases_scored` / `cases_total` and the last case, for a cheap tail |

Three properties hold it together, and each has its own tests in
[`tests/appsci_bench/eval/test_partial_progress.py`](../../tests/appsci_bench/eval/test_partial_progress.py)
(including one that SIGKILLs a real run in a subprocess -- nothing in-process can stand in for a signal
that runs no cleanup):

- **Writes are atomic.** The tracer and the experiment layer parse these files *while* a run writes
  them, so a torn file would be read rather than skipped. Every write goes to a sibling temp file and
  is `os.replace`d in, so a kill leaves the previous good version.
- **Bookkeeping cannot fail the run it observes.** The per-unit persist and both hooks
  (`generate`'s `on_record`, `StreamingScorer`'s `on_unit_scored`) swallow and log their own
  exceptions. A full disk costs one case's record, never the other 96.
- **A partial report can never read as a complete one.** `partial` travels with the score everywhere it
  is rendered -- `headline()`, `RunMetrics.report_is_partial`, the `†` in the results table,
  `experiments show` -- and the worker refuses to mark a cell DONE while it is set. `final_score` on a
  partial report is the weighted mean over the cases that finished: a live progress figure, not a task
  score, and not comparable with a full run or a published number.

**The one lag worth knowing:** a unit's results are flushed when the *next* unit is collected rather
than the instant its last objective returns, because every flush happens on the thread driving
generation (which is what keeps the store single-writer and lock-free). For a task spending minutes per
case that is invisible; the effect is that a kill may lose the most recent case's scores, never the
ones before it.

## Module map (the elicitation diagnostic)

A **standalone** diagnostic, reported next to the task score and never folded into it: of the facts the
agent had to obtain by asking the build-time customer simulator, what share did it actually get? It
shares this package because it runs in the eval phase, but it scores the *build* conversation, not the
artifact. Design: [`docs/elicitation_score_design.md`](../../docs/elicitation_score_design.md).

- `build_transcript.py` -- read a finished run's build conversation, either from its user-sim event log
  (paired by turn) or from a plain captured terminal transcript (the form the human-baseline attempts
  used), and render it for the judge. The render deliberately carries no run id, timestamp or duration
  so it is byte-identical across items -- that is what lets it serve as a cached prompt prefix.
  Missing or corrupt input reports as *unreadable* rather than as an empty conversation, because zero
  and unmeasurable mean opposite things.
- `elicitation_rubric.py` -- load + validate a task's rubric (`tasks/<id>/elicitation_rubric.yaml`,
  schema `schema/elicitation_rubric.schema.json`). Held-out answer-key material, pointed at from the
  spec's `evaluation_data`. Two validation layers: the file in isolation (shape, the 50-item cap, id
  uniqueness) and the cross-file check that every objective an item claims to feed is a real scored
  objective of that task.
- `elicitation.py` -- the judge (one item, one model call, one verdict) and the scorer that fans a whole
  rubric out over the eval phase's shared pool. The first item is judged on the calling thread so the
  cached prefix exists before the rest launch. Four statuses, and the one that matters is that
  *unmeasurable* is not a zero.
- `workers.py` -- the eval phase's one bounded pool, shared by scoring and the elicitation fan-out so a
  run has one concurrency budget rather than two that cannot see each other.

The result rides on `EvalReport.elicitation`, beside `results`/`views`/`final_score` and never inside
them, is written out separately as `elicitation.json`, gets its own section in `report.html`, and is
summarised once per run on the trace as `eval.elicitation`.

## Module map (the optimization diagnostic)

The second **standalone** diagnostic here, and the same shape as elicitation: reported next to the task
score, never folded into it, and about the *build* rather than the artifact. It answers six questions
about the agent's optimization loop -- how many times it ran, whether it stopped early on saturation or
merely ran out of cycle budget, how many of its redeploys failed to build, how many of its own changes
it kept and how many it rolled back, how many durable candidates it left, and where its own
internal-eval score started, peaked and ended. Best is kept apart from final
because a change is kept when it beats the *previous* measurement rather than the best one. Whether the
loop actually ENDED below its best is a separate field (`ended_below_best`) and is decided on the
candidate's content hash, not on `final < best`: the loop's own eval is not deterministic, so most runs
where the numbers fall had never changed the artifact at all. The roll-back counts are also what
reconcile the deploy tally with the checkpoint tally: a restore is a redeploy that is not a change, so
it leaves a checkpoint the deploy counts do not explain. Design:
[`docs/optimize_agent_design.md`](../../docs/optimize_agent_design.md) §9.

- `build_optimization.py` -- `read_optimization(run_dir)`, the whole diagnostic. Nothing is judged and
  no model is called: every number is counted out of records the run already wrote, which is what makes
  it re-runnable over runs that finished before the metric existed. Three sources, each for what it
  authoritatively knows -- the agent's event log for the calls, the orchestrator's `build.start` for
  whether the optimization route existed at all, and the run's checkpoint manifest for which deploys
  survived as candidates. A checkpoint is attributed to the loop when it was created while a call was in
  flight, bounded by the end of the build, since the manifest records the deploy and not what asked for
  it. Four statuses, and the distinctions are the point: the ablation arm (`disabled`), an agent that
  never called the tool (`not_attempted`) and a trace that cannot say (`unmeasurable`) would all read as
  "0 optimization runs" otherwise.

The result rides on `EvalReport.optimization`, is written out separately as `optimization.json` (by
the orchestrator, for runs that built something -- an eval-only trial's copy is the one under its
`eval/`, counted over the BUILD's run directory), gets its own section in `report.html` and in the
tracer's Eval tab, and is summarised once per run on the trace as `eval.optimization`. `scripts/score_optimization.py` produces the same result for runs that
have already finished, one run or a whole directory of them.

## Module map (Part 2 -- `helpers/`)

Everything a task designer imports to write an eval function; orchestration (Part 1) never imports
this package, it only calls whatever `eval_function_path` resolves to.

- `deterministic.py` -- `resolve_scalar` (smart single-value compare: numeric-with-units, boolean
  aliases, normalized-string, explicit "incomparable" fallback), `text_similarity` (continuous
  [0,1] free-text similarity, no LLM), `json_field_match` (per-field JSON equivalence composed from
  the two above), `make_json_field_eval_function` (factory: a ready `eval_function_path` target for
  the common single-field case).
- `llm.py` -- Level 0: `llm_judge_call` (a structured-output chat call via
  `appsci_bench.common.model_config`'s `ModelConfig`/`complete`, parsed into a pydantic
  `output_schema` with a fenced-code-block/embedded-JSON fallback, and re-sampled up to
  `JUDGE_PARSE_ATTEMPTS` times when the response cannot be parsed at all), `JudgeVerdict` (the default
  schema: `reasoning` then `score`, bounds-checked to [0,1]), `JudgeParseError`.
- `transcript.py` -- Level 1: `make_transcript_judge` (wraps `llm_judge_call` into a ready
  eval-function judging the CONVERSATION -- text/json only, files reduced to existence+type+name,
  never `file_data`), `render_conversation_for_judge`.
- `files.py` -- Level 2: the helpers that read full `file_data` (native document/image content
  blocks when the model supports them via LiteLLM's capability database, full text extraction
  otherwise (pptx/docx/xlsx/csv/txt/md, never a summary/sample); a `max_content_tokens` GUARD that
  only kicks in on actual overflow -- "guard, not limit"):
  - `make_file_similarity_judge` -- compares a PRODUCED file against the gold `expected_output.files`
    for functional equivalence; degrades the payload on overflow, flagged in `metadata["degraded"]`.
  - `make_source_grounding_judge` -- reads the query's INPUT source files and checks the produced
    output against them for fabrication/arithmetic errors; hard-bails to `None` on overflow
    (truncating a source figure would corrupt the check).
  - `make_source_coverage_judge` -- the same input-reading machinery asking the opposite question:
    what PROPORTION of the source material the output covers; degrades on overflow of the inputs like the similarity judge, and bails on overflow of the putput. 
- `judges/builtin.py` -- prebuilt Level-1 judges for cross-task metrics that repeat near-verbatim
  across tasks' rubric lists: `conversation_quality_judge`, `task_resolution_judge`,
  `process_compliance_judge`.

**Which level to reach for:**
- Comparable ground truth exists (a flag, an enum, a number, a short exact-ish string, a JSON blob
  with known fields)? → `deterministic.py`. No LLM, no cost, deterministic.
- Judging the AGENT'S CONVERSATION -- was it clear, did it resolve the task, did it follow a
  process/policy? → `transcript.py` (`make_transcript_judge`), or one of `judges/builtin.py`'s
  ready judges if your metric matches a cross-task pattern (check there before writing a new prompt).
- Judging a PRODUCED FILE'S CONTENT for functional equivalence against ground truth (e.g. two decks
  or PDFs that differ in structure but do the same job)? → `files.py`
  (`make_file_similarity_judge`). 
- Judging a produced output against the query's INPUT source files
  is it grounded (no fabrication)? → `make_source_grounding_judge`; does it cover the source
  (proportion of facts captured)? → `make_source_coverage_judge`. 
- Something bespoke that doesn't fit these shapes? → `llm.py`'s `llm_judge_call` directly, or a
  hand-written eval function with no helper at all -- the contract (below) is all that's required.

## Eval-function contract (for task designers)

```
<name>(*, data_point_id: str, conversation: list[ConversationTurn],
       expected_output: ArtifactAgentOutput | None = None,
       eval_rubric: str | None = None, **kwargs) -> tuple[float, dict]
```
Score MUST be a float in [0, 1]. Params are **keyword-only** (the orchestrator calls by keyword), so
the contract can add / reorder fields additively without breaking existing functions. Authoring
helpers live in `helpers/` (module map above) -- use them, or write the function by hand; both
are fully supported, the helpers just cover the common cases so most tasks don't have to.

**Where to declare it.** Anywhere importable **under the `tasks/` package** — `eval_function_path` is
resolved as `tasks.<path>`, where the last dotted segment is the function name and the rest is its
module. So `order_to_cash.eval.classification_correctness` imports `tasks/order_to_cash/eval.py` and
takes its `classification_correctness` attribute. (`resolve_eval_function` in `objectives.py`.)

Best practice (a convention, not enforced):
- Co-locate scoring with the task: put eval functions in `tasks/<task_id>/eval.py`, or an `eval/`
  subpackage (`tasks/<task_id>/eval/…`) if a task needs several modules. Reference them as
  `<task_id>.eval.<fn_name>`.
- Make sure the chain is importable (each dir reachable as a package) and keep import-time side
  effects out — modules are imported eagerly at load (fail-fast resolution), before any artifact runs.
- One function per objective; shared helpers can live alongside and be imported normally.

## Testing

Unit tests: `tests/appsci_bench/eval/` (Part 1) and `tests/appsci_bench/eval/helpers/` (Part 2,
incl. `helpers/judges/` for the prebuilt library). Fixtures + sample eval functions:
`tests/fixtures/eval_task/`. Run `python -m pytest tests/appsci_bench/eval/ -q`.

## Status

The generate -> score -> aggregate loop (`run_evaluation`) is wired into the CLI eval phase and
publishes + conformance-probes the session-API contract before scoring. The eval-function helpers
(deterministic comparisons, the LLM-judge call, the transcript judge + prebuilt library, the
file-similarity judge) all exist and are unit-tested with no live creds required. `--phase eval`
works today for `order_to_cash`, `slides_generator`, `airline_rebooking`, `customer_call_insights`,
and `banking_support` -- the tasks that have

real `objectives`/`EvalSample` data; every other task still raises the "not migrated" error until
its own dataset and `eval.py` are written. See the table at the top for the live per-part status.
