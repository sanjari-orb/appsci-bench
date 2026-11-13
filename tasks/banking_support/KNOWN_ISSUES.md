# Banking task — known issues

## 1. Benchmark contamination: the model may already know the tau2 banking domain

**What:** `banking_support` is offline-cast from **tau2-bench**'s `banking_knowledge` domain, which is
**public**. The same threat airline documents in its §1 applies here: a frontier model may hold the
Rho-Bank policy, the product catalogue and the tool signatures in training data, so it can reproduce
them from memory rather than retrieving them from the environment.

**Why it matters more here than it looks.** The task's whole premise is that correct handling is gated
on *retrieving the right document* — the knowledge base is what names the specialised tool to unlock.
An agent that recalls the catalogue instead of searching it can reach a correct outcome while scoring
`retrieval_recall` badly, or worse, reach it *and* search plausibly, which is indistinguishable from
genuine grounding in the score.

**Not yet measured for this task.** Airline's §1 rests on a specific trace showing verbatim recall;
nothing equivalent has been run for banking. The honest status is "the same threat exists, unquantified".
`scripts/detect_task_leakage.py` and a run with the corpus withheld would both bear on it.

**What is agent-visible on purpose:** the base tool signatures. Tool *discovery* is not what this task
measures. What the agent is NOT given: the policy, the KPIs, and the specialised tool names (those come
from the knowledge base at run time).

## 2. `groundedness` can be passed vacuously

The `ungrounded` control — which answers "based on what I know … that should be fine", retrieves
nothing and asserts nothing checkable — scored **groundedness 1.0** in the P7 run, while the gold agent
that retrieved four real documents scored 0.0.

The judge appears to reward the absence of unsupported claims rather than the presence of supported
ones, which inverts the metric's intent. It no longer distorts the headline number -- `groundedness`
moved to weight 0 in §7 -- but it does make the metric useless for the diagnostic job it was kept for,
so it still needs resolving before the column is read as meaning anything.

## 3. The reference agent is not yet a strong reference

`gold/banking_support/` retrieves correctly (`retrieval_recall` 1.0) but on the sampled cases picked the
wrong product and did not reach the golden DB state, so it does not currently beat the naive controls on
`task_success`. It is honest as a *wiring* reference and as the thing the controls are compared against;
it is not yet evidence of an achievable score. Deterministic validation of the measurement chain is done
separately, by a local smoke driver that replays a golden trajectory through the live tool API; that does
reach 1.0.

## 4. Two copies of the retrieval runtime, hand-synced

`tasks/banking_support/seed_data/upstream_src/` keeps the upstream sources for provenance, and
`tasks/banking_support/environment/_runtime/` holds the vendored, adapted copy the environment actually
runs. Nothing enforces that they stay in agreement — the same unguarded-duplication issue airline
records for its policy. `TAU2_COMMIT.txt` pins what the copy was taken from.

## 5. The build environment is carved to 8 real records, then augmented with 100 authored ones

**Resolved twice over, and the second half REVERSES a decision this entry used to record — read both.**
The two phases load different stores: the build phase gets a carved share of **8 real records plus 100
authored synthetic ones**, the eval phase keeps the full **768**. Nothing transactional is baked into the
image any more — the service boots holding no records and each phase's setup script loads its own store
through an admin-gated route — so the leak this entry used to describe is closed rather than merely
declared closed.

**Why the share is 8.** Withhold every record any eval case touches, add the two records a case is about
only in its prose (below), then close over the reference graph, and 760 of 768 records go. **32 of the 39
customers are touched by name** — not the 34 this entry once recorded, which came from scanning the case
files for each customer id as raw text: the two extra hits were `123` and `125` matching by coincidence
inside zip codes, a balance, an income and several transaction ids. The correction matters less than what
that method missed. Counting customers never followed the reference graph, so it never surfaced that
almost the whole store goes with them: the store is a hub around customers, 17 reference edges nearly all
terminating on one, so withholding a customer withholds their accounts, cards, transactions, referrals,
disputes and payment history too.

What the CARVE leaves is 6 customers and 2 bank accounts: zero cards of either kind, zero card accounts,
zero transactions of either kind, zero applications, zero referrals, zero disputes, zero payment or
verification history. Both surviving accounts belong to the same customer, so 5 of the 6 surviving
customers own nothing at all. That was the whole build store until the augmentation below; it is now the
real-record half of it.

