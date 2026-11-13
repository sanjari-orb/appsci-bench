# Splitting the task environment: a build env and an eval env

> **Status: approved, landed; per-task carves in progress.** Branch `mpk/split-task-env`.
>
> The **harness mechanism** (stages 1–5 of the plan) is approved and is what this change implements:
> two sequential environments, the handover, and the per-phase spec shape. Every task is migrated to
> the new shape.
>
> **Two tasks' data is now genuinely split**, so the sentence this note used to carry — "no task's
> data is split yet, so behaviour is unchanged and eval scores must not move" — no longer holds, and
> it was the belief under which scoring a *trial* against the build env looked harmless:
>
> | Task | Build phase | Eval phase |
> |---|---|---|
> | `order_to_cash` (#133) | 662 invoice rows | 1,095 |
> | `airline_rebooking` (#137) | 10 reservations | 2000 |
>
> "Eval scores must not move" is scoped to **build+eval runs**, which still load the eval store for
> scoring exactly as before. An **eval trial's** scores DO move on a split task, by design: before
> #136 a trial stood the *build* env up and scored against it. One airline trial, same artifact and
> case, scored 0.0 on the build share and 1.0 on the eval store. **Trial scores recorded against
> either task before #136 are invalid.**

## The problem

A run has one task environment, started before the build phase and torn down after the eval phase.
The AppSci agent explores it during the build phase — and for every task with a queryable data store
it can read every record. For airline that is the tool interface; for the relational tasks the coding
subagent writes a client and selects whatever it likes.

The held-out eval cases are drawn from those same records. So an agent that wanted to could enumerate
the data it is about to be scored on and shape its artifact around the specific rows rather than
around the requirements it elicited. Nothing in the harness prevents it, and nothing detects it.

That is eval-data leakage of a particular kind: the *answers* are not reachable, but the *inputs*
are, and the inputs are enough to overfit to.

## Decisions

| | Decision |
|---|---|
| **Threat bar** | The build env must contain no record any eval case touches. Fault patterns, category mixes and distributions may appear in the build env — only the specific records are withheld |
| **Topology** | Two environments, run **sequentially**. The build env serves the build phase and is torn down; the eval env is started for the eval phase only |
| **Container identity** | Both phases come up under the same declared name, so they share a container name and network alias |
| **Merge** | The eval env holds the task's **full store**. The build env holds a share carved out of it, containing no record any eval case touches — a subset, not a counterpart |
| **Container lifetimes** | The AppSci agent's container is halted before the eval env starts. The artifact's container survives the handover and is redeployed against the eval env. **In an eval trial there is no handover and no agent**: the trial starts the eval env itself and deploys the artifact onto it (see "An eval trial" below) |
| **Pre-eval access** | The artifact never touches the eval env before scoring begins. It does not need to: the schema, tool surface and knowledge-base layout are identical across phases |
| **Fidelity** | Identical schema, asserted at start-up. Data volume and distribution deliberately differ |
| **Split authorship** | Task owners author the **build share** explicitly; the eval env keeps the task's full store, so there is nothing to author on that side. Never computed at run time |
| **Split guideline** | Aim for roughly 20% of records in the build env, chosen for **category coverage** rather than matched proportions — but referential closure and the threat bar take precedence, and on a densely cross-referenced store they will hold the build env well under 20% (see below) |
| **Split method** | Carve-out from existing data only. Generating additional records is out of scope for now |
| **Verification** | The task owner's, and done **two ways**: run `scripts/carve_guard.py` (CI runs the same checks) *and* read the carve over by eye. The checker infers identifiers, references and categories from the data, so it cannot see a dependency your schema does not express |
| **Handover failure** | Logged, run continues |
| **Disclosure to the agent** | Unchanged. Stakeholder knowledge carries rules, policy and tool schemas, not data-specific detail |
| **Mandatory?** | Yes, except for tasks whose eval phase needs no environment at all |

## Vocabulary

**Build env** — the task environment that exists during the build phase. Holds the carved share, and
nothing else. The only environment the AppSci agent ever meets.

**Build share** — the records the build env holds: a subset carved out of the task's full store,
containing none that an eval case touches.

**Eval env** — the task environment that exists during the eval phase. Holds the task's **full store**,
the build share included. The only environment the artifact is scored against.

Avoid "eval share": there is no such thing. The two sides are not symmetric — one share is carved out
for the build phase, and the eval phase keeps everything.

**Case-bearing record** — a record an eval case is *about*, or that its golden actions mutate. These
are what the carve withholds.

**Reference record** — a record that describes the world rather than a case: a timetable, a supplier
list, a materials catalogue, a price book. **Not exempt from the carve.** If an eval case's golden
actions mutate it, it is withheld like any other record it touches.

Avoid: "playground", "production", "dev", "prod", "substrate", "scoring environment".

## The handover

Today the environment starts once and the agent's container is removed in cleanup, after scoring.
Both change. The new sequence, after the agent submits its artifact:

1. Snapshot the agent's filesystem and artifact directory to the host (unchanged; works on a halted
   container, which is why it comes first).
2. **Halt** the agent's container. Removal stays in cleanup, so the snapshots and log retrieval that
   follow still have a container to read.
3. Stop the user-sim (unchanged, already happens here).
4. Tear down the build env.
5. Start the eval env, under the same declared name.
6. Redeploy the artifact. This re-establishes every connection it holds, whatever pooling its author
   chose, and it reports a fresh endpoint — the published host port is ephemeral and is reassigned on
   each start, so the endpoint must be re-read rather than reused.
7. Health-gate the artifact, then score.

Steps 2 and 4 are ordered deliberately: the agent must be inert *before* the eval env exists, not
merely idle. Its own loop has ended by then, but a background process left behind by the coding
subagent would otherwise reach the eval env at the exact address it spent the build phase using.

### Why sequential beats swapping data inside one container

An earlier version of this design kept one container and reloaded its data between phases. Two
container instances is better for a reason beyond isolation: the agent *writes* to the environment
throughout the build phase — booking test reservations, inserting rows. With an in-place reload you
would have to demonstrate that none of those writes survived into the scored state, because airline's
database check hashes the entire store. A fresh container makes the scored state pristine by
construction.

The cost is that this design is only valid while the phases are strictly sequential. **If we ever
want build → score → build again** — an optimization loop that learns from its own scores — the
handover above is invalid and both environments have to coexist. Recorded here as a known boundary,
not a plan.

## An eval trial: the second, earlier eval start

The handover above is not the only place an eval env comes up, and reading it as though it were is
what produced the defect #136 fixed.

A `trials: k` cell of a replicate sweep is **one eval trial**: it deploys an artifact some earlier
build already produced and scores it, with no agent, no user-sim and no submission server. It has no
build phase, so there is nothing to hand over *from* — and it must still come up on the store the eval
cases are drawn from. Its sequence:

1. Record a teardown descriptor (before anything starts, so a start that throws part-way through is
   still torn down — the same reason step 4 of the handover records one).
2. **Start the eval env.** Not the build env. Scoring against the build share measures the artifact on
   the records the eval cases are deliberately *not* drawn from, and every surface reports healthy.
3. Build and run the artifact container, onto the task's declared network. For a task whose eval phase
   declares `none`, nothing came up at step 2 and so nothing created that network — the trial creates
   it itself (`TaskRunner.ensure_network`).
4. Health-gate the artifact, then score.

The environment comes up *before* the artifact does, for the same reason the handover redeploys at its
step 6: the artifact establishes its connections at start-up, so a store that appears afterwards leaves
it holding dead sockets.

No agent exists on this path at any point, so the halt-before-start ordering the handover needs has
nothing to order here. That is a property of the trial branch returning before any agent is
constructed, and it is asserted both from the source and by driving a run.

### Why a trial gets a fresh environment rather than the build's live one

It is what makes the k trials of one artifact exchangeable, which is the property pass^k's estimator
assumes. Scoring a build's own still-running container would make trial 0 different in kind from
trials 1..k-1: it would run against an environment an agent had been working in for hours.

## The standalone environment CLI has no default phase

`eval_artifact up` brings one environment up and leaves it running, and it serves two workflows with
opposite needs: a **human baseline** attempt authors against the build environment (which is all an
agent under test ever sees), while an **operator re-scoring** an artifact needs the eval one. Both
mistakes are silent — standing up the eval environment to author against hands a person the scored data
and inflates the baseline agent scores are compared to; standing up the build environment to score
against measures the artifact on a store its cases were carved out of, yielding a plausible and entirely
wrong number.

So the phase is **required**, not defaulted, and the two directions are guarded separately: `up` refuses to
run without a phase, and scoring refuses to attach to a build-phase environment. The human-baseline kit
asks for `build` explicitly.

## Data split rules for task owners

1. **The build env gets roughly 20% of case-bearing records; the eval env gets all of them.** The
   withheld 80% is what the eval cases are drawn from.
2. **Choose the 20% for category coverage, not for proportion — a default, not an absolute.** At least
   one instance of every value of every enumerated field: every membership tier, every cabin class,
   every document class. At a fifth of the data the proportions cannot match, and pretending otherwise
   is how a task owner ends up believing the build env is representative when it isn't. A task may
   deliberately withhold a category whose governing rule is stakeholder-only, to stop an agent
   inferring from data what it should have asked for; that decision is recorded with the task's carve,
   and coverage of the fields the guard checks stays hard. See `tasks/README.md`, "Choosing the
   split".

   **Coverage — and only coverage — is waivable, per task, with a recorded reason.** On a densely
   cross-referenced store the strict rule can empty whole collections, and an empty collection covers
   none of its categories however the records are chosen. `banking_support` is the measured case: the
   closed carve of its 768 records leaves **8**, with 15 of its 17 collections empty, so its coverage
   is zero and cannot be otherwise. A task in that position declares
   `task_runner.carve.coverage_waiver` in its spec — a reason, **and the specific enumerations that
   reason accepts**, named as the guard reports them. The guard then reports every uncovered value **in
   full** while not failing: an acknowledged gap, never a hidden one. A gap the waiver does not name
   still fails, which is what stops a waiver ageing into a general off switch on the one task that has
   one. Disjointness and closure are the threat bar and the schema-integrity bar and stay mandatory; the
   spec has no vocabulary for waiving either. See `tasks/README.md`, "Waiving coverage".
3. **The carve must be referentially closed.** Every record the build share references must also be in
   the build share, or the build env ships dangling references and the agent draws wrong conclusions
   about the schema. No record type is exempt — including reference data.
4. **Identifiers stay unique automatically — while the carve is carve-out.** A share taken out of the
   full store cannot collide with it, so there is nothing to enforce today. The rule only acquires teeth
   if generated records are ever allowed into the build share, since those would have to coexist with
   the originals in the eval env. Recorded so that whoever allows generation knows it becomes real.
5. **The eval env's dataset is authored as a single pre-merged file, not assembled at run time.** It
   is what the scoring oracle replays against, so it must be one unambiguous thing.
6. **Verifying that no eval-case record leaked into the build share is the task owner's job — done
   two ways, not one.** Run `python scripts/carve_guard.py <task_id>` — it checks all three invariants and CI runs the same code. Then read the result over yourself: the checker infers what counts as an identifier, a reference and a category from the data alone, so it cannot know which of your records a case depends on for a reason your schema does not express. Both steps, not either.

### Decided: no exemption for reference data, and closure beats the 20% target

Measured on airline:

| Carve rule | Build env size | Category coverage |
|---|---|---|
| Strict — withhold every record any eval case touches, flights included | 16 customers, 27 reservations, 182 flights (**1% of reservations**) | complete |
| Reference-exempt — flight timetables shared, customers and reservations carved | 100 customers, 401 reservations, 300 flights (**20% of reservations**) | complete |

Flights are dense: 56 eval reservations pull in 118 of the 300 flights, and customers own
reservations spread across the whole flight graph, so a referentially closed carve that also excludes
those flights collapses to 27 reservations. Strictly, golden actions *do* mutate flight records —
booking changes per-date seat availability — so under the letter of the threat bar flights are
case-bearing.

**Decision: the strict rule.** Nothing is exempt. Where the threat bar and the 20% target conflict,
the threat bar wins and the build env simply comes out smaller.

Two consequences to carry into the per-task carves:

- **The 20% guideline is an aspiration, not a target to hit.** On a store whose records
  cross-reference densely, a referentially closed carve that also excludes everything an eval case
  touches will land far below it. Airline lands near 1% of reservations. A task owner who forces the
  build env up to 20% has necessarily broken closure or admitted a touched record.
- **A small build env is still a usable one, and coverage is the property to check.** Airline's strict
  carve retains every category: all three membership tiers, all three cabin classes, both trip types,
  both insurance values. The agent sees one of everything, just not many of anything. That is what
  makes the strict rule affordable — and it is why rule 2 is about coverage rather than volume.

## Spec and directory shape

Per-phase data and its load step live under the task's environment directory; everything that
describes the *world* rather than the *data* stays shared.

```
tasks/<id>/environment/
  Dockerfile              shared — no data baked in
  docker-compose.yml      shared — same services, same names, both phases
  <service code>          shared
  schema/                 shared — table definitions, DDL
  build/
    data/…                the ~20% share
    setup.sh              loads it, and asserts it landed
  eval/
    data/…                the pre-merged whole
    setup.sh              loads it, and asserts it landed
```

In the spec, the existing single bundle-and-setup declaration becomes one per phase:

```yaml
task_runner:
  environments:
    build: { bundle: [...], setup_script: /scripts/setup.sh }
    eval:  { bundle: [...], setup_script: /scripts/setup.sh }
```

`eval: none` is the explicit declaration for a task whose eval phase needs no environment —
slides_generator, whose eval inputs travel as attachments on the case itself. Explicit rather than
absent, because the two-phase declaration is mandatory and a silently missing block reads the same as
an author who forgot.

**No data may be baked into the environment image.** Airline currently copies its store into the
image at build time, which would put the eval records inside the build-phase container. Both phases
run from the same image, so the data has to arrive per phase and the service has to start empty.

### Asserting the data actually landed

The container's health gate only proves the service answers, and that stops being evidence of anything
once a service can be healthy while holding nothing -- which airline's will be by design, since it has to
start empty to be fed per-phase data. A load that half-applied or never ran otherwise surfaces only as a
full sheet of zeros with nothing to explain them.

So a phase's setup script ends with assertions that its records are there: counts above zero for each
collection it expects. Not a content hash -- the datasets are fixed per phase, so there is nothing to
detect drifting. This deliberately lives in the setup script rather than in a check of its own: a failing
setup script aborts the run, and aborting is right here, because there is nothing worth measuring against
missing data. It is also one less concept for a task owner to learn.

## What the scoring oracle reads

Airline's database check computes the expected state **on the host**, by replaying each case's golden
actions against a fresh copy of the store, and compares that to the hash the container reports. Both
hashes are computed by us at scoring time from our own canonical serialization; nothing is inherited
from upstream and no expected hash is stored anywhere. What came from upstream is the golden action
list per case and the store itself.

So the oracle has to replay against **exactly what the eval env loads** — the pre-merged file under
`environment/eval/`. Point it anywhere else and every database check fails silently.

One consequence worth stating: because merge means the eval env holds the build share too, and
because the build share is a carve-out of the original store, **the eval env's dataset is the original
store, unchanged**. Airline's eval fixture needs no authoring, its 50 cases and their golden hashes
are untouched, and a given artifact should score identically before and after this change. The only
new data artifact is the build env's carved share. That makes the change independently verifiable:
if eval scores move, something is wrong.

**Scoped to build+eval runs.** An **eval trial's** scores on a split task *do* move, and moved by
design when #136 landed: a trial used to bring the build env up and score against it, so on airline
one trial of one artifact went 0.0 → 1.0 with nothing else changed. Read "scores must not move" as a
check on the carve, not on the trial path.

## Per-task position

| Task | Position |
|---|---|
| `airline_rebooking` | **Carve landed (#137), and it is wired in** — each phase bundles its own store to `/fixture/` and one shared setup script loads whatever it finds there. Build: 10 reservations, 10 flights, 5 users. Eval: 2000 / 300 / 500. (An earlier version of this row projected 16 customers / 27 reservations / 182 flights under the strict rule and described the carve as future work; the authored seed came out smaller.) Only task with per-case reset and the only one coupled to a host-side oracle |
| `compliance_review` | **Done.** Already disjoint — 50 fixture contracts, 50 eval-case contracts, zero overlap; eval-time contract text is attached per sample, never read from `contracts_db`, so there was no build share to carve. Layout applied: `build:`/`eval:` declared as two literal blocks |
| `customer_call_insights` | **Done.** Already disjoint — the `calls` table only ever holds the `seed-*` fixture; eval calls arrive as a file attachment and never enter it, so there is no build share to carve. Layout applied: `build:`/`eval:` declared as two literal blocks |
| `slides_generator` | Needs no eval env. Declares `eval: none` |
| `order_to_cash` | **Carve landed (#133), and it is wired in.** Build: 662 invoices and 662 PO lines; eval: 1,095 of each. The 10 contracts are shared whole with both phases. An earlier version of this row projected ~1,030 rows and described the carve as still to do; the disputed-invoice closure took more out than the eval cases named directly. Note that only 101 of the 433 withheld rows are the carve — the other 354 are a separate difficulty decision that withholds four fault categories from the build phase, so on the threat bar alone this build share would be 994 rows (91%). See its `KNOWN_ISSUES.md` §9–§10 |
| `banking_support` | **Carve landed, and it is wired in.** Build: 6 customers and 2 bank accounts — **8 records**; eval: the full 768, which it must keep because 34 of the 97 cases depend on base-store records their own starting state does not seed. The 698-document knowledge base is **not** carved: it is shared whole and stays baked into the image, because 46 of the 48 specialized tools are named only by documents the eval cases require. Two records are withheld by eye rather than by derivation — a customer an eval case names in prose (`user_id: 125` inside an English sentence) and an application linked to a case only by its customer's name — so the build share holds no card and no application of any kind. The **only task with a coverage waiver**: 15 of its 17 collections are empty, so its spec names 18 accepted enumerations under `task_runner.carve.coverage_waiver`. Three things had to be fixed in the checker before any of this could be measured — its store is a mapping of collection to `{data, notes}` and parsed as one pseudo-collection of 17 wrappers, so closure and coverage both reported NOT MEASURED; 15 of its record keys (customers `123`/`125`/`126`, bank accounts `01`–`08`) are too short for the identifier heuristic to see; and its day-first dates were inferred as categories, which put 7 unfixable date gaps into the coverage report |
| the other five | No environment authored yet; inherit the shape |

## Non-goals

- No check that runs *during a run*. The repo-wide checks (`scripts/carve_guard.py`, and the same code in CI) run against the files while you author, which is the point — a carve mistake has no runtime symptom, so finding it at run time would already be too late.
- No generated build data. Carve-out only.
- No distribution matching, checked or otherwise.
- No build phase after the eval phase (see the boundary noted above).
- No change to what the user-sim discloses.
