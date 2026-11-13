# appsci_agent_user_sim — the appsci-agent-user-sim service (one-stop guide)

The benchmark's stand-in for the human stakeholder an applied-science agent is building for. It
role-plays the task's persona (L1 manager / L2 engineer / L3 PM) and **answers** the agent's
questions during the build phase. It runs as a backend service in **its own container**, behind a
small HTTP API.

This README is the entry point for the whole appsci agent user sim. For the exact agent-facing wire contract
(routes, request/response shapes, error codes), see [`docs/user_simulator_api.md`](../../docs/user_simulator_api.md).
For the persona files that configure it per task, see [`docs/task_spec_reference.md`](../../docs/task_spec_reference.md).

**Decoupled by design:** this subpackage lives under `appsci_bench/` for org reasons but imports
**nothing** from the rest of `appsci_bench` (core/eval/runner) other than
[`appsci_bench.common`](../common/), a zero-dependency leaf (only `litellm`) shared with the
eval-side LLM-judge helpers — so this can still be containerized and shipped on its own by vendoring
that one small module alongside it. The benchmark depends on it (to manage its lifecycle), never the
reverse. It takes a structural view of the task (`TaskContextLike`) rather than the benchmark's
`TaskSpec` type.

---

## How it fits together

```
  agent container ──HTTP /ask──▶ appsci-agent-user-sim service (this package) ──▶ LiteLLM ──▶ model
        ▲                              │
        └── APPSCI_USERSIM_URL / TOKEN        └── one Session (holds the run's transcript)
           injected by the orchestrator, which starts/stops the service
```

- The **orchestrator** (`appsci_bench`) starts the service with the run's task + persona level +
  model, polls `/health`, and injects `APPSCI_USERSIM_URL` + `APPSCI_USERSIM_TOKEN` into the agent's container.
- The **agent under test** talks to it only over HTTP (`/health`, `/ask`) — it never sees the
  persona, the level, or any config. Inferring the working relationship is part of the task.
- The **service process IS the session**: one continuous conversation per run; `/ask` is
  history-aware while the model call underneath stays stateless.

## Behavioral contract (what the simulator does)

- **Answers; never leads.** It responds to questions; it won't propose next steps, drive the agenda,
  or tell the agent when to stop.
- **Closed-world, no fabrication.** It knows only the persona's `domain_knowledge` + `known_resources`.
  Anything else gets "I don't know" — it never invents a number, name, or value.
- **Stays in its lane** (the level is the difficulty knob):

  | Level | Role | Speaks to | Deflects |
  |-------|------|-----------|----------|
  | **L1** | Applied-Science / ML Manager | requirements, methods, metrics, trade-offs | doing the build for you |
  | **L2** | Senior SWE / Data Engineer | infra, data, schemas, integration, deployment | ML/modeling choices |
  | **L3** | Product Manager | business goals, users, outcomes, "good" by example | anything technical |

- **Refuses to do the agent's work** (draft docs, write SQL, run queries), **won't reveal a personal
  identity**, and **never ends the conversation** — termination is the agent's call plus a runner-side
  turn ceiling.

These rules live in the shared `personas/templates/_behavioral_contract.jinja2` partial (identical
across levels) plus each level's competence section.

## Layout

- [`core/`](core/) — simulator logic.
  - `persona.py` — renders the L1/L2/L3 prompt from a task + persona YAML; resolves `known_resources`.
  - `usersim_core.py` — the stateless `AppsciAgentLLMUserSim` (`answer(question, history)`), role-flip, `{message}` parsing. Model config + the LiteLLM call itself now live in [`appsci_bench/common/model_config.py`](../common/model_config.py) (`ModelConfig.from_env`, `complete`) — moved out once the eval-side LLM-judge helpers started reusing it too.
  - `task_context.py` — loads the persona-rendering task fields from a `spec.yaml` (no benchmark import).
- [`service/`](service/) — the FastAPI app (`/health`, `/ask`) + the one `Session`. The process is the
  session: `Session` accumulates the transcript + turn counter around the stateless `AppsciAgentLLMUserSim`.
- [`personas/templates/`](personas/templates/) — the L1/L2/L3 Jinja prompt templates + the shared
  behavioral-contract partial.

## Configuration (operator-facing)

The orchestrator sets these env vars when it starts the service; the agent never sees them. See
[`.env.sample`](../../.env.sample) for a copyable template.

| Env var | Required | Meaning |
|---------|----------|---------|
| `APPSCI_USERSIM_MODEL` | — | **This sim's own** LiteLLM model id. Unset ⇒ falls back to `MODEL_NAME`. Set by the orchestrator from `--appsci-usersim-model`, else forwarded from the host. |
| `MODEL_NAME` | — | Shared default model id behind `APPSCI_USERSIM_MODEL`; the provider prefix routes the call (default `anthropic/claude-opus-4-8`). e.g. `openai/gpt-5.5`, `gemini/gemini-2.5-pro`. |
| `<PROVIDER>_API_KEY` | yes¹ | Provider-standard key chosen by the model prefix: `anthropic/` → `ANTHROPIC_API_KEY`, `openai/` → `OPENAI_API_KEY`, etc. Shared with any other component using that provider. |
| `MODEL_BASE_URL` | — | A custom endpoint. **Set it → that endpoint is used** (self-hosted / gateway; keyless by default). **Unset → first-party provider**, which requires its `<PROVIDER>_API_KEY`. |
| `APPSCI_USERSIM_TOKEN` | yes | Per-run bearer token the agent must present on every request. |
| `APPSCI_USERSIM_SPEC_PATH` | yes | Path to the task `spec.yaml` (read for persona-rendering fields). |
| `APPSCI_USERSIM_PERSONA_PATH` | yes | Path to the bound persona YAML (sets the level + the closed fact set). |

¹ Required only when `MODEL_BASE_URL` is unset (first-party provider). Temperature is intentionally
left at the provider default (some current models reject any other value).

## Run it locally (manual check)

Chat with the simulator over the real HTTP service from your terminal — useful for eyeballing a
persona's behavior before any automated eval:

```bash
cp .env.sample .env          # fill in MODEL_NAME + the matching provider key
.venv/bin/python scripts/chat_appsci_user_sim.py --persona l2   # defaults: order_to_cash, l1
```

The script spins up the FastAPI service, prints the rendered system prompt, and drops you into a REPL
as the applied-science agent. See [`scripts/README.md`](../../scripts/README.md).

## Who manages it

The benchmark's [`appsci_bench/core/appsci_agent_usersim.py`](../core/appsci_agent_usersim.py) is the lifecycle
manager — it starts/stops this service as a subprocess and polls `/health` (subprocess spawning is
the remaining integration step). The evaluated agent talks to it only over HTTP via
[`appsci_agent/user_sim_client.py`](../../appsci_agent/user_sim_client.py).

## Tests

- `tests/appsci_bench/appsci_agent_user_sim/core/` — persona rendering, behavioral-contract clauses,
  the `AppsciAgentLLMUserSim` core (parsing + role-flip + `run_conversation`), `task_context`.
  (`ModelConfig`'s own tests moved to `tests/appsci_bench/common/` with the module.)
- `tests/appsci_bench/appsci_agent_user_sim/service/` — the FastAPI routes + `Session` history accumulation.
- `tests/appsci_bench/appsci_agent_user_sim/test_integration.py` — happy path against a real spec/persona (LLM mocked).
- `tests/appsci_agent/test_user_sim_client.py` — the agent-side HTTP client.

All run offline (the model is mocked). Run: `.venv/bin/python -m pytest tests/ -q`.
