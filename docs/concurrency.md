# Concurrency: can we run multiple tasks at once?

**Short answer: no.** As of this writing the harness runs task runs strictly one at a time. The only
parallelism inside the codebase is *within* a single run (eval cases and objective scoring). Every
batch path is an explicit sequential loop, and several process-wide singletons and shared Docker
resources mean a naive `asyncio.gather` over `BenchmarkOrchestrator.run()` does not merely collide —
it silently corrupts both runs.

This document records what is concurrent today, what blocks concurrent *runs*, and what it would take
to enable them. Line references are to the state of the tree when this was written; treat them as
pointers, not guarantees.

## What IS concurrent today

Two thread pools inside a single run's eval phase, and **three** fan-outs across them -- scoring and the
elicitation diagnostic share the second one:

| Layer | Unit of work | Workers |
|---|---|---|
| `eval/generation.py` | (data point x repeat) | `min(16, max(1, min(len(units), cpu_count or 2)))`, capped by the lane pool's width |
| `eval/scoring.py` (pool supplied by `eval/eval_system.py`) | (record x objective) | same formula, now in `eval/workers.py:pool_size` |
| `eval/elicitation.py` (same pool) | one rubric item | shares the row above |

The eval phase opens that second pool itself and injects it into both consumers, so scoring and the
diagnostic draw on one budget rather than two that cannot see each other. It is sized on whichever fan-out
is larger -- `max(records x objectives, rubric items)` -- because the two scale with different things: the
dataset and the rubric. The diagnostic's warm-up call runs on the eval phase's own thread, outside the
pool, so the first judge call can populate the prompt cache before the rest fan out.

`generation` runs one case at a time per task ENVIRONMENT, because the per-case hooks
(`setup_state` / `read_final_state`) reset and hash a whole store. The orchestrator sets them only
when `evaluation_data.reset_env_per_sample` is truthy **and** a task-env is running.

How many environments there are is now a choice (`--eval-lanes N`, default 1). At the default there is
one, so this reads exactly as the old `workers = 1` clamp did. Above it, `orchestrator/eval_lanes.py`
starts N-1 more copies of the environment -- each its own compose project, its own tool token, its own
artifact container -- and `eval/lanes.py` hands each case one copy for its whole life. Cases still
never share a store; there are just more stores. A task is refused above width 1 unless its spec
templates the environment address (`{workspace_container}`), since a literal one would point every
copy's artifact at copy 0.

Note this is still **within** one run. Two concurrent RUNS remain destructive for the reasons below,
and §8 in particular: lanes give one run several environments, they do not make two runs able to share
a task id.

> **Gap worth noting:** `score_records` takes no hook parameters, so the *scoring* pool is **not**
> serialized for `reset_env_per_sample` tasks — objective functions still run up to 16-wide. This is
> only safe as long as every eval function reads the captured `final_state` rather than live task-env
> state. Worth an audit before adding an objective that queries the env directly.

`max_workers` is overridable but nothing in the repo passes it, so the sizing formula always applies in
practice. `score_records` does now receive a `pool`, which is a different thing: the caller chooses the
executor, the formula still chooses its width.

> **Second-order effect of the sharing:** serializing the scoring pool to address the gap above would now
> also serialize the elicitation judge, which has no reason to be serial -- it only reads a finished
> conversation and never touches the task env. Split the pools, or thread the hooks through, rather than
> narrowing the shared one.

Three further single-worker pools (`generation.py`, `scoring.py`, and `elicitation.py`, each in its
own `_call_with_timeout`) exist purely as per-call timeout wrappers. They add threads, not parallelism.
The elicitation one opens per judge ATTEMPT, so up to three per rubric item across its retries -- still
one at a time per item, and deliberately not the shared pool, which a worker must never re-enter.

## What is sequential

- `scripts/run_tasks.sh:131` — a batch is a plain shell loop that invokes
  `python -m appsci_bench.orchestrator.run` once per task/persona and waits for each.
  (This used to cite `main.py:75` and a `--tasks a,b,c` flag; neither exists any more.)
- `orchestrator/run.py:1238` — `run_all_personas` awaits each persona in turn.
- `scripts/run_tasks.sh:37` — prints `"$total run(s) from $CONFIG (sequential)"`; one
  `python -m appsci_bench.orchestrator.run` per row, no `&`.

`BenchmarkOrchestrator.run` is `async`, but nothing ever gathers two of them.

