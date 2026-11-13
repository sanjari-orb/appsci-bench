# core + eval — the complete per-task file set

The files a task run touches, in one place: **build-side** data model (`core/`) and the **eval-side**
evaluator system (`eval/`). `core/` detail is below; `eval/` detail (the four-part split + status)
lives in [`eval/README.md`](../eval/README.md) — the one-liners here are an index, not the source of truth.

## `core/` — data model (build side)

- `spec.py` — load + validate a task `spec.yaml` (ground truth; never shown to the agent under test).
  Also owns the **agent-facing spec redaction** (`build_agent_spec` / `write_agent_spec` /
  `agent_spec_leaks`) — see [Agent-facing spec redaction](#agent-facing-spec-redaction) below.
- `agent_container.py` — Docker lifecycle manager for the appsci-agent container (`AppSciAgentContainerManager`),
  used by the orchestrator to build the artifact.
- `appsci_agent_usersim.py` — **lifecycle manager** for the build-time appsci-agent-user-sim service
  (`DockerAppsciAgentUserSimService`: starts/stops the container, polls `/health`); the simulator core
  lives in [`appsci_bench/appsci_agent_user_sim/`](../appsci_agent_user_sim/).

> The per-task **environment service** (e.g. the airline tools API) is started as a Docker container by
> the orchestrator's `TaskRunner` (`appsci_bench/task_runner/`) from the task's `docker-compose.yml` —
> never as a host-local process.

## Agent-facing spec redaction

**Design decision (why the raw `spec.yaml` is never mounted to the agent).** The task `spec.yaml` is
**ground truth**: alongside the agent-facing ask it carries the eval
dataset + scoring functions (`evaluation_data`), the reference solution (`gold_solution`), the persona
files (`user_personas`), orchestration wiring + admin tokens (`task_runner`), seed-corpus provenance
(`source`), and host paths (each resource's `location`). The agent container mounts the spec at
`/spec/spec.yaml`, and the agent's mini-swe shell can `cat` anything mounted — so filtering only inside
the in-container loader is **not** enough. Instead the orchestrator mounts an **already-redacted view**
in the raw spec's place; the raw spec is only ever handed to the **trusted** user-sim (which role-plays
the persona who legitimately knows the ground truth).

**How it works (`core/spec.py`):**
- `build_agent_spec(data)` — builds the redacted view from an **allowlist** of agent-facing top-level
  keys (`task_id`, `name`, `description`, `business_goals`, `dataset_files`, `technical_docs`,
  `interface_design`, `artifacts`, `budgets`) plus a reshaped `environment` (only the
  resource list with each host `location` stripped, plus `access_control`). Allowlist, not blocklist:
  any new ground-truth field added to the schema later is dropped by **default** rather than leaking.
- `agent_spec_leaks(view)` — a generic, **task-agnostic** leak detector (`_AGENT_SPEC_FORBIDDEN`) that
  scans the rendered view for structural ground-truth markers — repo dir prefixes (`tasks/`, `gold/`,
  `sop/`, `eval_data/`, `scripts/`), env-internal dirs (`environment/{db,data,setup}`), and gating/oracle
  annotations. It keys off **structure only, never per-task filenames**, so it generalizes to every task.
- `write_agent_spec(spec_path, dest)` — redacts + **fails fast** (`ValueError`) if the redacted view still
  trips a marker, rather than mounting a leaking spec. This is what the orchestrator writes and mounts.

**What is and isn't gated.** The *answer key* — `evaluation_data`/`eval_data/`, `gold/`, `eval.py`,
the vendored upstream corpus (`provenance/`), and host paths — is never agent-visible. The
*requirement docs* (a task's policy / tool reference / KPIs) are **not** answer key: they describe what a
compliant artifact must do, and are intentionally exposed to the agent (airline mounts them at `/sop`;
see [`tasks/airline_rebooking/KNOWN_ISSUES.md`](../../tasks/airline_rebooking/KNOWN_ISSUES.md)). The end
state — delivering requirement docs through the user-sim persona to test policy-elicitation — is a
follow-up; the answer-key gating above is unchanged and always enforced.

**Guardrails.** `tests/appsci_bench/core/test_agent_spec.py` runs the redactor + `agent_spec_leaks`
over **every** task's spec, asserts the runtime fail-fast rejects a leaky spec, and scans every
agent-mounted dir (`environment/{data,sop}` → `/data` + `/sop`) for ground-truth markers (a stray
README once leaked `eval_data/`/`gold/` paths this way).

## `eval/` — evaluator system

Scores a produced artifact agent over the task's dataset + objectives, then aggregates. See
[`eval/README.md`](../eval/README.md) for full detail; design in
[`docs/evaluator_system_design.md`](../docs/evaluator_system_design.md).

- `datatypes.py` — shared eval dataclasses (artifact query/output, `Dataset`, `Objective`, results, `EvalReport`).
- `dataset.py` — load + validate a task's `Dataset` (`parse_dataset` / `load_dataset`).
- `objectives.py` — load `Objective`s + resolve their `eval_function_path`s (fail-fast).
- `artifact_client.py` — session-API client: `ArtifactAgentClient` protocol + in-process impl + `Mcp`/`Http` stubs.
- `artifact_agent_user_sim.py` — the artifact agent user sim: `ArtifactAgentUserSimulator` protocol,
  the real `ArtifactAgentUserSim` (session-based, LLM-backed), and `ScriptedArtifactAgentUserSim`
  (deterministic test stub). Wired into `runner.py`'s `_score()`, which always constructs a real
  `ArtifactAgentUserSim` and passes it to `run_evaluation` -- a no-op for `single_turn` tasks,
  the actual driver for `airline_rebooking`'s `multi_turn` dataset.
- `generation.py` — step 1: run the artifact over each (data point, repeat) into transcripts.
- `store.py` — transcript "database": one JSON per conversation; root recorded on the report (`store_root`).
- `scoring.py` — step 2: per-(data point, objective) eval-function harness; failures → score 0 + error.
- `aggregate.py` — step 3: invert/average/weight into the four views + the weighted final score.
- `report.py` — render an `EvalReport` as text (`render_text`) or JSON (`render_json`).
- `eval_system.py` — `run_evaluation(...)` wiring steps 1-3 + a unique-per-execution `run_id`.
- `contract.py` — the versioned session-API contract registry, published to the agent at build time.
- `conformance.py` — pre-scoring session-API conformance probe (`check_conformance` / `ConformanceError`).

> Removed in the evaluator overhaul: `core/evalset.py`, `core/results.py`, `core/rubric.py`
> (the new dataset / objective / result types live in `eval/datatypes.py`). The **legacy** single-call
> artifact handoff (`respond(ArtifactQuery)`) also no longer lives here: it moved to `eval/artifact.py`
> (harness-side), is bridged into the session-API client by `eval.artifact_to_client`, and the example
> agent no longer depends on it.
