# Generic human-baseline template

Task-agnostic replacement for the workflow demonstrated in [`../pilot/`](../pilot/), which is now
**legacy** (order_to_cash-specific, and targets an older in-process solution shape that predates
the current artifact-deploy contract). Use `template/` for every new attempt on any task; do not
edit or extend `pilot/`.

See [`../PROTOCOL.md`](../PROTOCOL.md) for the full protocol this implements (cross-assignment
rules, cell prioritization, run procedure, trajectory-parity artifact requirements).

## Quickstart

You'll work in two places: **this repo** (to run setup/scoring commands) and a **standalone folder
outside the repo** (to write your actual solution in). Don't open Claude Code in this repo for the
solution — only in the standalone folder. See "Why two workspaces" below for why.

All commands run from `human_baseline/template/` in this repo, unless marked otherwise. The
`Makefile` wraps every script below so you don't retype `--task`/`--artifact-dir` each time:

```bash
# 1. Create the attempt (pick a task_id from tasks/, e.g. order_to_cash or airline_rebooking)
make attempt TASK=<task_id> LEVEL=<L1|L2|L3> BASELINER=<your_name>
# Prints attempt_id=<id> and remembers it -- every later `make` call below acts on this attempt
# automatically (pass ATTEMPT=<other_id> explicitly only if you need to switch to a different one).

# 2. Build your standalone workspace (outside the repo) and record its path in config.yaml
make standalone
#    -> creates artifact/ (edit this), sop/ (reference)

# 3. Bring the task's live environment up (DB, tool API, etc.) -- same as what a real agent gets
make up
```

Now **open Claude Code / your editor rooted at the standalone folder** (`make author` prints its
path) and start writing `artifact/Dockerfile` + `artifact/server.py`. Check the task's
`spec.yaml` (`interface_design`, `resources`, `task_runner.agent_env`) for exactly what your
server must implement and what resources you have.

While coding, test against the real environment as often as you like (back in the repo):

```bash
# 4. Build + run your CURRENT artifact against the live task-env, get back a URL to curl
make dev-deploy
```

Re-run step 4 after every edit — it rebuilds and redeploys, so this is your normal edit/test loop.

Whenever you need to clarify requirements, elicit from the task's user-sim persona (also from the repo):

```bash
python elicit.py --task <task_id> --persona <l1|l2|l3> --attempt <attempt_id>
```
This talks to the live user-sim service (not answer-key material) and logs to the attempt's
`logs/transcript.md`. `make elicit PERSONA=<l1|l2|l3>` is the equivalent `make` shortcut.

When you want a real progress score (checkpoints) or you're done (final score):

```bash
make checkpoint MARK=1h LIMIT=1    # repeatable, scores one sample: compatibility test
make final-score                   # scores everything, no limit -- the number that counts
```

When fully done, tear everything down:

```bash
make down
```

`make help` lists every target. Prefer running the raw Python commands instead of `make`? Each
target is a one-line wrapper — see "Running an attempt end-to-end" below for the equivalent
`python ...` invocation behind every target, and for the elicitation/reflection steps `make`
doesn't shortcut further (`make elicit PERSONA=l1`, then fill in `reflection.md` by hand).

## The solution contract

Every task in `tasks/` is scored the same way, regardless of environment shape (Postgres DB,
in-memory store, no DB at all): the deliverable is an **artifact directory** containing at least:

```
artifact/
├── Dockerfile     # CMD must launch a server bound to 0.0.0.0:$PORT ($PORT is injected at deploy time)
├── server.py      # or app.py / main.py -- whichever the code uses
└── ...            # any other code/deps the server needs
```

The server must implement:

| Endpoint | Contract |
|---|---|
| `GET /health` | Returns HTTP 200 when ready. |
| `POST /respond` | Body `{"messages": [...]}` (OpenAI-style running conversation) → `{"choices": [{"message": {"role": "assistant", "content": ...}}]}`. |

This is the exact contract `appsci_bench/orchestrator/deployer.py`'s `DockerArtifactDeployer`
validates and `appsci_bench/orchestrator/eval_artifact.py` deploys + scores — the same code path
the real BenchmarkOrchestrator uses after an appsci agent calls `deploy_artifact` + `complete`
(see `docs/task_runner.md` and `docs/benchmark-orchestrator-design.md`). A human-baseline artifact
scored through `eval_artifact.py` is therefore evaluated **identically** to an agent's artifact —
no separate grading path, no task-specific scoring code to write.

