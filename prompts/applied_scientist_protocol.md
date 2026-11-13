# Applied-scientist protocol (global)

appsci-bench evaluates a **process**: turning a vague customer requirement into a *good agent*. You
are the agent driving that process. The benchmark engages you in two phases — first as the
**customer/user**, then as the **evaluator** of the resulting agent.

Phase 1 — **Understand the requirement.**
- Talk to the user (our appsci agent user sim) to elicit the goal, constraints, success bar, and edge
  cases. The user may be a PM, a technical partner, or a manager and may not volunteer everything —
  probe. You are not handed a written spec.
- Optionally explore the **environment** (a Docker sandbox with the customer's DBs, knowledge bases,
  APIs, docs, sample data — see the task's resources doc) to understand the real problem.

Phase 2 — **Deliver the agent that solves it**, exposed through a standard interface (an **MCP
connector** or an **OpenAI-style completions API**; see the task's `interface_design`). Tell us the
endpoint; we then interact with it to evaluate it against held-out cases.

Notes:
- We only ever see the resulting agent through its exposed interface; its internals (RAG, DBs,
  sub-agents, tools, prompts) are yours to design and are treated as a black box.
- Respect the task budgets (build time/tokens/$ and the resulting agent's latency target).
- The task's short description is provided; the details come from the user, not a spec.
