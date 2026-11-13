# Splitting the banking task environment: a build env and an eval env

> **Status: landed.** Mechanism landed in #117; `order_to_cash` (#133) and `airline_rebooking` (#137)
> are carved, and `banking_support` now is too. Every step in the *Sequence* table below is done. The
> build phase serves **8 records**, the eval phase the full **768**, and the two phases bundle different
> data directories through one shared setup script. The residue and its cost are recorded in
> `tasks/banking_support/KNOWN_ISSUES.md` §6 and
> `tasks/banking_support/environment/build/README.md`.
>
> Read [`task_env_split.md`](task_env_split.md) for the design and vocabulary, and
> [`task_env_carve_spec.md`](task_env_carve_spec.md) for the carve rules, before this.

## The three decisions this plan is built on

Taken by the task owner, recorded here because each one changes what the carve is:

| | Decision | What it settles |
|---|---|---|
| 1 | **The knowledge base is shared whole with both phases.** | The corpus is not carved. It is identical in both phases, so it can stay baked into the image — which removes the hardest piece of engineering (below). |
| 2 | **No synthesized build records.** A strict carve-out, and whatever residue it leaves is the build share. | The build share is **8 records** — 13 from the derivation, less the two records a case is about only in prose and their dependents. Banking stays inside the "carve-out from existing data only" rule, so the docs conflict that airline's synthetic store creates does not block banking. |
| 3 | **Extend the carve checker rather than reshape banking's data to suit it.** | The checker learns banking's store shape and its bare-numeric keys, and category coverage becomes waivable with a recorded reason. |

Decision 1 is right for a reason worth keeping on the record: 46 of the task's 48 specialized tools are
named *only* by documents the eval cases require, so withholding those documents would not make the task
harder — it would make it unsolvable, and it would silently gut the retrieval metric. The corpus is also
read-only by construction (retrieval never touches the transactional store) and contains **no record id
from the store at all** — all 768 keys searched against all 698 documents, zero hits. The residual
exposure is that a *list* of required documents is close to an answer key; the protection is keeping that
list out of any agent-facing artifact, not carving the corpus.

## What decision 1 removes from the work

Sharing the corpus collapses most of what made banking harder than airline. **Verified directly:** an
empty transactional store constructs cleanly, a full corpus alongside it builds its retrieval indexes as
today, keyword search returns real results, and a read tool against the empty store answers "No records
found" rather than erroring. So the service can boot holding **no records but the whole corpus**, which
is all the carve needs.

Four problems therefore disappear rather than being solved:

- **Booting on an empty corpus.** The retrieval bundle refuses to build over an empty corpus. It never
  has to — the corpus is baked and present at boot.
- **Rebuilding the retrieval index on load.** The keyword and regex pipelines are built eagerly at
  construction and shared across every per-case copy precisely to avoid re-indexing ~700 documents per
  case. Nothing swaps the corpus, so nothing rebuilds.
- **Embeddings-cache validity after a corpus swap.** The on-disk cache is keyed by embedder config, not
  by corpus, so a swap would have been a silent-correctness question. There is no swap.
- **Two of the three host-side path repointings.** The corpus resolver and the second, independent corpus
  load inside the scoring code both keep resolving the one shared corpus. Only the **store** path moves.

What remains is close to airline's carve in shape: un-bake one JSON store, add a load route, one shared
setup script, one path constant repointed.

## What is measured

All figures come from replaying all 97 cases through the real environment — fresh copy, seed, apply the
golden actions with the read-log allowlist set as scoring sets it — and diffing the store before and
after. All 97 replayed with no raised exceptions, so mutation counts are measured, not inferred.

