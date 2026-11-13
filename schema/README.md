# schema — what each task is made of

> **Breaking change:** the legacy `evaluation_data.metrics`/`rubrics`/`sample_schema` fields and the
> `eval_sample.schema.json` file they pointed at have been **removed** (not just deprecated) --
> `evaluation_data` now only accepts `dataset`, `provenance`, `type`, `max_turns`, `repeat`, and
> `objectives`. If you have an in-progress branch or an unmigrated task spec still using
> `metrics`/`rubrics`/`sample_schema`, **pull this change and migrate that spec's `evaluation_data`
> to `type` + `objectives` before merging** -- schema validation will now reject those fields outright
> (`additionalProperties: false`) instead of silently ignoring them. See
> [Task-Specific Evaluation Function](../docs/evaluator_system_design.md#task-specific-evaluation-function)
> for the current eval-function contract.

A task is defined by a small set of files validated against these JSON Schemas. **Read these to
understand a task** (and see [`tasks/order_to_cash/`](../tasks/order_to_cash/) for a worked example).

| Schema | Defines | Lives at |
|--------|---------|----------|
| [`task_spec.schema.json`](task_spec.schema.json) | the task: description, environment, user-personas, evaluation data | `tasks/<id>/spec.yaml` |
| [`user_persona.schema.json`](user_persona.schema.json) | an appsci-agent-user-sim persona (L1/L2/L3); its `level` + closed `domain_knowledge` set the ambiguity | `tasks/<id>/user_personas/*.yaml` |
| [`dataset.schema.json`](dataset.schema.json) | the eval **dataset**: turn `type`, `max_turns`, `repeat`, and the `EvalSample`s (single/multi-turn input, optional expected output + rubric) | `evaluation_data` + `tasks/<id>/eval_data/<data_point_id>/sample.json` |
| [`objective.schema.json`](objective.schema.json) | one scored **objective**: `name`, `eval_function_path`, `positive`, `weight`, and an optional `scoring_kind` (`deterministic` / `llm_judge` / `hybrid`) declaring how it is scored | `evaluation_data.objectives[]` |
| [`elicitation_rubric.schema.json`](elicitation_rubric.schema.json) | the per-task list of facts the agent had to obtain by **asking** the build-time customer simulator, with the true answers written out -- scores the standalone elicitation diagnostic, never the task score. Held-out answer-key material: top level of the task dir, never under `environment/`. See [`docs/elicitation_score_design.md`](../docs/elicitation_score_design.md) | `tasks/<id>/elicitation_rubric.yaml` |
| [`contracts/session_api.schema.json`](contracts/session_api.schema.json) | the **session-API contract** the produced artifact must serve (create/query/delete + payload shapes), as a **versioned registry** — each version is a self-contained file `contracts/session_api.v<N>.json` | published to the agent at build time |

Field-by-field reference: [`docs/task_spec_reference.md`](../docs/task_spec_reference.md). Validate a
task with `python -m appsci_bench.core.spec <task_id>`.
