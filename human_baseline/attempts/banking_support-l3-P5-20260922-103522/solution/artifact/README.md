# Banking Support L3 Agent

Implements the `respond()` handler as an L3 banking support agent: it reads a running conversation
and returns the assistant's next turn, grounding every decision in the knowledge base (KB) and the
bank's records — never the customer's claims. Behavioral spec: [REQUIREMENTS.md](REQUIREMENTS.md).
Environment facts: [EXPLORATION.md](EXPLORATION.md).

## Status

**Phase 1 — default flow.** A robust, modular scaffold running the happy-path flow end to end
(classify → verify 2-of-4 → log once → search KB → unlock+call the named action → escalate when the
KB has nothing), plus a validation layer and a manual simulation app. Corner cases and hard logic
are Phase 2.

## Layout

```
server.py            Thin FastAPI shell: /health + /respond. Delegates to the agent package.
agent/
  config.py          Env resolution: BANKING_TOOLS_URL/TOKEN + model endpoint (MODEL_* w/ fallbacks).
  tool_client.py     HTTP client for the tool API. Defensive /tools/{name} -> /{name} path helper.
  model_client.py    Anthropic-compatible Messages wrapper (Opus by default).
  tools.py           Native tool schemas — BASE TOOLS ONLY (structural P3 enforcement).
  prompt.py          System prompt: the 4-step policy + P1/P2/P3 as hard rules.
  loop.py            The per-turn tool-use loop (Agent.run_turn).
sim/
  app.py, index.html Local web app to role-play the customer and inspect live records.
validation/
  scenarios.py       Record-grounded test scenarios (no scripted requests; no specialist names).
  harness.py         Runs scenarios, records mutating calls, writes REPORT.md.
  MANUAL_TEMPLATE.md  Template for logging manual integration results.
  REPORT.md          Generated validation report (auto results + manual slots).
```

## Configuration

The container is expected to inject (see EXPLORATION.md §2.2, §8):

- `BANKING_TOOLS_URL`, `BANKING_TOOLS_TOKEN` — the tool API.
- `MODEL_BASE_URL`, `MODEL_API_KEY` — the model endpoint. Fallbacks accepted:
  `APPSCI_AGENT_BASE_URL/API_KEY`, then `ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN`.
- `MODEL_NAME` (optional) — overrides the Opus default.
- `PORT` — the port `server.py` binds (deployer-injected).

No hosts, ports, or tokens are hardcoded. `/health` answers even before the model/tool vars are set.

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
PORT=8080 .venv/bin/python server.py           # the artifact
.venv/bin/python -m sim.app                     # sim UI at http://localhost:8090
.venv/bin/python -m validation.harness          # writes validation/REPORT.md
```

The sim app proxies to the artifact (`ARTIFACT_URL`, default `http://localhost:8080`) and calls the
base read tools directly for the inspector. Judge correctness on end-state **records**, not reply
wording (V-1).

## Design invariants (do not break)

- **P3 / R-19:** specialist `*_NNNN` action tools are never pre-registered. The only route is a KB
  procedure that names one → `unlock_discoverable_agent_tool` → `call_discoverable_agent_tool`.
- **P1 / R-4:** verification counts only when a field matches the looked-up record.
- **R-6/R-7:** exactly one `log_verification` per customer per conversation.
- **R-20:** the artifact holds only the tool token; it never touches `/admin/*` scoring routes.
