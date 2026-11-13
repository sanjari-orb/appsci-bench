# Order-to-Cash — known issues

Open problems in the task as it stands. Each says what it is, why it matters, and what would settle
it. Nothing here is a blocker for running the task today; several are validity threats that should be
settled before the numbers are published.

---

## 1. The store is single-currency, which is a simplification (RESOLVED, deliberately)

**Decided:** every invoice, PO line and contract is denominated in **USD**. Nothing in the fixture
carries EUR, GBP or anything else.

**Why, and it is not the reason it looks like.** The obvious framing is "the substrate's currencies
disagreed, so we normalised them" — 798 of its 1,189 invoices carry a currency differing from their own
supplier's master record, and `XXX` appears as a currency code. But that was never the live problem: the
generator has always re-derived currency from the supplier master rather than copying the invoice's, so
no fixture row ever disagreed with its own supplier.

The real problem was that **the materiality rule is dollar-denominated and the comparison is a plain
numeric one**. With a EUR/GBP/USD mix, `delta > 500` was applied to euro amounts as though they were
dollars, so a EUR 501 delta "exceeded $500", and the answer key wrote dollar signs onto euro invoices —
literally, `price delta $222.95 below $500 materiality threshold` on a EUR line. **26 of the 28
threshold-decided scored cases were not in USD.** The rule and the data disagreed about what a unit was.

**Relabelled, not converted.** The amounts are synthetic, so no exchange rate was needed and none was
invented. Converting would have rescaled every amount, moving the injected deltas relative to the $500
line and flipping the label on any case that straddled it. Verified after the change: **currency is the
only field that moved** across all 1,095 invoices, `labels.csv` is byte-identical, and every eval sample
is unchanged.

**What is genuinely given up.** Multi-currency handling is no longer testable here. A real
accounts-payable team does reconcile currency, and this store no longer asks an agent to. Nothing
measures it today — no objective reads currency and no fault type concerns it — so nothing measurable
was lost, but the world modelled is a step simpler than a real one.

**If you want currency back, it is a task-design change, not a data tweak.** It needs a
currency-mismatch fault type in the SOP and the taxonomy, per-currency thresholds or an explicit FX
step, the personas updated so the rule is elicitable, and new eval cases plus a re-baseline. Adding
currencies to the data alone would restore the incoherence above.

`FIXTURE_CURRENCY` in `scripts/gen_o2c_eval_and_fixture.py` is the single place this is set, and a test
asserts every fixture invoice matches its supplier's contract currency.

---

## 2. Invoice-number collisions: avoided by skipping, not by renaming

**What:** The substrate reuses invoice numbers. **73 of the 1,168 usable records share a number with
another record** (69 distinct numbers repeat; one appears three times). The invoices table keys on
invoice number and every insert uses a silent conflict-skip, so a fixture containing duplicates would
quietly lose rows — and a scored case whose invoice was the one dropped would still run, still be
scored, and would be reading a *different* invoice than its answer key describes.

**What was done:** these collisions turn out to be **avoidable, not merely containable**. The
background generator skips any candidate whose invoice number is already taken — **73 skipped** —
rather than renaming it or letting it collide. Three consequences, all good:

- The fixture contains **no duplicate invoice numbers at all**, so the row count the database ends up
  with is exactly the row count the file states.
- No scored case can ever resolve against the wrong invoice.
- The cost is ~73 rows of volume: the fixture holds **1,095** invoices rather than the 1,168 the
  substrate could theoretically supply. That is a good trade for a class of silent-wrong-answer bug.

Renaming the colliding rows instead would recover the last ~73, and the underlying invoice
identifiers are unique, so it is available if the volume is ever wanted. It is not worth the minted
numbers looking unlike the rest.

**Checked, not assumed:** the generator asserts the fixture is collision-free before writing, and
`environment/db/02_load_data.py` re-checks after load that every scored invoice is present and no
invoice number resolves to two rows. A partial load fails loudly instead of producing a plausible
wrong score.

---

## 3. The background rows had to be given faults, because the substrate has none

**What:** The 1,035 background invoices carry the **same injected fault mix** as the scored cases:
66% flagged against the scored set's 68%, with every fault type represented.