| | Store | Withheld | **Build residue** |
|---|---|---|---|
| Customers | 39 | 33 | **6** (5 own nothing at all) |
| Bank accounts | 33 | 31 | **2** (both the same customer's) |
| Debit cards | 8 | 8 | **0** |
| Card applications | 1 | 1 | **0** |
| Card accounts | 38 | 38 | **0** |
| Card transactions | 338 | 338 | **0** |
| Bank transactions | 266 | 266 | **0** |
| Referrals | 19 | 19 | **0** |
| Payment history | 18 | 18 | **0** |
| Everything else | 57 | 57 | **0** |
| **Total** | **768** | **760** | **8** |

The derivation on its own leaves 13 — 7 customers, 4 accounts, 1 debit card and 1 application. The extra
five come out through the by-eye pass (*Verification*, step 10): the owner withheld **customer `125`**,
whom `tau2_banking_34` is about, and **card application `f1a2b3c4d5e6f789`**, whose customer name is the
customer `tau2_banking_10` is about. Neither is visible to any derivation over this data — `125` occurs in
that case only inside an English sentence, and the application carries no customer id at all — so both are
seeded into the closure by hand, in `WITHHELD_BY_EYE` in `scripts/generate_banking_build_share.py`.
Customer `125`'s two accounts and her debit card then leave with her through the ordinary
incoming-reference rule rather than through a written-down cascade. That takes 13 to 8 and empties the last
two collections beyond customers and accounts.

Two structural facts that shape the sequence:

- **The eval cases largely bring their own world.** 557 of the records their starting states write do not
  exist in the base store at all, and the golden actions create 359 more. Those need no carving.
- **But 34 of the 97 cases depend on base-store records their own starting state does *not* seed.**
  Verified by replaying each case against the full store, the residue, and an empty store: residue and
  empty give identical results, so the dependency falls entirely on withheld records. **The eval env must
  keep the full store** — making it seeds-only would break a third of the suite.

The figure previously recorded in `KNOWN_ISSUES.md` — "34 of 39 users touched, leaving 5" — was wrong and
is now corrected there. It came from scanning the case files as raw text; the two extra hits were the
short numeric ids `123` and `125` matching inside zip codes, a balance, an income and several transaction
ids. The number mattered less than the method: counting customers never followed the reference graph, so
it never surfaced that almost the whole store goes with them.

## What the build environment holds, and what that costs

**Holds:** the full 698-document knowledge base, a fully working tool surface, the three stakeholder
personas, and 8 transactional records — 6 customers and 2 bank accounts, both belonging to one of them.

**Costs, stated plainly because they are real and were accepted:** the agent cannot learn fault patterns,
category mixes or distributions from data. It will see **no card record and no card application of any
kind**, so it cannot learn the shape of either from the environment — nor any card account, transaction of
either kind, referral, dispute, payment record or verification record. Of the five business scenarios, only
product advice has any data behind it, and card servicing now has none at all. Schema knowledge has to come
from the integration-engineer persona, which does describe the collections and the identity-verification
rule — so elicitation has a path, but a narrower one than airline's.

The card and the application went in the **by-eye** step rather than in the derivation, which makes their
absence a choice rather than a consequence of the mechanical rule. It was taken knowing exactly this cost;
`tasks/banking_support/KNOWN_ISSUES.md` §6 records the residue each of the four options leaves. The
alternative — keeping one pending debit card and one stale application so the agent could at least see
their field shapes — would have left the build environment holding two records an eval case is *about*.

**Expect build-phase quality on banking to drop, and read that as the carve working rather than as a
regression.** Score comparisons for this task should not straddle the carve.

## Sequence

The order is forced by one fact about the harness: **the bundle arrives two steps after the service
process has already started**, so a service that reads its store at process start can only ever read what
was baked into the image. Un-baking is therefore step one, not a tidy-up.

| # | Step | Done | Notes |
|---|---|---|---|
| 1 | Move the store to the eval phase's data directory | ✅ | A rename — it is already the full store. The corpus does **not** move (decision 1). |
| 2 | Repoint the store's default path at the eval copy | ✅ | One constant now, not three. Get this wrong and the oracle replays golden actions against different data than the container held: every state check fails, with nothing in the score to say why. |
| 3 | Boot the service with an empty store and the baked corpus; add an admin-gated load route that **replaces** the pristine copy | ✅ | Replace, not merge: the between-case reset restores the pristine copy, so a merging load would make every reset restore boot-time state instead of this phase's. Admin-gated because *which records exist* is the phase difference — the artifact must be able neither to load them nor to count them. |
| 4 | Stop the image baking the store; unset its path variable. **Leave the corpus baked.** | ✅ | |
| 5 | Write the shared setup script: load, assert against a manifest, delete the raw file | ✅ | One script, both phases, one fixed bundle destination, so it never learns which phase it is in. Delete the raw file once loaded — leaving it readable is a second, unaudited path to the data. |
| 6 | Author the build share with a checked-in, deterministic generator | ✅ | Derives the withheld set from the eval cases themselves and reports what it withheld. Keep that report out of anywhere the agent can reach. |
| 7 | Point each phase's declaration at its own data directory | ✅ | |
| 8 | Run the checker on every regeneration, from step 6 on | ✅ | Not once at the end. |
| 9 | Verify end to end through real containers | ✅ | Including the store-identity invariant: the hash the eval container reports must equal the oracle's replay base hash on the host. |
| 10 | Read the carve over by eye | ✅ | See *Verification*. It found two records an eval case is about that no derivation can see, and both are now withheld — which is what took the residue from 13 to 8. Recorded per record, with its case id, in `WITHHELD_BY_EYE` in the generator. |

**Steps 3, 4, 5 and 7 land in one commit.** Any two apart produce a system that is healthy, green and
wrong — see *Dangerous partial states*.

### The assertion step needs a manifest, not a non-zero check

The carve spec's "assert each collection loaded a non-zero count" **cannot be written here**: under a
strict carve 21 of the 23 collections a manifest lists are legitimately empty — 15 of the 17 the store
file carries, plus six the data model declares and this store never uses — so a non-zero check fails on a
correct build share and a total-only check passes a badly truncated one.

Replace it with an **expected-count manifest bundled beside each phase's store**, asserted exactly after
load. That is stronger than non-zero on both sides — it catches a partial load, a truncated file and a
regenerated share nobody re-checked — and it is the only form that serves two phases whose totals differ
by two orders of magnitude. The manifest is task-owner material and goes in the phase's data directory,
which is deleted after load, so it never outlives the setup step.

## Verification

**What the checker covers now that the phases differ:** text disjointness across every readable file under
the build sources, referential closure, and category coverage. Banking reports **disjointness ok, closure
ok, coverage WAIVED** with 18 acknowledged findings and no unnamed gap. **What had to change first
(decision 3, now done):**

- **The store's shape.** It saw each collection as a wrapper of records plus a notes string and collapsed
  the file into one pseudo-collection of 17 rows; closure found no keys and coverage inferred no
  enumerations, so both came back unmeasured — which the CLI and CI treat as failure. Extended.
- **Bare-numeric record keys.** Banking's customers are keyed by short numbers, below the identifier
  heuristic, so a leaked customer record carrying no alphanumeric field would have passed. The fix was not
  to lower the token threshold — those digits recur inside zip codes, incomes, balances and transaction ids
  — but to match the eval store's own confirmed keys structurally, per collection.
- **Day-first dates were read as categories.** Banking writes every date as `MM/DD/YYYY` rather than in ISO
  order, so seven date fields — a card's expiry and issue dates, a card order's date and expected delivery,
  a payment date, a date of birth, a card-closure timestamp — were inferred as enumerations and reported as
  gaps. Those gaps can never be closed by carving better (no small share holds every date a large store
  holds), so the only available response was to name all seven in the waiver, burying the real findings
  under seven artefacts of a date format. The date test now accepts both spellings. Measured: 7 of
  banking's 25 gaps go, and nothing changes for airline or order_to_cash, which both write ISO.
- **Category coverage became waivable, with a recorded reason and a named list.** Banking's coverage is
  zero and cannot be otherwise under decision 2. A waived run still reports the full gap: a waiver
  suppresses the failure, never the finding, and never touches disjointness or closure, which remain
  mandatory. A gap the list does not name still fails.

**Three gaps that stay in the waiver although they are not really categories**, and deliberately were not
fixed alongside the dates: a debit card's verification code, its last four digits, and a verification
record's address. They read as enumerable only because banking has eight debit cards and three verification
records, and no structural rule separates them from a genuine category the way a date pattern does. The
only rule that would silence them is a wildcard, and a wildcard entry in a waiver would later absorb a gap
in a *new* field of the same collection with nobody told — which destroys the property that naming each
enumeration exists to buy. So they are named one by one.

**What the coverage check actually reports, corrected.** An earlier version of this document implied the
check reports banking's 13 card product types and 15 spend categories as uncovered. It does not, and never
did: both are above the checker's cardinality cap, so neither is inferred as an enumeration at all. They
are indeed absent from the build share, but the check never claimed to look at them — the eye is the only
thing that catches those. What it does report is 18 lower-cardinality fields: every transaction type and
card type it can see, the referral statuses and account types, a debit card's status, issue reason and
cardholder name, the closure and card-order reasons, a shipping address, a card's credit limit, the account
class and level, and the three verification-record fields.

**Tests added** in `tests/tasks/banking_support/test_build_share.py`, adapting airline's build-seed test: the
share loads through the real environment code and survives the per-case copy; no record is pre-seeded with
state a golden action is supposed to create; every kept record is byte-identical to the full store's copy, so
a monetary value cannot change shape and move a state hash; the required-reads derivation returns the same
list on both sides; the two by-eye withholds are pinned ABSENT, naming their cases, so a regeneration cannot
quietly restore them; and no agent-facing text states a record count. That last was **violated when the plan
was written** — "~700 official documents" appeared twice in agent-facing spec text, and is now gone. Note
airline's version matches exact count strings, so an approximate count would have slipped past a copied
version of it, and this one matches approximate forms too. Note also that under decision 1 the document count
is measurable by the agent anyway: the corpus index is served on the tool token by design. The honest line is
to describe what the corpus contains, never how much of it there is.

**What only the eye catches** — and it did catch two things: that the build share is missing a category the
checker cannot see (banking's product tiers and eligibility rules live largely in document prose, which it
cannot parse; and its 13 card product types and 15 spend categories are above the cardinality cap); that a
gap was accepted deliberately rather than by accident; that a record is case-bearing for a reason the schema
does not express, such as a case naming a customer in prose rather than as a value; that the store's default
path resolves to the eval copy; and that the generator still runs.

The two case-bearing records the eye found are the whole reason step 10 exists, so they are worth stating as
the worked example. `tau2_banking_34` opens "You are Ayan Wang, a Rho-Bank customer (user_id: 125)", and that
sentence is the only place `125` appears in the file — the derivation matches whole values, and it must, or
`123` and `125` start matching inside zip codes and incomes. `tau2_banking_10` is about Wei Chen, and card
application `f1a2b3c4d5e6f789` carries her name in a `customer_name` field and no customer id at all, so
closure has no edge to follow. Both are now seeded into the withheld set by hand, with the case id and the
reason recorded per record.

## Dangerous partial states, in rank order

1. **Phase declarations differ while the store is still baked into the image.** The carved files exist, the
   checker now sees a split and reports it clean, CI is green, the container is healthy — and the agent
   reads the full store anyway. Everything says finished; nothing measures anything. Both airline's and
   banking's notes single this out.
2. **The oracle points at the wrong copy.** The carve is real, the agent is properly starved, and every
   state-based score is zero because the replay base is the wrong file. Reads exactly like an artifact that
   answered wrongly.
3. **No post-load assertions.** The service boots empty, nothing loads, health passes, and the run returns a
   sheet of zeros with nothing to explain them. At least this prompts investigation.
4. **The service crashes on boot** (un-baked with no load route). Loud, harmless, fix it.

## Eval trials

An eval trial deploys an already-built artifact and scores it, with no agent and no build phase, so it must
start an environment itself; #136 fixed it to start the **eval** one. Now that banking is carved, its trial
scores are measured against the same shape airline's and order_to_cash's are, so they are comparable across
tasks — but a banking trial recorded before the carve is not comparable to one recorded after it. Two
preconditions banking has
that airline does not: the trial must recover the tool token from the eval environment's descriptor, because
the eval-time simulated customer calls customer-held tools (49 of 97 cases have a golden action whose
requestor is the customer); and per-case seeding must be reachable on the eval container, because 75 of 97
cases carry a starting state and scoring refuses a seeded case whose live environment was not seeded.

## Filed while surveying — not carve work

- **Airline's build-share generator is broken on `main`.** It still resolves the eval store at the path #137
  deleted, so it crashes — including in the check mode that exists so a reviewer can confirm the carved
  files still regenerate. Banking's generator must resolve the store through the same named constant the
  scoring code uses, or it rots identically.
- **The corpus shell sandbox's escape check is defeated by quoting**, and its read policy does not deny the
  container's own data directory. Latent today because the image installs no sandbox runtime — but it reads
  the *container filesystem*, not the bundle, so leaving the store baked while making that tool work would
  make any carve decorative. Un-baking the store closes it for the store; the corpus is shared, so reading
  it is not a leak.
- **The corpus is invisible to the checker's disjointness check** — a required document planted in a build
  share passes clean. Moot for banking under decision 1, but a live gap for any future task that carves a
  document corpus.
- **Four cases seed a behaviour switch that exists in no store**, so no build env can show the agent it
  exists. Unfixable without synthesis; note it and move on.
- **Two pre-existing oracle defects**, reproducible against the untouched full store: one case's golden
  calls omit a required argument, and one case's trajectory trips a duplicate-request guard. Not carve bugs.
