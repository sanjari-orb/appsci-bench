# scripts/

Manual / developer utilities. **Not** part of the automated test suite.

## `run_tasks.sh` — run a batch of tasks end-to-end from a YAML config

```bash
./scripts/run_tasks.sh scripts/run_tasks_yamls/run_tasks.sample.yaml
```

Reads a YAML config listing which tasks to run, the persona level(s) for each (`personas: [l1, l3]`
or `all`), how many eval data points (`limit`, null = all), the `timeout`, and optionally which model
the agent under test runs (`agent_model`, top-level or per-run; omitted means no `--agent-model` is
passed, so the orchestrator falls back to `$AGENT_MODEL` and then to the agent's own default) — then
runs each (task × persona) **sequentially** through `appsci_bench.orchestrator.run` with
`--enable-observability` on, and prints a pass/fail summary. `scripts/_expand_runs.py` does the
config→runs expansion.
`scripts/run_tasks_yamls/run_tasks.sample.yaml` is a ready config for all 3 tasks × all personas × all data points.
Requires creds from `setup_local_with_gateway.sh` first; skips `slides_generator` rows if its
eval data isn't on disk (run `git lfs pull`).

If the observability port (default 8080) is already taken by a stale process/container, the
underlying run tries the next few ports (8081, 8082, ...) automatically and logs which one it
landed on — it never silently continues *without* a dashboard. Each task run also removes its own
agent-deployed artifact container on exit (success, crash, or timeout), which is what usually
squats on a port for the next run in the batch. Only if every port in the retry range is occupied
does the run hard-fail with exit code `3`; `run_tasks.sh` treats that as an environment blocker and
aborts the whole batch immediately (the same range will still be occupied for every remaining row),
rather than recording each one as a separate failure. Free a port (`lsof -nP -iTCP:8080 -sTCP:LISTEN`,
then `docker rm -f` the container holding it if applicable) and re-run.

## `evaluator_system_e2e.py` — run the evaluator end to end (mocked, no creds)

Exercises the whole evaluator loop (generate → score → aggregate) over fully mocked dependencies —
an in-process artifact client with canned outputs, a single-turn task and a multi-turn task,
deterministic eval functions (one lower-is-better), and a scripted artifact agent user sim. The quickest way
to confirm the orchestration runs and to eyeball the output. **No credentials needed.**

It prints each report as **text and JSON**, and writes the reports + transcripts to a gitignored
folder for easy inspection (a fresh `scripts/_out/<run>/` directory per run).

```bash
python scripts/evaluator_system_e2e.py                 # mocked
python scripts/evaluator_system_e2e.py --out-dir /tmp/x # choose where output goes
python scripts/evaluator_system_e2e.py --real-judge     # use a real LLM for one judge if creds exist
```

## `chat_appsci_user_sim.py` — chat with the appsci agent user sim over its real HTTP service

A human-in-the-loop check that exercises the full outside-world path: it spins up the FastAPI
appsci-agent-user-sim service (uvicorn), binds it to a task + persona, and drops you into a terminal REPL where you
play the applied-science agent. Your questions go over HTTP through the agent-side client
(`appsci_agent/clients/user_sim_client.py`) to `POST /ask`; the stakeholder's replies print back.

Requires **real model credentials** (it makes live LLM calls via the `APPSCI_USERSIM_*` env vars — see
[`docs/user_simulator_api.md`](../docs/user_simulator_api.md) §3). The easiest way: `cp .env.sample .env`
and fill in `MODEL_NAME` + the matching provider key (e.g. `ANTHROPIC_API_KEY`) — the script loads `.env` automatically.

```bash
# defaults: order_to_cash, L1 manager persona
.venv/bin/python scripts/chat_appsci_user_sim.py

# pick a persona / task / model
.venv/bin/python scripts/chat_appsci_user_sim.py --persona l3
.venv/bin/python scripts/chat_appsci_user_sim.py --task order_to_cash --persona l2 \
    --model anthropic/claude-opus-4-8
```

Ctrl-D or `quit` to exit.

## `chat_artifact_user_sim.py` — chat with the artifact agent user sim, in-process

The eval-time sim's sibling manual check -- shares almost no code with `chat_appsci_user_sim.py`
above: there's no HTTP service, no persona YAML, and the human plays the **other** side of the
conversation. The real sim initiates and drives every turn toward its seeded `user_task`/
`user_persona`, deciding for itself when to stop; you type the artifact's replies at an
`artifact>` prompt (no artifact is served anywhere in the repo yet, so this is the only way to
chat with the real sim today).

Requires **real model credentials** (same env vars / `.env` convention as above).

```bash
# seed from a real dataset sample
.venv/bin/python scripts/chat_artifact_user_sim.py --task airline_rebooking --data-point tau2_airline_5

# seed ad hoc
.venv/bin/python scripts/chat_artifact_user_sim.py \
    --user-task "You want a refund for your delayed flight." \
    --persona "You are Alex Rivera, a Gold member, mildly impatient." \
    --stop-criteria "the refund has been processed"
```

Prints `attach_files` each turn the sim chooses to attach one, so file-awareness behavior is
checkable by eye. Ctrl-D or `quit` (as the artifact's reply) to exit early, otherwise the
conversation ends when the sim stops itself or `--max-turns` is hit.

**Files flow both directions.** When the sim attaches one of its own files, the script
materializes it and writes a real copy under a fresh `scripts/_out/chat_artifact_user_sim_<run>/
incoming/` folder (gitignored), printing the path -- so you can actually open what a real artifact
would have received, not just see its name. After you type your reply, you're prompted for zero or
more local file paths (comma-separated, blank = none) to attach to it; `file_type` is inferred
from each path's extension and an unsupported/missing one is skipped with a warning.

## `score_elicitation.py` — score one build conversation against a task's elicitation rubric

```bash
# a benchmark run: reads the user-sim event log inside the run directory
python scripts/score_elicitation.py --task order_to_cash --run runs/order_to_cash_20260811_121805

# a human-baseline attempt: a captured terminal transcript
python scripts/score_elicitation.py --task airline_rebooking \
    --transcript human_baseline/attempts/airline_rebooking-l3-human-20260806-183947/logs/transcript.md
```

Of the facts the agent could only get by **asking** the build-time stakeholder, what share did it
actually get? Needs no artifact, no deployment and no dataset -- only a rubric and a conversation -- so
any run already on disk can be scored without re-running anything. That matters because the
conversations worth looking at were recorded before this metric existed. Design:
[`docs/elicitation_score_design.md`](../docs/elicitation_score_design.md).

Scoring a human attempt is what gives a machine number something real to be read against; an abstract
1.0 is not a reference point.

Prints the three rates plus the per-item table with each verdict's quoted evidence, and writes
`elicitation.json` next to the input (`--no-write` to skip). Options:

- `--update-report` folds the result into the run's **existing** `<run>/eval/report.{json,html}`, so
  the diagnostic shows up in the report you already have rather than as a second page. `--run` only.
- `--html <path>` writes a standalone page carrying only the diagnostic -- for `--transcript`, where
  there is no report to update.

Exit status is about whether the tool could run, never about the score: a low score, and a run that
cannot be measured at all, both exit zero. A rubric a task declares but does not have raises, because
that is a task bug rather than a run without a rubric.

## `score_optimization.py` — count what a finished run's optimization loop did

```bash
# one run on this machine
python scripts/score_optimization.py --run runs/order_to_cash_20260818_173200

# every run under a directory, as a table, plus a CSV for a notebook
python scripts/score_optimization.py --runs-dir runs --csv optimization.csv
```

Six questions about the agent's optimization route, for runs that have already finished: how many
times the loop ran, whether it stopped early because the scores had **saturated** (as opposed to
running out of cycle budget, which establishes nothing), how many of its redeploys failed to build,
how many of its own changes it kept and how many it rolled back, how many durable candidates
(`deployed` checkpoints) it left behind, and where its own internal-eval score started, peaked and
ended.

Best is printed next to final because they can differ: a change is kept when it beats the *previous*
measurement, not the best one. Whether the loop really ended holding a worse artifact is decided on the
candidate's content hash rather than on `final < best`, and the two cases are reported apart — the
loop's own eval is not deterministic, so a run can score lower on the very same program. When the run
also holds an eval report, the benchmark's score is printed beside the loop's final one — as a distance
between two different measurements, never as an error.

The roll-back counts are what let the deploy tally and the checkpoint tally be reconciled: a restore
is a redeploy that is not a change, so it leaves a checkpoint the deploy counts do not explain. The
detail view prints the arithmetic, and the rollup says how many runs it closes for.

**Nothing is judged here**, despite the name — which matches its siblings so the three are found
together. Every number is counted out of records the run already wrote: the agent's event log, the
orchestrator's `build.start`, and the run's checkpoint manifest. So it needs no artifact, no
deployment and no model, costs nothing, and gives the same answer twice. Reader:
[`appsci_bench/eval/build_optimization.py`](../appsci_bench/eval/build_optimization.py).

Writes `optimization.json` next to each run (`--no-write` to skip). Options:

- `--update-report` folds the result into the run's **existing** `<run>/eval/report.{json,html}`, so
  the diagnostic shows up in the report you already have rather than as a second page.
- `--runs-dir <dir>` scores every run one level under it, prints a per-run table and a rollup
  counting **runs** ("3 of 11 stopped early on saturation"). A directory is taken as a run when it
  holds an agent event log, so a README or a half-fetched directory is skipped rather than reported as
  a run with no optimization.
- `--csv <path>` writes one flat row per run.

A new run gets this for free: the orchestrator writes `<run>/optimization.json` at the end of every
run that **built** something under observability — including a `--skip-eval` build cell, where the
build is the phase the loop ran in — and the eval phase attaches the same diagnostic to its report,
which the tracer's Eval tab renders. An eval-only trial gets no file of its own: it has no build
phase, so its diagnostic is the one in `<trial>/eval/optimization.json`, read from the run that built
the artifact. This script is for everything that ran before that existed.

Exit status is about whether the tool could run, never about what it found. Four statuses, and the
difference between them is the point: `scored`, `disabled` (the ablation arm — no optimization route
at all), `not_attempted` (the agent had the route and never called it, a real zero), and
`unmeasurable` (the trace could not be read, or predates `build.start` carrying the flag, so those
last two cannot be told apart).

## `score_power.py` — the score^k metric over buckets of eval trials

```bash
# one --build per build, listing that build's eval trial run ids
python scripts/score_power.py \
    --build airline_rebooking_claude-opus-4-8_b0.t0_l3_20260910_224214 \
            airline_rebooking_claude-opus-4-8_b0.t1_l3_20260910_233728 \
            airline_rebooking_claude-opus-4-8_b0.t2_l3_20260911_011406 \
    --build <b1 trial> <b1 trial> <b1 trial> \
    --build <b2 trial> <b2 trial> <b2 trial>

# a build that produced nothing: an empty bucket, which scores 0 on every example
python scripts/score_power.py --build T1 T2 T3 --build T4 T5 T6 --build

# the same thing as JSON, on the command line or on stdin
python scripts/score_power.py '[["T1","T2"],["T3","T4"]]'
python scripts/score_power.py - < buckets.json
```

Keep **one trial from each of the k builds**. For every eval example, take the **worst** of the k
kept scores on that example, and average those over the examples. That is one value per way of
keeping one — 3 builds × 3 trials is 27 ways — and **score^k is their mean**, reported with their
std. A case counts only as much as the weakest build got it right, so this charges the builds both
for disagreeing with each other and for being wrong in different places.

Printed beside it, `average` is `avg(avg(b0 trials), avg(b1 trials), …)` over the trials' headline
scores — the optimistic number score^k is read against. Each build counts once whatever number of
trials it carries, the same weighting score^k gives it. Its `std` is the spread of those build means
across the builds: the thing score^k exists to charge for, shown on its own, before any minimum is
taken. The JSON carries it as both `average_std_across_builds` and `average_variance_across_builds`.

```
score^3    0.4919   std 0.0174   (mean of all 27 combinations)
one draw   0.5000                (one trial per build, seed 20260921: build 0 trial 0, ...)
worst      0.4200                (per example, the worst of all 9 trials -- no combinations)
average    0.6111   std 0.0409   (mean of the per-build trial means, spread across the 3 builds)

3 builds of 3, 3, 3 trial(s), 27 combinations, 50 examples

  build 0  mean 0.553   0.580  0.540  0.540
  build 1  mean 0.640   0.700  0.620  0.600
  build 2  mean 0.640   0.640  0.640  0.640
```

`one draw` is **one of those same 27 values, picked at random**: what the arm would have scored if
each build had been evaluated once and that was all you had. score^k is the expectation of it, and
the std is the size of the gap, so a draw far from score^k says a single trial per build would have
misled you. The draw is seeded so it does not move between runs over the same trials; `--seed N`
asks for a different one, and the trials it picked are printed so the number can be re-derived.

`worst` is the per-example minimum over **every** trial, averaged over the examples — no
combinations, no keeping one per build. Taking the minimum over more trials cannot raise it, so it
sits at or below every combination value and bounds score^k from below. Where the two are close the
builds fail the same examples and running more of them buys little; where `worst` is far below, they
fail different ones. It ignores how you bucketed the trials, being a statement about the trials
rather than about the builds.

**You say which trials belong to which build.** Nothing here reads `replicate.json`, parses a run
id, resolves a re-submitted build slot or guesses which artifact a trial scored. Every one of those
was an inference, and an inference that goes wrong does not fail — it reports a real-looking number
for a set of builds that never existed.

**A build that produced nothing is an empty bucket** (`--build` with no ids). It scores 0 on every
example, so score^k goes to 0 while the average stays well above it. k does not change: an arm asked
for three builds is a score^3 whatever came back. And k is not a free parameter — score^k falls as k
rises, so compare two arms only where their k agrees.

Every trial named must have a readable eval report carrying a per-example view. One that does not
**stops the run by name**, rather than being quietly dropped: dropping it would silently change
which builds the number is about.

`--per-example` prints all three per-example columns, worst first; `--json PATH` always carries them,
keyed by example:

```json
"per_example": {
  "tau2_airline_10": {"score": 0.6667, "one_draw": 0.0, "worst": 0.0}
}
```

Each column's mean is its headline above it, which is what makes these the decomposition of the
three numbers rather than a fourth one: they say which examples the set loses, not how much.

Runs are read from `runs/` when it holds their eval report and from the S3 run mirror otherwise,
through `aws s3 cp`, cached under `scripts/_out/score_power/.cache/`. A mirror failure that is not a
plain "this object does not exist" **aborts** — expired credentials would otherwise turn every run
into a missing one.

Exit status: 0 computed, 2 bad usage, 3 no build given, 4 a named trial could not be read, 5 too
many combinations to enumerate, 6 the mirror could not be read.

## `detect_task_leakage.py` — auto-detect task leakage from a completed run trace (#77)

Complements the **static** guardrail in `appsci_bench/core/spec.py` (`agent_spec_leaks`), which
checks the redacted `spec.yaml` text before a run happens. This tool instead audits an actual
completed run's trace (`runs/<trace_id>/`, produced by `appsci_bench.orchestrator.run` with
`--enable-observability`) for leakage that only shows up at **runtime** -- ground truth that made
it into what the agent-under-test's container had mounted, or into its own model calls.

It only scans surfaces representing what the agent-under-test actually had access to: its
filesystem snapshot (`agent_fs/appsci_agent/`), the deployed artifact snapshot (`artifact/`), and
its own model calls (`model_calls-appsci_agent.jsonl` / `model_calls-mini_swe_agent.jsonl`). The
user-sim's mounted files and model calls are a **trusted** surface (it legitimately knows the raw
spec, since it role-plays the persona who provides the requirements) and are never scanned.