**Why they could not simply be clean.** The substrate is **verifiably 100% clean on every dimension
the task scores**. Measured over the 1,181 invoices that resolve to a purchase-order line: zero
quantity mismatches, zero price mismatches, zero total-math errors, zero cases of invoicing more than
was received, zero supplier mismatches. So there are no organic faults to inherit — **clean background
would have meant a 0% fault rate sitting next to the scored set's 68%**, and the 60 scored invoices
would have stood out by inspection just as plainly as when they were the only rows in the database.
Dilution only works if the background is indistinguishable from what it hides.

**Why that is not a problem elsewhere.** Because every fault in the fixture is injected, every fault
is also *knowable*. There are no unlabelled positives hiding in the background — but there is also
**no committed answer key for it**, deliberately. What a background row is can be re-derived from the
fixture by comparing the invoice to its PO line and contract, which is the task itself, so a label
file would be duplicated state whose only possible future is drifting from the data it describes.
`tests/tasks/order_to_cash/test_fixture_volume.py` does that re-derivation in memory, and validates
its classifier against the 60 rows whose labels *are* committed before trusting it on the 1,035 that
are not. The eventual build/eval carve can classify the rows the same way when it needs to.

**What is still open:** nobody has checked that the two mixes match *well enough* — 66% against 68% is
close on the headline rate, but per-fault-type proportions differ (the background is proportionally
heavier on off-contract items and duplicates, because those scenarios have no per-record feasibility
constraint). Whether that is close enough to defeat inspection is a question for the carve work, not
this change. Deliberately deferred.

---

## 4. The contract's agreed price is arbitrary, and it contradicts the "clean" labels

**What:** Each supplier contract lists an agreed unit price per item. That number is **whichever
purchase-order line for that supplier-and-item happened to be written last** while the contract was
being built — it bears no relationship to the price on any particular line. Measured on the fixture:

- Only **10 of 60** scored purchase-order lines agree with their own contract's price.
- **All 8** cases labelled `CLEAN` — whose stated reason is *"matches PO and contract"* — have an
  invoice price that matches the purchase order exactly but differs from the contract price, often
  several-fold (e.g. invoice and order both 176.20 against a contract price of 384.48).

**Why it matters:** an agent that reads the contract table and checks the invoice against the agreed
price will flag **every one of the clean cases** and be marked wrong for it. The ground truth is only
self-consistent under one reading: price is checked against the **purchase order**, and the contract
governs **item coverage and fee allowlists only**. That reading is not stated anywhere the agent can
reach, and the label text actively contradicts it.

**Unaffected by the data growth.** Contracts are built from the full substrate, not from the sampled
rows, so the values did not move when the background was added — `contracts.json` is byte-identical
before and after. The number of rows an agent can inspect and find contradictory grew about
eighteenfold, though.

**What would settle it:** either give the contract a coherent price (derive it per supplier-item from
the orders it actually governs, and let a real price-vs-contract fault type exist), or drop
`agreed_unit_price` from the contract entirely and let the contract carry only what it is actually
used for. The second is smaller and matches how the task already scores. Either way the `CLEAN`
reason text should stop claiming a contract match it does not check.

---

## 5. Substrate licence is Unknown

**What:** The data is derived from the *Oracle Migration Dataset* on Kaggle
(`anjaliminakshi/oracle-migration-dataset`), whose card carries **no licence selection** and no
description-level grant. Full provenance, checksums and a vendored copy:
[`provenance/order_to_cash/`](../../provenance/order_to_cash/).

**Why it matters more than it did:** two things changed. The derived fixture went from 60 rows to
1,095 — same legal question, eighteen times the surface. And the eight original files are now
**committed verbatim** under `provenance/`, which is redistribution of the upstream work rather than a
transformation of it. That is a step beyond what the task shipped before.

**Mitigating facts:** the upstream is generated synthetic data imitating an Oracle system, not a
capture of any real one — no personal or commercial content, so no privacy exposure. It is also of no
evident commercial value: a single-version upload, 34 downloads, no votes.

**What would settle it:** ask the uploader, or stop depending on them. The generator's `--synthetic`
mode produces an equivalent clean chain we own outright with no third-party terms; switching costs a
re-baseline of the 60 scored cases and nothing else. Until then, treat both the fixture and the
vendored copy as derivative material of unsettled provenance.

---

## 6. Negative cases get thin at scale

**What:** The "below threshold, do not flag" cases sit in a narrow and shrinking window. The
materiality threshold is a **$500** total difference and the rounding tolerance is **$3**, but the
substrate's lines are large: median line value is about **$46,000**, and **778 of 1,168 lines have a
quantity above 166**, at which point a $500 total difference is under $3 *per unit*.