## Why concurrent runs are not just "unsupported" but actively destructive

Eight failure modes. The first seven are naming, ports and process globals -- all fixable, and
§1-§3 are the ones a reader usually spots first. **§8 is the one that decides how hard this is**,
and it is state rather than naming.

### 1. Fixed container names (breaks same-task concurrency)

No per-run uniquifier anywhere:

| Container | Name | Source |
|---|---|---|
| agent | `appsci-agent-<task_id>` | `core/agent_container.py:132` |
| usersim | `appsci-usersim-<task_id>` | `core/appsci_agent_usersim.py:212` |
| artifact | `appsci-artifact-<task_id>` (or spec-declared) | `task_runner/config.py:88-92` |
| workspace/db | `<project>-workspace-1` / `<project>-db-1`, project derived from spec `workspace_name` | `task_runner/runner.py:119-120, 164-182` |

Worse, `docker rm -f <name>` runs unconditionally **before** `docker run`, as pre-start hygiene —
`agent_container.py:281-291`, `appsci_agent_usersim.py:293-301`, `deployer.py:170-172`. Nothing
distinguishes "stale leftover" from "live container owned by another run", so a second run of the
same task force-kills the first run's containers. The ordering is pinned by tests
(`tests/appsci_bench/core/test_agent_container.py:64`).

Compose teardown compounds it: `task_runner/containers.py:199-207` runs
`docker compose down --volumes --remove-orphans` against the same deterministic project name, so one
run's teardown wipes the other's workspace/DB **volumes**, not just its containers.

### 2. The network-diff reaper (breaks cross-task concurrency too)

All tasks share `network_name: bench-network` — hardcoded in every task spec including
`_TEMPLATE`, and defaulted in `task_runner/runner.py:100`, `core/agent_container.py:48`,
`core/appsci_agent_usersim.py:176`. Network creation is explicitly idempotent-shared
(`task_runner/runner.py:184-208`); nothing is run-scoped.

`_cleanup_artifact_containers` (`orchestrator/run.py:106-150`) exists to reap an artifact container
the agent deployed but never reported (crash/timeout). It does so by diffing the network against a
baseline:

```python
leaked = (_docker_container_names_on_network(network_name)
          - artifact_network_baseline
          - {artifact_container_name})
for name in leaked:
    subprocess.run(["docker", "rm", "-f", name], ...)
```

There is **no ownership filter** — no name prefix, label, compose project, or trace id. The baseline
is captured at `run.py:818`, after this run's own compose env and usersim are already up (so those
are protected) and immediately before `agent_svc.start()`. Any container appearing on
`bench-network` after that point is force-removed in this run's `finally:`, including a *different
task's* workspace, db, agent, usersim, and artifact containers.

Skipped only when: `artifact_network_baseline is None` (any early failure before line 818),
`task_env is None`, or the other run's containers predate the baseline.

### 3. Callback port: silent cross-wiring, not a clean failure

`DEFAULT_CALLBACK_PORT = 9000` (`orchestrator/run.py:410`, `submission_server.py:77`), not exposed on
the CLI — the constructor kwarg is unreachable from `python -m appsci_bench.orchestrator.run`.

`ArtifactSubmissionServer.start()` (`submission_server.py:189-217`) runs
`asyncio.run(self._server.serve())` on a daemon thread, sleeps 0.5s, and unconditionally returns the
URL. **There is no preflight bind.** uvicorn's bind failure `sys.exit(1)`s inside that thread and the
parent never observes it. So a second concurrent run:

1. logs "Submission server ready at ...:9000" with a dead server;
2. its agent's `POST /submit` lands on **run A's** server — either hijacking A's
   `_artifact_endpoint` or receiving a 409 "Artifact already submitted";
3. its `POST /deploy` executes against A's `DeployContext` — A's artifact dir, A's container name.

This is a known bug class in this repo: `tracer/server.py:400-411` does an explicit preflight probe,
with a docstring saying uvicorn's bind "happens inside the background thread below, where a failure
would otherwise be silently swallowed instead of raised." The submission server is an unpatched
instance of the same pattern.

### 4. Process-global evaluator publisher

