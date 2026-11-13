# Task Runner Contract

This document defines the contract between the task runner and the task environment. The task runner is responsible for spinning up a task, transferring data, and providing access to external processes.

**A build+eval run does this twice, once per phase.** The task runner is called with a phase — `build`
or `eval` — and brings up that phase's environment: the build env the AppSci agent explores, and then,
after that one is torn down, the eval env the artifact is scored against. An **eval trial** calls it
**once**, for the eval phase only: it scores an already-built artifact and has no build phase at all.
Both come up from the same compose file
under the same project name, so they share a container name and network alias; the only thing that
differs is which data is bundled in and which script loads it. The flow below is one phase. See
[task_env_split.md](task_env_split.md) for why the split exists and how a task author chooses it.

## Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              TASK RUNNER                                    │
│                         (runs on HOST machine)                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. Bundle Creation          2. Container Setup        3. Data Transfer     │
│  ┌─────────────────┐        ┌─────────────────┐       ┌─────────────────┐   │
│  │ seed_data/      │        │ docker-compose  │       │ docker cp       │   │
│  │   fixture/      │───────▶│ up -d           │──────▶│ bundle.tar.gz   │   │
│  │ environment/    │        │                 │       │ → container     │   │
│  │   data/         │        │ (DB + workspace)│       │                 │   │
│  └─────────────────┘        └─────────────────┘       └─────────────────┘   │
│                                                               │             │
│                                                               ▼             │
│  6. Return Env Dict        5. Run Setup             4. Extract Bundle       │
│  ┌─────────────────┐        ┌─────────────────┐       ┌─────────────────┐   │
│  │ environment:    │◀───────│ docker exec     │◀──────│ docker exec     │   │
│  │  - container    │        │ bash setup.sh   │       │ tar -xzf        │   │
│  │  - compose_file │        │ (init DB, etc.) │       │                 │   │
│  │  - project_name │        └─────────────────┘       └─────────────────┘   │
│  └─────────────────┘                                                        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Important**: The TaskRunner runs on the **host machine** and uses `docker exec` and `docker cp` to interact with containers. The appsci-agent is a separate process that runs in its own container.

## Data Transfer Flow

### Step 1: Bundle Creation (on host)

The task runner creates a tarball containing all data needed by the task:

```
task_data.tar.gz
├── fixture/
│   ├── invoices.csv
│   ├── po_lines_used.csv
│   └── contracts.json
├── data/
│   └── examples.jsonl
└── scripts/
    ├── setup.sh          # Single entry-point script (required)
    ├── 01_schema.sql     # Schema init (called by setup.sh)
    └── 02_load_data.py   # Data loading (called by setup.sh)
```

> Note: agent-facing requirement docs (`kpis.md`, `contract_terms.md`) now live in
> `environment/sop/` and are mounted read-only to the agent container at `/sop` by the
> orchestrator — they are **not** part of this workspace bundle.

Source directories:
- `seed_data/fixture/` → `/fixture/` in container
- `environment/data/` → `/data/` in container
- `environment/db/` → `/scripts/` in container (setup scripts)

### Step 2: Container Setup

```bash
docker-compose up -d
```

This starts:
- **db**: PostgreSQL container with schema initialization
- **workspace**: Task workspace container

The task runner waits for health checks to pass before proceeding.

### Step 3: Data Transfer

```bash
docker cp task_data.tar.gz <workspace_container>:/tmp/
```

### Step 4: Extract Bundle

```bash
docker exec <workspace_container> tar -xzf /tmp/task_data.tar.gz -C /
```

This extracts:
- `/fixture/` - Seed data for DB loading
- `/data/` - Runtime data files for the agent
- `/scripts/` - Setup scripts

### Step 5: Run Setup Script

```bash
docker exec <workspace_container> bash /scripts/setup.sh
```

The setup script is the **single entry point** for all initialization. It should:
- Initialize the database schema
- Load fixture data into database tables
- Set up any other required resources (APIs, knowledge bases, etc.)
- Clean up sensitive fixture files (e.g., `rm -rf /fixture`) before the agent runs

### Step 6: Return Environment Dict

The task runner returns an environment dict with container lifecycle information:

```python
environment = {
    "workspace_container": "o2c-workspace-1",  # Container name for lifecycle management
    "compose_project": "o2c",                  # Project name for stop()
    "compose_file": "...",                     # Compose file path for stop()
    "network_name": "bench-network",           # Docker network name
    "phase": "eval",                           # Which phase this environment serves
    "reused_existing_container": False,        # True = compose attached instead of recreating
    "lane": 0,                                 # Which parallel copy of this environment
}
```

