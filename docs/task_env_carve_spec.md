# Spec: carve a build share out of each task's data

> Scope: the work remaining after the build/eval environment **mechanism** lands
> (`docs/task_env_split.md`, branch `mpk/split-task-env`). The mechanism is context here:
> two sequential environments, the handover, and the per-phase spec shape are done and tested. Every
> task currently declares both phases loading the **same** dataset, so the leak this feature exists to
> close is still open. This spec closes it.
>

## Problem Statement

A task owner wants the benchmark to measure whether an agent can build a working system from an
underspecified customer ask. Today it measures something weaker, and the owner has no way to tell how
much weaker.

During the build phase the agent explores the customer's environment, and for any task with a queryable
data store it can read **every record** — through the task's own tool interface, or by writing a
database client, which is what the coding subagent actually does. The held-out eval cases are drawn from
those same records. So an agent can enumerate the data it is about to be scored on and shape its
artifact around those specific rows rather than around the requirements it elicited. A task owner
reading a score cannot separate "elicited the rules well" from "read the answers off the substrate".

The environment mechanism to prevent this now exists, but no task uses it: every task declares its build
and eval phases loading the same dataset. The owner has the machinery and no split to put in it.

## Solution

The operation is asymmetric, and worth stating precisely because "split" invites the wrong picture.
The **eval env** keeps the task's **full store**, unchanged. The **build env** — the only environment the
AppSci agent ever meets — gets a **share carved out of** that store, containing **no record any eval case
touches**. The build share is a *subset* of what the eval env holds, not its counterpart, and the eval
env does not exist until the build env has been torn down.

`airline_rebooking` goes first, because it is the only task with per-case environment reset and the only
one whose scoring oracle reads a fixture on the host, so it exercises every part of the mechanism. Its
carve is measurable rather than a matter of taste: 34 customers, 56 reservations and 118 flights are
touched by its 50 eval cases, and a referentially closed carve excluding all of them leaves 16
customers, 27 reservations and 182 flights — about 1% of the volume, with every category still
represented.

A single repo-wide guard then asserts, for any task that has actually split, that no identifier
appearing in an eval case appears in its build data. A task owner who mis-carves finds out from a
failing test rather than from a quietly meaningless score — and can find out earlier still by running
the guard directly while carving: `python scripts/carve_guard.py <task_id>` reports the same three
verdicts CI does, against whatever is on disk.

Because the build share is carved out of the original store and the eval env keeps all of it, the eval
env's dataset **is** the original store unchanged. So this change has a hard acceptance test: a given
artifact must score the same before and after.

## Implementation Details

This section is the procedure a task owner follows to split their task. It assumes the mechanism is in
place: the harness already brings the environment up twice and already reads a per-phase declaration.
What is missing is the data, and the steps below are how it gets there.

### The rule you are satisfying

**Withhold from the build environment every record that any eval case touches — the records a case names,
the records its golden actions change, and the records those in turn point at. No category is exempt: not
lookup tables, not timetables, not catalogues.**

"Touches" is wider than "mentions". An airline case names one customer and one booking, but its golden
actions also change the seat counts on every flight that booking covers — so those flight records are
touched too, and they go as well.

**Where this rule and the "aim for roughly 20% of records" guideline disagree, the rule wins** and the
build environment simply comes out smaller. On airline, obeying it strictly leaves about 1% of bookings.
Exempting timetables would have given a comfortable 20%, and that was measured and rejected, because
golden actions do change timetable records.

### The procedure

Ordered. Several steps only work in this order, and each one below says what going wrong there looks
like — because most of these failures produce a plausible wrong number rather than an error.

**1. Move your existing data into the eval phase's directory.**
It is already the full store, so this is a rename, not an authoring step. Doing it first means the eval
side is correct before anything else moves.
*If you skip it:* you end up hand-building a second copy of data you already have, and the two drift.

**2. Repoint anything that reads that data by default.**
Something almost certainly resolves a default path to where the data used to be. On airline it is the
environment package, which resolves its default relative to its own location, and the scoring oracle
reaches it by loading with no path argument. It must now resolve to the **eval** phase's copy.
*If you skip it:* the worst failure in this whole change. The oracle replays golden actions against
different data than the container loaded, so every database check fails — with no error, no warning, and
nothing in the score to say why.

