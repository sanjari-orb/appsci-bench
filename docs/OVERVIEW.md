# appsci-bench — Overview

**appsci-bench evaluates the *process* of building a good agent from a vague customer requirement** —
clarification, design, build, validation — end to end. Most ML benchmarks score a model on a fixed
task; appsci-bench scores how well an applied-science agent turns an underspecified customer ask into
a working agent.

## How a run works

Per task, the benchmark:
1. **provides requirements** to the agent under test via an appsci agent user sim that plays the task's
   user-personas (L1 manager / L2 technical partner / L3 PM); each persona's level (not disclosed to
   the agent) plus its closed `domain_knowledge` set how much is known — the *ambiguity level*;
2. **exposes an environment** — a Docker sandbox of the customer's resources (DBs, knowledge bases,
   APIs, docs, sample data) — for the agent to explore;
3. takes the **artifact** the agent produces and **scores** it by querying it over a held-out
   dataset (single- or multi-turn) and running the task's **objectives** — per-data-point eval
   functions — then aggregating to a final score. See [evaluation_framework.md](evaluation_framework.md).

The agent's endpoint is the input to a run; the artifact is created during the run. A run is one
combined command per task.

**Artifact scope (v1):** an artifact is anything the benchmark can query (MCP / completions) or a set
of files it produced. v1 targets E2E agentic systems; static one-off deliverables (a raw dataset, a
model/chart from data) are out of scope, expandable later. **One agent is expected to handle all
domains** — there are no per-task agents.

### Models the benchmark uses

Four LLM roles per run. Everything the benchmark drives runs on Claude, with one deliberate
exception — the **eval-time** user simulator is pinned to tau-bench's user-simulator model, so the
simulated user we score against matches the benchmark our open-source tasks are converted from.

| Role | When | Default model | Model var |
|------|------|---------------|-----------|
| Agent under test | build | `claude-opus-4-8` | `AGENT_MODEL` (creds: `APPSCI_AGENT_*`, or a provider-standard key such as `FIREWORKS_API_KEY` when the model id names its own provider — with `APPSCI_AGENT_*` unset) |
| Appsci agent user sim (build-time customer) | build | `anthropic/claude-opus-4-8` | `APPSCI_USERSIM_MODEL` |
| Eval-side LLM judges | eval | `anthropic/claude-opus-4-8` | `JUDGE_MODEL` |
| Artifact agent user sim (eval-time user) | eval | **`openai/gpt-5.2`** (what the v1.0.1 airline submissions declare) | `ARTIFACT_USERSIM_MODEL` |

One model var per role. `MODEL_NAME` is the shared default behind `APPSCI_USERSIM_MODEL` and
`JUDGE_MODEL`, and `MODEL_API_KEY`/`MODEL_BASE_URL` the credentials they share. The eval-time sim
takes neither: it is pinned cross-provider, so its credentials come from LiteLLM's provider-standard
vars (`OPENAI_*`) and `multi_turn` eval needs those too — gateway serves gpt-5.2, and the setup script
wires it up. Rationale + the override for
environments without OpenAI access: [evaluator_system_design.md](evaluator_system_design.md#model).

## Tasks

Two source types. **Internal** tasks are authored by us on synthetic/curated data; **open-source**
tasks are converted offline from public agentic benchmarks (tau-bench first; others can be added).

| Internal | Open-source (source) |
|----------|----------------------|
| order_to_cash *(worked example)* | airline_rebooking (tau2-bench) |
| customer_call_insights | banking_support (tau2-bench) |
| compliance_review | crm_insights (CRMArena) |
| slides_generator |  |

## Where things live

- `schema/` — what a task is made of (`task_spec`, `user_persona`, `dataset`, `objective`, `artifact_agent_api`; `eval_sample` is legacy); read its README first.
- `tasks/<id>/` — `spec.yaml` + `RESOURCES.md` + `user_personas/` + `eval_data/`. `order_to_cash` is filled in end to end.
- `appsci_bench/` — the benchmark (core data model, the **evaluator** in `appsci_bench/eval/`, the Docker orchestrator in `appsci_bench/orchestrator/`). Includes `appsci_bench/appsci_agent_user_sim/` — the build-time user-simulator service (persona rendering, the LLM call, the `/health`+`/ask` HTTP app), a decoupled subpackage that runs in its own container. See [appsci_agent_user_sim_api](user_simulator_api.md).
- `appsci_agent/` — an example agent under test (write your own).
- `prompts/applied_scientist_protocol.md` — the instructions given to the agent.
- `docs/` — [task_spec_reference](task_spec_reference.md), [evaluation_framework](evaluation_framework.md), [evaluator_system_design](evaluator_system_design.md), [artifact_agent_api](artifact_agent_api.md), [appsci_agent_user_sim_api](user_simulator_api.md).

## Run

A run has two phases: **BUILD** (the AppSci agent clarifies requirements with a business appsci-agent-user-sim,
authors + deploys an artifact, optimizes it, submits it) then **EVAL** (the harness scores the
deployed artifact).

```bash
# order_to_cash (single_turn, deterministic + LLM-judge objectives)
python -m appsci_bench.orchestrator.run --task order_to_cash \
  --persona tasks/order_to_cash/user_personas/l1.yaml

# airline_rebooking — full build->eval with the baseline AppSci agent.
# Set ANTHROPIC_API_KEY for LLM access. Add --limit N for a quick smoke; --verbose to watch
# the build-phase narrative.
python -m appsci_bench.orchestrator.run --task airline_rebooking \
  --persona tasks/airline_rebooking/user_personas/l1.yaml --verbose

python -m appsci_bench.core.spec airline_rebooking      # validate a task spec
```

See [evaluation_framework.md](evaluation_framework.md#status) for the airline end-to-end details.

## Status

On the **build** side, `airline_rebooking` runs the full build→eval loop: the AppSci agent authors,
deploys, and submits its own artifact (see `gold/airline_rebooking/`). On the **eval** side, the
orchestration run loop and the eval-function authoring helpers (deterministic comparisons +
LLM-judge mechanism) run end to end (`python scripts/evaluator_system_e2e.py`); see
[`appsci_bench/eval/README.md`](../appsci_bench/eval/README.md). Five tasks — `order_to_cash`,
`slides_generator`, `airline_rebooking`, `customer_call_insights`, `banking_support` — have real
`objectives`/`EvalSample` eval code: `order_to_cash` scores end to end via the CLI; `slides_generator`
scores its deterministic and LLM-judge objectives (5 of its 12 objectives are still `score=None`
placeholders pending further judge work); `airline_rebooking` runs `--phase eval` via the CLI end to
end, including its `multi_turn` dataset (driven by the artifact agent user sim, wired into the CLI
eval phase). `customer_call_insights` is wired end to end on a 50-sample held-out set of
hand-authored synthetic calls (see its `KNOWN_ISSUES.md` for two known label defects);
`banking_support` scores all 97 of its cases through the same path, adding per-sample DB seeding, an
agent action trace, and a user simulator that can call the customer's own tools — with two known gaps
recorded in `tasks/banking_support/KNOWN_ISSUES.md`.


Stubbed / pending (`TODO` in code): real agent transports (MCP/completions) + the Docker environment,
and the offline open-source converter.