Each hit is classified into one of three failure modes and printed in a findings table:

| Failure mode | Meaning |
|---|---|
| `mounted_privileged_file` | the wrong files got mounted into the agent container (a ground-truth dir/path leaked in) |
| `context_leak` | task details (SOP text, gold answer, "ground truth"/oracle wording) ended up in the model's own prompt/completion |
| `eval_pointer_leak` | a pointer into the eval dataset (its path, or a ground-truth resource location) reached the agent |

Three detection layers, all applied to mounted files (the first two also apply to model context):
- a generic, task-agnostic marker regex -- a narrowed variant of
  `core.spec._AGENT_SPEC_FORBIDDEN`: its bare `sop/` prefix is dropped, since every real trace
  legitimately mentions the agent's OWN `/sop` mount (by path, and by the agent's own exploration
  output, e.g. an `ls /` it ran) -- reusing the spec-text-oriented marker verbatim here false-
  positived on that collision on a real e2e run.
- literal snippets pulled from **that run's own** `spec.yaml` (`gold_solution`,
  `evaluation_data.dataset`, and any resource `location` that itself points at `eval_data`/`gold`)
  -- catches verbatim content leaks the generic markers alone would miss. A resource's `location`
  is otherwise *not* treated as forbidden, since it's routinely the agent's own legitimate mount
  path or connection string (e.g. a task's DB resource), not a secret.
- content-hash identity against the task's real `evaluation_data.dataset`/`gold_solution.path`
  files on disk -- catches an exact-copy leak mounted under an innocuous name/path with no marker
  words in its content, regardless of filename.

```bash
# against a real trace, auto-resolving the task from the trace's own events
.venv/bin/python scripts/detect_task_leakage.py runs/<trace_id>

# skip task_id resolution and point straight at a spec.yaml (useful for an ad-hoc trace)
.venv/bin/python scripts/detect_task_leakage.py runs/<trace_id> --spec-path tasks/<task_id>/spec.yaml

# print findings without writing runs/<trace_id>/leakage_report.json
.venv/bin/python scripts/detect_task_leakage.py runs/<trace_id> --no-write
```

Writes `leakage_report.json` into the trace dir (unless `--no-write`) and exits `1` if anything
was flagged, `0` if clean. **Wired into `run_tasks.sh`**: after each row with
`--enable-observability` on, the batch runner runs this against that row's trace automatically and
fails the row (`LEAK`, in the summary) if anything is flagged -- a detector-side error only warns,
so an infra hiccup in the checker can't fail an otherwise-good run. See
`tests/scripts/test_detect_task_leakage.py` for worked examples of clean vs. leaky traces.
