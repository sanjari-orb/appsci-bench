# `optimize_agent` Design

How the AppSci agent scores its own candidate and improves it during the build phase, and why the
pieces sit where they do.

`docs/applied_scientist_agent_design.md` describes the agent's original three tools and predates this
layer. Start there for the agent; this file covers `eval_generator` and `optimize_agent`.

---

## 1. What problem this solves

The agent builds an artifact and submits it. Left there, it has no way to tell a good artifact from a
bad one before submitting — the benchmark's own eval runs host-side, after the build is over, and the
agent never sees it. So the agent builds a candidate, guesses, and hands it over.

This layer gives the agent its own measuring instrument:

- **`eval_generator`** turns the agent's written understanding of the task into a runnable harness.
- **`optimize_agent`** scores the deployed candidate against that harness and improves it from what
  failed, repeatedly, until the scores stop moving or a budget runs out.

The internal eval is the agent's own work, judged by the agent's own criteria. It is **not** the
benchmark eval and does not have to agree with it — see §8.

---

## 2. Scoring and optimizing are one call, in that order

The question this doc exists to answer: *when is the artifact scored, and when is it optimized?*

Both happen in a single `optimize_agent` call, scoring first. The critical part is that **a change is
judged by the *next* call**, not the one that made it.

One call does this:

| Step | Who | What |
|---|---|---|
| 1 | tool | **Preconditions.** Refuse early if there is no harness, the artifact changed since the last deploy, or the candidate is not answering. Spends nothing. |
| 2 | tool | **Score.** Run the sanctioned harness against the deployed container, in code. This number is what the cycle is judged on. |
| 3 | tool | **Resolve the previous change.** If one is pending, compare this score to `score_to_beat`. Better by ≥ `MIN_IMPROVEMENT` → keep. Otherwise → restore the snapshot and redeploy. |
| 4 | sub-agent | **Improve.** Read the report, diagnose the weakest metric, edit the candidate. Skipped entirely when the call may not edit. |
| 5 | tool | **Deploy and park.** If the source actually changed, redeploy (which mints a checkpoint) and record the change as pending with the score it must beat. |

### Why the verification lands on the next call

The harness measures the *deployed container* over HTTP. An edit is invisible to it until the artifact
is rebuilt, and only this tool can rebuild — the sub-agent has no Docker socket, by design. So a
sub-agent that re-ran the harness right after editing would measure the candidate it just replaced.

```
call N      tool: score  ->  sub-agent: diagnose -> edit  ->  tool: redeploy, park as pending
call N+1    tool: score  ->  keep or roll back call N's change  ->  maybe edit again
```

**This is why a budget of N needs more than N calls.** Verifying N changes takes at least N+1 calls,
and more if any are rejected. The agent's stopping rule is the two flags on the result, never a count
of its own calls.

### Why the tool measures rather than the sub-agent

The harness contract is fully pinned — `run.py` as the entry point, the endpoint in
`CANDIDATE_ENDPOINT_URL`, one aggregate JSON object on stdout — so nothing about running it needs a
model. It used to be run by the sub-agent, which had to reconstruct the stdout-to-file redirect from
its brief every cycle. That lost measurements three ways: any stray stdout line corrupted the JSON, a
cycle that ran the harness and then hit its step limit before saving threw the score away, and the
step was re-derived by an LLM each time.

What genuinely needed a model was the *recovery*: a harness that will not start because a dependency
is missing. That is kept, as a fallback — one narrowly-scoped repair sub-agent (may install, may not
touch the harness or the candidate, may not write a score) and one retry. A harness that ran and
returned a low number is not repaired; `0.0` is a legitimate score.

A consequence worth knowing: a call that may not edit starts no sub-agent to improve the candidate.
The one exception is that repair path — and because it runs on measure-only calls, where the caller
takes no snapshot, the candidate is snapshotted around it and any edit it makes is undone.

---

## 3. What a cycle costs

A **cycle** is one attempt to change the candidate. It is spent when a call actually changes the
source — compared by content hash, not taken on trust, so a sub-agent that says it fixed something and
did not spends nothing.

These spend **no** cycle:

- a call rejected by the preconditions (stale artifact, missing harness, dead candidate)
- a call that only measures (`score_only`, a spent budget, saturation)
- a call whose sub-agent made no change

The budget bounds sub-agent edits and rebuilds — the expensive part — not measurements. A pending
change counts against the budget before it reaches the history, or a budget of 1 would fund two
changes.

---

## 4. When the loop stops

Two independent conditions, whichever comes first. Neither overrides the other, and both are reported
separately so a reader can tell which one ended the run.