**Why it matters:** on a high-quantity line, the sub-threshold negative and the trivial-rounding
negative become nearly the same object — a per-unit price nudge too small to mean anything, separated
from "material" by a margin thinner than the rounding tolerance it is supposed to be distinguishable
from. The generator already concedes this: when the quantity is too large to fit a clean sub-threshold
case it **gives up and emits a material price fault instead**, so the negative silently becomes a
positive. The scored set has only **3** sub-threshold cases out of 60, which is part of why this has
not bitten yet.

**At scale it is more visible, not worse:** the background carries 107 sub-threshold rows, so the
near-degenerate shape is now present in quantity. Since the background is unscored this costs no
metric accuracy today — but any future expansion of the *scored* set would inherit it, and would be
measuring false-positive discipline against distinctions of a few dollars on a $46,000 invoice.

**What would settle it:** make the thresholds proportional rather than absolute (a percentage of line
value, with an absolute floor), or restrict sub-threshold cases to lines whose quantity leaves a
meaningful window. Both change the SOP, so both need the stakeholder personas updated in step.

---

## 7. Eval-only tells remain in the fixture (partly diluted)

**What:** Several fault injections leave a signature that identifies the row as generated rather than
organic. Before the background rows existed, every one of these marked a **scored** invoice:

| Tell | Scored rows | Background rows | Still diagnostic? |
|---|---:|---:|---|
| `-DUP` invoice-number suffix | — | — | **REMOVED** — see below |
| populated `duplicate_of` field | 3 | 88 | **No** — diluted |
| fabricated `ITEM-X…` item numbers | 2 | 89 | **No** — diluted 44:1 |
| a duplicate whose original invoice is absent | 3 | 88 | **No** — but see below |

Adding the background fixed the diagnostic value of these by accident of volume, which is the point of
dilution. Beyond that:

- **The `-DUP` suffix is gone, and dilution turned out to be the wrong bar for it.** Duplicates used to
  be named `<original>-DUP`; they now carry an offset number in a range the substrate does not use
  (`INV-3046` → `INV-13046`, see `DUPLICATE_NUMBER_OFFSET` in the generator). Volume had made it
  non-diagnostic of the eval *set* while leaving two things it could not fix: an artifact could flag
  every duplicate by matching the row's **name** instead of reading `duplicate_of`, and — the expensive
  one — a regex of `INV-\d+` captures `INV-3046` out of `INV-3046-DUP` and stops, so the lookup misses
  and the case scores zero with the duplicate rule correctly implemented three lines further down. That
  was measured, not hypothesised: four of six build phases lost exactly the three duplicate cases this
  way, and because a build share containing `-DUP` rows teaches the agent the format before it writes
  the regex, withholding the DUPLICATE category made a **data defect look like a difficulty result**.
  Pinned now by `test_no_invoice_number_announces_that_it_is_a_duplicate`, which asserts over the whole
  store rather than the scored rows.
- **The remaining signature is still unnatural.** A real off-contract item does not have `X` in its
  number. An agent cannot use it to find the eval set any more, but it can use it to find *injected
  faults* — a shortcut around actually comparing the invoice to the order.
- **Duplicates still have no original.** A duplicate invoice whose original is nowhere in the database
  is structurally impossible in a real ledger, and all 91 `duplicate_of` values point at invoices absent
  from the store. So the documented rule — "second invoice for an already-invoiced PO line" — is false of
  the data: there is no first invoice, because the generator renames the row it built rather than adding
  a sibling. An artifact cannot verify a duplicate against its original; it can only trust the field.
  **Still open.**

  The "fix both together or neither" note this entry used to carry was about a different pairing than it
  reads like. The asymmetry it warned of is between **populations** — making background duplicates
  coherent while scored ones stayed broken would turn "duplicate with no original" into a sharper tell
  for the scored set. The naming fix above was applied to every duplicate, scored and background alike,
  so it introduces no such asymmetry and did not need to wait for this one.

**What would settle the rest:** draw off-contract items from a real catalogue of non-contracted items
rather than fabricating identifiers, and write the original invoice alongside each duplicate. Both touch
the scored set. The second changes the store's row count, so unlike the naming fix — which was a pure
rename and left every count identical — it cannot be done without a fresh baseline.