**3. Generate the build share with a script, and check the script in.**
Deterministic from a seed, so a reviewer can re-run it and get your files back rather than taking a large
data diff on trust. Derive the withheld set **from the eval cases themselves**, not from a list you typed
out. Write the result into the build phase's directory, and have the script also report what it withheld
and which categories survived.
*If you skip it:* the carve is unreviewable, and it silently rots the first time someone adds an eval
case.

**4. Choose that share for coverage, not for size.**
At least one instance of every value of every enumerated field — every tier, class, status,
document type. A category whose governing rule is stakeholder-only may be withheld deliberately, to
stop an agent inferring from data what it should have asked for; record that with the task's carve. At a fifth of the data the proportions cannot match the real store, so do not try; what
matters is that the agent can see one of everything. The share must also be **closed**: every record it
references is also in it, or the agent reads dangling references and draws wrong conclusions about your
schema.
*If you skip it:* the agent builds against a world missing a category it will be scored on.

**5. Stop your image baking any data into itself.**
Both phases run from the same image, so anything baked in is present in the build-phase container no
matter what was bundled.
*If you skip it:* the build container carries the full store on its filesystem and the carve is
decorative.

**6. If your environment reads its data at start-up, change that first.** (See below — this is the one
step that is a code change rather than a data change, and step 5 crashes the service without it.)

**7. Write a setup script for each phase, ending in assertions that its data landed.**
The setup script makes that phase's data live, and its last lines check the records are actually there —
counts above zero for each collection you expect. Container health does not tell you this: a service can
answer while holding nothing, which airline's will by design. A failing setup script aborts the run,
which is what you want when the data is missing.
*If you skip the assertions:* an absent or partial load reads as success, and the run comes back all
zeros with nothing to explain it.

**8. Point each phase's declaration at its own directory, and delete the anchor.**
Every task's spec currently ties both phases to one dataset with a YAML anchor, deliberately, so the
not-yet-split state is visible. Splitting means giving each phase its own source and removing that
anchor.
*If you skip it:* your carve exists on disk while both phases still load the same thing. The change looks
finished and measures nothing.

**9. Run the checker against what you have built.**
`python scripts/carve_guard.py <task_id>`. It checks all three invariants — nothing leaked, nothing
dangling, every category present — against your files as they stand, and exits non-zero if any fails. Do
this from step 3 onwards, every time you regenerate, not once at the end: the checks are cheap and the
failure they catch is invisible at runtime. CI runs the same code, but by then you have already opened a
PR.
*A `NOT MEASURED` line is not a pass.* It means the checker could not read your data as records, so that
invariant is unverified — the same standing as a failure.
*A `WAIVED` line is not a pass either, but it is a decision.* Coverage — and only coverage — can be waived
in your spec when a strict carve cannot satisfy it; the waiver names the enumerations it accepts, the gaps
are still printed in full underneath, and they are what a reviewer reads. A gap the waiver does not name
fails. See "Coverage is waivable per task" below.

**10. Verify by score parity.**
Run the task end to end and compare its score with a pre-split run of the same artifact. Because the eval
environment holds the data it always held, the score should not move.
*If it moves:* you have a defect in the carve, not a finding about the agent.

Steps 5 and 6 are one-off per **environment type**, not per task. Once one relational task has done them,
the next relational task is steps 1, 2, 3, 4, 7, 8, 9, 10.

### Where the files go

Each phase's data and its scripts live under the task's `environment/` directory. Everything that
describes the *world* rather than the *data* stays shared, because a carve must not fork it.

```
tasks/<task_id>/environment/
  Dockerfile              shared  -- and bakes NO data
  docker-compose.yml      shared  -- same services, same names, both phases
  <service code>          shared
  schema/                 shared  -- table definitions / DDL
  build/
    data/...              the carved share
    setup.sh              makes it live, and asserts it landed
  eval/
    data/...              the task's full store
    setup.sh              makes it live, and asserts it landed
```

They sit under `environment/` rather than directly under the task folder because they are one half of the
environment definition and the shared half sits beside them. The payoff is that anyone reading the tree
can see which records each phase is *able* to load without reading a line of code.

### How your data reaches the container

Four steps, all but one of them shared machinery you do not write:

1. Your phase declaration names its source directories, relative to the task folder. **This is the only
   thing that decides which records can enter that phase's container.**
2. The harness packages those directories, copies the archive into the workspace container and extracts
   it.
3. Your phase's setup script runs, makes the data live, and asserts it landed.

Both phases come up under the same container name, so the artifact reaches the environment at the same
address either way and needs no re-pointing when the environment changes underneath it.

