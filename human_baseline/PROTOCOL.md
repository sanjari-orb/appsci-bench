# Human Baseline Protocol

This document was previously discussed only in chat/meeting notes. It is committed here so that
`human_baseline/pilot/` and future attempts have a durable, citable reference.

## 1. Purpose

The appsci-bench team serves as the human baseline. Human submissions are scored by the same Oracle
pipeline as agent submissions — no separate grading.

**What this study produces — and what it does not.** The goal is a **human comparison score** per task:
the number the agent is measured *against*. It is **not** a ground truth and **not** a set of reference
solutions. Because human attempts run through the same Oracle objectives as agent runs, a human attempt
is a *point on the same scale*, not a definition of the correct answer — a baseliner can score poorly,
and that is data about the task's difficulty, not an error to be corrected.

This distinction drives the design in §3: we need **enough attempts to estimate a human level with an
interval**, not enough to canonicalize an answer. Confidence that the *scale itself* is trustworthy comes
from **meta-evaluation** (validating that Oracle scores track human-perceived artifact quality), not from
this study. Using human attempts as both the yardstick and the validation of the yardstick would be
circular.

RE-Bench-informed update: the RE-Bench paper argues that human baselines are most useful when humans
and agents operate under equivalent conditions, use the same scoring pipeline, and produce comparable
trajectory artifacts. It also shows that humans may improve more with longer time budgets, while
agents can benefit from many fast attempts; our protocol should therefore report both final score and
time/attempt allocation, not only a single aggregate score.

## 2. Cross-Assignment Rule

No one baselines a task they helped design.

| Task | Ineligible | Eligible |
|---|---|---|
| Order-to-cash | a contributor, a contributor, P2 | a contributor, P1, a contributor, P5 |
| Airline_rebooking | a contributor | P2 |
| Slide generator | P5, a contributor | a contributor, P2, a contributor, P1, a contributor |

(complete once 5 tasks are locked)

## 3. Cell Prioritization

Not every (task × L-level × style) cell gets filled. The design is **capped and purpose-built** to get
a defensible per-task human number *and* validate the L1/L2/L3 difficulty gradient without consuming
the team — **human baselining + task QA together stay ≤ 50% of collective bandwidth.** Two estimands,
two blocks, linked by a hierarchical model:

- **Breadth block (per-task coverage).** Every locked task at a single anchor — **L3-collaborative**
  (most informative gap vs. the agent) — at **2 independent attempts** each.
- **Depth block (difficulty-gradient probe).** Fully cross **only 2 anchor tasks** (order-to-cash +
  airline_rebooking) across **L1/L2/L3 at collaborative style**, 2 attempts/cell. Their
  L3-collaborative cells are shared with the breadth block, so depth only *adds* L1 + L2.
- **Style probe.** **One** anchor task (order-to-cash) adds **L3-terse** × 2 attempts — enough to show
  style is second-order vs. level.

**Attempts:** default **2 independent attempts** per cell; reserve a 3rd only for a depth cell whose
two attempts diverge sharply. Only the 2 anchor tasks are level-crossed; the rest are anchored at
L3-collaborative.

**Budget:** ~10 breadth + ~8 depth-incremental + ~2 style ≈ **20 attempts** total (vs. 40–60 for a
full every-task × L1/L2/L3 × 2-styles × 3-attempts grid).

**Analysis:** fit `score ~ level + style + (1|task) + (1|baseliner)`; partial pooling lets the
L3-only breadth tasks borrow strength from the depth set's level estimates. The difficulty-gradient
claim rests on the 2-task depth probe, stated as such — not on all tasks.

**Frontloading:** pilots start on the tasks that already have eval code (order-to-cash,
airline_rebooking, slides_generator) rather than waiting for the full portfolio.

## 4. Attempts Cap

- Max 3 attempts per cell, each by a different eligible member.
- Total attempts bounded by availability survey.
- Each attempt = assign → setup → execute → submit/abandon → reflect → score, by one eligible
  baseliner. Attempts should not reuse testers — 3 attempts = 3 testers trying independently.
- Scoring does not contribute to caps on timing (time is allotted for problem solving).

## 5. Tooling Disclosure

All baselines use humans + Claude Code. Paper disclosure:

> "Human baselines were conducted by applied scientists on the authoring team, each working with
> Claude Code. No participant baselined a task they helped design."

## 6. Protocol Design

### Design Principles

- **Trajectory parity**: human runs produce identical trace artifacts as agent runs — same logs,
  snapshots, scoring outputs, and submission records.
- **Equivalent conditions**: humans and agents receive the same task spec, sandbox, simulator
  access, scoring entry point, and allowed resources, with any human-only assistance disclosed.
- **Blind-to-design**: no one baselines what they built.
- **Bounded effort with actual-time reporting**: keep the 4h soft cap for planning, but always
  record actual elapsed working time, breaks, pauses, and over-cap continuation.
- **Multiple independent attempts where informative**: for high-priority cells, preserve independent
  attempts rather than collapsing them too early; RE-Bench found substantial human-attempt variance.
- **Score@k / best-of-k awareness**: when comparing with agents, report the allocation used (for
  example 4h×1, 2h×2, or 1h×4) because agents and humans can have different returns to time versus
  repeated attempts.
- **Tooling transparency**: humans use Claude Code — disclosed upfront and separated in logs from
  human-authored decisions where possible.

### Protocol Flow