`shared/observability/publisher.py:23-24` holds a module-level `_publisher` (not thread-local, not a
contextvar). `init_publisher` **closes the incumbent** first (`:309-310`), and `close()` sets
`_enabled = False`, after which `emit()` early-returns. So two runs in one process do not interleave
traces — run B's `_register_eval_publisher` (`orchestrator/run.py:780`) silently *kills* run A's
evaluator event stream, and `_close_eval_publisher` (`:887`) may close whichever publisher happens to
be global at that moment.

The global is deliberate (`run.py:166-172`): eval-path `emit()` calls are made deep in code with no
reference to the run's publisher. The orchestrator's *own* publisher is a local
(`run.py:527-532`) and is unaffected.

Separate OS processes each get their own copy, so the singleton itself is not a cross-process
problem — but the log path is:

### 5. Trace id has second resolution and no PID

`run.py`: `trace_id = compose_run_name(task_id, agent_model=..., persona_path=..., label=...)`,
which is `<task>_[model]_[label]_[persona]_<YYYYMMDD_HHMMSS>` at second resolution. Two runs of the
same task starting within the same second share a trace dir and the same
`get_source_log_path` (`shared/observability/storage.py:45-61`), breaking the one-writer-per-file
invariant the storage layer documents at `publisher.py:104-106`. Mitigating: each emit is a single
newline-terminated `write` + `flush`, and `consolidate_trace` dedups by `event_id` and tolerates
unparseable lines — so the likely symptom is two runs merged into one trace in the UI rather than
corrupt JSON.

### 6. The PID lock guards only the eval-only CLI — in both directions

`orchestrator/eval_artifact.py` keeps a per-task lockfile at
`<tmpdir>/appsci-task-env/<task_id>.eval.lock` (`:72, :156-157`), refusing with rc 2 when a live
holder exists (`:338-347`); liveness is `os.kill(pid, 0)` and stale locks self-clear (`:166-182`).

The check/acquire/release live only inside `_cmd_eval`. `BenchmarkOrchestrator.run()` never reads or
writes the lock, and nothing outside `eval_artifact.py` imports it. Consequences both ways:

- a full orchestrator run will happily stomp a locked task-env;
- an `eval-artifact eval` invocation sees no lock from a live orchestrator run, so its decision table
  (`:118-123`) classifies that healthy env as an **orphan from a crashed run** and tears it down.

### 7. Tracer port retry is real but has a TOCTOU window

`_start_tracer_with_retry` (`run.py:343-371`) tries `[port, port+19]` (20 attempts,
`AGGREGATOR_PORT_RETRY_LIMIT = 20`), and works precisely because `TracerServer.start()` preflights.
Range exhaustion is a deliberate hard stop, re-raised past the run's `try/except` (`:553-557`) and
mapped to `sys.exit(3)`, which `run_tasks.sh:85-92` treats as a batch abort. The one caveat: the
probe socket closes before uvicorn binds, so two orchestrators probing the same port simultaneously
can both pass, and the loser dies silently in its thread.

### 8. A task environment is one shared mutable instance per task id, ADOPTED rather than created

This one was missing from the list until review pushback made it worth stating plainly, and it is the
reason unique container names do not by themselves unblock same-task concurrency. Everything above is
naming, ports and process globals; this is state.

`TaskRunner.start(task_id)` derives the compose project purely from the spec and runs
`docker compose up -d --build`. Compose is **idempotent**, so a second concurrent cell of the same task
does not get its own environment — it silently **attaches to the first cell's running one**. It then
copies its own data bundle in and re-runs the setup script against that live container
(`task_runner/runner.py:120-123`), and on finish runs `down --volumes` on it
(`task_runner/containers.py:199-208`).

For `reset_env_per_sample` tasks it is worse than a collision, because the reset is global. Before each
eval case the orchestrator `POST`s `/admin/reset` into the workspace container and reads
`GET /admin/state_hash` after it (`run.py:359-374`), and both are whole-store operations on one
in-memory DB. Two concurrent cells of one task means B's reset wipes A's mid-conversation state and A's
captured hash is contaminated — **a wrong score, silently**, in the same class as the port-9000 hazard
in §3 rather than a visible failure.

The same cause used to bite one scope down, inside a single run, where `generation.py` forced
`workers = 1` whenever the env hooks were set. **That half is now solved, and how it was solved is the
template for this one.** `--eval-lanes N` gives one run N copies of its environment: each copy is its
own compose project (`<project>-l<n>`, so its own containers and volumes), holds its own tool token,
and has its own artifact container pointed at it. Nothing is shared, so nothing has to be serialised.

