# CRM Analyst/Manager Insights — Task SOP (ground truth)

Comprehensive definition of the task. **Not shown to the agent** — it is the source from which the
user-personas, environment, and oracle are authored.

## Problem

Managers and analysts repeatedly ask questions about what's happening across accounts, cases, and
agents — "which state closes cases fastest," "who's our slowest agent this quarter," "what's the
top complaint about this product." Today, answering means someone manually digs through the CRM.
They want an agent that answers correctly and points to the actual records backing the answer, so
the answer is trustworthy and actionable, not just plausible-sounding.

Source: CRMArena (Huang et al., arXiv:2411.02305) — see `seed_data/PROVENANCE.md` for exactly what
was reused vs. self-authored. This task covers CRMArena's 6 analyst + manager question categories;
the 3 service-agent categories (case handling actions, not reporting questions) are not part of
this benchmark.

## The 6 question categories

| Category | Example question shape | Answer shape |
|---|---|---|
| `case_routing` | "Given this new case, which agent should it go to?" | agent Id |
| `handle_time` | "Which agent has the shortest/longest handle time in \<period\>?" | agent Id |
| `transfer_count` | "Which agent transferred the most cases in \<period\>?" | agent Id |
| `top_issue_identification` | "What's the most common problem with \<product\> in \<period\>?" | issue Id |
| `monthly_trend_analysis` | "Is there a month where \<product\> cases spiked?" | month name |
| `best_region_identification` | "Which state closes cases fastest in \<period\>?" | 2-letter state code |

Full schema + domain conventions (quarters/seasons, handle-time/transfer-count definitions):
`environment/data/schema_reference.md`.

## Success Criteria

- The reported answer matches what a correct query against `crm_db` would produce for that
  question (exact value — these are all point answers, not ranges or approximations).
- The answer is grounded: backed by specific, real case/record ids from `crm_db`, not a bare
  assertion.

## KPIs (customer success)

See `environment/data/kpis.md` — `answer_correctness` (primary) and `grounding` (trust/secondary).

## Sections by audience

- **L3 (business stakeholder — manager or analyst)**: poses the business question directly (any of
  the 6 shapes above), in plain language, with no schema/SQL knowledge and no awareness of exactly
  how the answer should be computed. Cares only about getting a correct, cite-able answer.
- **L1 (applied-science manager)**: can discuss the two objectives, why answers must be exact
  (these are point-value lookups, not judgment calls), why grounding matters even when the number is
  right, and the category mix (6 categories, evenly weighted, no category should dominate).
- **L2**: not used for this task's first pass — no deep domain/systems-architecture axis is needed
  beyond what L1 already covers (see `KNOWN_ISSUES.md`).
