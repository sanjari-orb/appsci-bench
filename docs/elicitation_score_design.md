# Elicitation score design

**Status: built and running.** This document defines the metric, the per-task rubric that grounds it,
and where it runs. It is the reference for anyone authoring a rubric or reading a score. Seven tasks
ship rubrics — `airline_rebooking`, `banking_support`, `compliance_review`, `crm_insights`,
`customer_call_insights`, `order_to_cash`, `slides_generator` — and every other task reports
`no_rubric`, which is a status rather than a gap.

## What it measures

The benchmark scores the process of building an agent from a vague requirement. The requirements that
matter are deliberately out of the agent's reach — they are held by the build-time customer simulator
and the agent's only route to them is asking. The **elicitation score** measures whether the agent
took that route: of the facts it had to obtain by asking, what share did it actually get?

This is a *process* measure, deliberately separate from outcome. The task's objectives already score
the artifact the agent produced. Scoring elicitation by the same standard would re-measure outcome
under a new name. Keeping the two apart is what makes the number useful: it distinguishes an artifact
that is correct *because the agent asked* from one that is correct because the agent guessed, or
already knew the domain from its training data.

**It is a standalone diagnostic.** It is reported next to the task score, never folded into it. A
weighted average of "did the artifact work" and "did the agent ask" would be a number that answers
neither question.

## Vocabulary

| Term | Meaning |
|---|---|
| **Rubric** | The per-task list of facts the agent had to elicit. One file per task. |
| **Item** | One entry in the rubric: a fact, or a tightly-related bundle of facts, judged as a unit. |
| **Critical** | The inclusion test: without this fact, the agent could not have built the perfect artifact. |
| **Elicited** | The agent asked about the item, and the simulator's answer conveyed the true fact. |
| **Asked but not elicited** | The agent asked, and the true fact did not come back — the simulator declined, did not know, answered wrongly, or answered around it. |
| **Missed** | The agent never asked about the item at all. |

The three verdicts partition the rubric, so the three rates sum to one. An item the judge failed to
score at all is not a fourth verdict competing with them — it is **excluded from the denominator**,
counted, and reported separately. That is deliberate: an unjudged item says nothing about whether the
agent asked, so leaving it in the denominator would charge the agent for our own instrument failing. It
is the same treatment an eval function's "not applicable" already gets from the aggregator. The
consequence is that the denominator varies between runs of the same task, so the judged count is
reported wherever the rate is.

The headline number is the elicited rate. The other two are the decomposition that makes it readable:
a score of 0.3 means something very different when the rest is "missed" than when the rest is "asked
but not elicited".

## The rubric

Each task carries its own rubric. Different tasks have different lists; the score is not comparable
across tasks, and is not meant to be.

### What an item holds

| Field | Purpose |
|---|---|
| `item_id` | Stable identifier, so a per-item verdict can be tracked across runs. |
| `fact` | The true content of the item, stated completely enough that a judge can check an answer against it. Not a label — the judge decides whether the *truth* arrived, so the truth has to be in the item. Scoped to this item only, not the task's whole ground truth. |
| `objectives` | Which of the task's scored objectives this fact feeds. At least one, and prefer one the task gives weight to -- see rule 3: an objective weighted at zero is still reported, but its absence can no longer move the score. |
| `judge_hint` | Optional. Author guidance to the judge on what counts as conveying this item. |

### The six authoring rules

1. **Critical only.** The test is whether a perfect artifact is possible without the fact. If the
   answer is yes, the item does not belong in the rubric. This keeps the denominator meaningful and
   the rubric small.

2. **Phrased so no agent-facing file can satisfy it.** An item that the agent could have read for
   itself is not an elicitation item, and including it means every agent loses a point for reading
   efficiently. The same fact often has a handed form and an elicitable form, and the granularity of
   the phrasing decides which one you wrote down. One task's mounted business document lists its fault
   categories informally in prose while only the exact category spellings live with the simulator:
   "knows the categories exist" is a handed item, "knows all six exact spellings" is an elicitable one.
   Write the second.

3. **Every item names at least one objective it feeds — and a *priced* one wherever it can.** A fact
   no objective scores cannot be critical, because a perfect artifact is perfect *as measured*. This
   is a validation rule, not a suggestion: it makes criticality auditable instead of a matter of
   taste, and it makes rubric bloat structurally impossible.

   Weight is part of that. An objective a task computes but weights at zero is still reported, and
   its absence can no longer move the task score — so an item resting only on one is making a much
   weaker claim than the rule appears to promise. That is sometimes still the right answer: airline
   zeroed `policy_compliance` for comparability with published tau2 numbers while the policy itself
   stayed ask-only. What must not happen is arriving there unnoticed, which is what happened when the
   check compared objective *names*. Items in that position are named in the task's own rubric test,
   with the reason.