```
┌─────────────┐     ┌──────────────────┐     ┌───────────────────┐
│ S1: Setup   │────▶│ S2: Pilot        │────▶│ S3–S4: Execution  │
│ • Avail.    │     │ • 1-2 members    │     │ • L1 cells (S3)   │
│   survey    │     │   run same task  │     │ • L3 cells (S4)   │
│ • Cross-    │     │   independently  │     │ • Artifacts       │
│   assign    │     │ • Human+Claude   │     │   captured        │
│   matrix    │     │   synthesize GT  │     │                   │
└─────────────┘     └──────────────────┘     └───────────────────┘
                                                      │
                                                      ▼
                                              ┌───────────────────┐
                                              │ S5: Scoring       │
                                              │ • Oracle scores   │
                                              │   human = agent   │
                                              │ • Comparison      │
                                              │   tables          │
                                              └───────────────────┘
```

### Per-Attempt Lifecycle

1. **Assign** → coordinator assigns a cell to an eligible baseliner.
2. **Setup** → baseliner receives task spec, sandbox, and simulator endpoint.
3. **Execute** → work in `/work`, interact with the simulator, follow the 4h soft cap.
4. **Submit or Abandon** → entry-point artifact = submitted; explicit stop = abandoned.
5. **Reflect** → write a ~5-sentence post-attempt note and a quantitative score at scale of 1-5.
6. **Score** → scored by the Oracle (`tasks/<task>/eval.py` via `python -m appsci_bench`), same as for agents.

## 7. Run Procedure

**Setup**: baseliner receives task spec, sandbox, simulator endpoint, and cell assignment; confirms
no design conflict. Start time is logged.

**During**: work in `/work`, use the simulator like the agent, follow a 4h soft cap with 3h30
notification, keep scratch in `/work/scratch`.

**Ending**:
- Progress checkpoints = run the scoring or validation entry point whenever meaningful progress is
  made, and at minimum near the 1h, 2h, 3h30, and end-of-run marks when feasible.
- Technical issue handling = pause the timer only for platform or simulator failures outside the
  baseliner's control; log the issue, who resolved it, and whether extra time was granted.
- No post-hoc synthesis for scored baselines = independent attempts remain independent for
  human-vs-agent comparison. Any later "best combined solution" should be clearly labelled as
  curation or gold-solution work, not a baseline attempt.
- Submitted = artifact at entry point + session ended.
- Abandoned = explicit stop; partial artifacts scored as incomplete.
- Baseliner writes a ~5-sentence reflection and a quantitative score 1-5.

## 8. Trajectory Parity

Human runs should leave the same kind of trail as agent runs, so scores and paths taken can both be
compared. Working backward from the target analyses keeps the capture plan complete.

### Target Analyses

| Analysis | Question it helps answer | Required artifacts |
|---|---|---|
| Score parity | Do humans and agents achieve comparable quality under the same rubric? | System artifact, Oracle score report |
| Efficiency comparison | How much time, iteration, and tool use does each attempt require? | Time log, conversation log, FS snapshots, scoring checkpoints |
| Strategy divergence | Do humans and agents solve the task through different plans/edits/recovery steps? | Conversation log, FS snapshots / diff sequence |
| Failure-mode taxonomy | Where do humans get blocked, and are those blockers different from agent failure modes? | Reflection, conversation log, abandoned-run metadata, technical-issue log |
| Difficulty calibration | Which cells appear intrinsically hard, even for eligible human baseliners? | Score distribution per cell, time log, reflection with 1–5 difficulty score |
| Attempt variance | How much do independent human attempts on the same cell differ? | Multiple system artifacts and scores per cell, kept independent |
| Time-budget returns | Do humans keep improving with more time while agents benefit more from many shorter attempts? | Checkpoint score log, actual elapsed time, attempt allocation, final score |
| Score@k / best-of-k | What is the best result from k independent attempts under the same total time budget? | Independent attempt IDs, per-attempt score, per-attempt duration, cell metadata |
| Tooling attribution | How much of the trajectory is human-directed versus delegated to Claude Code? | Conversation log with human turns separated from tool/model turns |

### Artifact Table

| Artifact | How captured | Analyses it supports |
|---|---|---|
| Conversation log | Automatic; preserve human turns separately from Claude Code/tool/model turns | Strategy divergence, failure-mode taxonomy, tooling attribution, efficiency comparison |
| FS snapshots | Automatic, at the same intervals as agent runs | Strategy divergence, efficiency comparison |
| System artifact | Submitted by baseliner at the task entry point | Score parity, attempt variance |
| Doc artifact | Optional, when relevant to the task/rubric | Score parity where documentation is scored |
| Score checkpoints | Run at meaningful milestones; record timestamp, score, and whether score was visible to the baseliner | Time-budget returns, efficiency comparison, score@k comparison |
| Time log | Start/end time plus breaks, pauses, over-cap continuation, and reason for any paused timer | Efficiency comparison, difficulty calibration, time-budget returns |
| Reflection | Manual post-attempt note | Failure-mode taxonomy, difficulty calibration |
| Technical-issue log | Manual note for platform/simulator/dependency failures; severity, resolution, time impact | Failure-mode taxonomy, feasibility validation |
| Abandoned-run metadata | Manual note with reason for stopping and partial-progress summary | Failure-mode taxonomy |

## 9. Scoring

Same Oracle objectives (`tasks/<task>/eval.py`), rubric, and deterministic checks. No human-specific leniency.

Reporting guidance from RE-Bench: publish per-cell distributions, not just means. At minimum include
best score, median score, number of attempts, total human-hours, and score@k / best-of-k under
comparable total time budgets. Keep invalid, abandoned, and technically blocked attempts visible with
labels rather than silently dropping them.
