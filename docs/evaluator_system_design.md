# Evaluator System Design

## Definition

We call the system that

- Gets from each task its evaluation dataset and required metrics
- Queries the final artifact agent endpoint with the task’s dataset and gathers outputs
- Evaluates these outputs with the task’s specified metrics
- Outputs the score(s) as the final score(s) of the applied scientist agent to the benchmark runner

the evaluator system.

## Protocols

### Artifact agent APIs

After the appsci agent runs a task, we should have a served artifact agent with an endpoint to call. The endpoint should allow a suite of `/create_session`, `/query_session`, and `/delete_session` calls.

**Direction of the contract — who implements which half.** This API is a contract with two ends:

- **Server end = the artifact agent** (what the appsci agent builds and serves). Its produced artifact must *expose* `/create_session`, `/query_session`, `/delete_session`. This is the half the appsci agent is responsible for — its deliverable must speak this protocol.
- **Client end = the evaluator** (this system). The evaluator is the *caller*: it holds a client that calls those endpoints. The client is transport-agnostic — the same generation loop drives the artifact whether it is served over MCP, over HTTP, or wrapped in-process for tests. The client is **not** the artifact agent; it is the mirror of the contract on the calling side.

The `ArtifactAgentQuery` / `ArtifactAgentOutput` objects below are the shared wire format both ends agree on. In one run: the build phase produces and serves the artifact (server end); the eval phase points a client at it and, per data point, calls `create_session` → `query_session` (once for single-turn, in a loop for multi-turn) → `delete_session`.

> **Migration note:** the current repo hands the artifact off as a single `respond(query)` call, not a session API. Having the appsci agent's artifact *serve* `create/query/delete` sessions is real work on the agent side (an adapter can bridge an in-process artifact in the meantime). Tracked as part of wiring the orchestration to a real served artifact.

> **Contract publication & conformance.** Two things make an artifact scorable, and both are built.
> (1) **Publication** — the contract here is handed to the appsci agent at build time as
> machine-readable JSON, not per-task prose: it is loaded from the
> versioned contract registry via `session_api_contract()` (`appsci_bench/eval/contract.py`) when
> the runner assembles the agent interface. (2) **Conformance** — before eval, the evaluator runs a
> probe session (`create → query → delete`) that validates the served artifact actually implements
> the session API with the right shapes (`check_conformance`, `appsci_bench/eval/conformance.py`),
> aborting the run with a clear `ConformanceError` rather than producing garbage scores.

#### `/create_session`

- Input: an optional `context` object (reserved for future per-case starting-state; accepted today
  but not yet populated or consumed by anything)
- Output: a `session_id`, string