4. **At most fifty items.** A hard cap. Rubrics are authored and reviewed by people, and a judged
   list that nobody can hold in their head stops being a rubric. Where a task's elicitable material
   overflows the cap, bundle related facts into one item rather than dropping them.

5. **A bundled item states its own completeness.** "All four cancellation eligibility conditions",
   not "cancellation rules" — because items are judged all-or-nothing, and the judge has to know what
   complete means. Which facts to bundle is the task designer's call; the only requirement is that the
   boundary is stated.

6. **Written from the source, never from a conversation.** The only inputs to a rubric are what the
   simulator holds (its documents and each persona's declared knowledge), what the agent can read (to
   apply rule 2), and the task's objectives. A run's transcript is not source material, and reading one
   while authoring destroys the thing being measured in both directions: items the agent happened to ask
   about get included, and items nobody asked about get quietly dropped. Either way the rubric stops
   being an independent measuring stick and becomes a description of one conversation. This is why the
   rubric is part of *defining* a task rather than part of scoring it afterwards.

### Items are all-or-nothing

Each item is one yes/no question: did *everything* in this item come back? A bundle where two of four
conditions arrived is not elicited. This is what keeps a coarse item honest — the author chose to
bundle, so the author owns the completeness bar, and there is no partial credit to argue about.

### The rubric is answer-key material

A rubric is the concentrated list of exactly what the agent is supposed to extract, with the true
answers written out. It is held-out ground truth, in the same tier as the eval dataset:

- It lives at the top level of the task directory, never under the task's `environment/` tree —
  everything there is mounted into the agent's container and readable.
- It is pointed at from the task spec's evaluation-data block, which is dropped wholesale from the
  redacted copy the agent receives, so the pointer cannot leak by construction.
- The existing leakage guards cover it: the static spec check already treats a repo path into the
  task tree as a leak marker, and the guardrail that scans every task's agent-facing mounts for
  ground-truth markers extends to the rubric path.

## How an item is scored

Two questions per item, in order:

1. **Did the agent ask about this item?** No — the verdict is *missed*, and the second question is
   moot.
2. **Did the simulator's answer convey the true fact?** Yes — *elicited*. No — *asked but not
   elicited*. "No" covers every way the truth can fail to arrive: the simulator said it did not know,
   refused, answered a different part of the question, or stated something that is not the fact.

The judge is given the build conversation and the item's own true fact. It is given nothing else — not
the simulator's source documents, not the artifact, not the other items. Withholding the source
documents is deliberate: a judge that can see what the persona *knew* starts confusing "the persona
held this" with "the agent was told this".

Every verdict comes back with the exchange it was based on, quoted. That is what makes the per-item
table auditable and lets judge drift be spotted by reading rather than by a second scoring pass.

**Judged against the truth, always.** A confidently wrong answer is not an elicitation success, so the
comparison is always against the item's stated fact and never against whatever the persona happened to
say. (Some tasks carry adversarial personas that deliberately hold mistaken beliefs. Those personas
are out of scope for this metric — it is not intended to be run with them — but scoring against the
truth means the metric does not silently invert if someone does.)

### What gets recorded

Per run: the three rates, the rubric's item count, how many of those items were actually judged, and
the per-item table — every item's verdict, the quoted evidence, and the judge's reasoning. The rates
alone cannot answer "why is this 0.3"; the table can.

**A run where nothing could be judged is unmeasurable, not zero.** Rates of 0.0 are what an agent that
asked nothing looks like, so reporting an instrument failure that way would state the worst possible
result about the agent on no evidence. A partial failure still scores, over the items that were
judged.

**Repeats do not multiply it.** A repeat re-runs the data points against the artifact the agent
already built; the interview that the rubric scores happened once, before any of them. So a run
carries exactly one elicitation score however many repeats its eval phase does. Nothing averages
the score across runs — the sweep dashboard shows each run's own elicited-over-judged fraction,
and a cross-run figure is deliberately not computed, because averaging rates whose denominators
differ (items go unjudged at different times) would weight the runs unequally without saying so.

