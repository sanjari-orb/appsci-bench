# appsci_bench — the benchmark

`appsci-bench` evaluates the *process* of building a good agent from a vague customer requirement:
clarify → design → build → validate. Per task, the benchmark provides requirements (an appsci agent
user sim over the task's user-personas) and an environment to the agent under test; the agent returns an
artifact, which the benchmark queries and scores against held-out samples + a ground-truth oracle.

Start at [`docs/OVERVIEW.md`](../docs/OVERVIEW.md); architecture in
[`docs/evaluation_framework.md`](../docs/evaluation_framework.md).

## Models

| Role | Default model | Model var |
|------|---------------|-----------|
| Build-time appsci-agent-user-sim (the customer) | `anthropic/claude-opus-4-8` | `APPSCI_USERSIM_MODEL` |
| Eval-side LLM judges | `anthropic/claude-opus-4-8` | `JUDGE_MODEL` |
| Eval-time artifact-agent-user-sim | **`openai/gpt-5.2`** — tau-bench's user-simulator model at v1.0.1 | `ARTIFACT_USERSIM_MODEL` |

(The agent under test, outside this package, uses `AGENT_MODEL` + `APPSCI_AGENT_*`.) The first two
default to `MODEL_NAME` and share `MODEL_API_KEY`/`MODEL_BASE_URL` (`config_for()` in
[`common/model_config.py`](common/model_config.py)); only the eval-time sim departs from Claude, so the
simulated user we score against matches the benchmark our open-source tasks come from, and it takes
credentials from LiteLLM's provider-standard vars instead. Rationale + overrides:
[`docs/evaluator_system_design.md`](../docs/evaluator_system_design.md#model).

## Layout
- [`common/`](common/) — shared leaf utilities with no dependency on `core`/`eval`/`appsci_agent_user_sim` (currently: `model_config.py`, the `ModelConfig`/`complete` LiteLLM transport used by both the build-time appsci-agent-user-sim and the eval-side LLM-judge helpers).
- [`core/`](core/) — data model: task spec. (The legacy artifact handoff moved to `eval/artifact.py`.)
- [`eval/`](eval/) — **the evaluator system** (under active overhaul): scores a produced artifact agent over a task's dataset + objectives via per-data-point eval functions, then aggregates. See its [README](eval/README.md) for the 4-part split + status; design in [`docs/evaluator_system_design.md`](../docs/evaluator_system_design.md). Try `python scripts/evaluator_system_e2e.py`.
- `core/applied_scientist.py` — the contract the benchmark hands the agent (`TaskContext`) + an in-process loader.
- `core/appsci_agent_usersim.py` — **lifecycle manager** for the appsci-agent-user-sim service (starts/stops it, polls `/health`). NOT the simulator itself.
- [`appsci_agent_user_sim/`](appsci_agent_user_sim/) — the build-time appsci-agent-user-sim **service** (persona rendering, the LLM call, the `/health`+`/ask` HTTP app). A **decoupled subpackage**: it imports nothing from the rest of `appsci_bench` other than the `common/` leaf, so it can run in its own container. See its [README](appsci_agent_user_sim/README.md). NOTE: entirely separate from the artifact agent user sim (eval Part 3).
- [`../shared/observability/`](../shared/observability/) — **tracing for build + eval**: per-source JSONL event logs + out-of-band model-I/O capture that stitch every process in a run into one trace. See its [README](../shared/observability/README.md) for the architecture, the language-neutral wire contract, and the extensibility story for other agent frameworks.
- [`tracer/`](tracer/) — the read side (**[README](tracer/README.md)**): merges the per-source logs and serves the live dashboard (`python -m appsci_bench.tracer --port 8479 --runs-dir runs`). Also renders **runs from other machines**: observability runs mirror themselves to `s3://<your-bucket>/runs/`, and `/?trace_id=<run_id>` pulls an id that isn't local yet into `--runs-dir` and renders it through the ordinary local path — so one URL loads any run, wherever it came from. See the [observability README](../shared/observability/README.md#s3-mirror-sharing-runs) for setup (`./scripts/setup_s3_sso.sh`).
- `orchestrator/` — the run entrypoint (`python -m appsci_bench.orchestrator.run`): container lifecycle, submission server, eval phase.

The **agent under test lives outside the benchmark** (root-level [`appsci_agent/`](../appsci_agent/) —
an example; submitters write their own).

## What's NOT here
The **agent under test** (`appsci_agent/`) and the **task definitions** (`tasks/`, `schema/`) live
outside this package. The user-simulator service is a decoupled subpackage under `appsci_agent_user_sim/`
(see Layout) — `core/appsci_agent_usersim.py` only manages its lifecycle.

## Run
```bash
python -m appsci_bench.orchestrator.run --task order_to_cash \
  --persona tasks/order_to_cash/user_personas/l1.yaml
# add --limit N for a quick smoke; --enable-observability for the live tracer UI
```
One combined run per task: the agent endpoint is the input; the artifact is created during the run.