---

## 8. The scored answer key is bundled into the task-env container (RESOLVED)

**Fixed.** `labels.csv` now lives at `seed_data/labels.csv`, above both phase directories, so it is
bundled into no container at all. Previously it sat inside the fixture that gets copied wholesale into
the task environment.

**Why it was worth fixing even though nothing could reach it.** The setup script deleted `/fixture`
after loading and the agent has no shell in that container, so there was no live path to it. But the
protection came from a property of the environment rather than from the data being gated: anything
that later gave the agent command execution in the task environment, or stopped the cleanup step
running, would have exposed the answer key with no other change looking wrong.

**What it cost.** The loader's post-load check used to read the key to assert every scored invoice had
landed. It cannot now, and it should not -- the same loader serves both phases, and the build share
contains no scored invoice by design. It asserts row-count parity against each bundled file instead,
which catches a partial load without knowing which rows are scored, and is what the carve spec asks a
phase's setup to end with.

**Second benefit, and it forced the move.** The carve guard treats every collection in the eval store
as something the build share must cover. With the answer key inside the fixture it was a collection,
so the guard demanded the build share carry it -- exactly backwards. Out of the fixture, the question
does not arise.

A test asserts both phase fixtures hold exactly their three data files and that the key still exists
host-side, so neither the leak nor an accidental deletion can come back quietly.

---

## 9. The build share is 60% of the store, not the ~20% the guideline suggests

**What:** the build environment holds 662 of the 1,095 invoices. The carve guideline aims for roughly
20%, and airline's strict carve lands near 1%. (354 of the withheld rows are the deliberate fault-type
exclusion in §10; without it the share would be 91%.)

**Why this is the correct outcome rather than a soft carve.** The rule is to withhold every record any
eval case touches. What a case touches depends entirely on what it does, and an order-to-cash case
*reads one invoice and mutates nothing*: it returns a flag and a reason. So the touched set is 60
invoices, their PO lines, the purchase orders they name and the item numbers their faults invented --
101 rows in total. Airline is small for the opposite reason: its golden actions WRITE, so booking
changes cascade into per-date seat counts on every flight a booking covers, and closure drags in most
of the store.

A task with no write actions has a small touched set and therefore a large build share. Forcing it
down to 20% would mean withholding records no case touches, which buys nothing and costs the agent
coverage.

**Two different things now hold the share down, and they should not be read as one number.** 433 rows
are withheld in total. 101 of them fall under the *threat bar* — the 60 scored invoices plus 41
background rows sharing a purchase order or an invented item number with a scored case. 354 fall under
the *fault-type decision* in §10. (22 rows qualify on both counts, which is why the two figures sum to
more than 433.) Only the first is a carve in the sense the guideline means. On the threat bar alone
this task's build share would be **994 invoices, or 91%** — that is the figure comparable to airline's
1%, and it is the honest answer to "how much does the carve withhold". The other 31 points are a
difficulty choice, and quoting 60% as "the carve" credits the threat bar with work the difficulty
choice did.

**What is worth watching.** The build share is large enough to be *representative*, which the
guideline never promises. An agent can infer the fault distribution from it quite accurately -- 66% of
background rows carry an injected fault (§3). That is not a leak of the eval set, but it does mean an
agent can tune a flag rate against the build data and have it transfer. Whether that is a validity
problem or simply the task being learnable from its own environment is an open question, and the
first scored run after the split is the evidence to look at. §10 is a partial answer to it: for the
four rules where inferring-from-data is most damaging, the data is no longer there to infer from.

**The distribution an agent can now infer is skewed, and that is a second-order cost of §10.** The
build share's own flag rate is not the store's: withholding four positive categories removes 354
flagged rows and no clean ones, so an agent that tunes a flag rate against the build data will tune it
low and carry that bias into eval. Before the hardening the build share ran at roughly the store's own
rate; it no longer does. This does not make the eval set unanswerable — the rate is not what any
objective scores — but a run that under-flags should be read with it in mind rather than as pure
elicitation failure.


## 10. Four fault types are withheld from the build share on purpose

**What:** the build environment contains no `OFF_CONTRACT_FEE`, `TOTAL_MATH`, `OFF_CONTRACT_ITEM` or
`DUPLICATE` invoice. All four are present in the eval store and all four are scored.
`BUILD_WITHHELD_FAULTS` in `scripts/gen_o2c_eval_and_fixture.py` is the single place this is set.

