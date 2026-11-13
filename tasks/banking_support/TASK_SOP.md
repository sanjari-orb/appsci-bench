# Banking Customer-Service Agent (Knowledge-Grounded) — Task SOP (ground truth)

Comprehensive definition of the task. **Not shown verbatim to the artifact under test** — it is the
source from which the user-personas, the environment, and the per-case oracle are authored.

The policy, KPIs, and action space each live in their own file and are **linked, not copied**, so each
has a single source of truth. Read them alongside this document.

This used to be `sop/TASK_SOP.md.j2`, a jinja template that inlined the policy and the KPIs and
rendered the tool table out of the tools reference. #107 deleted the renderer after finding it had no
caller outside its own smoke test, which left the template unrenderable *and* unreadable — the includes
and the `{{ ... }}` placeholders sat there as literal text, so the one document that is supposed to be
the authoritative statement of the task could not be read straight through by the people it is for.
It is now a plain committed document that links, the shape airline and slides_generator moved to in the
same change. The authored summary and the audience-scoped section pointers live here, in the sections
below -- there is no separate machine-readable copy, because nothing ever read one.

## Problem

The customer ask is the `description` field in [`spec.yaml`](spec.yaml), with the business goals beside
it. In short: handle inbound Rho-Bank customer requests — product advice, applications, referrals,
disputes, account servicing — the way a great human banker would, so the customer gets an accurate
answer and the correct outcome.

The hard part is not the happy path. The rules the agent must follow — product terms, fee schedules,
eligibility, procedures, and even the **names of the specialized tools** it needs — live in a large
(~700 document) **knowledge base**. Retrieval over that corpus is **provided out of the box** (tau2's
`KB_search_bm25`, `KB_search_dense`, `grep`, and `shell` tools); the agent's job is to **wire those
tools** into its reasoning — search the KB, **ground every answer in what it finds**, and **not
fabricate**. It must also **verify the customer's identity** before account actions, and only take
actions the knowledge base sanctions (transferring to a human otherwise).

## Action space (base tools the artifact drives)

Retrieval over the knowledge base is **provided**: `KB_search_bm25` (BM25 keyword search, no key),
`KB_search_dense` (embedding search; needs a key, degrades gracefully without one), `grep` (regex over
the corpus), and `shell` (a read-only corpus sandbox) — all **READ-only** over the corpus. Specialized
*transactional* tools are **discoverable**: the knowledge base names them, and the agent unlocks/uses
them via `unlock_discoverable_agent_tool` + `call_discoverable_agent_tool` (agent side) or hands a
customer-side tool over with `give_discoverable_user_tool`.

The always-available base tools, with their per-tool argument lists, are in
[`seed_data/tools_reference.json`](seed_data/tools_reference.json) — which also lists the three
provided retrieval tools (`grep` is not among them; the exposed surface is upstream's `all_tools`). That
copy is the curated provenance record. The copy a RUN reads is
`user_sim_docs/common/tools_reference.json`, mounted into the user-sim container only, so the L2
stakeholder can give both the base tool names and their arguments when asked. The two must stay identical
(guarded by `tests/tasks/banking_support/test_tools_reference.py`).

`interface_design` in [`spec.yaml`](spec.yaml) is what actually reaches the agent, and it states the
retrieval and discoverable-tool mechanism without naming the action tools or their signatures — so the
base tool names are elicited, not read.

## Policy (the ground-truth rules the built agent must obey)

[`seed_data/prompts/components/policy_header.md`](seed_data/prompts/components/policy_header.md)
— the Rho-Bank operating rules. Vendored from upstream and pinned by
[`seed_data/TAU2_COMMIT.txt`](seed_data/TAU2_COMMIT.txt); see
[`seed_data/PROVENANCE.md`](seed_data/PROVENANCE.md).

Detailed, scenario-specific rules (product terms, fees, eligibility, which discoverable tool to use)
are **not** all there — they live in the knowledge base the agent must search (with the provided
retrieval tools) and ground in.

## Success KPIs

[`user_sim_docs/success_kpis.md`](user_sim_docs/success_kpis.md) — sim-only, declared
under `known_docs` on the L3 personas, so the agent has to ask what "good" means rather than read it.

## How a case is scored (oracle)

Each eval case carries the tau2 ground truth. The reward is the **product** of its `reward_basis`
components — for banking, one of:

- **DB**: apply the case's golden `actions` to a fresh DB **seeded with the case's `initial_state`**;
  the agent's final DB must match. Correct customer-side actions (e.g. `apply_for_credit_card`)
  count — they are what the DB check compares.
- **ACTION**: the agent's action trajectory must match the golden `actions`.

(`communicate_info` is empty for every banking case, so tau2's COMMUNICATE component is vacuous here.)
`required_documents` lists the knowledge-base docs the correct handling depends on — the ground-truth
grounding set.

`task_success` is declared as the task's `reward_objective`, so the eval report also carries the
per-case pass/fail this reward defines and the experiment layer can compute pass^k from it across
repeated eval trials of one artifact.

## Sections by audience (anti-leakage)

- **L3 (PM / customer-experience owner):** the business outcome + KPIs — accurate grounded answers, no
  fabrication, verify before acting, correct outcome. NOT the data shapes, the retrieval solution, or
  the tool internals.
- **L2 (integration engineer):** the environment — the transactional DB shape, the ~700-doc corpus
  (searchable via the provided retrieval tools, and also served raw), the base tool surface + the
  discoverable-tools mechanism, the fixed reference "now". Does NOT set the success metric.
- **L1 (applied-science manager):** approach — the corpus is too large to stuff into context, so the
  agent searches it with the provided retrieval tools; correct handling (incl. which discoverable tool
  to use) is gated on retrieving the right doc; ground answers and don't fabricate; verify before
  acting; the reward is the correct DB/ACTION outcome.