- **`budget_exhausted`** — the cycle allowance is spent. Resolved from
  `budgets.build.optimize_cycles`, else the config, else no ceiling. A call parameter may only narrow
  it, never widen it: a budget a spender can raise is not a budget.
- **`saturated`** — the last `SATURATION_PATIENCE` (2) measured attempts each failed to beat the best
  score *as of when they started*. Measured against the prior best rather than the record before, so a
  pair of changes that swings down and back up is not mistaken for progress. Only attempts count; a
  baseline measurement is not an attempt at anything.

The allowance is a ceiling, not a quota. Stopping with cycles unspent is a good outcome when there is
nothing worth trying. The one hard rule is never to call `complete` while a change is awaiting
verification — that submits a candidate nothing has scored.

---

## 5. Trust boundaries

The agent is the thing being measured, so it must not be able to move the ruler. Three mechanisms,
each guarding a different actor.

**The sanction.** `eval_generator` records a fingerprint of the harness it produced. `optimize_agent`
refuses to score against a harness that does not match. This is what makes "only `eval_generator` may
change the eval" a rule rather than a request — on a live run the agent expanded `eval_data.json` from
32 to 46 cases through a direct `mini_swe_agent` call.

**The fingerprint comparison, twice.** The harness is hashed before the call does anything, and
compared **after measuring** and again **after the improvement step**. Both are needed: measuring can
delegate a dependency repair, so measuring is an actor too. Checking only after the improvement step
left every measure-only path unguarded — and with a budget of N, the call resolving the last change
always has the budget spent, so every budgeted run ended on that path. A mismatch discards the score
and restores the candidate.

**The snapshot.** The candidate's source is copied aside before any edit, so a rejected change is
restored byte-for-byte. The dependency-repair sub-agent gets the same treatment even though it is not
supposed to touch the candidate at all: it runs on measure-only calls, where the caller takes no
snapshot, so an edit there would be live, unrecorded and un-rollbackable. Verified on a real run: checkpoint 3 hashed identical to checkpoint 1 and
checkpoint 6 to checkpoint 4, and the submitted artifact hashed identical to the one candidate the
loop kept.

### The workspace layout is part of this

Ownership, not just paths. The agent process is root; every sub-agent command runs as an unprivileged
user. A directory the sub-agent *owns* can be **renamed** by it — renaming needs write permission on
the parent — so anything it must write has to be a leaf under a root-owned parent.

```
<workspace>/.appsci/                  root       root-owned, so neither child can be renamed
<workspace>/.appsci/handoff/          sub-agent  eval_handoff.md
<workspace>/.appsci/optimize/         root       score history, pending change, snapshots, report
                                                 (world-readable: the sub-agent reads the report)
<workspace>/.appsci/optimize/inbox/   sub-agent  change_note.md — the only thing written back
```

Root must create this before the first sub-agent runs, because the handoff is written by a
`mini_swe_agent` call that precedes both tools. If the sub-agent's own `mkdir -p` wins that race it
owns the whole chain. Hence: created at agent start-up, and both tools call the same idempotent
helper, which also reconciles ownership rather than only creating.

**The tree alone is not sufficient, and review caught this PR claiming it was.** Renaming an entry
needs write permission on the *parent*, so a root-owned `.appsci` inside a sub-agent-owned
`/workspace` is still one `mv` away. `/workspace` is therefore root-owned, world-writable and sticky
— the `/tmp` model: the sub-agent creates its own files freely and cannot rename or delete entries it
does not own. Root ownership of `/workspace` is load-bearing, because the sticky bit exempts a
directory's own owner.

Nothing here lives in the artifact directory, and that is deliberate. The artifact directory's content
hash *identifies a candidate* — it drives the deploy staleness check, the checkpoint name, and the key
a score joins back to a candidate by. A file written there after a deploy would give unchanged code a
new identity on every measurement.

---

## 6. The harness contract

Pinned in `eval_generator`, which produces the harness, and imported by `optimize_agent`, which runs
it — one definition each, so a rename cannot leave the two disagreeing.

| Term | Value |
|---|---|
| Entry point | `run.py` (`RUN_SCRIPT_FILENAME`) |
| Required files | `eval_data.json`, `run.py`, `README.md` |
| Candidate endpoint | read from `CANDIDATE_ENDPOINT_URL` |
| Output | one JSON object on **stdout**: `headline_metric` (float 0..1), `metrics_breakdown`, `cases` |
| Exit code | **0 required.** A non-zero exit is never scored, whatever it printed |

`headline_metric` is validated as a real number in 0..1 before anything uses it, because every
decision downstream — progress, saturation, keep-or-roll-back — is arithmetic on it, and a string or a
NaN would make each comparison quietly false.