`start()` returns **`None`** instead of a dict when the task declares that this phase needs no
environment (`eval: none`). `stop(None)` is a no-op, so a caller can pass the result straight through.

### Several copies of one environment (`lane=`)

`start(task_id, phase, lane=n)` brings up an independent COPY of a phase's environment. The eval phase
uses it to score several cases at once on a task whose per-case reset would otherwise force them
through one store one at a time (`--eval-lanes N`).

Lane 0 derives the historic, unsuffixed project (`o2c`, container `o2c-workspace-1`) — nothing that
already addresses an environment by name changes meaning. Lane *n* gets `o2c-l<n>`, and since compose
keys containers, volumes and compose-managed networks off the project, a suffixed project **is** the
isolation.

Two things the caller must then supply, because the environment is no longer identifiable from the
task id alone:

- **The address.** Pass the returned `workspace_container` to anything that talks to the environment.
  `TaskConfig.resolved_agent_env(workspace_container=..., tool_token=...)` fills the spec's
  `{workspace_container}` / `{tool_token}` placeholders in for one copy; a spec that writes either
  literally is refused above one copy (`TaskConfig.parallel_env_blockers`).
- **The token.** `env_overlay=` is merged into the environment of the `docker compose up`, so a
  compose file written as `${TASK_ENV_TOKEN:-<default>}` gives each copy its own tool token. The spec
  names that var as `task_runner.lane_token_var`.

`teardown_descriptor(task_id, lane=n)` takes the same lane, and must match the one that was started.

`reused_existing_container` is a **handover diagnostic**. `docker compose up` under an existing project
name **adopts** a container that is already there rather than recreating it, so that container still holds
whatever was loaded into it before and this phase's data lands on top. Every other surface still reports
healthy, which is why the runner compares the workspace container's id across the call.

Because the run still finishes and returns a complete-looking score, the orchestrator reads this flag at
**every** phase start and emits a `task_env.reused` observability event naming the phase. That event is
the durable record: it lands in `runs/<trace>/events-orchestrator.jsonl` and is mirrored to S3, so
`grep task_env.reused` over a sweep's runs names every cell whose environment was not the fresh one it
was supposed to be. The flag is *also* copied into `BenchmarkResult.metadata` as
`task_env_reused_existing_container` for a caller holding the object, but that dict is not the record —
nothing persists or reads it.

It has to be captured per phase rather than read at the end, because the build phase's descriptor is
replaced by the eval one at the handover. **Three starts can report it**, not one: the build phase, the
eval phase after the handover, and an eval trial. An earlier version of this paragraph said the build
phase was the only one where it could be true, reasoning that the handover tears the build env down with
a checked call. That reasoning holds for the handover and not for a trial — a trial has no build phase
and so no preceding teardown, which makes it the case most likely to adopt a leftover container: trials
of one build run back-to-back on the same host, minutes apart.

Without the record, a contaminated cell is indistinguishable from a clean one once the logs have
rotated — and the worker keeps only a log's tail, while this fires near a cell's start.

Whether a phase's data actually *loaded* is not tracked here: a phase's setup script asserts that itself,
and a failing setup script aborts the run, so there is no state left to report.

**Note**: The agent receives its task context (db_url, resources, etc.) via `TaskContext`
built from spec.yaml by the benchmark orchestrator, not from TaskRunner. The environment
dict is only for container lifecycle management (stop()).

## spec.yaml Integration

The task runner reads configuration from `spec.yaml`:

```yaml
environment:
  # The compose file is the only build input: the runner runs `docker compose up -d --build`,
  # so the Dockerfile path belongs in this file's own build block.
  docker_compose: tasks/<task_id>/environment/docker-compose.yml
  resources:
    - { name: oltp_db, type: database, location: "postgres://...", description: "..." }
    - { name: kpis, type: kpi, location: "/sop/kpis.md", description: "..." }

task_runner:
  network_name: bench-network
  workspace_name: <task_abbrev>-workspace
  environments:                      # one block per run phase
    build:
      bundle:
        - { src: environment/build/fixture/, dest: /fixture/ }   # this phase's data only
        - { src: environment/data/, dest: /data/ }
        - { src: environment/db/, dest: /scripts/ }
      setup_script: /scripts/setup.sh   # Single entry-point script (container path)
    eval:
      bundle:
        - { src: environment/eval/fixture/, dest: /fixture/ }    # the task's FULL store
        - { src: environment/data/, dest: /data/ }
        - { src: environment/db/, dest: /scripts/ }
      setup_script: /scripts/setup.sh   # ends by asserting the data landed
```