| Task | Build share | Full store |
|---|---|---|
| `order_to_cash` (#133) | 662 invoices | 1,095 |
| `airline_rebooking` (#137) | 10 reservations | 2,000 |
| `banking_support` | **8 carved + 100 authored** (6 customers) | 768 records (39 customers) |

### The reversal: synthesized records were declined, and are now in

This entry used to say the owner's decision was **a strict carve-out with no synthesized records**, that 8
"is the answer rather than a starting point", and that an earlier argument for synthesis had been
"considered and declined". **The owner reversed that on 2026-09-11.** The build store now carries **100
authored synthetic records** on top of the carved 8 — 108 in total. The withholding rule is untouched: the
augmentation is a separate stage that runs after the carve, and the carve's own residue is still asserted
at 8, so an augmentation can never mask a carve that started leaking.

**A measurement changed the decision, not an argument.** The earlier case for synthesis was that a share
this small is an empty build environment rather than a thin one. The case that actually landed is narrower
and evidenced: on run `banking_support_b1_l3_20260910_023628` the agent's own eval generator authored a
debit-card freeze case against an invented customer (`usr_marcus_reilly_demo`), because the store held no
card to freeze and no customer who owned one. Its judge prompt states the consequence itself — *"the
persona uses plausible demo identifiers rather than a shared mutable record"* — so it graded the
conversation instead of the outcome. It scored itself **0.9175**, rising to 0.96 over six checkpoints; the
artifact then scored **0.021** on the real eval, whose entire score is a database state hash. The agent's
internal measurement and the benchmark's had become measurements of disjoint things, and an internal score
that saturates at 0.96 while the true value is 0.02 cannot steer an optimizer.

So the cost this entry recorded — "expect build-phase quality to drop and read that as the carve working" —
turned out to include a failure mode nobody had priced: not that the agent would build a worse artifact,
but that it would lose the ability to *tell* whether it had.

The counter-arguments recorded here before still stand on their own terms and are worth keeping: ids in
this environment are content hashes, so synthesized ids are not free (you choose content and the id
follows), and staying inside "carve-out only" kept banking clear of the docs conflict airline's synthetic
store creates. Both were accepted as costs. The authored ids are written by hand in the store's own
conventions rather than generated, and this entry plus `environment/build/README.md` are the docs that
would otherwise have gone stale.

**What the authored records are.** 100 records hanging off the six customers the carve left, giving each one
situation an internal eval case can be built from: a card `PENDING` activation, a card `FROZEN` with a
replacement in flight, a duplicated charge with an open dispute, a payment history with a late month and a
missed one, a limit increase denied for `past_due_balance`, a closed card with its logged reason, referrals
in every state, and three verification records showing the shape the agent must itself write. Authored in
`scripts/banking_build_synthetic.py`; the rules they obey are documented there and in
`environment/build/README.md`.

**None of it comes from the eval environment**, and that is enforced rather than asserted: no authored key
may exist in the full store, be named by any eval case, or collide with an id a golden action mints, and
every reference must resolve inside the build share. The generator refuses to write otherwise.

**Two records the by-eye read flagged, which the automated derivation cannot see — now withheld.** The
carve matches record keys as whole values, which is what keeps the short customer ids `123` and `125` from
being "found" inside zip codes and amounts. The same property makes an identity claimed in a case's PROSE
invisible, and two kept records were named that way:

- **Customer `125` (Ayan Wang) and everything of hers.** `tau2_banking_34` opens "You are Ayan Wang, a
  Rho-Bank customer (user_id: 125). You have a Green Account checking account." The id appears only inside
  that sentence, never as a structured value, so neither the derivation nor `carve_guard.py`'s structural
  pass can see it — the checker excludes free text deliberately, for the zip-code reason. Four records
  were hers: the customer row, bank accounts `03` and `04`, and debit card `dbc_7a3f9c2b1e`. The case is
  ACTION-basis (it counts transfer requests) and its golden actions mutate nothing in the base store, so
  nothing was scored *off* those records — but the case is *about* her, which is the vocabulary's
  definition of case-bearing.
- **Card application `f1a2b3c4d5e6f789`.** Its `customer_name` is "Wei Chen", the customer
  `tau2_banking_10` is about (user `76ad9cc60e`, withheld). The record carries no `user_id`, so there is
  no reference edge for closure to follow even with her customer row gone. That case is about referral
  bonuses rather than this application, and no golden action touches it.

Both are now seeded into the withheld set by hand, in `WITHHELD_BY_EYE` in
`scripts/generate_banking_build_share.py`, which records per record which case it belongs to and why the
derivation is blind to it. They are fed INTO the closure rather than deleted from its output, so customer
`125`'s two accounts and her debit card leave with her through the ordinary incoming-reference rule and no
cascade is written down anywhere to fall out of step with the data.

| Carve | Residue |
|---|---|
| Derivation alone | **13** — 7 customers, 4 accounts, 1 debit card, 1 application |
| Also withhold customer `125` | **9** — 6 customers, 2 accounts, no card at all |
| Also withhold the application | **12** — no application at all |
| **Both — as shipped** | **8** — 6 customers, 2 accounts, nothing else |
| **Plus the authored records** | **108** — every one of the 17 collections non-empty |

**What the carve alone cost, which is why it was augmented.** Taking both by-eye withholds left the build
environment with no card record and no application record of any kind, so the agent could not see either
one's shape from data — nor any card account, transaction of either kind, referral, dispute, payment record
or verification record. Of the five business scenarios only product advice had any data behind it, and card
servicing had none. The authored records close that: schema and mechanism are now learnable from data.

**What it still costs.** The agent sees a plausible six-customer bank, not the real distribution. It
**cannot learn fault patterns, category mixes or real-world proportions** from this store, because 108
records chosen to cover situations are not a sample of 768 — volume, frequency and co-occurrence are wrong
by construction, and an artifact tuned to what is common *here* is tuned to nothing. Those still have to
come from the integration-engineer stakeholder, who describes the collections and the identity-verification
rule. **Score comparisons for this task must not straddle either change** — not the carve, and not the
augmentation.

Category coverage went from zero to most of the way. Under the carve alone it could not be anything but
zero — 15 of 17 collections were empty, and an empty collection covers none of its categories — so this task
carries `task_runner.carve.coverage_waiver` in its spec. The augmentation closed **10 of the 18** enumerations
that waiver used to name: both account classes and all twelve levels, every bank-transaction type, both
credit limits, both closure reasons, both replacement-order reasons, and every debit-card status and issue
reason. The waiver is down to **8**, and what remains cannot be closed without copying the eval customers'
own identities into the build store — card secrets, printed cardholder names, verification contact details,
a shipping address — plus the card-type mix in the transaction history, where covering every value would
mean six customers holding eleven credit cards. The guard reports every uncovered value in full while not
failing; disjointness and closure stay mandatory and both pass.

**The knowledge base is deliberately NOT carved.** Both phases hold the same 698 documents and the
corpus stays baked into the image. 46 of the 48 specialized tools are named *only* by documents the eval
cases require, so withholding those would not make the task harder — it would make it unsolvable and
would gut the retrieval metric. The corpus is read-only by construction and contains no record id from
the store at all. The residual exposure is that a *list* of required documents is close to an answer key;
the protection is keeping that list out of any agent-facing artifact.

See `docs/banking_carve_plan.md` for the sequenced plan and the measured per-collection residue, and
`environment/build/README.md` for the share itself.

**Consequence for scores.** Build+eval runs still load the eval store for scoring, so their numbers are
unaffected on the eval side by either change. An **eval trial** on this task is measured against the
same carved shape airline and order_to_cash use, so trials are comparable across tasks again — but trials
recorded on banking before the carve are not comparable to ones recorded after it, and neither are ones
recorded before the augmentation.

## 6. What is elicited, and the limits of the current cut

Three of the bank's operating rules moved out of the agent-facing spec and into
`user_sim_docs/common/operating_rules.md`, so the agent must ask the stakeholder for them
(mechanism: #80; precedent: #89 for order_to_cash, #100 for airline). The KPI document moved to
`user_sim_docs/success_kpis.md` and is scoped to the L3 personas — before this it sat in `sop/` where
nothing read it and **nobody** had it, not even the simulator.

| Rule now elicited | Cases that lean on it | Scored by |
|---|---|---|
| Identity verification is mandatory before an account action | 81 of 97 | `verification_compliance`, and `task_success` — the verification write lands in the hashed store |
| The unlock-then-call protocol for specialist tools | 69 of 97 | `task_success` — 730 of 955 golden actions |
| Escalate only when the knowledge base has no applicable action | 16 of 97 | `verification_compliance` |

**90 of 97 cases** lean on at least one, leaving **7** that follow entirely from handed material as a
baseline-competence control. That is a heavier cut than airline's (35 of 50, 15 controls). Scores from
before this change are not comparable to scores after it.

### Three limits, stated rather than implied

**The unlock-then-call protocol leaks through the tool descriptions — but to the SIMULATOR now, not to
the agent.** The reference describes `unlock_discoverable_agent_tool` as *"Unlock an agent discoverable
tool that was found in the knowledge base"* and `call_discoverable_agent_tool` as *"Call an agent
discoverable tool that you have previously unlocked."* Those two sentences give the sequence away to
anyone who reads them.

This used to be recorded here as a hole in the hide, on the understanding that the agent received the
reference. It did not — no agent-facing field names those tools and no route enumerates them — so for a
while the descriptions leaked to nobody. The reference is now sim-only
(`user_sim_docs/common/tools_reference.json`), which changes what the limit IS: a stakeholder asked about
the specialist tools will convey the ordering readily, because it is written in the material they hold.
That is the intended behaviour rather than a defect — the point is that the agent has to ask — but it does
mean the rubric's sequence item is easier to satisfy than its four-step wording suggests, since two of the
four steps come free with the tool names.

The descriptions are left unedited deliberately: they are upstream tool signatures, which #100 decided
against hiding for airline, and rewording them would break byte-provenance with upstream tau2-bench. What
is genuinely elicited beyond them is that most work *requires* this path, that specialist tools cannot be
enumerated in advance, and that some are customer-side.

**The rules exist in two places on the simulator's side.** The personas still state them in inline
`domain_knowledge`, and the new document states them again. Nothing enforces agreement, so the two can
drift. Left as-is on precedent: #100 relocated airline's documents without stripping persona knowledge,
and #80 explicitly left inline persona content "uncounted by choice" — and stripping it would alter the
elicitation source, which is a scoring-relevant change of its own.

**Relocation alone does not prove the score measures elicitation.** This task is cast from a public
benchmark (see §1), so a model may hold these rules from training rather than from asking. Rewording
them is the actual fix and is separate work. Until then, a high `verification_compliance` is consistent
with either eliciting the rule or recalling it.

## 7. `final_score` is `task_success` alone; four diagnostics are disabled by default

The score used to be a blend: the tau2 reward at 0.5, plus groundedness 0.2 and retrieval recall,
verification compliance and no-fabrication at 0.1 each. It is now the tau2 reward at weight 1.0 with the
other four at weight 0. They initially remained calculated and reported; they now have `enabled: false`
in `spec.yaml`, so default evaluations skip their calculation and omit them from new reports. Set
`enabled: true` on an individual objective to calculate it again without changing its weight or code.
The separate build-elicitation diagnostic is unaffected. `task_success` is tau2's own banking
reward and the only figure here that can be read against a published tau2 number, so a blended headline
was a number no upstream figure could be compared to. Do not give any of the four a non-zero weight
without accepting that it breaks comparability.

Three consequences worth holding onto:

**Scores from before the reweight are not comparable to scores after it** -- the same run's headline
moves. Any figure quoted from an earlier run needs the weights it was computed under stated beside it.

**Comparability now rests on the caveats in `seed_data/PROVENANCE.md`, not on the weights.** Our
retrieval surface is now exactly upstream's `all_tools` variant -- `grep` was dropped and `shell` made
real, both in the sidecar change -- so the superset caveat that used to sit here is gone. What replaces it
is a narrower one: before that change `shell` was declared but dead, so every earlier banking number is a
no-shell number and is not comparable to a published figure or to a post-sidecar run of our own.

**One degenerate case still lets the judged axes reach the score.** The aggregator falls back to a plain
unweighted mean when every surviving metric has weight 0 (a deliberate, tested behaviour in
`appsci_bench/eval/aggregate.py`, for tasks that leave weights unset). `task_success` returns `None` --
excluded rather than zero -- when the harness cannot measure a case, so a run where it is unmeasurable on
*every* case would produce a `final_score` that is really the average of the four judges. The conditions
are whole-run ones (the per-sample reset seam off, or an asymmetric read-log allowlist), and the report
does warn in this case -- `objective 'task_success': every data point returned score=None` -- so it is
visible rather than silent. Reading that warning before reading the score is the mitigation; making the
aggregator return no score instead of a judge average would be a change to shared behaviour every task
uses, and has not been made.