Two pieces of that generalise directly to concurrent runs, and both are already in the tree:

- **`TaskRunner.start(task_id, lane=n)`** takes a project suffix, so "one environment per task id" is
  no longer an assumption baked into the runner. A per-RUN suffix is the same mechanism with a
  different key.
- **`task_runner.agent_env` is templated.** `{workspace_container}` and `{tool_token}` are filled in
  per environment (`TaskConfig.resolved_agent_env`), which is exactly the "spec-format change across
  tasks" item 1 below called out as the hard dependency. airline and banking already carry it;
  `TaskConfig.parallel_env_blockers` names any task that does not.

What remains for concurrent runs is the rest of item 1 (run-scoping the agent, usersim and artifact
container names, and the image tag) plus items 2-4. A model sweep holds the task fixed while varying
the model, so the cells we would most want to parallelise are exactly the same-task ones.

### 9. Every task and every phase packs its data bundle to the same host path

Also missing from this list until review pushback, and worth its own entry for the same reason §8 is:
it is **not** fixed by run-scoping Docker identity, so it survives the work everyone assumes covers
this ground.

`BundleManager.create_bundle` names the archive `bundle.tar.gz` (`task_runner/bundle.py:84-85`) and
`TaskRunner` writes it into `tempfile.gettempdir()` (`task_runner/runner.py:327-329`). No task id, no
phase, no run id, no PID -- so one path serves every task and both phases. Two processes whose packing
windows overlap by a few hundred milliseconds and one copies the other's archive into its container:
the wrong task's data, or the other phase's records, loaded with nothing reporting anything. The
container side is safe on its own (`/tmp/bundle.tar.gz` inside a container is private to it); the
collision is entirely host-side.

Note the module's own docstrings expect otherwise -- two of them give the example path as
`/tmp/o2c-bundle.tar.gz`, with a task prefix (`bundle.py:37,61`). Only the implementation is unprefixed.

The per-phase environment split (`docs/task_env_split.md`) does not introduce this, but it doubles how
often the path is written, since a run now packs a bundle twice instead of once.

Fix is one line: put the task id and phase in the name, or use `mkdtemp`. Unlike §8 it needs no spec
change and no environment-per-run seam.

## What it would take to support concurrent runs

Roughly, in dependency order:

1. **Run-scope every Docker identity.** Suffix agent / usersim / artifact container names and the
   compose project with a per-run id. This alone unblocks same-task concurrency and removes the
   `rm -f`-on-start and `down --volumes` hazards.

   Two dependencies that are easy to miss, both found while answering review pushback that "a UUID in
   the container name fixes it" — it does, for the containers, but not for these:
   - **The artifact image tag follows the container name.** `run.py` and `eval_artifact.py` both build
     `image_name = f"{artifact_container}:latest"` and the deployer does `docker build -t <that>`. Two
     concurrent same-task runs otherwise build to one tag and the loser's `docker run` starts the
     winner's image. Uniquify the tag with the name.
   - **The compose project cannot be uniquified alone,** because task specs used to hardcode the
     resulting container name into a URL the agent is handed. **Done, for the two tasks that needed
     it:** `agent_env` values carry `{workspace_container}` / `{tool_token}` and are resolved per
     environment by `TaskConfig.resolved_agent_env`, and `TaskRunner.start` takes a project suffix.
     A task that has not been converted is named by `TaskConfig.parallel_env_blockers` rather than
     silently pointed at the wrong container.
2. **Scope the network reaper to this run's own containers** (label or name-prefix filter), or give
   each run its own network. The current whole-network diff must not survive concurrency.
3. **Preflight-bind the submission server** and either retry like the tracer or take an ephemeral
   port. Silent bind failure is the most dangerous item on this list because it produces wrong
   results rather than an error.
4. **Make the evaluator publisher run-scoped** (contextvar, or thread the publisher through), and add
   a PID/uuid suffix to `trace_id`.
5. **Serialize the scoring pool** under `reset_env_per_sample`, or assert that objectives only read
   `final_state`.
6. Keep the per-task PID lock as the guard against two runs contending for one task env, and have the
   orchestrator path honor it too.
7. **Uniquify the data bundle's host path** (§9) -- task id plus phase in the filename, or `mkdtemp`.
   Independent of everything above and the cheapest item on the list.

Only after 1-4 does `asyncio.gather` over `orchestrator.run` become viable.
