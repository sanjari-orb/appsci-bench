# Environment Usage Guide (v0)

How to build, run, and inspect appsci-bench Docker task environments.

This guide covers the **shared** pieces (base image, conventions, build helper)
and how a **task owner** wires up their own environment. Per-task data and
Dockerfiles belong to the task under `tasks/<task_id>/environment/`; the shared
base and scaffold live under `environments/` and `tasks/_TEMPLATE/environment/`.

## Prerequisites

- Docker installed and running
- Repository cloned locally
- Run commands from the repository root

## Model

- One **shared base image** (`appsci-base:v0`) with common tooling and the
  canonical directory contract.
- One **task image per task**, built from `tasks/<task_id>/environment/Dockerfile`
  which does `FROM appsci-base:v0` and adds only task-specific deps.
- **Data is injected at runtime** by the TaskRunner (`appsci_bench/task_runner/`)
  via bundle + `docker cp` + a task `setup_script` — it is never baked into images.

> **Opt-in:** using `appsci-base:v0` is a recommendation, not a requirement.
> A task can still `FROM python:3.11-slim` directly if its owner prefers; it just
> re-implements the canonical dirs and keep-alive behavior itself. The base
> exists so tasks don't have to.

## Runtime paths (TaskRunner bundle)

These are typical bundle destinations — **not pre-created in the base image**.
The TaskRunner builds a tarball from `task_runner.bundle` in `spec.yaml` and
extracts it with `tar -xzf -C /`, which creates the directories:

| Path | Purpose |
|------|---------|
| `/data` | agent-facing resources (docs, samples, KPIs) |
| `/fixture` | raw data loaded into backing stores (e.g. DB) |
| `/scripts` | setup/util scripts run by the TaskRunner |
| `/work` | agent scratch space (writable); often `/work/out` for outputs |

`appsci-base:v0` includes **loguru** for opt-in in-container logging. Task scripts
can `from loguru import logger`; tune with `LOG_LEVEL` (default `INFO`). File
sinks are opt-in via `LOG_DIR` in compose or setup scripts.

## Build

```bash
# Base + a task image in one step:
bash environments/build_task_image.sh <task_id>

# Or manually:
docker build -f environments/base/Dockerfile -t appsci-base:v0 .
docker build -f tasks/<task_id>/environment/Dockerfile \
  -t appsci-task-<task-id>:v0 tasks/<task_id>/environment
```

## Run a disposable interactive shell

```bash
docker run --rm -it appsci-task-<task-id>:v0 bash
```

## Smoke check (generic)

The base image ships a smoke check for **base tooling only** (writable `/work`,
Python, loguru). Runtime bundle paths are validated in the task's `setup_script`
after TaskRunner extraction.

```bash
docker run --rm appsci-task-<task-id>:v0 /opt/appsci/smoke.sh
# -> "env smoke: OK"
```

Task-specific validation (DB loaded, resources present, API reachable) belongs
to the task's own `setup_script`, run by the TaskRunner after data injection.

Smoke-check the base image alone (no task overlay):

```bash
docker build -f environments/base/Dockerfile -t appsci-base:v0 .
docker run --rm appsci-base:v0 /opt/appsci/smoke.sh
```

## Adding a new task environment

**New task** — the task dir must already exist (it holds your `spec.yaml`), then
copy the scaffold *into* it:

```bash
mkdir -p tasks/<task_id>
cp -r tasks/_TEMPLATE/environment tasks/<task_id>/environment
```

1. Edit `tasks/<task_id>/environment/Dockerfile` — add only your task's system
   packages and Python deps; keep `FROM appsci-base:v0`.
2. Add a `db` (or other backing service) to `docker-compose.yml` only if needed;
   keep the `workspace` service on the external `bench-network`.
3. In `spec.yaml`, set `environment.docker_compose` to the compose file (its own
   `build:` block names the Dockerfile) and declare `task_runner.bundle` + `setup_script`.

**Existing task** (already has `environment/` — e.g. `order_to_cash` on `main`):
do **not** copy the scaffold (that would nest `environment/environment/`).
Instead, edit the existing `environment/Dockerfile` to `FROM appsci-base:v0` and
keep only task-specific apt/pip deps — drop duplicated tooling, `mkdir`s, and
keep-alive CMD the base already provides. That migration is the task owner's
opt-in follow-up; this PR only ships the shared base and template.

## What belongs to whom

| Shared (this PR / `environments/`) | Task owner (`tasks/<id>/environment/`) |
|------------------------------------|----------------------------------------|
| Base image + tooling + smoke check | Task Dockerfile deps + bundle config |
| Generic smoke check | Task `setup_script` / data loaders |
| Build helper + scaffold | `docker-compose.yml` backing services |
| Conventions & docs | Agent-facing data + fixtures |

Do **not** put hidden benchmark assets in task images: held-out eval samples,
non-agent-facing SOP content, or user-persona files used by the simulator.

## Troubleshooting

- **`appsci-base:v0` not found:** build the base first (the helper does this).
- **`network bench-network not found`:** `docker network create bench-network`
  (the TaskRunner creates it automatically whenever it starts a phase, and an eval trial of a task whose
  eval phase declares `none` creates it explicitly, since no phase start happens there to do it).
- **Permission issues writing outputs:** ensure any host-mounted dirs exist.
- **Stale image behavior:** rebuild with `--no-cache` when debugging layers.