## Where it runs

**In the eval phase, once per task run**, after the artifact's objectives have been scored. The
per-item judging fans out over the same bounded worker pool the eval phase already uses for scoring,
rather than opening a second pool alongside it — one concurrency budget for the whole eval phase, no
matter how many items a rubric has.

**Also standalone**, against either a finished run directory or a plain conversation transcript. The
run-directory path lets every run already on disk be scored without re-running anything. The
plain-transcript path exists because the human-baseline attempts captured their elicitation
conversations in that form: scoring those with the same rubric gives a human reference point, so a
machine score reads against what a person actually achieved rather than against an abstract 1.0.

**A caveat that applies to every attempt currently on disk.** Both predate the change that put their
task's material behind the simulator, so neither is yet a quotable human baseline — the airline one
especially, whose conversation happened two weeks before that policy stopped being handed to the agent,
which makes its zero a fact about the setup and not about the participant. The scores are recorded and
each carries a note saying so
([`ELICITATION_NOTE.md`](../human_baseline/attempts/airline_rebooking-l3-human-20260806-183947/logs/ELICITATION_NOTE.md)).
A conversation captured after the move is what would make the comparison mean what this paragraph
claims for it.

**Reported in its own section, on all three surfaces that show a run.** The run's own HTML report, the
tracer's Eval tab, and the sweep dashboard's results table each carry it — the first two as a section
with the headline rate, the three-way breakdown, and a folded per-item table; the sweep as a single
`elicited/judged` fraction in a Diagnostic-metrics column, because a row there has space for one number
per run. The three deliberately mirror each other's wording and counts, so two views of one run cannot
disagree. In none of them does it appear in the per-metric table, the score matrix, or the weighted
average: anything rendered alongside the objectives eventually gets averaged with them.

The judge's own model calls are not shown anywhere. Its evidence and reasoning are already extracted
per rubric item into that folded table, so a list of raw calls beside it would be a second copy.

**Zero is a real score; unmeasurable is not zero.** A run where the agent never contacted the
simulator scores zero, correctly and informatively — that has already happened in practice. A run
whose conversation log is missing or unreadable is *unmeasurable*, and must be reported as such. The
two look identical in a bare number and mean opposite things.

**A third case: there was nobody to ask.** The no-user ablation arm (`--no-user-sim`) starts no
simulator and hands the agent the same documents as files. Its status is `no_stakeholder` and its
rates are `None`. This is deliberately not the informative zero above: the agent had nothing to
elicit, so "missed 100% of the rubric" would be a false statement about it, and a table averaging
`elicited_rate` across arms would report the arm at 0% for a question it was never asked. The
diagnostic never runs on such a run — the status comes from the transcript's own
`no_stakeholder` marker, which the orchestrator sets, so it costs no model call and cannot be
confused with an empty log.

## Model and cost

The judging runs on the eval-side judge role, the same model configuration the other judges use — not
a fourth model knob.

**One model call per item.** The alternative — one call carrying the whole rubric — asks the model to
follow fifty independent instructions in a single pass, which is exactly where reliability falls off.
Per-item calls also make the quoted evidence natural and let items fan out in parallel.

The cost of that is contained by prompt caching. The conversation transcript is identical across all
fifty calls and is the bulk of the input, so it goes in the cached prefix with only the item's own text
varying after it. Cache reads cost about a tenth of normal input price against a one-time write
premium, which turns fifty full-price transcript reads into roughly one — an eight-fold reduction on a
typical transcript. Three constraints come with it:

- **The prefix must be byte-identical across items.** A run identifier, a timestamp, or an item index
  anywhere before the cache boundary silently drops every call to full price with no error.
- **The first call must complete before the rest fan out.** A cache entry only becomes readable once
  the first response has begun; fifty simultaneous calls with the same prefix all miss and all pay the
  write premium. So: one call, then the fan-out.
- **It has to be verified end to end, not assumed.** Model calls here go through a gateway. If the
  caching directive is dropped in transit the results are still correct and the cost is simply eight
  times higher, with no error to notice — so the check is that the reported cache-read token count is
  non-zero on the second call.

A short transcript — an agent that asked one question — falls below the model's minimum cacheable
length and will not cache at all. Expected, not a fault.

Even uncached, fifty calls on one transcript is small next to an eval phase that already drives fifty
data points through multi-turn conversations and several judges. Caching is worth doing because it is
nearly free to add; it is not what makes the metric affordable.