Write `eval: none` for a task whose eval phase needs no environment at all — every case carries its own
inputs and nothing in the eval path touches a container. Declare it explicitly; an omitted block is
indistinguishable from an author who forgot, and would silently skip the data load.

## Docker Network Architecture

All containers (task environment and appsci-agent) run on a shared Docker network. This enables direct communication between containers using container names as hostnames.

### Network Topology

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              bench-network                                   │
│                                                                              │
│  ┌─────────────────────┐    ┌─────────────────────┐    ┌──────────────────┐  │
│  │ appsci-agent        │    │ artifact container  │    │ task environment │  │
│  │ (builds artifacts)  │    │ (built by agent)    │    │                  │  │
│  │                     │    │                     │    │ ┌──────────────┐ │  │
│  │ mini-swe-agent runs │    │ Serves the artifact │    │ │ db           │ │  │
│  │ bash, docker build, │───▶│ endpoint on :8000   │    │ │ (PostgreSQL) │ │  │
│  │ docker run          │    │                     │    │ │ :5432        │ │  │
│  │                     │    │                     │    │ └──────────────┘ │  │
│  │ Port 8080 (unused)  │    │ Port 8000 (mapped)  │    │                  │  │
│  └─────────────────────┘    └─────────────────────┘    │ ┌──────────────┐ │  │
│           │                          ▲                 │ │ workspace    │ │  │
│           │ TCP (DB queries)         │ HTTP            │ └──────────────┘ │  │
│           └──────────────────────────┼─────────────────┴──────────────────┘  │
│                                      │                                       │
└──────────────────────────────────────┼───────────────────────────────────────┘
                                       │
                    ┌──────────────────┴──────────────────┐
                    │         HOST MACHINE                 │
                    │                                      │
                    │  Orchestrator queries artifact via   │
                    │  http://localhost:<mapped-port>      │
                    │  (translated from Docker DNS URL)    │
                    └──────────────────────────────────────┘
```

### How It Works

1. Docker creates an internal DNS for `bench-network`
2. Container names (`db`, `o2c-workspace`, `o2c-agent`) become hostnames on that network
3. From inside any container on the network, `db` resolves to the PostgreSQL container's IP
4. **Database**: Direct TCP connection via Docker DNS (e.g., `psql postgres://...@db:5432/...`)
5. **Artifact container**: The appsci-agent builds and deploys the artifact as a separate container on the same network
6. **Artifact queries**: The orchestrator (on host) queries the artifact via `http://localhost:<mapped-port>` after URL translation

### Network Setup

Create the shared network (one-time setup):

```bash
docker network create bench-network
```

### Task Environment docker-compose.yml

```yaml
# tasks/order_to_cash/environment/docker-compose.yml
services:
  db:
    image: postgres:15
    container_name: o2c-db
    environment:
      POSTGRES_USER: sandbox
      POSTGRES_PASSWORD: sandbox
      POSTGRES_DB: oltp
    volumes:
      - ./db/01_schema.sql:/docker-entrypoint-initdb.d/01_schema.sql
    networks:
      - bench-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sandbox -d oltp"]
      interval: 5s
      timeout: 5s
      retries: 5

  workspace:
    build: .
    container_name: o2c-workspace
    environment:
      DATABASE_URL: postgres://sandbox:sandbox@db:5432/oltp
    depends_on:
      db:
        condition: service_healthy
    networks:
      - bench-network

networks:
  bench-network:
    external: true
```

## Process Access Patterns

This section clarifies which process accesses what resources and how.

### TaskRunner (Host Machine)

The TaskRunner runs on the **host machine** (not in a container) and uses Docker CLI commands to manage containers:

| Operation | Command | Purpose |
|-----------|---------|---------|
| Start containers | `docker-compose up -d` | Spin up DB + workspace |
| Transfer bundle | `docker cp bundle.tar.gz container:/tmp/` | Copy data into container |
| Extract bundle | `docker exec container tar -xzf ...` | Unpack files in container |
| Run setup | `docker exec container bash /scripts/setup.sh` | Initialize DB/resources |
| Stop containers | `docker-compose down` | Tear down environment |

### appsci-agent (Container on bench-network)

The appsci-agent runs in its **own container** on the same Docker network as the task environment. It builds and deploys artifacts as follows:

| Resource | Access Method | Example |
|----------|---------------|---------|
| **Database** | Direct TCP via Docker DNS | `psql postgres://sandbox:sandbox@db:5432/oltp` |
| **Artifact deployment** | Agent builds Docker image and runs container | `docker build -t o2c-agent . && docker run -d -p 8000:8000 --network bench-network o2c-agent` |

**Important**: The appsci-agent does NOT serve the artifact itself. Instead, it:
1. Builds artifact code (Dockerfile, Python server, etc.)
2. Runs `docker build` to create an artifact image
3. Runs `docker run -p <port>:<port> --network bench-network` to deploy the artifact as a **separate container**
4. Submits the artifact endpoint URL (e.g., `http://o2c-agent:8000/respond`) to the orchestrator

**Note**: The appsci-agent does NOT use `docker exec`. Because it's on the same Docker network as the database, it can connect directly via TCP using the container hostname (`db`).

### Artifact Container (Built by appsci-agent)

The artifact is a **separate container** deployed by the appsci-agent:

| Property | Value |
|----------|-------|
| **Built by** | appsci-agent via `docker build` |
| **Runs on** | `bench-network` (same network as DB, workspace) |
| **Port** | Typically 8000 (must be mapped with `-p` for host access) |
| **Endpoint** | `http://<container-name>:8000/respond` (Docker DNS) |

**URL Translation**: Since the orchestrator runs on the host (not in Docker), it cannot resolve Docker DNS names like `http://o2c-agent:8000`. The orchestrator translates these URLs to `http://localhost:<mapped-port>` by querying `docker port <container> <port>`.

### Evaluator/Benchmark Orchestrator (Host Machine)

The orchestrator runs on the **host machine** and queries the artifact container:

| Step | Action |
|------|--------|
| 1. Receive artifact URL | Agent submits `http://<artifact-container>:8000/respond` |
| 2. Translate URL | Convert Docker DNS to `http://localhost:<mapped-port>/respond` |
| 3. Query artifact | Send evaluation queries to the artifact endpoint |

The artifact endpoint uses a simple completions-style protocol:

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Health check |
| `POST /respond` | Query the artifact with `{"messages": [...]}` |

## Directory Structure Reference

```
tasks/<task_id>/
├── spec.yaml                 # Task specification
├── RESOURCES.md              # Resource documentation
├── TASK_SOP.md               # Standard operating procedure
├── README.md                 # Task overview
│
├── environment/
│   ├── Dockerfile            # Workspace container image
│   ├── docker-compose.yml    # Multi-container setup
│   ├── sop/                  # Agent-facing requirement docs (→ /sop/, mounted by orchestrator)
│   │   ├── kpis.md
│   │   └── contract_terms.md
│   ├── data/                 # Runtime sample data (→ /data/ in container)
│   │   └── examples.jsonl
│   └── db/                   # DB setup (→ /scripts/ in container)
│       ├── setup.sh          # Single entry-point script (required)
│       ├── 01_schema.sql     # Schema init (called by setup.sh)
│       └── 02_load_data.py   # Fixture loading (called by setup.sh)
│
├── seed_data/
│   ├── PROVENANCE.md         # Data provenance
│   └── fixture/              # DB fixture files (→ /fixture/ in container)
│       ├── invoices.csv
│       ├── po_lines_used.csv
│       └── contracts.json
│
├── eval_data/
│   ├── README.md             # Eval data documentation
│   └── samples.jsonl         # Held-out eval samples (NOT transferred to container)
│
├── evaluators/
│   ├── deterministic/        # Automated scoring scripts
│   │   ├── check_flag_accuracy.py
│   │   └── ...
│   └── ai_judge/             # LLM rubric templates
│       ├── reason_quality.jinja2
│       └── design_quality.jinja2
│
├── user_personas/            # Persona definitions
│   ├── l1.yaml
│   ├── l2.yaml
│   └── l3.yaml
│
└── scripts/                  # Task-specific utility scripts
    └── validate_samples.py
```

## Bundle Contents Summary

| Source (host) | Destination (container) | Purpose |
|---------------|-------------------------|---------|
| `seed_data/fixture/` | `/fixture/` | Raw data loaded into DB |
| `environment/data/` | `/data/` | Runtime files for agent |
| `environment/db/*.py` | `/scripts/` | Setup scripts |
| `environment/db/*.sql` | `/docker-entrypoint-initdb.d/` | Auto-run by postgres (via docker-compose volume) |

**Note**: `eval_data/samples.jsonl` is NOT included in the bundle. It's held-out evaluation data used only by the evaluator, never exposed to the agent or the task environment.
