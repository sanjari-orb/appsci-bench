# Task Environments — shared base & conventions (v0)

This directory holds the **common, cross-task** environment building blocks.
Per-task images, data, and setup scripts are the **task owner's** responsibility
and live under `tasks/<task_id>/environment/`.

> **Adoption is opt-in.** The base image is offered as a shared, well-tested
> starting point. New tasks are encouraged to build on it; existing tasks can
> migrate whenever their owner sees value. Nothing here forces a change to a
> task that already works.

## What lives where

| Concern | Location | Owner |
|---------|----------|-------|
| Shared base image | `environments/base/` | shared |
| Generic smoke check | `environments/base/smoke.sh` | shared |
| Build helper | `environments/build_task_image.sh` | shared |
| Task env scaffold | `tasks/_TEMPLATE/environment/` | shared (copy me) |
| Task Dockerfile / compose / requirements | `tasks/<task_id>/environment/` | task owner |
| Task data (agent-facing + fixtures) | `tasks/<task_id>/environment/data/`, `seed_data/` | task owner |
| Runtime injection + setup orchestration | `appsci_bench/task_runner/` | harness |

## The base image

`environments/base/Dockerfile` builds `appsci-base:v0`: common tooling
(`bash`, `curl`, `git`, `jq`, `tini`), **loguru** for in-container logging, and
`WORKDIR /work` for agent scratch space.

Runtime paths used by tasks are declared in each task's `task_runner.bundle`
config and **created when the TaskRunner extracts the bundle** into the
container (see `appsci_bench/task_runner/runner.py`). Typical destinations:

| Path | Purpose | Populated by |
|------|---------|--------------|
| `/data` | agent-facing resources (docs, samples, KPIs) | TaskRunner (bundle) |
| `/fixture` | raw data loaded into backing stores (e.g. DB) | TaskRunner (bundle) |
| `/scripts` | setup/util scripts run by the TaskRunner | TaskRunner (bundle) |
| `/work` | agent scratch space (writable); often `/work/out` for outputs | image WORKDIR + agent |

The base image does **not** pre-create bundle destinations — extraction creates
them. Do not bake task data into the image.

Task setup scripts may use loguru directly (`from loguru import logger`). Tune
verbosity with `LOG_LEVEL` (default `INFO`). File sinks are opt-in — set
`LOG_DIR` in the task's compose or setup script if needed.

Containers stay alive (`tail -f /dev/null`) so the TaskRunner can
`docker exec` into them.

## Adding a new task environment

**New task** — the task dir must already exist (it holds your `spec.yaml`), then copy the scaffold
*into* it:

```bash
mkdir -p tasks/<task_id>
cp -r tasks/_TEMPLATE/environment tasks/<task_id>/environment
```

1. In `tasks/<task_id>/environment/Dockerfile`, add only your task's system packages and Python
   deps. Keep `FROM appsci-base:v0`. Any task-specific dirs beyond the base contract (e.g. `/docs`)
   are `mkdir`-ed here in your overlay.
2. Add a backing `db` (or other services) to `docker-compose.yml` only if needed.
3. Point your `spec.yaml` `environment.dockerfile` / `environment.docker_compose` at these files,
   and declare `task_runner.bundle` / `setup_script`.

**Existing task** (already has an `environment/` dir — e.g. built before this base): do **not** copy
the scaffold (it would nest into `environment/environment/`). Instead, edit the existing
`environment/Dockerfile` to `FROM appsci-base:v0` and keep only your own apt/pip deps — drop the
duplicated tooling, `mkdir`s, and keep-alive CMD the base now provides.

## Build

```bash
bash environments/build_task_image.sh <task_id>
```

This builds `appsci-base:v0`, then `appsci-task-<task-id>:v0` from the task's
own `environment/Dockerfile`.

## Verify

After building, smoke-check the image against the canonical contract:

```bash
docker run --rm appsci-task-<task-id>:v0 bash /opt/appsci/smoke.sh
```

`smoke.sh` confirms `/work` is writable, Python is available, and loguru is
installed — the base-image contract before TaskRunner bundle injection. After
bundle extraction, validate task-specific paths in the task's `setup_script`.

To smoke-check the base image alone (no task overlay):

```bash
docker build -f environments/base/Dockerfile -t appsci-base:v0 .
docker run --rm appsci-base:v0 /opt/appsci/smoke.sh
```

Migrating an existing task (e.g. `order_to_cash`) to `FROM appsci-base:v0` is the
task owner's opt-in follow-up — it is **not** part of this PR.

See `docs/environment_usage.md` for the full build/run/debug guide.