### If your environment reads its data at start-up

Airline is the example, and it is the awkward case. Its environment keeps records in memory inside the
service process and reads them from a file the moment the process boots. The harness copies a phase's
data in **after** the container is already running — so by the time the data arrives, the service has
already decided what it holds. That is why airline bakes its records into its image today, and why a
carve cannot work until this changes.

Two changes to the service:

- **Boot with nothing.** No file read at start-up.
- **Add a "load this file" command that only the harness can call.** Token-gated, on the same private
  channel the harness already uses to reset data between eval cases. The artifact must never reach it.

The phase's setup script calls that command once its data has landed.

One detail matters for scoring. The service keeps one untouched copy of whatever it loaded, and hands each
eval case a fresh duplicate to work on, so cases cannot contaminate each other. The load command
**replaces** that untouched copy rather than adding to it — and that is what makes the between-case reset
return to this phase's records instead of the previous phase's.

### Mistakes that produce a wrong number instead of an error

Collected because these are the ones no test will catch for you:

- **Pointing the oracle at the build share.** It is a subset, so every expected state gets computed from
  fewer records than the eval environment actually holds.
- **Leaving the anchor in place.** Both phases load one dataset, the repo-wide guard skips your task
  because it sees no split, and nothing reports that the work is unfinished.
- **Baking data into the image.** Present in both phases regardless of what you bundled.
- **Putting the full store, or your script's report of what was withheld, anywhere the agent can reach.**
  The build share is agent-facing by construction. Those two are not.

### Airline, concretely

Its 50 eval cases touch 34 customers, 56 bookings and 118 of 300 flights once you follow the bookings
through to the flights their golden actions change. A closed carve excluding all of them leaves **in the region of 20
customers, 35 bookings and 190 flights** — roughly 1% of the booking volume, with every membership tier,
cabin class, trip type and insurance value still represented. Small, but one of everything. The exact
figures depend on how the carve script resolves closure, so treat them as the scale rather than a target;
what matters is that the guard passes on the result.

Its eval side needs no authoring at all: the build share is carved out of the existing store and the eval
environment keeps that store unchanged, so there is no such thing as an "eval share" to write. That is
also what makes score parity a usable acceptance test.

## Testing Decisions

**One repo-wide guard. No per-task carve tests at all.** Every invariant a carve has to satisfy is
checkable generically, so a task owner writes data and scripts, never test code. This is the same
principle already settled for disjointness, extended to the rest of it — a task that splits inherits the
whole check by existing, and so does every task that splits later.

**What makes a good test here.** These assert externally observable properties of data: which records a
share contains, whether two sets intersect, whether a value appears. None should reach into how a carve
was computed. A carve produced by a different script must still pass.

### The three invariants the guard checks, and how each is made generic

- **Disjointness** — no record any eval case touches appears in the build share. Extract
  identifier-shaped tokens from the task's eval cases and search the build share for them. The heuristic
  must reject date-like and purely numeric tokens, which appear on both sides and would flag a correct
  carve.

  That last rule has a cost, and `banking_support` is where it came due: it keys customers `123`, `125`,
  `126` and bank accounts `01`–`08`, and accounts `05` and `07` are the argument of a golden action in
  three of its 97 cases — so a build share keeping a scored record passed a green line. A **second,
  structural pass** covers exactly the keys the token rule cannot recognise. It compares *positions*
  rather than spellings: a short key counts as used by a case when the case carries it under a field named
  after its collection, and as present in the build share when the build share keys a record by it or
  carries it under such a field. Free text never contributes on either side, which is what keeps it quiet
  on the `NY 11235`, `$1,123.65`, `125000` and `txn_28cb62125078` that a shorter token or a substring match
  would have flagged. Its own limits are recorded in the code: it cannot see a short key named only by a
  setup script, and it does not follow a reference the schema declines to name (order_to_cash keys
  contracts by supplier id, and reading `supplier_id` as a contract reference would flag a tenth of a
  correct build share).
- **Referential closure** — every record the build share references is also in it. For a relational task
  this comes free: the database rejects a load that violates its declared foreign keys, so a broken carve
  fails at load time rather than needing a test. For a document store, collect each collection's record
  keys, then assert that any value in the build share matching a key of some collection is present in
  that collection.

  A collection's keys come from two places, unioned, and both are needed. Where a file **keys its
  collections by their primary key**, that is a statement rather than an inference. Where it does not,
  the values of fields named after the collection are all there is. Airline's two sources agree exactly;
  banking's do not — name inference finds a key for 3 of its 17 collections (its debit cards are keyed
  `card_id`, its credit-card accounts `account_id`, its payments `payment_id`), so on that store the
  file's own keys are what closure runs on at all.
