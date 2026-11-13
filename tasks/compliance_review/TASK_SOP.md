# Compliance Review — Task SOP (ground truth)

Comprehensive definition of the task. **Not shown to the agent** — it is the source from which the
user-personas, environment, and oracle are authored. Personas reveal only audience-scoped subsets.

## Problem
A compliance/legal ops reviewer manually checks each customer-facing document (contracts,
disclosures, marketing copy) against internal policy and regulatory rules before it ships, hoping to
catch issues by hand. Misses lead to non-compliant documents reaching customers. Volume: hundreds of
documents/month.

## Success Criteria
Given a document and a policy (a named set of rules over clause/disclosure categories), report every
violation with the offending span, the broken rule, and a rationale; route ambiguous clauses to a
human instead of flagging them; and give an overall compliant verdict.

Rule types:
- `must_have` — violation if the category is **absent** from the document (no evidence span; it's
  missing)
- `must_not_have` — violation if the category is **present** (evidence = the offending span)
- `needs_review` — not pass/fail; if present, route to a human — **not** a violation

Recall on real violations matters more than precision — missing a real violation is the worst
outcome — but too many false positives erode reviewer trust and get the tool ignored.

## KPIs (customer success)
**Primary metrics:**
- **Violation recall** — catch nearly all real violations across the policy's rules.
- **Violation precision** — keep false positives low enough that reviewers trust the flags.

**Secondary metric:**
- **Evidence quality** — when a violation is flagged, the cited span should be the actual offending
  language, not just a correct category+rule pairing.

## Information boundary: appsci-agent-user-sim vs agent

**What the agent sees (environment/data/):**
- `kpis.md` — success criteria and key metrics
- `policies/*.md` — the 4 named policies, and which deal type each applies to. No clause-level rules
  — those are elicited, not mounted (see the design principle below)
- `contracts_db` — read-only Postgres access to the build-time-safe contract corpus (raw text only)

**What the agent does NOT see (must ask the appsci agent user sim):**
- The clause-level content of any policy: which categories it requires, prohibits, or flags for
  review
- That the artifact's expected output shape is exactly `{compliant, violations[], needs_review[]}`
  with `violations[]` = `{category, rule, present, evidence_spans, rationale}`
- That a `needs_review` hit is never itself a violation
- The full rule-type taxonomy (`must_have`/`must_not_have`/`needs_review`) in the abstract

The agent is **expected to ask the appsci agent user sim** for each applicable policy's actual clause
rules, the expected output contract, and how `needs_review` differs from a violation. All three
persona types (L1, L2, L3) can answer these questions, from two different sources: the
`needs_review`/violation distinction and the rule-type taxonomy live in their `domain_knowledge`; each
policy's actual clause rules and the expected output contract live in `user_sim_docs/common/` (reaches
every persona automatically, not declared per-persona).

## Sections by audience
- **L3 (compliance program owner):** business goal, current pain, who reviews, success signal. Does
  NOT know infra/DB layout.
- **L2 (engineer who owns the policy corpus + document pipeline):** storage layout, data shape,
  deploy constraints (read-only, no schema changes), freshness of the sample corpus.
- **L1 (applied-science manager):** the same closed set of facts as L2/L3, framed for someone
  evaluating approach trade-offs and class imbalance (most documents are clean against most
  policies).

## Design principle: realistic documents, not solution prompts

Agent-facing documents (`policies/*.md`, `kpis.md`) must read like real internal policy/business
documents — not solution prompts that tell the agent exactly how to detect a violation. Which policy
applies to which deal type is business context, safe to hand over; it says nothing about how to
determine whether a clause is present in a document's text. The clause-level content of a policy
(what it actually requires, prohibits, or flags), by contrast, is the decision rule this task exists
to test elicitation of — a document that named it would be a solution prompt, not a business
requirement, so it lives only with the stakeholder.

## Build-time vs. eval-time contract disjointness (see seed_data/PROVENANCE.md)

The eval set (owned by a contributor, `ground-truth/compliance_v1/` on PR #34) computes gold verdicts
*directly* from CUAD's lawyer clause-presence annotations — unlike this task's build-time corpus,
where no such annotation is ever exposed. The 50 contracts in `contracts_db` and the 5 labeled
calibration records at `seed_data/labeled_calibration_examples.jsonl` (held out, never mounted to the
agent) are drawn only from the ~462 CUAD contracts outside a contributor's frozen 48-title
eval holdout list (vendored at `seed_data/eval_holdout_contracts.txt`). This boundary must be
preserved by anyone regenerating seed data — see `seed_data/PROVENANCE.md` for the enforcement detail.