The harness runs under the same sandbox `mini_swe_agent` uses for its own commands: it is LLM-authored
code, and run unwrapped from the tool process it would execute as root with read access to the
harness's own source at `/app`. It gets its own process group, killed as a group on timeout — the
command is layered (`env` → `setpriv` → `sh -c` → `python`), so killing the direct child would leave
the harness itself running and making model calls while the retry ran alongside it.

---

## 7. What each score is joined to

Every record in `score_history.json` carries the **checkpoint** the running container was built from,
by content hash, matching an entry in the run's `checkpoints_manifest.json`. That manifest holds the
only durable copy of the files that produced the score.

A null checkpoint is honest rather than lazy — `checkpoint_note` says whether the deploy status was
unreachable, nothing was deployed through the host route, or the run keeps no checkpoints.

This is what makes the internal trajectory auditable after the fact: every score points at the exact
files that produced it, and those files can be re-scored against the real eval.

---

## 8. The internal eval is not the benchmark eval

Everything above is the agent's own instrument, built from its own understanding, inside the build
phase. The benchmark's eval runs host-side afterwards against the submitted artifact.

They can disagree, and on the one measurement taken so far they did. Six checkpoints from one airline
build were re-scored against the real eval on a fixed 10-case subset:

- Two artifacts were each scored twice (the roll-back restores byte-identical code), and both pairs
  spread by exactly 0.100 — the real eval's run-to-run noise at that sample size.
- The only movement exceeding that noise was the one change the loop **kept**, and it went the wrong
  way: 0.650 mean before, 0.450 after, while the internal eval read the same change as 0.702 → 0.725.

Two measurements per artifact and roughly two discriminating cases is not enough to characterise the
relationship. It is enough to say the internal harness is not yet a usable proxy for the real score,
which matters before anyone tunes against it.

Re-scoring is a loop over `python -m appsci_bench.orchestrator.run --artifact-dir <checkpoint>`, with
`--limit` giving a deterministic prefix of the dataset so every checkpoint is scored on the same cases
— a paired comparison, which is what makes small differences readable at all at that size.

---

## 9. Reading the loop back off a finished run

Everything above is about how the loop behaves during a build. Six questions about it are worth
answering after the fact, and they are what the **optimization diagnostic** reports:

- **how many times the loop ran** — `optimize_agent` calls, and separately the cycles they spent. A
  call always measures; only a call that changes the candidate spends a cycle, so the two numbers
  differ and both are reported.
- **whether it stopped early on saturation** — `saturated` with budget still unspent. That is the only
  state saying the optimizer had nothing left to find; a spent budget (§4) establishes nothing either
  way, so the two are never collapsed, and a run where both held gets its own stopping reason.
- **how its redeploys went** — per cycle that changed the candidate, whether the rebuild produced a
  running artifact or failed (in which case the tool restored the previous source, so the failure cost
  the change, not the run).
- **how many of its own changes it kept, and how many it rolled back** — the verdict each call
  reached on the previous call's change, plus how the roll-back itself went: whether the previous
  candidate was restored and redeployed, or whether the restore did not take and the rejected code
  stayed running.
- **how many valid checkpoints it produced** — checkpoints with a `deployed` outcome, which are the
  only durable copies of the candidates the loop built (§7).
- **where its own internal-eval score started, peaked and ended** — the quantity the loop is steering.
  First, best (with the call that measured it), final, and the change across the run.

**Why best is reported separately from final, and why the comparison is on the bytes.** The
keep-or-roll-back rule (§3) compares a new measurement against the *immediately preceding* one, not
against the best the loop has ever seen, so best and final are genuinely separate numbers: run
`airline_rebooking_claude-opus-4-8_b1_l3_20260911_023444` accepted 0.949 over 0.938 while it had
already measured 0.974.

What the diagnostic must not do is report `final < best` as a finding. Measured over the 39-build
`optimize_cycles5` sweep, six runs finished below their own best and **four of them had never changed
the artifact**: best and final were taken on the same content hash, 0.001 to 0.016 apart. The loop's
own eval is not deterministic, so re-scoring one program returns two numbers, and a warning that fires
on that is wrong two times out of three. `ended_below_best` therefore asks whether the best-scoring
call and the last measuring call scored *different candidates*, decided on the content hash rather
than the checkpoint name — a restore captures the same bytes under a new name, and comparing names
would call that a regression.

**How the remaining two lost their high-water candidate.** Not through the keep rule, which held: the
incumbent's score never fell across a change the loop made. Through deploys the loop never saw. The
agent can call `deploy_artifact` directly between two `optimize_agent` calls, and such a change has no
snapshot, so the next call has nothing to compare against and nothing to restore — it records the new,
worse score as a fresh baseline. In `customer_call_insights_claude-haiku-4-5_b2` the loop reached
`nothing_to_fix` at 1.000, the agent then deployed twice on its own, and the next call measured 0.708
on a different artifact with no verdict to reach. `order_to_cash_gpt-oss-120b_b1` is the same shape and
worse: 8 checkpoints, 3 from the loop, 0.667 down to 0.200. `deploys_outside_loop` counts these, and it
is the only mechanism in the sweep that lost an accepted candidate.