## Reading the number honestly

- **It measures the agent and the simulator together.** "Asked but not elicited" merges two causes: a
  persona that genuinely could not answer, and a broad question whose answer skirted the fact. Both
  are correctly "the agent asked and did not get it", which is what the score is for — but the bucket
  is not evidence that the persona was withholding. The recorded evidence lets the two be told apart
  by reading, with no re-scoring.
- **A broad question is a legitimate strategy.** If a single sweeping question brings the fact back,
  the fact was elicited. The metric measures what the agent obtained, not how elegantly it asked.
- **Persona level shifts what is attainable, by design.** A stakeholder who does not hold a fact
  cannot give it up, and the run scores accordingly. The number is what this agent got out of *this*
  stakeholder, not what an ideal stakeholder could have supplied.
- **A high task score with a low elicitation score is a signal about the task.** It means the artifact
  was built without the conversation — usually from the model's own knowledge of a public domain. The
  metric is applied uniformly and gets no per-task exemptions; where that combination shows up, it is
  telling you something about how much the task actually withholds.
- **One run is one conversation, so a single score is noisy.** Read it across runs where you can.
- **The judge itself is a second source of noise, and it is not yet quantified.** Each item is decided
  by a single sample at the provider's default temperature — no temperature or seed is sent, deliberately
  and repo-wide — so re-scoring the same conversation can move an individual verdict and therefore the
  rate. No agreement measurement against human labels exists yet, for this judge or any other in the
  repo, so the number is not calibrated: read it as a diagnostic with its per-item evidence attached,
  not as a measurement with a known error bar. The recorded quote and reasoning on every item are the
  audit path, and disagreement found by reading them is the signal that the rubric or the prompt is wrong.
- **A run's own eval output carries the answer key.** The withheld facts are written verbatim into
  `report.json`, `elicitation.json` and `report.html`, so a run directory is held-out material in the
  same tier as the rubric it came from, and the tracer that serves those files is a host-local surface
  (it binds to loopback for this reason). The schema's rule about never placing a rubric under
  `environment/` covers the input side only; this is the output side of the same boundary.
- **There is a second, indirect reading of the same thing.** The baselines
  ([`docs/baselines.md`](baselines.md)) measure what elicitation costs by *difference*: run the same
  task with the requirements handed over and again with them withheld, and the gap between the two task
  scores is the price of having to ask. That answers "what did asking cost?" This score answers "what
  came back?" — directly, per fact, without a second arm to compare against. They are worth reading
  together: a large arm gap with a high elicited rate says the agent asked well and the task is simply
  hard to build; a large gap with a low rate says it never got the material.
- **Rubrics are author-dependent.** Two people writing the same task's rubric will not produce
  identical denominators. Requiring every item to name a scored objective — and to say so explicitly
  when the only one it names carries no weight — is what keeps that spread bounded, and review is what
  keeps it honest.

## Validating a rubric

**Author first, then score.** Rule 6 makes the order matter, not just the inputs. Scoring a real run
against a finished rubric is the most informative check there is — the per-item table can be read
against the conversation by hand, and a verdict that disagrees with a human reading means the rubric or
the judge prompt is wrong. But it is a check on the *instrument*, and the only edits it licenses are
ones that fix a defect in an item: a fact stated too loosely to judge, a bundle whose boundary is
unclear, an item no persona could hold. "The score came out higher than expected" is not a defect, and
adjusting a rubric until a run scores plausibly is how a measurement becomes a description.

Two checks are mechanical and belong in the test suite: every item names a real objective of that
task, and the item count is within the cap.

One check needs a run: **the all-refusals persona test.** Run the task with a persona that answers
nothing, and see which requirements the artifact still satisfies. Anything satisfied without the
conversation was not critical, and should not be in the rubric. This doubles as a check on the task's
own information placement — material that survives a fully uncooperative stakeholder was reachable
some other way.

## Task designer responsibility

**A new task ships its own rubric.** The task designer is the only person who knows which facts are
load-bearing for their objectives, and the rubric is part of defining the task, not part of scoring
it after the fact. Task authoring guidance and the schema reference carry the rules above. The first
three tasks (`order_to_cash`, `slides_generator`, `airline_rebooking`) got rubrics as a one-off
backfill; four more have been written since, and any task still without one has yet to be authored
rather than been exempted.

The designer owns the granularity decisions — what to bundle, where to draw an item's boundary. The
only hard constraints are the six authoring rules and the cap.
