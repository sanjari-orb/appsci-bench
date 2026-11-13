# Known Issues — Compliance Review

## 2026-08 — H2 migration: output contract elicited, labeled calibration examples held out

**Output contract.** The exact response shape (`compliant` / `violations` / `needs_review` field names
and types) used to live inline in every persona's `domain_knowledge.expected_output`, so the agent
never had to ask for it — it could read the contract straight off any persona regardless of level.
That's the same convention PR #89 fixed for `order_to_cash` (H2): the response contract now lives in
`user_sim_docs/common/output_contract.md`, sim-only, so the agent has to elicit it. Personas no longer
carry `expected_output`.

**Labeled calibration examples.** `environment/data/examples.jsonl` mounted 5 fully-labeled contracts
(real excerpts plus gold `violations`/`needs_review`) directly to the agent's `/data`. This was a
deliberate, documented design choice (see `seed_data/PROVENANCE.md`) — verified disjoint from the real
eval holdout, intended as a calibration aid, not an eval-set leak. But PR #89's underlying reasoning
for `order_to_cash` applies regardless of disjointness: labeled input→verdict examples teach the agent
the decision rule directly, so it can fit the pattern instead of eliciting/reasoning about the policy
itself — the thing this task exists to measure. Moved to
`seed_data/labeled_calibration_examples.jsonl` (held out, not mounted); the `examples` resource is
removed from `spec.yaml` and every persona's `known_resources`.

**Not changed:** `contracts_db`, `policy_kb`, `kpis` resources; the deterministic objectives
(`violation_set_f1`, `verdict_accuracy`, `evidence_span_overlap`) already read the artifact's declared
structured fields directly — no judge-over-prose problem existed here, so nothing needed fixing on
that front.

## 2026-08 — policy clause content moved off the agent-facing mount

**Was:** `environment/data/policies/*.md` (mounted at `/data/policies/`, agent-facing) carried the same
required/prohibited/flag-for-review clause lists as every persona's `domain_knowledge.policies`,
verbatim. The agent could read the full rule set directly from its own mount and never needed to ask
the stakeholder anything — the elicitation conversation these decision rules are supposed to force was
optional. Same failure shape as the slides_generator worked-example leak and airline's
`interface_design` leak (see `docs/task_information_placement.md`, open question #6: "should `/sop`
[here, `/data`] and `domain_knowledge` be allowed to duplicate?").

**Now:** the four policy files under `/data/policies/` state only the policy name and which deal type
it applies to (structural, derivable, safe to hand). The actual required/prohibited/flagged clause
lists live in `user_sim_docs/common/{vendor_standard,ip_protective,risk_averse,procurement_clean}_rules.md`
— sim-only, reaches every persona via the `common/` convention, never mounted into the agent's
container. Personas' `domain_knowledge` no longer restates the clause lists (removed, since the sim
renders `known_docs`/`common/` content as internalized knowledge on its own — restating it in
`domain_knowledge` too would just be redundant, not another leak).

The H2 migration above already put `user_sim_docs/common/` in place for this task (`output_contract.md`);
this change adds the four `*_rules.md` files to that same directory rather than adopting the mechanism
fresh. `RESOURCES.md`'s `policy_kb` description reflects the trimmed mount; `spec.yaml`'s agent-facing
description was reworded to match without naming `user_sim_docs/` (that path must never appear anywhere
agent-visible).

`environment/sop/` holds `kpis.md` — business framing only, no acceptance criteria. `environment/data/`
holds `policies/` (structural: name + applies-to only) and `kpis.md`. See `tasks/order_to_cash/` for the
reference layout this task now mirrors.

A new test (`tests/tasks/compliance_review/test_seed_data_rules_pinned.py`) pins the `*_rules.md` files
to `scripts/build_seed_data.py`'s `POLICIES` table, so the two can't drift apart silently. This task has
no top-level `sop/` directory, so the repo's generic mirror-drift guard
(`test_published_sop_doc_copies_match_their_ground_truth_source`, which compares `user_sim_docs/`
against a `sop/` ground-truth tree) doesn't apply here — the new test above is the equivalent guard for
this task's actual ground-truth source, `build_seed_data.py`'s `POLICIES` table.

**Not changed:** which personas/levels know the policy content (all six still do, same as before —
this was a relocation, not a new information-access decision). `contracts_db` and `kpis` resources
untouched.