**Why the roll-back count is what reconciles the deploy tally with the checkpoint tally.** The deploy
counts cover the cycles that *changed* the candidate, because those are what the cycle budget is spent
on. A roll-back redeploys too, in the other direction: it restores the previous candidate and puts it
back in front of traffic, which writes a checkpoint like any other deploy. So a run with four changes
and one roll-back leaves five checkpoints, and before roll-backs were counted the gap could only be
explained in prose. It now closes arithmetically —
`checkpoints_valid == deploys_succeeded + rollback_redeploys` on a run whose every checkpoint came
from the loop, which held for all 39 builds of the `optimize_cycles5` sweep. `deploys_attempted` is
deliberately not widened to include restores: it is the count that pairs with `cycles_used`, and a
restore spends no cycle and produces no new candidate.

A roll-back that *fails* is counted apart from one that takes, because only one of them is
bookkeeping. `restore_failed`, `redeploy_failed` and `no_snapshot` all leave the code the loop decided
against running, which is a fact about the artifact the run went on to submit.

**The benchmark's score sits beside the loop's final one, in the report and the tracer.** They are two
different measurements of one artifact: the loop scores its own generated cases under its own metric
names, the benchmark scores the task's declared objectives. The number between them is therefore
labelled a distance, never an error and never an agreement score, and it is computed where it is
displayed rather than stored on the diagnostic — which stays readable from a build-only cell that
never reached an eval at all.

Counted, never judged: the numbers come off records the run already wrote, so this costs nothing and
is deterministic. Three sources, each for what it authoritatively knows — the agent's event log for
the calls (each carries the tool's `trace_output_keys`), the orchestrator's `build.start` for whether
the route existed at all, and the run's checkpoint manifest for which deploys survived as candidates.
A checkpoint is attributed to the loop when it was created while a call was in flight: the manifest
records the deploy, not what asked for it, and the run's earlier build-phase deploys are in there too.

It reaches a reader four ways: as `runs/<trace>/optimization.json`, written at the end of every run
under observability — including a `--skip-eval` build, which is the cell an eval never reaches; on the
eval report (`report.optimization`, beside the elicitation diagnostic and never folded into the
score); in the tracer's Eval tab; and through `scripts/score_optimization.py` for every run that
finished before the metric existed.

The four statuses carry as much as the counts. `disabled` is the ablation arm — no optimization route
at all — `not_attempted` is an agent that had the route and never called it, and `unmeasurable` is a
trace that could not be read *or* one predating `build.start` carrying the flag, where those first two
are indistinguishable. A single "0 optimization runs" would read identically on all three, and only
one of them is a finding about the agent.

---

## 10. Known gaps

- **The internal eval does not reliably verify final state.** A generated harness has twice bound its
  cases to records that do not exist — first ids recalled from a public benchmark, then invented ones —
  and degrades to judging conversation transcripts rather than treating an unresolved record as
  unbindable. The generator brief now forbids both and the airline spec says asking the stakeholders is
  how to get a real id, which fixed it on the next run. It still rests on the agent choosing to ask.
- **`0.0` and "not measured" are the same number to every comparison downstream.** A harness that
  scores nothing usable and reports `0.0` is read as a real measurement, and the loop will keep
  whatever change it last made. Requiring exit 0 closes the common shape; a harness that exits 0 having
  bound nothing still gets through.

---

## 11. Where the code is

| Concern | File |
|---|---|
| The cycle, budget, saturation, keep/roll-back | `appsci_agent/tools/optimize_agent.py` |
| Harness authoring, the contract, the sanction | `appsci_agent/tools/eval_generator.py` |
| Workspace tree and per-node ownership | `appsci_agent/tools/workspace_layout.py` |
| Privilege drop and the sandbox wrapper | `appsci_agent/tools/mini_swe_agent.py` |
| Stopping rule the agent is told | `appsci_agent/core/agent.py`, `appsci_agent/prompts/system_prompt.jinja2` |
| Tool descriptions the model reads | `appsci_agent/tools/tool_configs/{optimize_agent,eval_generator}.yaml` |
| Reading the loop back off a finished run (§9) | `appsci_bench/eval/build_optimization.py` |
| The same, for runs that already finished | `scripts/score_optimization.py` |
| Where it lands for a reader | `appsci_bench/eval/report_html.py`, `appsci_bench/tracer/templates/dashboard.html` |
