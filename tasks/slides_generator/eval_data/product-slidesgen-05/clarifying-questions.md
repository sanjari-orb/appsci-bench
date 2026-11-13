# Expected clarifying questions — Example 5

The input (`PROMPT.md`) is deliberately sparse. A good agent should **pause and ask a short, targeted set of clarifying questions before building** — not invent the missing facts. These are the questions it should surface, grouped by the material gap. Asking roughly these — and no more — is part of what this example grades.

## Timeline
1. Is there a target timeline or a go-live / cutover date? (The notes give a meeting day — "Monday" — but no project dates.)

## Ownership & stakeholders
2. Who is the overall project owner, and who leads from Speech/ML, Platform, and Analytics?
3. Is Finance actually in scope? The notes say "maybe finance?" — and if so, for what (e.g., cost sign-off)?

## Success metrics
4. How will we measure "cut maintenance load" and "faster onboarding," and are there targets (e.g., maintenance hours −X%, ramp from N→M weeks)?

## Cost
5. The "GPU inference cost" worry — is there a current figure, a budget cap, or a target reduction?

## Migration scope
6. Big-bang or phased cutover? Any backfill / reprocessing of historical conversation data, and how do the three pipelines differ (volume, latency/SLA)?

## Decision needed
7. Is Monday's deck for alignment only, or do you need a decision / approval (e.g., budget)?

> The agent should NOT ask about things already provided — the three pipelines are already named (batch, real-time, partner) and the goals are stated. Asking those back is a failure mode (over-asking).
