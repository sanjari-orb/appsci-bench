# Evaluation framework (`appsci_bench`)

How the benchmark evaluates an applied-science agent. A run has two phases per task:

```
build : the benchmark provides requirements (an appsci agent user sim over the task's user-personas) and an
        environment (a Docker sandbox of the customer's DBs/KBs/APIs/docs/sample data) to the agent
        under test; the agent (given the task's description) gathers requirements, explores the env,
        and returns an ARTIFACT -- a served artifact agent.
eval  : the benchmark queries the artifact over the task's held-out dataset and scores each objective
        with the task's eval functions, then aggregates to a final score.
```

The agent's endpoint is the **input** to a run; the **artifact is created during the run**. The task
spec + dataset are ground truth, never shown to the agent. A run is a single combined command per task
(`runner.py`; `--phase`/`--stop-after` are dev escape hatches).

> The evaluator scores a produced artifact agent against a task's dataset using **objectives** bound
> to per-data-point eval functions. Design: [`docs/evaluator_system_design.md`](evaluator_system_design.md);
> implementation status and module map: [`appsci_bench/eval/README.md`](../appsci_bench/eval/README.md).

## The artifact and the session API

The artifact is a **served artifact agent** the evaluator queries over a small session API
(`create_session` → `query_session` → `delete_session`, exchanging `ArtifactAgentQuery` /
`ArtifactAgentOutput` = `{text?, json?, files?}`). The full contract — endpoints, payloads, versioning,
conformance — is documented once in [`docs/artifact_agent_api.md`](artifact_agent_api.md) (schema:
[`schema/contracts/session_api.schema.json`](../schema/contracts/session_api.schema.json)) and is
**published to the agent** at build time as part of its build context. The benchmark drives it
through a transport-agnostic client (`eval/artifact_client.py`): in-process today, MCP/HTTP later.

> **Compatibility note:** the legacy `appsci_bench/eval/artifact.py` single-call `respond(ArtifactQuery)` handoff is
> bridged into the session API by `eval.artifact_to_client` until artifacts serve sessions natively.

## Datasets, objectives, eval functions

A task's `evaluation_data` carries (see [`schema/dataset.schema.json`](../schema/dataset.schema.json)
+ [`schema/objective.schema.json`](../schema/objective.schema.json)):