Some tasks also require specific env vars to reach their tool-API (e.g. airline_rebooking's
`AIRLINE_TOOLS_URL`/`AIRLINE_TOOLS_TOKEN`) — these are injected automatically by the deployer from
the task's `spec.yaml` (`task_runner.agent_env`); the artifact just needs to read them from its own
environment. Check `tasks/<task_id>/spec.yaml` directly for these var names and the task's
`interface_design` field.

## Why two workspaces

1. **This repo** (`appsci-bench`) — where the task-env actually runs. `new_attempt.sh`,
   `elicit.py`, `checkpoint.py`, `score_final.py`, `dev_deploy.py`, and `python -m
   appsci_bench.orchestrator.eval_artifact up/down/eval` all run from here, because standing up a
   task's containers means calling the real orchestrator's `appsci_bench.task_runner.TaskRunner`
   directly against `tasks/<task_id>/` — same `docker compose` invocation, same bundling, same
   setup script, whatever the real agent gets. There is deliberately no copy of `environment/`,
   `docker-compose.yml`, or any Dockerfile anywhere else: they never contain answer-key material
   (that lives only in `eval_data/`, `evaluators/`, and `seed_data/fixture/labels.csv`), so there is
   nothing to protect by relocating them. **You never open an editor/Claude Code session rooted in
   the repo** — only `spec.yaml` and `environment/` get read, by scripts, not by you.
2. **A standalone workspace outside the repo** (default `~/Documents/baseline-work/<attempt_id>/`)
   — built by `setup_standalone.py`, containing only what you author: an `artifact/` scaffold
   (Dockerfile + server.py stub) and a copy of `environment/sop/` if the task has one (the same
   docs the real agent sees at `/sop`). **Root Claude Code here** for the actual solution-authoring
   work. It never contains `eval_data/`, `evaluators/`, `gold/<task_id>/`, `seed_data/`, or any
   task-env source code — there is nothing task-specific to leak because nothing beyond the
   artifact scaffold and the public SOP is copied here at all.

## Running an attempt end-to-end

All commands below run from `human_baseline/template/` unless noted.

**1. Create the attempt:**
```bash
./new_attempt.sh <task_id> <ambiguity_level> <baseliner>
# e.g. ./new_attempt.sh order_to_cash L3 P5
```
Creates `human_baseline/attempts/<attempt_id>/` with `config.yaml`, `logs/`, `reflection.md`.

**2. Build the standalone workspace** (still from the repo):
```bash
python setup_standalone.py --task <task_id> --dir ~/Documents/baseline-work/<attempt_id>
```
Writes `artifact/` (scaffold to edit) and `sop/` (if the task has one). Update `standalone_dir` in
the attempt's `config.yaml` to match.

**3. Bring the task environment up** (from the repo — this is `TaskRunner.start()` under the hood,
identical to what happens before a real agent run):
```bash
python -m appsci_bench.orchestrator.eval_artifact up --phase build --task <task_id>
```
`--phase build` is not optional and it matters. A task environment exists in two versions: the **build**
one, holding only the records no eval case touches, and the **eval** one, holding every record. An agent
under test only ever sees the build one, so a baseline attempt must author against the build one too —
otherwise you are solving the task with the scored data in front of you, and your attempt is not
comparable to an agent's. See [`docs/task_env_split.md`](../../docs/task_env_split.md).