> **Open question (deferred):** the client signature already reserves a `context` param on
> `/create_session` for forward compatibility, but nothing populates or consumes it yet, so it does
> not actually set up the artifact environment's *starting state* for a given data point. See the
> deferred note under [Datasets](#datasets). Tracked, not built.

#### `/query_session`

- Input
  - `session_id`, string
  - `query` (an `ArtifactAgentQuery` object), which may contain
    - An optional string `text` field
    - An optional string `json` field
    - An optional list of objects `files` field. Each File element contains
      - A required string `file_type` field, which should only be one of pdf, docx, pptx, csv, xlsx, jpg, png, txt, md
      - A required string `file_name`.
      - A required binary field `file_data`
- Output (an `ArtifactAgentOutput` object), may contain
  - `text`, string; exposed to the artifact agent user sim
  - `json`, string; exposed to the artifact agent user sim
  - `files`, list of File defined the same way as above. The artifact agent user sim sees only each file's `file_type` and `file_name` (and that a file exists), never the `file_data` content.

Enforce application/json for now. We can worry about large files later.

#### `/delete_session`

- Input: `session_id`, string
- Output: `success`, boolean

### Datasets

The task spec should provide a dataset for evaluating the artifact agent. The dataset format should be:

- A required string field `type`: either single_turn or multi_turn
- An optional integer field `max_turns`: defaults to 10. Controls how many turns the artifact agent has during user simulation.
- Alternatively, `max_steps` selects a shared simulation-step budget and disables the exchange
  ceiling; setting both is rejected. Banking uses 200. Customer text/stop and artifact text each
  consume one step. Tool requests and results consume two, including failed tools. A tool pair
  that begins one step below the ceiling finishes one step above it, as upstream allows the
  pending environment result before checking termination. Limits are checked before another
  speaker runs, including when a stop message itself reaches the ceiling.
  Banking enforces tool steps inside its environment, polls the shared counter during artifact
  queries, and stops/restarts an in-flight artifact before the lane is reused. Transcripts retain
  `simulation_steps` and `termination_reason: max_steps`; banking scores that termination as zero.
  Existing artifacts keep their interface: each individual HTTP tool request is a tool pair.
  This differs from upstream's grouping of parallel calls in one model message. Private/internal
  artifact computation and tools that do not reach the task environment are not simulation steps.
- An optional integer field `repeat`: defaults to 1, Controls how many times each data point should be rerun for better stability.
- A required field `data`, which is a list of objects, each being an EvalSample. For each object, it has
  - `data_point_id`: a unique string id of the data point
  - An optional `single_turn_input` field, an `ArtifactAgentQuery` object (the query field of the Input of the /query_session endpoint)
  - An optional object `multi_turn_input` field, which contains
    - A required string `user_task` field
    - A required string `user_persona` field
    - An optional string `user_stop_criteria` field. The default is “The user task has been successfully completed.”
    - An optional list of objects `files` field, defined the same way as above.
  - An optional `expected_output` field, an `ArtifactAgentOutput` object (the Output of the /query_session endpoint)
  - An optional string field `eval_rubric`, which optionally allows the task designer to define what "success" looks like for THIS DATA POINT, to help the LLM evaluator. This is per-data-point and
    varies sample to sample (or is absent for most samples) -- it is **not** where a metric's own
    scoring criteria goes. A metric's rubric/scoring bands are authored once by the task designer and
    belong baked into that metric's own eval function/prompt (fixed for every sample of that task),
    never squeezed into this per-sample field. See [Task-Specific Evaluation Function](#task-specific-evaluation-function).

**On disk, a task's dataset is a directory, not a single file:** one subdirectory per
`data_point_id`, each holding a `sample.json` (the EvalSample above, pretty-printed for
human review) plus any files it needs. A `files[]` entry may give either inline base64
`file_data` (small files) or **`file_path`**, a path relative to that sample's own subdirectory
(large files -- avoids inlining base64 into a shared file). Exactly one of the two is required.
`file_path` is a dataset-authoring convenience only, resolved and read lazily by the loader
(`appsci_bench/eval/dataset.py`) -- it is **not** part of the `/query_session` wire protocol above,
which always carries real `file_data`; the loader materializes a sample's files into real bytes
before it's ever handed to the artifact client or an eval function. See
`schema/dataset.schema.json` and `tasks/_TEMPLATE/eval_data/` for a worked example.

> **Open question (deferred):** the dataset will also need to encode the per-case *starting state* of the artifact agent's environment, and we will need a protocol with the artifact environment to set that state up before each session. Not designed yet — tracked, not built.

### Metrics and Objectives

The task spec should provide a list of objectives, defining certain metrics to calculate over certain fields. Each Objective object in this list should contain:

- A required string field `name`, e.g. classification_correctness, conversation_quality, task_resolution
- A required string field `eval_function_path`. This is the python importable path under the tasks/ directory of the function used to calculate the metric. e.g. if function_path is order_to_cash.eval.classification_correctness , import tasks.order_to_cash.eval.classification_correctness as eval_function needs to be valid.
  - See [Task-Specific Evaluation Function](#task-specific-evaluation-function) for details
- A required boolean field `positive`, where True means the higher the score on this metric is, the better the agent did over this metric, and False otherwise. When False, the aggregator inverts the score (uses 1 - score) before combining, so the final score is always higher-is-better.
- A required float value `weight`, which tells the final score aggregator how much weight this metric has within the final score
  - Note that weight of all metrics for one task does not need to add up to 1. A weighted average will be performed.

Note: certain combinations of metric and data does not make sense. It is up to the task designer to make sure that the ones defined in their evaluation functions do. The evaluator system promises to

- Make an attempt to flag non-compatible data addresses and metrics when possible before running over each data point
- Fail without breaking the whole benchmark orchestration when failure is unavoidable

## Task-Specific Evaluation Function

The task designers are responsible for bringing in their own eval_functions, as defined in [Metrics and Objectives](#metrics-and-objectives). The function must conform to the following rules

- The function signature must be (params are **keyword-only** — the orchestrator calls by keyword,
  so the contract can grow / reorder fields additively without breaking existing eval functions)

  ```
  <name>(*, data_point_id: str, conversation: list[ConversationTurn], expected_output: ArtifactAgentOutput | None = None, eval_rubric: str | None = None, **kwargs)
  ```

  - `data_point_id`: the id of the data point
  - `conversation` is a list of ConversationTurn objects, defined by
    - `query`: the input to the agent (by the artifact agent user sim or directly from the data) in one turn, an `ArtifactAgentQuery` (the query parameter of the Input to the /query_session endpoint)
    - `response`: the output of the agent in one turn, an `ArtifactAgentOutput` (the Output of /query_session)
  - `expected_output` is the same as the one in data (an `ArtifactAgentOutput` or None)
  - `eval_rubric` is the same as the one in data (a string or None) -- a per-DATA-POINT note, not a
    place for the metric's own scoring criteria (that belongs in the eval function's own prompt,
    fixed at authoring time; see the warning under "Datasets" above)
  - Two more keywords ride along inside `**kwargs`, for the eval functions that want them. Both are
    additive: an eval function that ignores them is unaffected.
    - `final_state` (a dict or None) -- whatever generation's `read_final_state` hook captured after
      the conversation, e.g. `{"env_state_hash": ...}` for a DB-state check.
    - `termination_reason` (a string or None) -- HOW the conversation ended: `user_stop` (the user sim
      judged its stop criteria met), `complete` (single-turn: the one query was answered),
      `max_turns` (the turn ceiling cut the sim off, so the conversation is TRUNCATED), or `error`
      (a query failed). The benchmark only records the fact; what it means for a score is the task's
      decision, because it genuinely differs per task -- a truncated conversation invalidates a
      reward defined on the end state, but may be unremarkable for an open-ended generation task.
      `None` means not recorded (a hand-built record, or a transcript written before this field
      existed) and must be read as UNKNOWN, never as premature. `tasks/airline_rebooking/eval.py`
      is the worked example: it scores a truncated trace 0 before evaluating anything else.
- The function must output
  - A score, which is the score of the metric for the input data point
    - The score MUST be a float in [0, 1], OR `None`.
    - `None` means this metric does not apply to this data point (e.g. a false-negative-rate
      metric on a sample the ground truth says should NOT be flagged). A `None` result is
      EXCLUDED from aggregation entirely -- it contributes to no average, at any level (the
      per-data-point view, the per-metric view, or the final task-wide score) -- rather than
      being penalized. This is distinct from an eval-function error (design: below), which is
      still forced to the worst score (0.0), a penalty, not an exclusion. If every data point
      returns `None` for a given metric, that metric's aggregated score is also `None` (and a
      warning is recorded, not a failure -- see Evaluation System step 3). A task designer who
      wants to intentionally exclude a metric from the final score should use `weight: 0` on its
      objective, not rely on the eval function always returning `None` -- the two look identical
      in effect but only `weight: 0` is discoverable by reading the spec.
  - A dictionary metadata, which is most likely empty but could contain things like LLM judge reasoning, etc.

How `expected_output` is compared against the agent's actual output (parsing the `json` string, choosing which fields matter, numeric tolerances, etc.) is the task designer's responsibility inside their eval_function; the system does not impose a comparison semantics.

The evaluation system provides out-of-box helper functions for constructing evaluation functions
(`appsci_bench/eval/helpers/`; see [`appsci_bench/eval/README.md`](../appsci_bench/eval/README.md#module-map-part-2----helpers) for the full module map and a "which level to reach for" guide):

- Simple mechanical prediction-vs-ground-truth comparisons: `resolve_scalar` (numeric-with-units, boolean aliases, normalized-string), `text_similarity` (continuous free-text similarity, no LLM), `json_field_match` (per-field JSON equivalence composed from the two) — `helpers/deterministic.py`.
- An LLM judge function that calls a predefined LLM and a prompt, in three levels of composition: `llm_judge_call` (Level 0, a generic structured-output call); `make_transcript_judge` (Level 1, judges the conversation — text/json only, never file content); `make_file_similarity_judge` (Level 2, the only helper that reads file bytes, judging functional equivalence of produced files against ground truth, not structural/byte identity) — `helpers/llm.py` / `transcript.py` / `files.py`.
- A set of LLM judge functions with pre-defined metrics, such as the ones for conversation quality, task resolution, etc. — `helpers/judges/builtin.py` (`conversation_quality_judge`, `task_resolution_judge`, `process_compliance_judge`).

Note that task designers are not limited by these ways of evaluating a single data point. Any function can be written as the eval_function, given that it conforms to the input and output rules mentioned above.

For example, it is technically possible for the task designer to calculate scores of different data points differently inside their eval_function, based on the data_point_ids, making per-sample granularity possible.

The evaluation system promises that, if the eval_function errors out due to any failure, it would not break the main evaluation loop. All errors from the eval_function are caught and converted to a score of 0, with the error message recorded in an `error` field on that result (so the failure is visible in the output rather than truly silent). Each eval_function call is also subject to a timeout; a timeout is treated like any other failure (score 0 with the error recorded).

- Task designers are encouraged NOT to rely on this behavior, and instead catch their own errors and output more appropriate scores in different scenarios.

## Artifact Agent User Sim

> Note: this artifact agent user sim is an entirely separate component from the appsci agent user sim used while the appsci agent is building the artifact (requirements elicitation). They share nothing and should not be conflated.

By design, the artifact agent user sim sees the `text` and `json` fields of the output and gives appropriate responses based on them. It does not see file content in either direction (files may be too large): for the artifact's output files it sees only that a file exists, plus `file_type`/`file_name`; for its own seeded input files (`multi_turn_input.files`, see [Datasets](#datasets)) it likewise knows only a file's name and type when deciding whether to attach it, never its content. Transcripts generated this way contain the text and json queries and responses between the simulator and the artifact agent.

Implemented in [`appsci_bench/eval/artifact_agent_user_sim.py`](../appsci_bench/eval/artifact_agent_user_sim.py) (`ArtifactAgentUserSim`, session-based, LLM-backed) and constructed by the orchestrator's eval phase (`appsci_bench/orchestrator/run.py`) for every `multi_turn` task run.

### Model

The artifact agent user sim runs on **the user-simulator model the published airline submissions declare** — `openai/gpt-5.2`. Our tasks are converted from tau-bench, so matching its simulated user keeps eval-time user behavior comparable to the published benchmark instead of introducing a second, unmeasured variable.

**The authority is the submissions, not tau2's code default.** This used to track `DEFAULT_LLM_USER` in `src/tau2/config.py`, which is still `gpt-4.1-2025-04-14` even at v1.0.1 — but that constant is only the default behind `--user-llm`, and the published runs override it. Every text airline submission at `tau2_bench_version` 1.0.1 declares `user_simulator: "gpt-5.2"`, including both rows we compare ourselves against (Claude Opus 4.5 at airline `pass_1` 84.0, Qwen3.5-397B at 81.5); the rows still declaring `gpt-4.1-2025-04-14` are all on v0.1.3. No changelog entry announces the switch, because it is a submission-time run choice rather than a code change — which is exactly why reading the config file was misleading. Matching a default we are not measured against is the wrong kind of faithfulness.

**The pin is floating, reversing the previous rule.** It was deliberately a dated snapshot so an alias re-point could not move eval scores without a code change. No submission names a dated gpt-5.2 snapshot, so a dated pin would name something the leaderboard does not and drift from the thing we are matching. We accept the floating id and inherit its risk: if the provider re-points gpt-5.2, our scores can move with no commit here. That is now a known uncontrolled variable (see `docs/baselines.md`), not a guarantee.

This is deliberately **not** Claude like the rest of the benchmark. Every LLM role has its own model var — `AGENT_MODEL` (agent under test), `APPSCI_USERSIM_MODEL` (build-time customer sim), `JUDGE_MODEL` (eval-side judges), and `ARTIFACT_USERSIM_MODEL` (this sim). `APPSCI_USERSIM_MODEL` and `JUDGE_MODEL` default to Claude via the shared `MODEL_NAME`/`MODEL_API_KEY`/`MODEL_BASE_URL`; `AGENT_MODEL` has its own credentials (`APPSCI_AGENT_BASE_URL`/`_API_KEY`) and its own hardcoded default, reading `MODEL_NAME` nowhere. Only this sim — the one whose behavior is being held comparable to a public benchmark — is pinned to tau-bench's choice.

**Configuration is one variable:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `ARTIFACT_USERSIM_MODEL` | `openai/gpt-5.2` | LiteLLM model id for this sim |

Unlike the two roles that share the `MODEL_*` namespace it does **not** fall back to `MODEL_NAME`, and it has no matching `_API_KEY`/`_BASE_URL` pair, because none is needed: credentials come from LiteLLM's provider-standard resolution — `OPENAI_API_KEY` for the default model, `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` (or `OPENAI_BASE_URL` etc.) for whatever an override points at. That covers both a first-party API and a gateway without a second set of knobs to keep in sync with `MODEL_*`.

The consequence to plan for: this sim needs OpenAI credentials reachable from wherever eval runs, and they are *not* inherited from `MODEL_API_KEY`/`MODEL_BASE_URL` (the Anthropic gateway is the wrong endpoint for an `openai/` model id).

Two ways to satisfy that:

- **First-party OpenAI** — export `OPENAI_API_KEY` and leave `ARTIFACT_USERSIM_MODEL` unset. This is the true tau2 pin, and what a reported score should run on.
- **Internal gateway (gateway)** — gateway serves gpt-5.2 (`gateway models --provider openai` → `openai-gpt-5.2`, active) via an OpenAI-compatible endpoint at `$ANTHROPIC_BASE_URL/v1`. `the setup steps in README.md` maps that onto `OPENAI_BASE_URL`/`OPENAI_API_KEY` and sets `ARTIFACT_USERSIM_MODEL=openai/openai-gpt-5.2`. The gateway does not recognize the first-party id (`unknown model "gpt-5.2"`), so its own alias is required — same model, but what the gateway routes the alias to isn't verifiable from our side, so treat it as near-parity rather than identical. The id is duplicated across two setup scripts and the human-baseline overlay; `tests/appsci_bench/eval/test_user_sim_id_mirrors.py` asserts they agree, because a stale copy fails silently — an unknown model errors every case, errored cases are forced to `0.0`, and the run reports a clean `0.000` with a `done` status.

That distinction is enforced in code, not just prose: `parity_status()` grades a resolved model as **exact** (the pin), **near** (a known gateway alias in `_NEAR_PARITY_ALIASES`), or **diverged** (anything else). Near-parity logs at INFO and is *not* recorded on the report — it is the documented internal path, so flagging it per run would train readers to skip `EvalReport.warnings`. Diverged logs a warning and appends to `warnings`, so a report whose scores aren't tau-bench-comparable says so on the artifact itself.

Pointing `ARTIFACT_USERSIM_MODEL` at a *different* model (e.g. a Claude id) is a legitimate local-debugging move but makes the run no longer tau-bench-comparable, so it shouldn't be how a reported score is produced. The superseded gpt-4.1 ids now grade as **diverged** rather than near-parity, so a stale override left in an environment or on the jobs host is loud instead of silently accepted.

**One uncontrolled variable, recorded rather than fixed:** this sim sends no sampling parameters at all — `complete()` forwards only the model, messages, retries, credentials, headers and `response_format`, and a test locks the absence of `temperature`. So the sim runs at whatever the provider's defaults are, and `ModelConfig` has nowhere to put a reasoning-effort setting. Worth knowing because on the *agent* side the leaderboard shows a 30.5-point airline swing from that one flag; the submissions declare reasoning effort for the agent, not for the user simulator, so there is nothing to match here yet.

## Evaluation System

The evaluation system goes through 3 steps.

1. Using the evaluation data provided by a task and the predefined endpoint, generate the output of the agent for each data point.
   - Multi-thread this process
   - For each data point, create an artifact agent session, query the session, store the full transcripts and artifacts generated, and delete the session.
   - Each artifact query is subject to a timeout; a timed-out or failed query is recorded as such (and scored as a failure downstream) rather than stalling the run.
   - Log everything generated in a “database”. This can be a tmp file directory.
     - Each conversation should be their own json file.
2. Using the transcripts and artifacts generated and the eval functions provided by the task, calculate the scores of each data point
   - Parallelize this process per data-point-metric pair; task pool structure since each pair can have very different latency (e.g. static string comparison vs. LLM judge).
3. Aggregate the scores from each metric and each data point. Several views to support
   - Per data-point-metric score matrix, averaged across repeats
   - Per data point score list by weighted average of metrics for each data point
   - Per metric score list by averaging across data points
   - Final task-wide score through weighted averaging of metric and data points.

   Individual scores need not be reproducible across runs (LLM judges and the artifact agent are non-deterministic), but the final score output MUST have a deterministic ordering (e.g. data points sorted by `data_point_id`, metrics by `name`).

**Steps 1 and 2 interleave, and step 3 runs repeatedly.** The three steps above describe what is
computed, not that each finishes before the next begins. In the implementation a case is generated,
scored, persisted and emitted before the run moves on, and the report is re-aggregated and rewritten
after every scored case, flagged `partial` until the dataset is done.

The reason is that an eval is long enough to be killed: against a single task environment a full
`banking_support` run is ~4h of cases taken one at a time (per-sample DB seeding is a whole-store
operation, so two cases cannot share one environment -- `--eval-lanes N` gives them N), and one
SIGKILLed by its worker
timeout with 80 of 97 cases generated recorded **none** of them — the store was written after the last
unit, scoring had not started, and the store root was a temp dir copied out at the end. Treating the
steps as phases is what made a partial run worth zero.

Two constraints come with it, both load-bearing:

- The "database" of step 1 is **not** a tmp directory for a real run. The orchestrator points the store
  at `runs/<trace_id>/eval/`, which is what the tracer reads and the S3 mirror ships, and every write is
  atomic — those files are parsed by other processes while the run is still writing them.
- A report that describes part of a dataset must be unmistakable. `partial` plus
  `cases_scored`/`cases_total` ride on the report and travel with the score into every renderer; a
  partial `final_score` is the weighted mean over the finished cases, which is a progress figure and not
  comparable with a complete run. See
  [`appsci_bench/eval/README.md`](../appsci_bench/eval/README.md#partial-progress-a-killed-eval-keeps-the-cases-it-finished).

   A `None` score (design: Task-Specific Evaluation Function) is excluded from every view above,
   not counted as 0 or averaged in. If a metric's score is `None` at every data point (or, per
   data point, every metric's score is `None`), the corresponding aggregated cell is `None` too --
   the aggregator records a loud warning (not a run failure) so a metric that's silently never
   applicable doesn't go unnoticed.

## Implementation Split

We build this system in four independent parts. Each has its own implementation plan; they meet at narrow, well-defined seams so they can be built and tested separately. Live status for all four
lives in [`appsci_bench/eval/README.md`](../appsci_bench/eval/README.md) — that table is the source
of truth; this section only describes what each part is and consumes.

1. **Eval system orchestration** — the task contact and the main run loop. This part reads each task's dataset and objectives, talks to the served artifact agent over the session API (`/create_session`, `/query_session`, `/delete_session`), drives the 3-step run (generate outputs → score each data-point-metric pair → aggregate), stores transcripts/artifacts, and emits the final scores. It owns the shared data types (`ArtifactAgentQuery`, `ArtifactAgentOutput`, `File`, `ConversationTurn`, the dataset and objective objects). It is the most fully specified part above — see [Datasets](#datasets), [Metrics and Objectives](#metrics-and-objectives), and [Evaluation System](#evaluation-system) — and is built **first**. It consumes Parts 2 and 3 only through interfaces (an eval-function callable resolved by path; an artifact-agent-user-sim object), which it stubs until those parts land.

2. **Eval-function authoring helpers** — what task designers use to write their per-data-point eval functions, including the LLM-judge calling mechanism. Orchestration only imports an eval function by its `eval_function_path` and calls it; this part makes those functions easy to write correctly: the mechanical prediction-vs-ground-truth helpers, the LLM-judge function (predefined LLM + prompt), and the library of pre-built judges (conversation quality, task resolution, etc.). See [Task-Specific Evaluation Function](#task-specific-evaluation-function).

3. **Artifact agent user sim** — the multi-turn driver used by Part 1's generation step for `multi_turn` datasets. It is an **entirely separate** component from the appsci agent user sim that talks to the appsci agent during build-time requirements elicitation; they share nothing and must not be conflated. It sees the `text` and `json` of each artifact output (never file content) and produces the next user turn. See [Artifact Agent User Sim](#artifact-agent-user-sim).

4. **Task migration / override** — writing each task's `spec.yaml` `objectives`, `EvalSample` dataset, and `eval.py` in the new format instead of the pre-overhaul `metrics`/`oracle` model, which conflicts with the Part 1 contract and cannot be dual-supported. See [Task Migration](#task-migration).

This document specifies all four.

## Task Migration

`order_to_cash`, `slides_generator`, `airline_rebooking`, and `banking_support` are fully migrated: their `spec.yaml`
carries `objectives` (never the old `metrics` list), their dataset files are `EvalSample`-shaped,
and each objective has real `eval.py` content behind it. `crm_insights` is now migrated too (real
dataset + `eval.py` — see `tasks/crm_insights/seed_data/PROVENANCE.md`). `compliance_review` and `customer_call_insights` are migrated the same way, so all seven tasks
have real datasets and `eval.py`. `_TEMPLATE` carries only the spec shape, as a starting point.

**The standard for writing a task's eval code: override, don't reconcile.** `evaluation_data` may
carry `objectives` or the legacy `metrics` list, never both — the schema enforces this as a hard
either/or. There is no dual-format shim and no batch-mode entry point kept around "for
compatibility": a deterministic check is a per-data-point, keyword-only eval function (see
[Task-Specific Evaluation Function](#task-specific-evaluation-function)).

**Rubric/LLM-judge metrics point directly at the LLM-judge mechanism.** A rubric/LLM-judge metric
(e.g. `order_to_cash`'s `reason_quality`, most of `slides_generator`'s dimensions) uses
`appsci_bench.eval.helpers` directly — `make_transcript_judge` for a task-specific jinja2 prompt,
`make_file_similarity_judge` for a metric judging a produced file's content, or a `judges.builtin`
prebuilt judge for a cross-task pattern like process/policy compliance.