- **Category coverage** — every enumerated value appears at least once. Made generic by **inferring which
  fields are enumerations from cardinality**: a field whose distinct-value count across the full store is
  small is an enumeration; one with hundreds of values is an identifier, a date or free text. Measured on
  airline, a threshold in that region picks out exactly `membership`, `cabin`, `flight_type` and
  `insurance`, and ignores ids, timestamps and airport codes. It also picks up small integer counts like
  bags-per-booking, which is not strictly an enumeration but is worth covering anyway.
One rule was added after running the checks against airline's real store, because it was the only false
positive left: **a category is a label, not a quantity.** Bags-per-booking has nine values (0 to 8) and
low cardinality, so a rule based on cardinality alone treats it as a category — and a build share of
thirty-odd bookings can never hold all nine, so every correct carve would be flagged. Strings and flags
count; numbers do not. The cost is that a task encoding a category as an integer goes unchecked, which is
the safer error: flagging a correct carve is what gets a check switched off by the first person it
obstructs.

### Coverage is waivable per task; the other two are not

Coverage is the one invariant a *correct* carve can be unable to satisfy, so it is the one a task may
decline — in its own spec, under `task_runner.carve.coverage_waiver`, which the guard reads and reprints.
`banking_support` is why: the closed carve of its 768 records leaves 8, with 15 of its 17 collections
empty, so its coverage is zero and no choice of records changes that. Its waiver names 18 enumerations.

A waiver has two required parts: a `reason`, and `enumerations` — the specific gaps that reason covers, each
named `collection.field`.

Four properties make it a decision rather than an off switch:

- **A gap the waiver does not name still FAILS.** This is the load-bearing one. A waiver carrying only a
  reason accepts whatever the check happens to find, for as long as the block survives: the data moves, a
  field stops being covered by accident, and a reason written about a measured 25 gaps silently absorbs a
  26th nobody looked at. Naming them keeps the check alive on the same task that waived it.
- **A waived run reports every uncovered value, in full.** The verdict changes from FAILED to WAIVED and
  the exit status stops caring; the findings are printed unchanged, under the owner's reason. A waiver that
  shortened the report would leave a reviewer no better informed than a check nobody ran. A partly-waived
  run prints both blocks: the unnamed gaps as failures, the named ones as acknowledged findings.
- **A reason is required and has to be substantive** (the schema rejects `n/a`), and a waiver without a
  readable reason waives nothing — a half-written block leaves coverage hard rather than quietly off.
- **Disjointness and closure have no waiver, and the spec cannot express one.** `additionalProperties:
  false` on the carve block means naming either is a validation error, not a setting. Those are the threat
  bar and the schema-integrity bar; a carve that fails them is broken, not hard.

The cost of naming them is a list that churns with the task's data — banking's runs to 25 entries — and a
list that rots is worse than a reason that does not. That is paid for in the report rather than the schema.
An unnamed gap fails with the exact entries to add, sorted, in the same form the gap is reported in, so
regenerating the list is a copy-paste that diffs minimally. An entry that no longer matches a gap is
reported as dead weight — the build share covers it now, or the full store no longer enumerates it at all —
and is deliberately **not** a failure: a rename in a task's data should not turn a correct carve red. If a
dead entry was a typo for a gap that is still open, that gap is unnamed and fails on its own account, so
the mistake surfaces either way. The identifier itself is the most stable name available: it depends on the
schema, not the data, so a build share gaining rows or a full store gaining a value leaves every entry
pointing at the same thing — including a collection that is empty today and merely incomplete tomorrow.

It lives in the spec rather than beside the build share because the build share is agent-facing and a
waiver is a list of what was withheld — the same mistake this document already names under "Mistakes that
produce a wrong number". Spec redaction is an allowlist that drops `task_runner` wholesale, so nothing
declared there reaches the agent.

### One thing the guard reports without judging

**Schema documentation.** Some stores carry the task owner's field descriptions beside the records —
banking documents 15 of its 17 collections that way, and the agent exploring the build env reads them as
the schema. A carve script that copies the records and drops the prose therefore costs the agent real
information while leaving all three invariants green, so the guard prints a line saying so. It never fails:
whether the build share should carry the same prose, different prose, or none is an editorial call, and
failing a correct carve over an editorial call is what gets a check switched off.

