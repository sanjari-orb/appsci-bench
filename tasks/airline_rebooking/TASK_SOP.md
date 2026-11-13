# Airline Reservations Agent (Policy-Compliant) — Task SOP (ground truth)

Comprehensive definition of the task. **Not shown verbatim to the artifact under test** — it is the
source from which the user-personas, the environment, and the per-case oracle are authored.

The policy, KPIs, and action space each live in their own file and are **linked, not copied**, so
each has a single source of truth. Read them alongside this document.

## Problem

The customer ask is the `description` field in `../spec.yaml`, with the business goals beside it.
In short: handle inbound booking / modify / cancel / refund-compensation requests the way a good
human agent would — a valid outcome for the customer, and never a policy breach.

The hard part is not the happy path. Customers are often **wrong or imprecise** about their own
status, eligibility, and entitlements ("I'm pretty sure I'm Gold"; "I was told I didn't need
insurance"), and the **tools do not enforce policy** — the agent must check the rules itself before
it writes. A correct agent frequently has to **refuse** or **transfer to a human**.

## Action space (tools the artifact must drive)

The 14 read / write / transfer tools, with their signatures, are in
[`user_sim_docs/common/tools_reference.json`](user_sim_docs/common/tools_reference.json). `interface_design` in `../spec.yaml`
lists them by name and states the two standing constraints: validate eligibility before any write,
and obtain explicit user confirmation, one tool call at a time.

## Policy (the ground-truth rules the built agent must obey)

[`user_sim_docs/common/policy.md`](user_sim_docs/common/policy.md) — the booking, modify, cancel, and refund/compensation rules.
Vendored from upstream and pinned by
[`provenance/airline/TAU2_COMMIT.txt`](../../provenance/airline/TAU2_COMMIT.txt); see
[`provenance/airline/AIRLINE_PROVENANCE.md`](../../provenance/airline/AIRLINE_PROVENANCE.md).

## Success KPIs

[`user_sim_docs/success_kpis.md`](user_sim_docs/success_kpis.md).

## How a case is scored (oracle)

Each eval case carries the upstream ground truth. The reward is the **product** of its
`reward_basis` components — for airline, **DB × COMMUNICATE**:

- **DB**: apply the case's golden `actions` to a fresh airline DB; the agent's final DB must
  match. An empty `actions` list means the correct outcome makes **no DB change** (e.g. a
  policy-required refusal) — any write is then wrong.
- **COMMUNICATE**: every substring in `communicate_info` must appear in an agent message to the user.

`nl_assertions` describe the policy-correct behaviour in natural language.

## Sections by audience (anti-leakage)

- **L3 (PM / customer-experience owner):** the business outcome + KPIs — resolve requests correctly,
  never break policy, keep records right. NOT the rule tables or tool/DB internals.
- **L2 (integration engineer):** the environment — DB shape, the tool/action space, read/write
  boundaries and the confirmation gate. Does NOT set the success metric.
- **L1 (applied-science manager):** approach — policy is unenforced by tools so the agent must
  self-check; many cases are refusals/transfers; scoring is DB × COMMUNICATE.
