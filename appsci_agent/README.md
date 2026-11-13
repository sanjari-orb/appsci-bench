# appsci_agent — the applied-science agent under test

The **reference applied-science (meta) agent** the benchmark evaluates — deliberately kept **outside**
the benchmark code (`appsci_bench/`). One agent handles **all** domains (order-to-cash, CRM, support
triage, slides, …): it's given each task's description + environment and must work it out. Submitters
can swap in their own agent behind the same `build(context) -> Artifact` entry point.

**Trust boundary.** This directory is what the evaluated agent sees — and *all* it sees. It reaches the
appsci agent user sim only over HTTP (`APPSCI_USERSIM_URL` + `APPSCI_USERSIM_TOKEN` → `/ask`) via
[`clients/user_sim_client.py`](clients/user_sim_client.py); it does **not** import `appsci_bench`, so it
can't see benchmark internals or the simulator's hidden persona — only ask and read answers.

**What the coding sub-agent sees is narrower still.** mini-swe runs in-process inside the agent
container but under a second, unprivileged user (`swe`, uid 1001), while this whole directory is
installed at `/app` mode 0700. So the shell commands the agent-under-test issues can write the work
dirs and read the task's own material (`/data`, `/sop`, `/spec`, the task tools and databases) but get
a permission error on the harness's own code — the LLM client and its gateway wiring, the user-sim
client, the tool implementations. Its environment is also scrubbed of the harness's plumbing
(`APPSCI_USERSIM_*`, `ORCHESTRATOR_SUBMIT_URL`, `OBSERVABILITY_*`), which are routes around
`talk_to_user` / `deploy_artifact` rather than capabilities the coding work needs; the LLM credentials
stay, because the sub-agent has to smoke-test the artifact's model client before deploying it. The
reason it used to read that source at all was that nothing told it how the artifact reaches a model —
so [`core/model_access.py`](core/model_access.py) now states those facts (variable names, never values)
in both the agent's system prompt and the sub-agent's task brief. Mechanics:
[`tools/mini_swe_agent.py`](tools/mini_swe_agent.py) (`SubAgentEnvironment`) and the
[`Dockerfile`](Dockerfile). Outside the container there is nothing to protect and no root to drop
from, so commands run unchanged.

## One agent, with optimization wired into its build loop

**The agent** — an LLM-driven, iterative build loop (clarifying with the user and exploring/building
interleave; once a candidate is built it's optimized, then published):
- `core/` — `agent.py` (the agentic loop, litellm multi-provider), `llm_client.py`, `config.py`, `task_context.py`.
- `tools/` — `base.py` (`BaseTool`/`ToolContext`/`ToolResult`) + `talk_to_user`, `mini_swe_agent`, `deploy_artifact`, `eval_generator`, `optimize_agent`, `complete`.
  Two ablation knobs drop tools from this registry *and* from every prompt surface:
  `optimization_enabled=False` removes `eval_generator` + `optimize_agent`, and
  `user_sim_enabled=False` removes `talk_to_user` (the no-user arm, which mounts the task's
  requirement documents at `$APPSCI_REQUIREMENTS_DIR` in their place).
- `prompts/` — system-prompt builder + Jinja templates.
- `artifact/` — `builder.py` + `deployer.py` (run the artifact as a local service).
- `applied_science_agent.py` — `build(context) -> Artifact`: the single benchmark entry point.

**Optimization layer** (PRD A.9) — *not* a separate agent. It's the **`optimize_agent` tool**, an
explicit build stage the agent calls **repeatedly** once it has a working, deployed candidate and
before `complete` ends the BUILD phase. Each call scores that candidate on the agent's own internal
eval (`eval_generator`'s harness, distinct from the benchmark's), records the score against the
checkpoint it was measured on, then hands the failing cases to `mini_swe_agent` to change the
candidate — prompt, backing model, or topology — redeploys, and re-scores. A change that doesn't help
is reverted and the incumbent redeployed. Every call reports whether the scores have saturated, which
is the agent's signal to stop calling it. See [`tools/optimize_agent.py`](tools/optimize_agent.py).

```bash
# one agent (optimization hook included; passthrough in v1)
python -m appsci_bench.orchestrator.run --task order_to_cash --persona tasks/order_to_cash/user_personas/l1.yaml
```
