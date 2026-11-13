# Order-to-Cash — Task SOP (ground truth)

Comprehensive definition of the task. **Not shown to the agent** — it is the source from which the
user-personas, environment, and oracle are authored. Personas reveal only audience-scoped subsets.

## Problem
The AP team manually checks supplier invoices against the purchase order (PO) and, where relevant,
the underlying contract, before paying. Misses lead to overpayment. Volume: ~12k invoices/month.

## What "material" means (success criteria)
Flag an invoice for human review when any holds:
- price or total delta vs the PO **> $500**;
- quantity does not match the PO;
- a line item or fee **not covered by the contract** (any amount).
Trivial roundings (≤ a few dollars) must **not** be flagged (noise erodes trust). The AP team is the
final reviewer — the system flags + gives a short reason; it does not auto-pay or auto-reject.

## KPIs (customer success)
**Primary metrics:**
- **False negative rate** — catch ~all material discrepancies (recall on material cases). Missing a faulty invoice is the worst outcome.
- **False positive rate** — keep noise low enough that AP trusts the flags.

**Secondary metric:**
- **Classification accuracy** — when flagging, correctly identify the type of problem (price, qty, off-contract, duplicate). Helps AP act faster.

## Information boundary: appsci-agent-user-sim vs agent

**What the agent sees (environment/sop/, mounted at /sop):**
- `kpis.md` — success criteria and key metrics (FNR, FPR, classification accuracy)
- `contract_terms.md` — what contracts are, where to find contract data
- Database schema and access

**What the agent does NOT see (must ask the appsci agent user sim):**
- Specific flagging rules (what counts as a "problem", what doesn't)
- Materiality thresholds ($500, etc.)
- The decision logic for when to flag vs not flag
- The shape the answer has to come back in, and the fault-type vocabulary
- Any labeled invoice at all — there is no dev/example set; every labeled row is held out
- Fault type enum names (internal taxonomy)

The agent is **expected to ask the appsci agent user sim** about flagging criteria. All three persona types
(L1, L2, L3) have this information in their `domain_knowledge` and can answer these questions.
This tests the agent's ability to gather requirements through conversation.

## Sections by audience
- **L3 (PM / AP director):** business goal, what "material" means via examples, who reviews. Does
  NOT speak to thresholds/architecture.
- **L1 (applied-science manager):** can discuss metrics, class imbalance, approach trade-offs.

## Design principle: realistic documents, not solution prompts

Agent-facing documents (`contract_terms.md`, `kpis.md`) must read like real business documents — not
solution prompts that tell the agent exactly how to solve the task.

**Why this matters:**
- The agent should DISCOVER the validation logic by understanding business rules
- Real business documents don't contain solution algorithms
- Giving away the solution makes the benchmark too easy / less realistic

**What agent-facing docs SHOULD contain:**
- Business context, policies, and success criteria (in business language)
- Where to find data (table names, schemas)
- Key metrics the system will be evaluated on

**What agent-facing docs should NOT contain:**
- Fault type enum names (internal taxonomy)
- Step-by-step validation algorithms
- "If X then flag as Y" decision logic
- Specific thresholds and flagging rules (agent should ask the appsci agent user sim)
