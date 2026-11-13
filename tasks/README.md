# Tasks

Each task is `tasks/<task_id>/` with:

| File / dir | What it is |
|------------|-----------|
| `spec.yaml` | the task spec (full description + environment + personas + evaluation_data). Schema: [`schema/task_spec.schema.json`](../schema/task_spec.schema.json); reference: [`docs/task_spec_reference.md`](../docs/task_spec_reference.md). |
| `RESOURCES.md` | where the customer's resources live inside the task's Docker environment. |
| `user_personas/*.yaml` | appsci-agent-user-sim personas (L1/L2/L3) that *provide* requirements to the agent under test; they gate how much is revealed. Schema: [`schema/user_persona.schema.json`](../schema/user_persona.schema.json). |
| `user_sim_docs/*` | optional. Documents mounted into the **user-sim** container only (at `/user_sim_docs`) — the agent has no path to them and must ask for what's in them. (The [no-user ablation arm](#the-one-exception-the-no-user-ablation-arm) is the one run where they go to the agent instead.) See [Documents only the user-sim can read](#documents-only-the-user-sim-can-read). |
| `environment/build/`, `environment/eval/` | each phase's data and the script that loads it. A run brings the environment up twice — the build env the agent explores, then the eval env the artifact is scored against — and **the build env must contain no record any eval case touches**. See [Two environments, one per phase](#two-environments-one-per-phase). |
| `elicitation_rubric.yaml` | the facts the agent had to obtain by **asking** the user-sim, with the true answers written out. Held-out answer-key material: top level of the task dir, never under `environment/`. Feeds the standalone **elicitation score**, never the task score. Schema: [`schema/elicitation_rubric.schema.json`](../schema/elicitation_rubric.schema.json). See [Ship an elicitation rubric](#ship-an-elicitation-rubric). |
| `eval_data/*.jsonl` | held-out eval samples ([`schema/dataset.schema.json`](../schema/dataset.schema.json)): `data_point_id` + a single/multi-turn input + optional `expected_output`/`eval_rubric`, scored by the spec's `evaluation_data.objectives`. `order_to_cash`, `slides_generator`, and `airline_rebooking` have real datasets in this format today; the other tasks have a spec-shape migration only (`eval.py` not authored yet). |

**Artifact scope (v1).** An *artifact* is anything the evaluator can **query** through a standard
interface (MCP / completions). v1 artifacts are **E2E agentic systems**. A produced file
(deck / report / chart) is in scope as an `output`-kind artifact; `documentation`-kind artifacts
(design notes, validation) are diagnostic only. **Out of scope for v1
(expandable later):** static one-off deliverables with no queryable system behind them — a raw
dataset, or a standalone model/chart produced from data (MLE-bench territory).

Copy [`_TEMPLATE/`](_TEMPLATE/) to add a task. Selection rationale + the plan: [`docs/OVERVIEW.md`](../docs/OVERVIEW.md).

Most tasks are still spec-only stubs. **`order_to_cash`**, **`slides_generator`**, and
**`airline_rebooking`** are fully worked (spec + personas + RESOURCES + a real `eval.py`) — use
`order_to_cash` to see what an eval looks like end to end.

## Documents only the user-sim can read

Some of what a task requires — a policy, eligibility rules, thresholds — should be something the agent
has to **ask about**, not something it can open and read. If you put a policy in a file the agent can
read, you are testing whether it can read a file. If you put it behind the user-sim, you are testing
whether it can find out what the customer actually needs.

### Where a document can go

There are two containers in a run: the **agent** container (the system under test) and the **user-sim**
container (the simulated stakeholder it talks to). A directory is mounted read-only into one of them,
never both:

| Put it here | Mounted into | Visible there as | Use it for |
|---|---|---|---|
| `environment/data/` | **agent** container | `/data` | sample data, examples |
| `environment/sop/` | **agent** container | `/sop` | requirements you are happy to hand over |
| `user_sim_docs/` | **user-sim** container | `/user_sim_docs` | requirements the agent must elicit |

(Under `--no-user-sim` the last row moves: `user_sim_docs/` is mounted into the **agent** container
at `/requirements/docs`, `user_personas/domain_knowledge/` at `/requirements/policies`, and no
user-sim runs. See [the no-user ablation arm](#the-one-exception-the-no-user-ablation-arm).)

Two consequences worth being precise about, because both are easy to get backwards:

- **The user-sim cannot read `/data` or `/sop`.** Those are mounted into the agent's container only. A
  persona that lists them in `known_resources` learns *that they exist* — name, type, description — and
  can point the agent at them. It does not see a single byte of their contents.
- **The agent has no path to `user_sim_docs/`.** Not mounted, not named in its spec, not listed in
  `RESOURCES.md`. Its only route to that material is asking the sim, and the content reaches the sim's
  prompt with no filename and no path attached (so it cannot point at a file the agent cannot open).

### How to use it

**1. Put the document in `user_sim_docs/`.**

```
tasks/my_task/
  user_sim_docs/
    policy.md
    common/
      tools_reference.json
```

**2. List it in the personas that should know it.** Paths are relative to `user_sim_docs/`:

```yaml
# tasks/my_task/user_personas/l1.yaml
known_docs:
  - policy.md
```

**3. That's it.** The persona now answers from that document as someone who has read and absorbed it.

### The `common/` folder

Anything in `user_sim_docs/common/` goes to **every** persona automatically — no need to list it. Use it
for the baseline facts without which the task is impossible at any level (e.g. what the tools are). Keep
the material that *should* differ by level (the policy, the thresholds, the success criteria) out of
`common/` and in each persona's `known_docs`, or your L1/L2/L3 personas stop differing.

### One rule: never name the file anywhere the agent can see

Not in `spec.yaml`, not in `RESOURCES.md`, not in a persona's `domain_knowledge` prose, not in a file
under `environment/`. Naming a file the agent cannot open sends it hunting for something that isn't
there, and it may then fall back on guessing.

### The one exception: the no-user ablation arm

`--no-user-sim` (sweep key `user_sim_enabled: false`) runs the benchmark with no stakeholder at all.
No user-sim container starts, `talk_to_user` is removed from the agent's tool registry and every
prompt surface, and what the stakeholder would have held is mounted read-only into the **agent**
container instead, in two halves:

| On disk | Mounted at | Holds |
|---|---|---|
| `user_sim_docs/` | `/requirements/docs` | documents a persona was handed whole |
| `user_personas/domain_knowledge/` | `/requirements/policies` | the per-level extracts of what a persona otherwise knows only inline |

It exists to measure what elicitation costs: the same task, the same facts, the same scoring,
differing only in whether the agent had to ask.

**Both halves matter, and a task with only the first is usually not ready.** A persona's knowledge is
its `known_docs` plus the `domain_knowledge` block written inline in its YAML, and nothing can mount
a YAML block. A task that keeps its decision rules there hands the agent an interface contract and no
rules, which deletes those requirements rather than un-eliciting them. Extract them to
`user_personas/domain_knowledge/<level>_domain_knowledge.md`, verbatim: a fact that is in the extract
but not in the persona is handed to one arm and withheld from the other, and the two stop being
scored on the same facts.

Nothing about how you author a task changes for it. Three things are worth knowing:

- **A task with neither tree cannot run this arm.** There would be nothing to hand over, and the run
  would measure a harder task rather than an ablation. The orchestrator refuses it before anything
  starts. Either tree alone is allowed; when the policy half is missing the run warns that any
  requirement living only in a persona's inline `domain_knowledge` is absent from it.
- **Nothing but Markdown belongs in `domain_knowledge/`.** The tree is mounted whole and read by the
  agent, so a persona YAML copied in for reference would publish the adversarial `mistaken_belief_*`
  plants and the withholding design. The orchestrator refuses a run whose extracts tree contains
  YAML.
- **It takes no persona at all.** Both trees are mounted whole, not one persona's share, so L1/L2/L3
  would be the same condition — and every level's policy extract is handed over at once, which is
  more than any single stakeholder would have said. So `--persona` is optional on the CLI (accepted
  and ignored, with a warning, if you pass one) and `personas:` in a `user_sim_enabled: false` sweep
  entry is refused at submit. The run is named `nouser` rather than claiming a level it never used.
  An adversarial persona would have no effect either — its pressures reach the agent only through
  the simulator — so this arm produces no red-team trace.
- **There is no elicitation score for it.** The diagnostic reports `no_stakeholder` with no
  rates at all, deliberately not the zero it reports for an agent that had a stakeholder and never
  asked. Compare artifact scores between the arms, not elicitation rates.

The rule above still holds for the task tree itself: `user_sim_docs/` must not be named in
`spec.yaml`, `RESOURCES.md`, a persona's prose, or anything under `environment/`. The arm changes
what is mounted at run time, not what the task says about itself.

So **don't** write:

```yaml
technical_docs:
  - {name: "policy.md", description: "the rules"}          # agent now knows a file it can't open
domain_knowledge:
  where: "the rules are in /sop/policy.md"                 # that path does not exist for the agent
```

**Do** convey that rules exist in ordinary prose, and let the agent ask:

```yaml
description: >
  "...the customer gets a valid outcome and we never break airline policy."
```

### How much to include

These documents sit in the sim's prompt on **every single turn**, so they cost tokens every time the
agent asks anything.

| | Per persona (its `known_docs` + `common/`) |
|---|---|
| Aim for | under 5,000 tokens (~20 KB) |
| Hard limit | 20,000 tokens (~80 KB) — above this the task will not run |

Include as little as the persona genuinely needs.

### Supported file types

Text only — the content is injected into a prompt, so it has to be readable as text.

| Extension | Typical use |
|---|---|
| `.md` | policies, rules, KPIs — the common case |
| `.txt` | plain notes |
| `.json` | structured references (e.g. a tool / action-space reference) |
| `.jsonl` | line-delimited records (e.g. labeled examples) |

**Anything else is rejected**, including `.pdf`, `.csv`, `.docx`, `.pptx`, `.xlsx`, and images. You get an
error naming the extension — the file is never silently skipped. `.csv` is the one that catches people
out: convert the table to a Markdown table, or save it as `.jsonl`.

(Dotfiles like `.DS_Store` inside `common/` are ignored rather than rejected, so a stray one cannot fail
your run.)

### If you get it wrong

You get a clear error and **that task alone** fails — other tasks in a batch keep running. Missing file,
unreadable file, wrong type, over the limit, or a leaked filename all fail loudly, before any container
starts. There is no silent-degradation mode: a sim that quietly lacks its policy is indistinguishable
from an agent that never asked, so we refuse to run instead.

Check your task before running it:

```bash
python scripts/preflight_usersim_docs.py my_task     # or --all
```

## Ship an elicitation rubric

Putting a requirement behind the user-sim (above) makes it *askable*. The rubric is what turns that into
a number: it lists the facts the agent had to obtain by asking, with the true answers written out, and the
**elicitation score** reports how many of them actually came back. It is a standalone diagnostic reported
next to the task score, never folded into it. Reasoning and full definitions:
[`docs/elicitation_score_design.md`](../docs/elicitation_score_design.md).

**You write it, because only you know which facts are load-bearing for your objectives.** The rubric is
part of defining the task, not part of scoring it afterwards. Copy the stub from
[`_TEMPLATE/elicitation_rubric.yaml`](_TEMPLATE/elicitation_rubric.yaml) and point at it from your spec:

```yaml
evaluation_data:
  dataset: tasks/my_task/eval_data
  elicitation_rubric: tasks/my_task/elicitation_rubric.yaml   # optional; absent -> reported as no_rubric
```

### Two questions decide whether something is an item

1. **Could a perfect artifact exist without this fact?** If yes, it does not belong. This is what keeps
   the denominator meaningful. Each item names the objectives it feeds, and the honest version of this
   question is asked against an objective your spec gives *weight*: one weighted at zero is still
   computed and reported, but its absence can no longer move the score. An item resting only on a
   zero-weight objective is not automatically wrong -- `airline_rebooking` has three, because it zeroed
   `policy_compliance` for comparability with published tau2 numbers while the policy stayed
   ask-only -- but it has to be declared in the task's rubric test with the reason, so nobody arrives
   there silently.
2. **Could the agent have read it in a file you mounted?** If yes, including it means every agent that
   reads efficiently loses a point for it.

The second question is where rubrics usually go wrong, because the same fact has a handed form and an
elicitable form and the phrasing decides which one you wrote. `order_to_cash` mounts a document listing
its fault categories informally in prose, while only the exact spellings sit with the sim: "knows the
categories exist" is handed, "knows all six exact spellings" is elicitable. Write the second.

### The six authoring rules

1. **Critical only** — see question 1 above.
2. **Phrased so no agent-facing file can satisfy it** — see question 2.
3. **Every item names at least one objective it feeds.** A fact no objective scores cannot have been
   critical, because a perfect artifact is perfect *as measured*. Enforced by the loader. Prefer an
   objective your spec actually weights — see question 1 — and where the only one available carries
   weight 0, declare the item in your rubric test rather than leaving it to look stronger than it is.
4. **At most fifty items.** A hard cap. Where your task's elicitable material overflows it, bundle
   related facts rather than dropping them.
5. **A bundled item states its own completeness.** "All four cancellation eligibility conditions", not
   "cancellation rules" — items are judged all-or-nothing, so the judge has to know what complete means.
6. **Written from the source, never from a conversation.** The inputs are your `user_sim_docs/`, your
   personas' `domain_knowledge`, what the agent can read, and your objectives. Reading a run's transcript
   while authoring steers inclusion both ways and turns the rubric into a description of one conversation.

### Check it

```bash
python scripts/score_elicitation.py --task my_task --run runs/<a finished run>
```

Add `tests/tasks/my_task/test_elicitation_rubric.py` with one line -- the shared helper checks the file
validates, the pointer is wired, every objective is real, the cap holds, and no item is satisfiable from
something you mounted:

```python
from tests.tasks.conftest import assert_rubric_is_sound

def test_the_rubric_is_sound():
    assert_rubric_is_sound("my_task")
```

## Task source types

**Internal** — authored by us on synthetic/curated data.

| id | name | what it tests |
|----|------|---------------|
| `order_to_cash` | Order-to-Cash Discrepancy Detector | extraction + detection under imbalance (worked example) |
| `customer_call_insights` | Customer-Call Insights | extraction + summarization |
| `compliance_review` | Compliance Review | RAG + rule adherence |
| `slides_generator` | Slides Generator | generation, objective-graded |

**Open-source** — derived from public agentic benchmarks. The eval data is produced **offline /
asynchronously** by converting upstream tasks into our `eval_data` format; `source` records
provenance. tau-bench is our first/best-fit source — **others (e.g. CRMArena) can be added**, and
the open-source set is not limited to any one benchmark. Each task's provenance file records how its data was converted.

| id | name | source |
|----|------|--------|
| `airline_rebooking` | Airline Policy-Compliant Rebooking | tau-bench (airline) |
| `banking_support` | Banking Customer Support | tau2-bench (banking knowledge) |
| `crm_insights` | CRM Analyst/Manager Insights | CRMArena |

## Two environments, one per phase

The agent explores your task environment during the build phase, and for any task with a queryable
data store it can read **every record** — the tool interface for one, a database client for another.
Your eval cases are drawn from those same records. So unless the two are separated, an agent can
enumerate the data it is about to be scored on and shape its artifact around those specific rows
instead of around the requirements it elicited.

So a run brings the environment up **twice, sequentially**:

| Phase | Holds | Who sees it |
|---|---|---|
| **build env** | roughly 20% of the records — and **nothing any eval case touches** | the AppSci agent, for the whole build phase |
| **eval env** | the task's **full store** — the build share is a subset of it, not a counterpart | only the artifact, during scoring |

Both come up from the same compose file under the same `workspace_name`, so they share a container
name and network alias — which is why the artifact needs no re-pointing when the environment changes
under it. Declare them in `spec.yaml` under `task_runner.environments`; the full shape is in
[`_TEMPLATE/spec.yaml`](_TEMPLATE/spec.yaml) and
[`docs/task_env_split.md`](../docs/task_env_split.md).

### Choosing the split

1. **Aim for roughly 20% of records in the build env** — but the rules below take precedence, and on a
   densely cross-referenced store they will hold it well under that. A build env that is 1% of the
   volume is a correct outcome, not a mistake.
2. **Choose the build share for category coverage, not proportion — a default, not an absolute.** Aim
   for at least one instance of every value of every enumerated field: every tier, every class, every
   document type. At a fifth of the data the proportions cannot match, and believing otherwise is how
   you end up thinking the build env is representative when it isn't.

   **A task may deliberately withhold a category to force elicitation.** Where the rule that governs a
   category is stakeholder-only, an example of it in the build env lets an agent infer from data what
   it was supposed to ask for. Withholding one is a legitimate difficulty choice — `order_to_cash`
   withholds four of its six fault types for exactly that reason. Two conditions: record the decision
   where the task's carve is documented, so a choice stays distinguishable from an oversight; and keep
   the rule reachable by asking, or the case becomes unanswerable rather than harder. Coverage of the
   fields the guard can actually see stays a hard requirement — an accidentally missing supplier or
   unit of measure is a bug, not a difficulty setting.

   **Withholding a category is not part of the carve, and should not be reported as one.** The carve
   answers a leakage question and the withheld category answers a difficulty question, so a build share
   held down by both has two numbers and only one of them is comparable to another task's. State them
   separately: `order_to_cash`'s build share is 60% of its store, but 91% on the threat bar alone, and
   the second figure is the one to put beside a task that carves to 1%.

   **Prefer withholding a whole family over half of one.** Two categories governed by the same
   stakeholder rule leak that rule through whichever one stays visible; `order_to_cash` withheld the
   off-contract *fee* fault while leaving the off-contract *item* fault in place, and the build share
   went on showing the agent that supplier contracts govern what may be invoiced. Withhold both, or
   accept that the rule is only half hidden.

   **Stop before the categories an agent could derive unaided.** A withheld category an agent could
   have worked out from the data in front of it no longer measures elicitation — a run that misses it
   might have failed to ask, or might simply have failed to compute, and the score cannot tell you
   which. `order_to_cash` keeps its two directly-comparable fault types visible for that reason. It is
   also worth checking what the withholding does to the build share's distribution: withholding only
   positive categories biases the share towards clean records, and an agent that tunes any rate against
   it will carry that bias into eval.

   **And a task whose strict carve cannot cover anything may waive coverage — see
   [Waiving coverage](#waiving-coverage).** That is a different situation from the paragraph above: not
   one category held back on purpose, but a build share so small that whole collections are empty and
   no choice of records changes it.
3. **The carve must be referentially closed** — every record the build share references is also in the
   build share, or the agent reads dangling references and draws wrong conclusions about your schema.
   Nothing is exempt, including reference data like catalogues and timetables.
4. **Identifier collisions are impossible while you carve rather than generate.** Your build share is a
   subset of the full store, so it cannot collide with it. This only becomes a rule to enforce if
   generated records are ever allowed into the build share.
5. **You do not author an eval dataset.** The eval env keeps the task's full store, so that side is a
   file move. It has to be one unambiguous whole because a state-based scoring oracle replays against it.
6. **Verifying that no eval-case record leaked into the build share is your job** — but you do not have
   to do it by eye. Run `python scripts/carve_guard.py <task_id>` and it checks all three invariants
   (nothing leaked, nothing dangling, every category present) against the files as they stand. Do it
   while you are still carving: this is the one failure with no runtime symptom, so left unchecked the
   run looks completely normal and the score is quietly meaningless. The same checks run in CI, but CI
   is a late place to find out. See [Checking your carve](#checking-your-carve).

### Checking your carve

```bash
python scripts/carve_guard.py order_to_cash   # one task
python scripts/carve_guard.py                 # every task
```

Exit status is 0 when every check passed and 1 when any failed, so it drops into a pre-commit hook or a
loop while you iterate.

It reports one line per invariant:

| Line | Meaning |
|---|---|
| `+ disjointness: ok` | no identifier any eval case uses appears in the build share |
| `+ closure: ok` | every record the build share points at is also in the build share |
| `+ coverage: ok` | every value the full store enumerates appears at least once in the build share |
| `- <name>: FAILED` | with the offending file and identifiers listed |
| `~ <name>: NOT MEASURED` | **not a pass** — the checker could not read your data, so that invariant is unverified |
| `! coverage: WAIVED` | you declared a waiver naming these gaps; every one is still listed underneath it. See below |
| `! schema documentation: reported` | your build share dropped field documentation your full store carries. Never a failure |
| `not split` | both phases still load the same data, so there is nothing to check yet |

Treat `NOT MEASURED` as a failure: it means the carve might be wrong and nothing can tell. The usual
cause is data in a format the checker cannot parse as records (it reads `.json`, `.jsonl` and `.csv`).

The checker is generic — it infers your identifiers, references and categories from your own data, so
you write data and scripts, never test code. Its logic lives in
[`scripts/carve_guard.py`](../scripts/carve_guard.py) and CI runs the same code through
`tests/appsci_bench/core/test_carve_guards.py`.

### Waiving coverage

Coverage is the one invariant a correct carve can be unable to satisfy. On a densely cross-referenced
store, withholding every record an eval case touches and then closing over the reference graph can empty
whole collections — and a collection with no rows covers none of its categories, no matter which records
you pick. `banking_support` is the measured case: its 97 eval cases reach 760 of its 768 records once the
records they name only in prose are added and the graph is closed, leaving 6 customers and 2 bank accounts.
Eight records, 15 of its 17 collections empty, and a waiver that ran to 18 entries.

**That task has since added authored synthetic records on top of the carve** — records that hang off the
customers the carve left and reuse no id the eval store owns — which filled every empty collection and cut
its waiver to 8. Worth knowing before you copy the pattern: a waiver is the right answer when the gap is
structural, and authored data is the right answer when the gap is that the build environment cannot support
the work. Banking needed both, in that order. See `tasks/banking_support/environment/build/README.md`.
The 8 that remain are the gaps authored data *cannot* close, because covering them would mean copying the
eval customers' own identities into the build store.

So coverage — and **only** coverage — can be waived, in the task's own spec. A waiver has two parts: why
the gaps are acceptable, and **which gaps** that reason covers.

```yaml
task_runner:
  carve:
    coverage_waiver:
      reason: >
        Strict carve, no synthesized records: withholding every record an eval case touches, closed over
        the reference graph, leaves 8 of 768 records — 15 of 17 collections empty — so no build share
        that adds nothing can hold one of every category.
      enumerations:          # one per gap, copied from the guard's own output
        - accounts.class
        - accounts.level
        - bank_account_transaction_history.type
        - debit_cards.status
        - referrals.referral_status
        # ... and so on, for every gap the reason above accounts for
```

Five things to know about it:

- **The reason is the point.** It is required, and it has to be long enough to be a sentence; the schema
  rejects `n/a`. A waiver with no readable reason waives nothing, so a half-written block leaves coverage
  hard rather than quietly switching it off.
- **A gap you do not name still fails.** That is what keeps a waiver a record of a decision rather than an
  off switch: your reason was written about the gaps you measured, and a gap that turns up next quarter
  because the data moved is not one of them. You do not have to work the names out — the failure prints
  exactly the lines to add, sorted, in the same `collection.field` form it reports the gap in, so
  regenerating the list is a copy-paste and diffs minimally against the old one.
- **A waived run still reports every gap, in full.** The line becomes `! WAIVED` instead of `- FAILED`
  and the exit status stops caring, but not one uncovered value is dropped from the output. The value of
  a waiver is that the gap is acknowledged; a waiver that also hid it would leave a reviewer no better
  informed than a check nobody ran.
- **Disjointness and closure cannot be waived**, and the spec has no vocabulary for trying — those are the
  threat bar and the schema-integrity bar, and a carve that fails either is broken rather than hard.
- **Entries go stale, and the guard says so instead of failing.** An entry whose gap has closed, or that
  names something the full store no longer enumerates at all (a typo, a renamed field, a field that has
  grown past the cardinality line), is reported as no longer doing anything — remove it. It is not a
  failure: dead weight in a list this churn-prone should cost you a line in the report, not a red build on
  a carve that is correct. When every entry is dead the guard says to remove the whole waiver, because
  leaving one in place is how the next real coverage gap goes unreported.

It lives in the spec rather than in a file beside your build share for a reason worth repeating: the build
share is agent-facing by construction, and a waiver is a list of what you withheld. Spec redaction drops
the whole `task_runner` block before the agent sees anything, so a waiver declared there cannot leak.

### If your eval phase needs no environment

Write `eval: none`. That is the right answer when every case carries its own inputs and nothing in the
eval path touches a container or the network — `slides_generator` is the example: its cases ship the
previous deck, the template, the notes and the metrics as attachments, and its objectives read produced
files. Declare it explicitly rather than omitting the block, so an author who *decided* stays
distinguishable from one who forgot.

### Assert your data actually loaded

End your phase's setup script with checks that the records are there — counts above zero for each
collection you expect. The container's health gate only proves the service *answers*, which stops meaning
anything once a service can be healthy while holding nothing (airline's starts empty by design, so it can
be fed per-phase data). A load that half-applied, or never ran, otherwise shows up only as a run of zeros
with nothing to explain it.

Put them in the setup script rather than a separate check: a failing setup script aborts the run, and
aborting is the right outcome when the data is missing — there is nothing worth measuring.