**4. Author the solution.** Open Claude Code / your editor rooted at the standalone dir. Edit
`artifact/Dockerfile` and `artifact/server.py` (replace the stub's `respond()` logic) — check
`tasks/<task_id>/spec.yaml`'s `interface_design` for the exact contract. Log start time in the
attempt's `logs/time_log.yaml` by hand.

**5. Test against the live task-env while coding** (from the repo, any time — this is the
dev-loop step, not scoring):
```bash
python dev_deploy.py --task <task_id> --artifact-dir ~/Documents/baseline-work/<attempt_id>/artifact
```
Builds and runs your CURRENT `artifact/` on `bench-network` next to the task-env from step 3 —
reachable at the exact same container-DNS addresses (`db:5432`, `airline-workspace-1:8080`, ...)
the deployed artifact gets in a real run or a real eval, via the same `DockerArtifactDeployer` those
paths use. Prints a host-reachable URL and a ready-to-run `curl` for `/respond`. Re-run it after
every edit — it's idempotent, so this command IS the edit/test loop. Stop the dev container with
`--stop` when you're done for now; it never touches scoring or `logs/score_checkpoints.jsonl`.

**6. Elicit requirements** whenever needed (from the repo — this talks to the live user-sim
service, not answer-key material):
```bash
python elicit.py --task <task_id> --persona <l1|l2|l3> --attempt <attempt_id>
```
Logs to the attempt's `logs/transcript.md`.

**7. Checkpoints** at 1h/2h/3h30/end (PROTOCOL.md section 7) — real scores, not placeholders. Since
step 3 already brought the task-env up, pass `--attach` so each checkpoint reuses it instead of
paying setup cost every time:
```bash
python checkpoint.py --task <task_id> --attempt <attempt_id> \
    --artifact-dir ~/Documents/baseline-work/<attempt_id>/artifact --mark 1h --limit 5 --attach
```
Deploys the current artifact and scores it against 5 held-out samples, appending a row to
`logs/score_checkpoints.jsonl`.

**8. Final score** — the number comparable to an agent run:
```bash
python score_final.py --task <task_id> --attempt <attempt_id> \
    --artifact-dir ~/Documents/baseline-work/<attempt_id>/artifact
```
Scores the FULL held-out eval set (no `--limit`) and writes
`human_baseline/attempts/<attempt_id>/logs/final_report.json` plus a browsable `report.html`. This
call brings its own task-env up and tears it down in a `finally` (it does not take `--attach`), so
run step 10 to stop the one from step 3 first if you want a clean handoff — or just let this one
run standalone.

**9. Reflect.** Fill in the attempt's `reflection.md` (~5 sentences + 1–5 difficulty score).

**10. Tear down** the task-env brought up in step 3, and the dev container from step 5 if it's
still running (both from the repo):
```bash
python -m appsci_bench.orchestrator.eval_artifact down --task <task_id>
python dev_deploy.py --task <task_id> --artifact-dir ~/Documents/baseline-work/<attempt_id>/artifact --stop
```
Delete the standalone dir by hand once you're sure you're done — nothing here does it for you.

## Files

- `Makefile` — short `make` targets wrapping every script below (`attempt`, `standalone`, `up`,
  `author`, `dev-deploy`, `dev-deploy-stop`, `elicit`, `checkpoint`, `final-score`, `down`).
  `make attempt` remembers the id it created in `.last_attempt` (gitignored), so every later
  target defaults to it with no `export` step — pass `ATTEMPT=<other_id>` explicitly to switch.
  Once resolved, `task_id`/`standalone_dir` are read straight out of that attempt's `config.yaml`,
  so you never repeat `--task`/`--artifact-dir` either. `make help` lists every target. Purely a
  convenience layer — every target is a one-line call to one of the scripts below.
- `new_attempt.sh` — bootstraps `human_baseline/attempts/<attempt_id>/` (config.yaml, logs/, reflection.md).
- `setup_standalone.py` — builds the standalone authoring workspace: `artifact/` scaffold and
  `sop/`. Does not touch `environment/` or any Dockerfile/compose file — those run from the repo
  via `eval_artifact.py up`/`down`, which call `appsci_bench.task_runner.TaskRunner` directly (the
  same class the real BenchmarkOrchestrator uses), so task-env setup is inherited, not reimplemented.
- `artifact_scaffold/` — the `Dockerfile` + `server.py` + `requirements.txt` stub copied into every
  new standalone workspace's `artifact/` dir.
- `dev_deploy.py` — builds + runs your CURRENT `artifact/` against the live task-env for iterative
  testing (reuses `DockerArtifactDeployer`, the same class `eval_artifact.py` uses to deploy — no
  scoring, no report, just a reachable URL). This is the step that lets you test your code against
  the task's real resources (DB, tool API, ...) the way the agent does, before you ever run eval.
- `elicit.py` — chat with the task's user-sim persona, logging to the attempt's transcript.
- `checkpoint.py` / `score_final.py` — wrap `python -m appsci_bench.orchestrator.eval_artifact eval`
  (checkpoint passes `--limit`, score_final does not) so scoring is real from the start.
- `reflection.md.template` — copied into each new attempt by `new_attempt.sh`.

## Why there's no answer-key exclusion logic here

Earlier drafts of this template mirrored `environment/` (Dockerfile, docker-compose.yml, bundle
sources) into the standalone dir and took `--exclude` globs to keep answer-key files out of that
copy. That mirroring step is gone: the task-env now always runs from the repo via `TaskRunner`
(through `eval_artifact.py up`/`down`), so nothing under `environment/` is ever copied anywhere,
and there is no second copy to exclude files from. `setup_standalone.py` only ever writes
`artifact/` (the scaffold you edit) and `sop/` — none of which can carry answer-key content, since
`environment/sop/` is the same directory the real agent is allowed to read from at `/sop`.
Answer-key material (`eval_data/`, `evaluators/`, `seed_data/fixture/labels.csv`,
`gold/<task_id>/`) is read only by scripts running in the repo (`eval_artifact.py`, via
`evaluate_deployed()`), never copied anywhere a human or Claude Code session is rooted.