- a **dataset**: `type` (`single_turn` | `multi_turn`), `max_turns`, `repeat`, and `data` — a list of
  `EvalSample`s (`data_point_id`, a single- or multi-turn input, optional `expected_output`, optional
  `eval_rubric` -- a per-DATA-POINT NL note, NOT where a metric's own scoring criteria goes; that's
  authored once per metric, baked into its eval function's own prompt);
- a list of **objectives**: each `{name, eval_function_path, positive, weight}`. `eval_function_path`
  is an importable per-data-point eval function under the `tasks/` package; `positive=False` means
  lower-is-better (the aggregator inverts it); `weight` sets its share of the final score.

An **eval function** is the task designer's per-data-point scoring code. Its keyword-only signature,
authoring rules, and where to declare it are documented once in
[`appsci_bench/eval/README.md`](../appsci_bench/eval/README.md#eval-function-contract-for-task-designers)
(see that README's module map for the authoring helpers + LLM-judge mechanism).

## Layout

The per-module map for `appsci_bench/eval/` — and the complete per-task file set across `eval/` +
build-side `core/` — lives in [`appsci_bench/eval/README.md`](../appsci_bench/eval/README.md) and
[`appsci_bench/core/README.md`](../appsci_bench/core/README.md). Not repeated here.

The end-to-end demo (`scripts/evaluator_system_e2e.py`, a manual check) runs the whole loop over mocks
(no creds), prints each report as text + JSON, and writes them + transcripts under a fresh
`scripts/_out/<run>/` directory.

## Eval flow

```
run_evaluation(spec, client):
  dataset    = load_dataset(spec)           # type + samples
  objectives = load_objectives(spec)        # eval functions resolved by path (fail-fast)
  conformance: probe create->query->delete before scoring (abort on non-conformance)
  step 1  generate: per (data point x repeat) open a session, run single/multi turn, capture
                    the conversation, delete the session; store transcripts; failures recorded.
  step 2  score:    for each (data point, objective) call the eval function -> 0..1 + metadata;
                    any error/timeout/bad-return -> 0 with the reason in `error`.
  step 3  aggregate: positive=False inversion, repeat-averaging, four views (matrix, per-data-point,
                    per-metric, final weighted score), deterministic ordering -> EvalReport.
```

Individual scores need not be reproducible (LLM judges / the artifact are non-deterministic), but the
final output's **ordering is deterministic** (data points by id, metrics by name). Each report's
**`run_id`** is unique per execution (a wall-clock + sequence tail, so archived reports never overwrite
each other) but prefixed with a stable config hash of task + full dataset content + objectives (incl.
paths/weights/direction), so runs of the same config stay greppable/groupable.

## CLI

```bash
# the full mocked loop (no creds) -- the quickest way to see eval run end to end:
python scripts/evaluator_system_e2e.py

# a real run (build then eval). The eval phase needs the task migrated to the new
# evaluation_data (objectives + dataset type); unmigrated tasks fail with a clear message.
python -m appsci_bench.orchestrator.run --task <id> --persona <persona.yaml>
python -m appsci_bench.core.spec <id>      # validate a task spec
```

## Status

The orchestration run loop and the eval-function authoring helpers (mechanical comparisons + the
LLM-judge mechanism) work end to end. Three tasks have real `objectives`/`EvalSample` content built
on top of them:

- `order_to_cash` (`single_turn`) runs end to end —
  `python -m appsci_bench.orchestrator.run --task order_to_cash --persona <persona.yaml>` scores its 3
  deterministic objectives plus `reason_quality` (LLM judge) for real.
- `slides_generator` (`single_turn`) resolves and scores all 11 of its objectives for real: 3
  deterministic, 4 transcript-rubric, 2 file-content judges reading the query's source files
  (`content_coverage` via `make_source_coverage_judge`, `groundedness` via
  `make_source_grounding_judge`), and 2 vision judges scoring a render of the produced deck
  (`template_adherence`, `layout_quality`). Its 5 product
  samples are vendored in the repo via Git LFS (run `git lfs pull` if you see only pointer files).
  (The 4 local `slidesgen-01..04` samples are temporarily removed
  pending a finalized sample format.)
- `airline_rebooking` (`multi_turn`) runs build→eval end to end: the artifact agent
  user sim (`appsci_bench/eval/artifact_agent_user_sim.py`, wired into the evaluator's scoring step)
  drives the multi-turn conversation for real, and both `task_success` and `policy_compliance`
  score against it.

`compliance_review` and `customer_call_insights` have real dataset content and `eval.py` too. `crm_insights` now has real dataset content + `eval.py` (see
`tasks/crm_insights/seed_data/PROVENANCE.md` and `docs/crm_insights_e2e_instructions.md`).

Also outstanding: the real MCP/HTTP transports, the Docker environment.

The `airline_rebooking` task exercises both the **build** and **eval** sides end to end:

- **Task environment as a service** (`tasks/airline_rebooking/environment/service.py`, containerized
  via `environment/docker-compose.yml` and managed by the Docker orchestrator's `TaskRunner`): a
  FastAPI service exposing the airline tools over HTTP (token-gated) plus an eval-only admin
  reset/state-hash seam. The artifact connects to it to *act*; the evaluator resets the per-case DB
  and reads its state.
- **Live build phase:** the orchestrator launches a business appsci-agent-user-sim (`DockerAppsciAgentUserSimService`, bound to
  the task spec + a persona) and the baseline AppSci agent authors + deploys its *own* artifact,
  optimizes it (`optimize_agent`), and submits it.

```bash
# Full build->eval with the baseline AppSci agent (the agent writes its own artifact).
# Set ANTHROPIC_API_KEY for LLM access; --verbose streams the build narrative.
python -m appsci_bench.orchestrator.run --task airline_rebooking \
  --persona tasks/airline_rebooking/user_personas/l1.yaml --verbose
```

Still deferred (follow-ups): MCP transport. `optimize_agent` now runs the live coder+deployer loop —
it hands failing eval cases to `mini_swe_agent`, redeploys, re-scores, and reverts a change that
didn't help.