**Why.** The inclusion test is whether an agent could establish the rule from an invoice and its PO
line alone. Four of the six faults fail it: both contract faults need a per-supplier contract lookup;
a total needs the arithmetic *and* the $500 materiality line, including the fact that a *permitted*
fee legitimately raises a total; and re-invoicing needs knowing the condition is flaggable at all.
The other two — `PRICE_DELTA` and `QTY_MISMATCH` — are direct comparisons and stay visible. Leaving
examples of the first four in the build share lets an agent pattern-match its way to those rules
without ever asking the customer what they are, and eliciting the rules is what the benchmark
measures.

**Why it went from two to four.** The original pair withheld the fee fault but left the off-contract
*item* fault in place, which still told the agent that contracts govern what may be invoiced — half a
disclosure is a disclosure, and the half that remained pointed at the same lookup the withheld half
needed. Both contract-dependent faults now go together. Re-invoicing joined them on the same test.

**What stays visible is deliberate, not residual.** Withholding the two derivable faults as well would
have made the task harder without making it more informative: a run that misses a withheld category
failed to elicit, while a run that misses a visible one failed to compare two numbers. Those are
different failures with different fixes, and the split preserves the ability to tell them apart. The
constant is where to revisit that if the evidence says otherwise.

**This inverts the evidence rather than merely hiding it, and that is intended.** The build share
keeps 207 invoices carrying fees and every one of them is permitted; it keeps small rounding gaps but
no material ones; every item it shows is on its supplier's contract; and no invoice it shows
duplicates another. An agent that induces rules from the data available to it will conclude that fees
are always acceptable, that totals always reconcile, that everything invoiced is on contract and that
invoices are never re-sent, and will score zero on all four categories. An agent that assumes a rule
from the absence of counterexamples *should* be penalised -- that is the failure this prices in.

**The duplicate marker column goes empty, and that is an accepted cost.** Unlike fees, re-invoicing
has no benign form: the column naming the duplicated invoice is populated only by the fault itself, so
withholding the category empties it across all 662 rows. The decision was to accept the empty column
rather than keep the category. An agent that notices a column that never carries a value has a reason
to ask what it is for, which is the behaviour being measured; one that concludes re-invoicing does not
happen scores zero on the category. Neither reading is misled, because the column is empty rather than
wrong. A test asserts the emptiness so a later regeneration cannot quietly repopulate it and hand back
a worked example of the withheld rule.

**The trade-off.** Before the original change both withheld categories scored ~100% and told you
nothing; afterwards they may score ~0% and tell you nothing in the other direction. The informative
outcome is a split across agents, not a uniform floor. Doubling the withheld set doubles that
exposure: four of the six scored categories now rest entirely on elicitation, so an agent that elicits
nothing has a much lower ceiling than before and the floor is closer to the whole score. If every
agent fails all four, a ceiling has been traded for a floor and the choice should be revisited -- the
constant makes that a one-line reversal, and reverting to the original pair is a two-word edit.

**Not yet measured.** No scored run exists against the four-category share. The two-category version
was itself never validated by a run (the §9 note still calls the first scored run "the evidence to look
at"), so this change compounds an untested difficulty decision rather than building on a tested one.
Read any first result as evidence about the carve as much as about the agent, and do not compare a
score across the change: build-phase conditions differ, so o2c numbers from before the hardening and
after are not the same measurement.

**Relationship to the coverage invariant.** `docs/task_env_carve_spec.md` holds that a build share
must show one instance of every enumerated value. This is a deliberate exception, and the carve guard
cannot detect it: the fixture carries no fault-type column, so `carve_guard.py` sees full coverage of
everything it can see and passes. The exception is therefore recorded here rather than enforced.
`tasks/README.md` ("Choosing the split") states the general rule and its two conditions -- record the
decision, and keep the rule reachable by asking. Coverage of the fields the guard *can* see (supplier,
unit of measure, currency) remains a hard requirement, and a test still asserts it: withholding 354
rows by fault must not also cost a supplier or a unit of measure, and it does not.

**Tests.** `tests/tasks/order_to_cash/test_fixture_volume.py` asserts each half: the four types are
absent from build, still present in eval, fees themselves still appear in the build share, every build
item is on contract with a non-empty contract behind it, and no build invoice is marked as a duplicate.