### How the guard knows a task has split

By comparing the two phases' declared sources. A task whose phases still point at the same dataset has
not split, and is skipped — which is every task today.

**That makes vacant passing the main risk on day one**, and it is a known trap in this repo rather than a
hypothetical: the sim-only-docs guard file carries a docstring warning that most of its checks pass
vacantly because no task has adopted the feature, and it points at a companion file that plants each leak
in a synthetic task to prove the checks fire. This guard needs the same companion — a synthetic task whose
build share contains a record its eval case references must fail, one per identifier shape, plus
assertions that the guard stays quiet on a correct carve and on a not-yet-split task. Without that, a
green run is not evidence of anything.

Both heuristics — identifiers and cardinality — are tuned the same way: **quiet on airline's real carve,
firing on a planted leak.** If either cannot be made both quiet and sharp with one shared rule, escalate
rather than loosen it. A per-task pattern is the fallback and it is a decision to raise, not to take
quietly.

### What is genuinely per-task, and is not carve testing

Only a task's own **environment service**. Airline's has to start empty, replace rather than merge when it
is told to load, and restore the loaded records on a between-case reset — and its load command must be
refused without the harness credential. Those test that service's code, not its carve; they exist because
the service changed, and any task with its own service already has its own service tests. Likewise a
phase's setup script asserting its data landed is that script's business, and a failing one aborts the
phase rather than coming up empty and reporting itself fine.

### The one check that is not a test

**Reproducing a carve from its script.** Checked when reviewing the carve, not in the suite: running a
data generator inside the test run is slow, and "these files are what the script produces" is a property
of one commit rather than a standing invariant. A reviewer re-runs the script and confirms the diff is
empty.

**Score parity.** One full airline run, its score compared against a pre-split run of the same artifact.
Because the eval environment holds the records it always held, the score should not move; any movement is
a defect in the carve rather than a finding about the agent. This needs Docker, an agent container and a
live environment, so it is a recorded manual check with its numbers written down. It is also a one-off:
"the eval store is unchanged from before the split" is a migration property, not a standing invariant, so
it belongs here rather than in the suite.

### Prior art

`tests/appsci_bench/core/test_usersim_docs_repo_guards.py` is the model to copy — one file, parameterised
over every task, asserting information-placement invariants so a task owner cannot merge a declaration
that leaks, with a companion file proving the guards fire.
`test_every_task_agent_data_dir_is_clean` is the closest prior art for scanning a task's data directories
for content that must not be there.

## Out of Scope

- **Generating additional records.** Carve-out from existing data only.
- **Any check that runs during a run.** Closure and category coverage ARE enforced repo-wide, by
  `scripts/carve_guard.py` and the same code in CI — this section previously said they were airline-only,
  which contradicted the procedure above. What stays out of scope is checking at run time: a carve
  mistake has no runtime symptom, so that would be too late to help.
- **Distribution matching between the shares**, checked or otherwise.
- **Telling the agent that its environment will be replaced.** The non-disclosure decision stands, which
  is also why the artifact is redeployed against the eval environment rather than expected to reconnect.
- **Any build phase after the eval phase.** The sequential handover is only valid while phases run once,
  in order.
- **Changing the handover, the spec shape, or the redeploy** — all landed and tested.

## Further Notes

The mechanism's own risk register carries forward. The one worth restating: bringing a compose stack up
under an existing project name attaches to a running container rather than recreating it, so a build
environment that failed to come down would be silently reused and the eval data would load on top of the
build data with every surface reporting healthy. The mechanism detects this by comparing container
identity across the call, and a carve makes the consequence worse rather than better — the two shares
would merge in the wrong container. Anyone touching the handover should read that guard before changing
it.

A second-order effect of the strict rule deserves watching. A build share at roughly 1% of volume gives
the agent one of everything and many of nothing, which is fine for learning record shapes and poor for
noticing that a full-table scan per request will not survive contact with the real store. That failure
appears as a latency problem during scoring, attributed to the artifact. If it shows up, the fix is a
sizing decision, not a carve bug.

Finally: the guidance says verifying the carve is the task owner's responsibility, and the guard in this
spec is a safety net rather than a replacement for that. It checks the one invariant that can be checked
generically. An owner who satisfies the guard and violates closure or coverage still ships a bad carve,
and nothing will tell them.
