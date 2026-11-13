# Task-spec reference

A task is `tasks/<task_id>/` containing a `spec.yaml` plus its personas, resources doc, and eval
data. This page documents the four artifacts a task author fills in. Validate with
`python -m appsci_bench.core.spec <task_id>`.

The task spec is **ground truth**: the agent under test under test never sees it. It learns the task by
talking to the **personas** (the appsci agent user sim) and exploring the **environment**; the **oracle** in
the eval data is used only to score the artifact the agent produces. See
[evaluation_framework.md](evaluation_framework.md) for the run lifecycle.

**Artifact scope (v1).** An *artifact* is anything the evaluator can **query** through a standard
interface (MCP / completions). v1 artifacts are **E2E agentic systems**. A produced file
(deck / report / chart) is in scope as an `output`-kind artifact; `documentation`-kind artifacts
(design notes, validation) are diagnostic only. **Out of scope for v1
(expandable later):** static one-off deliverables with no queryable system behind them — a raw
dataset, or a standalone model/chart produced from data (MLE-bench territory). A task may declare
**multiple artifacts** (a primary queryable agent + optional file artifacts) via `artifacts.expected`.

## 1. `spec.yaml` — [`schema/task_spec.schema.json`](../schema/task_spec.schema.json)

| Field | Req | What it captures |
|-------|-----|------------------|
| `task_id`, `name` | ✓ | identity (snake_case id matches the dir) |
| `description` | ✓ | **short** customer ask the agent starts from (no thresholds / solution detail). |
| `business_goals`, `dataset_files`, `technical_docs`, `interface_design` | | context passed to the persona templates (so the simulated stakeholder knows the goals, available data/docs, and what the system should do). `dataset_files`/`technical_docs` are `{name, description}` lists; `interface_design` is prose describing the expected action space. |
| `task_source_type` | ✓ | `internal` \| `open-source` |
| `source` | | open-source provenance `{benchmark, domain, notes}` — conversion is **offline**, this is for audit |
| `environment` | ✓ | the Docker sandbox the agent builds/runs against: `docker_compose` (the only build input — the runner runs `docker compose up -d --build`, so the Dockerfile path lives in the compose file's own build block), plus optional structured `resources[]`. Each resource has a **unique `name`** (required; e.g. `oltp_db`) plus `type` (∈ database/knowledge_base/api/docs/sample_data/**kpi**/**live_traces**), `location`, and optional `description`. The `name` is what a persona's `known_resources` references (see §3), and what the agent's prompt lists — the host `location` is redacted before the agent sees the spec. All agent-facing, distinct from the hidden held-out eval set. Each task's `RESOURCES.md` is author-facing documentation only: it is never mounted into the agent container, so it is not a spec field. |
| `artifacts` | | the artifact(s) produced: `output_dir` + `expected[]` (`{id, kind, primary, path, required}`). `kind`: **agent** (primary queryable system, scored) · **output** (a file a run produces, e.g. a deck — scored) · **documentation** (design notes/validation — diagnostic only). |
| `user_personas` | ✓ | pointers to user-persona files (span L1/L2/L3); the appsci agent user sim plays them and *provides* requirements |
| `budgets` | | `build` (turns/tokens/$/wall_clock_s on the agent under test) + `latency_slo_ms` (artifact speed target) |
| `evaluation_data` | ✓ | the eval **dataset** (`type` single/multi-turn + samples) and the **objectives** that score it. See §1.1. |
| `gold_solution` | | pointer to a curated reference solution for **human-baseline curation** (not consumed by the evaluator) |
| `owner`, `co_owners` | ✓ | drives the no-self-baseline rule |

> **Where a task's ground truth lives.** It is *not* a spec field. Each task keeps its authoring
> material on disk by convention — `TASK_SOP.md` (the full definition personas, environment and
> oracle are written from), plus per-task seed/provenance dirs. Nothing in the spec points at it and
> no runtime code reads it: it exists for whoever authors or audits the task.
>
> Don't confuse that with **`tasks/<id>/environment/sop/`**, which *is* runtime-relevant: it is
> bind-mounted read-only at `/sop` in the agent container, so every file in it is published to the
> agent under test. Located by directory convention, never named in the spec. A persona referring to
> material the agent has must cite the agent's mount path (`/sop/<file>`), never the repo path —
> `appsci_bench/core/usersim_docs.py` fails the run otherwise.
>
> Renaming one of the two so the names stop colliding is still worth doing, but it changes the agent's
> container contract (the `/sop` mount, the snapshot list, the leak guards' path matching) and 10
> tasks' layouts, so it belongs in its own change.

### 1.1 `evaluation_data` — dataset + objectives (new evaluator)
Scored by the new evaluator (design: [evaluator_system_design.md](evaluator_system_design.md); how it
runs: [evaluation_framework.md](evaluation_framework.md)). `evaluation_data` carries:
- **dataset** — `type` (`single_turn` | `multi_turn`), optional `max_turns` (default 10) and `repeat`
  (default 1), and the samples (inline `data[]`, or a `dataset` directory pointer -- see §4). Each
  `EvalSample`: `data_point_id`, a `single_turn_input` *or* `multi_turn_input`, optional
  `expected_output`, optional `eval_rubric`. Schema: [`schema/dataset.schema.json`](../schema/dataset.schema.json).
- **objectives** — a list of `{ name, eval_function_path, positive, weight, enabled, scoring_kind }`. `eval_function_path` is
  an importable per-data-point eval function under `tasks/` (e.g. `order_to_cash.eval.correctness`);
  `positive=False` = lower-is-better (aggregator inverts to `1 - score`); `weight` = its share of the
  weighted final score; `scoring_kind` declares how it is scored — `deterministic` (no model call),
  `llm_judge` (score comes from a model call) or `hybrid` (calls a model on some path) — so a caller can
  select a subset, e.g. re-run only the judged metrics. Declared by hand, then checked against the eval
  function by `tests/appsci_bench/eval/test_scoring_kind_declarations.py`; absent means *undeclared*, not
  deterministic. Schema: [`schema/objective.schema.json`](../schema/objective.schema.json).
  `enabled` is optional and defaults to `true`. Set `enabled: false` to skip the metric's calculation
  and omit it from new reports; `weight: 0` alone still calculates and reports it. At least one
  objective must remain enabled. Banking disables `groundedness`, `retrieval_recall`,
  `verification_compliance`, and `no_fabrication` this way in
  [`tasks/banking_support/spec.yaml`](../tasks/banking_support/spec.yaml). Set `enabled: true` on any
  of those entries to re-enable it for subsequent evaluations, including `eval_artifact` and queued
  trials. The implementations remain available; Python callers can explicitly load all metrics with
  `load_objectives(spec, include_disabled=True)`. This setting controls artifact objectives; the
  separate build-elicitation diagnostic still runs when its rubric and transcript are available.
- **elicitation_rubric** *(optional)* — pointer to `tasks/<id>/elicitation_rubric.yaml`, the list of
  facts the agent had to obtain by **asking** the build-time customer simulator, with the true answers
  written out. Feeds the standalone **elicitation score** (never the task score). Held-out answer-key
  material: it sits at the **top level** of the task directory, never under `environment/` (which is
  mounted into the agent container), and living in `evaluation_data` is what keeps the pointer out of
  the agent-facing spec. A task without one reports `no_rubric`. Schema:
  [`schema/elicitation_rubric.schema.json`](../schema/elicitation_rubric.schema.json); design:
  [elicitation_score_design.md](elicitation_score_design.md).

> **Breaking change:** the old `metrics[]` + `rubrics[]` menu (with `type`/`track`/`weight` and a
> two-track *headline*) and the `sample_schema` field have been **removed** from the schema, not just
> deprecated -- `evaluation_data` no longer accepts them (`additionalProperties: false`). If you have
> a task spec still on that shape, migrate it to `type` + `objectives` + a `dataset` directory of
> per-sample `sample.json` files before pulling/merging past this point, or `python -m
> appsci_bench.core.spec <task_id>` will reject it.

## 2. `RESOURCES.md`
Plain-language map of where each customer resource lives inside the task's Docker container (paths,
connection hints, read-only vs writable). The agent under test (task-time) and artifact (eval-time)
use it to find resources.

## 3. `user_personas/*.yaml` — [`schema/user_persona.schema.json`](../schema/user_persona.schema.json)
One appsci-agent-user-sim persona per file; a task provides several spanning L1/L2/L3. Key fields:
`level`, `role`, `domain_knowledge`, and optional `known_resources`. Ambiguity is set by two things, not a separate policy field:
- the bound persona's **level is not disclosed** to the agent — recognizing the working relationship is part of the ambiguity; `level` gates which kinds of questions the persona is competent to answer (L1 = AI/ML, L2 = infra only, L3 = business only).
- `domain_knowledge` — the **closed set** of facts the persona knows; it answers only from here and otherwise says it doesn't know. Withholding is just "don't volunteer": the agent must know to ask.

`known_docs` (optional) lists documents from `tasks/<id>/user_sim_docs/` whose **full content** this persona has read and internalized. That directory is mounted only into the user-sim's container, so the agent cannot read these — it has to ask. Everything in `user_sim_docs/common/` is included for every persona automatically. Budget: aim under 5,000 tokens per persona, hard limit 20,000 (they sit in the prompt on every turn). Supported types: `.md`, `.txt`, `.json`, `.jsonl` (text only; `.pdf`/`.csv`/binary are rejected with an error, never skipped). Never name one of these files anywhere the agent can see. Full guide: [`tasks/README.md`](../tasks/README.md#documents-only-the-user-sim-can-read).

`known_resources` (optional) is the per-level **visibility knob**: a list of `environment.resources` **names** this persona knows exist (e.g. L1 lists DB/infra resources, L3 few or none). Names that don't match a resource fail loud at load time. Absent/empty = the persona knows no specific resources. **Metadata only** — the persona learns the name, type, description, and location, and can point the agent at them; it never reads their contents. For document content the agent cannot read, use [`known_docs`](../tasks/README.md#documents-only-the-user-sim-can-read).

Because a resource's `location` is shown to the persona verbatim, it must be somewhere the **agent** can reach — a container path (`/data/…`, `/sop/…`), a connection string, or prose. A repo-relative path (`tasks/<id>/environment/sop/x.md`) exists in neither container and makes the sim send the agent hunting; declaring one for a resource a persona lists is a validation error.

The appsci agent user sim never ends the conversation (the agent + an orchestration turn-ceiling own termination).

## 4. `eval_data/` — the held-out samples
The dataset's samples can live inline in `evaluation_data.data[]` or, pointed at by
`evaluation_data.dataset`, in a **directory of per-sample subdirectories** -- one per
`data_point_id`, each holding a `sample.json` (pretty-printed, so it's reviewable by hand) plus any
files it needs. **New format** ([`schema/dataset.schema.json`](../schema/dataset.schema.json)),
each `EvalSample`: `{ data_point_id, single_turn_input | multi_turn_input, expected_output?, eval_rubric? }`
— where `single_turn_input` / `expected_output` are `ArtifactAgentQuery` / `ArtifactAgentOutput`
(`{text?, json?, files?}`) and `multi_turn_input` is `{user_task, user_persona, user_stop_criteria?, files?}`.
A `files[]` entry gives either inline base64 `file_data` (small files) or `file_path` (large files),
a path relative to that sample's own subdirectory -- resolved and read lazily by the loader
(`appsci_bench/eval/dataset.py`), never inlined into the shared JSON. `file_path` is a dataset-
authoring convenience only; it never reaches the `/query_session` wire protocol, which always
carries real `file_data`. Everything the evaluator needs to drive the artifact and score it; stored
out-of-spec (can be large).

> **Legacy:** unmigrated tasks still use the old per-case format
> ([`schema/eval_sample.schema.json`](../schema/eval_sample.schema.json):
> `{ id, intended_task, success_criteria, stopping_criteria, customer_info_access[], oracle{type,data}, metrics[] }`).
> The new evaluator does not read it.

## Worked example
[`tasks/order_to_cash/`](../tasks/order_to_cash/) is filled in completely: spec, `RESOURCES.md`, three
personas (`l1`, `l2`, `l3` spanning manager / data-engineer / PM), and `eval_data/samples.jsonl` (3
samples with oracle). Run it: `python -m appsci_bench.orchestrator.run --task order_to_cash --persona tasks/order_to_cash/user_personas/l1.yaml`.

## Add a task
1. `cp -r tasks/_TEMPLATE tasks/<id>` and fill `spec.yaml`, `RESOURCES.md`, `user_personas/`, `eval_data/`.
2. `python -m appsci_bench.core.spec <task_id>` to validate.
