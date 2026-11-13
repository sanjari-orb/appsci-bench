# Benchmark Orchestrator Design

The orchestrator runs a single benchmark evaluation end-to-end: launches containers, coordinates the agent build phase, receives artifact submissions, and cleans up.

## Components

| Component | Container | Purpose |
|-----------|-----------|---------|
| **Submission Server** | *(on host, port 9000)* | Receives agent artifact submissions |
| **Task Environment** | `o2c-workspace-1`, `o2c-db-1` | DB + workspace (managed by TaskRunner) |
| **Appsci Agent User Sim** | `appsci-usersim-<task_id>` | Simulates stakeholder via REST API |
| **AppSci Agent** | `appsci-<task_id>` | Builds the artifact agent |
| **Artifact Agent** | *(inside appsci container)* | The deliverable; queried by evaluator |

All containers share `bench-network` for inter-container communication.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              HOST MACHINE                                        │
│                                                                                  │
│  ┌──────────────────────────────────────────────────────────────────────────┐   │
│  │                    BenchmarkOrchestrator (Python)                         │   │
│  │                                                                           │   │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │   │
│  │  │ Submission      │  │ AppsciUserSim   │  │ AgentContainer  │           │   │
│  │  │ Server (:9000)  │  │                 │  │ Manager         │           │   │
│  │  └────────▲────────┘  └────────┬────────┘  └────────┬────────┘           │   │
│  │           │                    │                    │                    │   │
│  └───────────┼────────────────────┼────────────────────┼────────────────────┘   │
│              │                    │                    │                        │
│              │ /submit + /deploy  │ docker run         │ docker run             │
│              │                    ▼                    ▼                        │
│  ┌───────────┴────────────────────────────────────────────────────────────┐    │
│  │                        Docker Network: bench-network                    │    │
│  │                                                                         │    │
│  │   ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐      │    │
│  │   │ o2c-db-1        │   │ o2c-workspace-1 │   │ appsci-usersim  │      │    │
│  │   │ (PostgreSQL)    │   │ (task files)    │   │ (FastAPI :8000) │      │    │
│  │   └─────────────────┘   └─────────────────┘   └────────▲────────┘      │    │
│  │                                                        │               │    │
│  │                              HTTP /ask                 │               │    │
│  │                         ┌──────────────────────────────┘               │    │
│  │                         │                                              │    │
│  │   ┌─────────────────────┴───────────────────────────────────────┐     │    │
│  │   │ appsci-xxx (Agent Container)                                 │     │    │
│  │   │                                                              │     │    │
│  │   │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐   │     │    │
│  │   │  │ appsci_agent │───▶│ mini-swe-    │───▶│ Artifact     │   │     │    │
│  │   │  │ run.py       │    │ agent        │    │ (port 8080)  │   │     │    │
│  │   │  └──────────────┘    └──────────────┘    └──────────────┘   │     │    │
│  │   │         │                                                    │     │    │
│  │   │         │ complete tool                                      │     │    │
│  │   │         │ POSTs to /submit ──────────────────────────────────┼─────┘    │
│  │   │         ▼                                                    │          │
│  │   └──────────────────────────────────────────────────────────────┘          │
│  │                                                                              │
│  └──────────────────────────────────────────────────────────────────────────────┘
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

## Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           BENCHMARK ORCHESTRATOR                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  1. Start Submission Server      2. Setup Task Environment                   │
│  ┌──────────────────────┐         ┌──────────────────────┐                  │
│  │ ArtifactSubmission   │         │ TaskRunner.start()   │                  │
│  │ Server()             │────────▶│ - docker compose up  │                  │
│  │ - FastAPI on :9000   │         │ - transfer bundle    │                  │
│  │ - /submit + /deploy  │         │ - run setup.sh       │                  │
│  └──────────────────────┘         └──────────────────────┘                  │
│                                            │                                │
│                                            ▼                                │
│  3. Start Appsci Agent User Sim   4. Start AppSci Agent                     │
│  ┌──────────────────────┐         ┌──────────────────────┐                  │
│  │ AppsciAgentUserSim   │         │ AppSciAgentContainer │                  │
│  │ - docker run         │────────▶│ Manager              │                  │
│  │ - wait for /health   │         │ - docker run         │                  │
│  │ - return URL + token │         │ - inject env vars    │                  │
│  └──────────────────────┘         │ - mount spec.yaml    │                  │
│                                   └──────────────────────┘                  │
│                                            │                                │
│                                            ▼                                │
│  5. Wait for Submission           6. Agent Build Phase                      │
│  ┌──────────────────────┐         ┌──────────────────────┐                  │
│  │ submission_server    │◀────────│ Agent works:         │                  │
│  │ .wait_for_completion │         │ - talks to appsci sim│                  │
│  │ (timeout=N seconds)  │         │ - explores task env  │                  │
│  └──────────────────────┘         │ - builds artifact    │                  │
│           │                       │ - calls complete                        │
│           │                       └──────────────────────┘                  │
│           ▼                                                                 │
│  7. Stop Appsci Agent User Sim    8. Run Evaluator                          │
│  ┌──────────────────────┐         ┌──────────────────────┐                  │
│  │ usersim.stop()       │         │ HttpArtifact(url)    │                  │
│  │                      │────────▶│ Evaluator.evaluate() │                  │
│  └──────────────────────┘         └──────────────────────┘                  │
│                                            │                                │
│                                            ▼                                │
│                                   9. Cleanup                                │
│                                   ┌──────────────────────┐                  │
│                                   │ agent_svc.stop()     │                  │
│                                   │ task_runner.stop()   │                  │
│                                   │ submission_server.stop│                  │
│                                   └──────────────────────┘                  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Implementation

### Key Files

| File | Purpose |
|------|---------|
| `appsci_bench/orchestrator/run.py` | `BenchmarkOrchestrator` - main coordinator |
| `appsci_bench/orchestrator/submission_server.py` | `ArtifactSubmissionServer` - FastAPI server for submissions |
| `appsci_bench/core/appsci_agent_usersim.py` | `AppsciAgentUserSimService` - manages usersim container |
| `appsci_bench/core/agent_container.py` | `AppSciAgentContainerManager` - manages agent container |
| `appsci_bench/eval/artifact.py` | `HttpArtifact` - queries artifact via HTTP |
| `appsci_agent/run.py` | Agent container entry point |
| `appsci_agent/tools/complete.py` | Tool that POSTs to submission server |

### Dockerfiles

| File | Image Name | Purpose |
|------|------------|---------|
| `appsci_bench/Dockerfile.appsci_agent_usersim` | `appsci-bench-usersim` | Appsci agent user sim service |
| `appsci_agent/Dockerfile` | `appsci-bench-agent` | AppSci agent |

## Step-by-Step Implementation

### Step 1: Start Submission Server

```python
submission_server = ArtifactSubmissionServer(host="0.0.0.0", port=9000)
submit_url = await submission_server.start()
# submit_url = "http://host.docker.internal:9000/submit"
```

The submission server:
- Runs FastAPI in a background thread
- Exposes `POST /submit` (endpoint registration) and `POST /deploy` (host-side artifact deploy)
- Agent POSTs `{"artifact_endpoint": "http://appsci-artifact-{task_id}:8080/respond"}` when done
- `wait_for_completion()` blocks until submission received or timeout

### Step 2: Setup Task Environment

```python
task_runner = TaskRunner(tasks_dir)
task_env = task_runner.start(task_id)
# task_env = {
#     "workspace_container": "o2c-workspace-1",
#     "network_name": "o2c_default",
#     "compose_project": "o2c",
#     ...
# }
```

TaskRunner:
- Runs `docker compose up` for task environment
- Waits for containers to be healthy
- Transfers data bundle (fixture, data, scripts)
- Runs `setup.sh` (creates DB schema, loads data)

### Step 3: Start Appsci Agent User Sim

```python
usersim = AppsciAgentUserSimService(
    task_id=task_id,
    spec_path=spec_path,
    persona_path=persona_path,
    network_name=task_env["network_name"],
)
usersim.start()
# usersim.url = "http://usersim-order_to_cash:8000"
# usersim.token = "<generated bearer token>"
```

Docker command executed:
```bash
docker run -d --name appsci-usersim-{task_id} \
    --network bench-network \
    -v {spec_path}:/spec/spec.yaml:ro \
    -v {persona_path}:/spec/persona.yaml:ro \
    -e APPSCI_USERSIM_TOKEN={generated_token} \
    -e APPSCI_USERSIM_SPEC_PATH=/spec/spec.yaml \
    -e APPSCI_USERSIM_PERSONA_PATH=/spec/persona.yaml \
    -e ANTHROPIC_API_KEY=... \
    appsci-bench-usersim:latest
```

Health check via `docker exec` calling `/health` endpoint.

### Step 4: Start AppSci Agent

```python
agent_config = AppSciAgentContainerConfig(
    task_id=task_id,
    workspace_container=task_env["workspace_container"],
    appsci_usersim_url=usersim.url,
    appsci_usersim_token=usersim.token,
    orchestrator_submit_url=submit_url,
    spec_path=spec_path,
    network_name=task_env["network_name"],
)
agent_svc = AppSciAgentContainerManager(agent_config)
agent_svc.start()
```

Docker command executed:
```bash
docker run -d --name appsci-{task_id} \
    --network bench-network \
    -v {spec_path}:/spec/spec.yaml:ro \
    -e APPSCI_USERSIM_URL=http://appsci-usersim-{task_id}:8000 \
    -e APPSCI_USERSIM_TOKEN={token} \
    -e TASK_WORKSPACE={workspace_container} \
    -e TASK_SPEC_PATH=/spec/spec.yaml \
    -e ORCHESTRATOR_SUBMIT_URL=http://host.docker.internal:9000/submit \
    -e ANTHROPIC_API_KEY=... \
    appsci-bench-agent:latest
```

Note there is **no `/var/run/docker.sock` mount**: the agent container has no access to the Docker
daemon. It authors its artifact and calls `deploy_artifact`, which the host services via
`POST /deploy` (see `e2e_instructions.md`).

### Step 5: Wait for Submission

```python
result = await submission_server.wait_for_completion(timeout=3600)
artifact_endpoint = result["artifact_endpoint"]
# artifact_endpoint = "http://appsci-artifact-order_to_cash:8080/respond"
```

The agent signals completion by calling the `complete` tool:
1. Agent builds and deploys its artifact (via `deploy_artifact`), which records the endpoint
2. Agent calls `complete` (no arguments)
3. `complete` POSTs the recorded endpoint to `ORCHESTRATOR_SUBMIT_URL`
4. Submission server receives POST and unblocks `wait_for_completion()`

### Step 6: Stop Appsci Agent User Sim

```python
usersim.stop()
# Agent is done talking to user, usersim no longer needed
```

### Step 7: Run Evaluator

```python
artifact = HttpArtifact(endpoint_url=artifact_endpoint)
artifact.wait_for_health()

evaluator = Evaluator()
report = evaluator.evaluate(spec, artifact)
```

**Note**: the evaluator system itself (`appsci_bench/eval/`) is feature-complete and unit-tested
(see `appsci_bench/eval/README.md`); what's untested is this orchestrator's end-to-end wiring to
it, since that requires a live Docker build to produce a real `artifact_endpoint` to evaluate.

### Step 8: Cleanup

```python
agent_svc.stop()      # Stop agent container
usersim.stop()        # Stop usersim (if not already)
task_runner.stop(task_env)  # docker compose down
await submission_server.stop()  # Stop FastAPI server
docker rm -f <artifact_container_name>  # Remove the agent-deployed artifact container
docker rm -f <leaked containers>  # Backstop: anything else new on bench-network since agent start
await aggregator_server.stop()  # Stop tracer server (if observability enabled)
obs_publisher.close()   # Close this run's own (BUILD-phase) observability publisher
close_eval_publisher()  # Close the global EVALUATOR publisher registered before evaluate() ran
```

The artifact runs in its own container, deployed **host-side** by the orchestrator's
`POST /deploy` route when the agent calls `deploy_artifact` (the agent has no Docker
socket -- see Step 4) -- the orchestrator never started it as one of its own services,
so it isn't covered by `agent_svc.stop()`. Its name is **spec-declared**
(`task_runner.artifact_container`, defaulting to `appsci-artifact-<task_id>`) and so is
known before the agent even starts, which makes teardown deterministic on every outcome
including crash/timeout. The container-DNS URL the agent reported via `complete`
(`http://<artifact-container-name>:8080/...` -- the deployment contract requires this
form and rejects localhost URLs) is also parsed, and takes precedence if it names a
different container. Removed with `docker rm -f` here.

The spec-declared name covers the normal case, but a container deployed under some
*other* name still wouldn't be caught -- so there's a second, name-agnostic backstop: the
orchestrator snapshots the container names on `bench-network` right before starting
the agent, and after `agent_svc`/`usersim`/`task_runner` have all been stopped above,
diffs the network again. Anything still present that wasn't in the baseline can only
be a leftover from this run's agent (everything else that could have created it has
already been torn down by that point), and gets `docker rm -f`'d too. This is safe for
the sequential-batch case (`run_tasks.sh`) but assumes no *other* orchestrator run is
using the same network concurrently -- see "Known Limitations".

## Environment Variables

### Appsci Agent User Sim Container

| Variable | Example | Purpose |
|----------|---------|---------|
| `APPSCI_USERSIM_TOKEN` | `ab-xyz123...` | Bearer token for auth |
| `APPSCI_USERSIM_SPEC_PATH` | `/spec/spec.yaml` | Path to mounted spec |
| `APPSCI_USERSIM_PERSONA_PATH` | `/spec/persona.yaml` | Path to mounted persona |
| `APPSCI_USERSIM_MODEL` | `claude-opus-4-8` | This sim's model (optional; falls back to `MODEL_NAME`) |
| `MODEL_NAME` | `claude-sonnet-4-20250514` | Shared default model, behind `APPSCI_USERSIM_MODEL` (optional) |
| `MODEL_BASE_URL` | `https://...` | Model gateway base URL (optional, e.g. gateway) |
| `MODEL_API_KEY` | `...` | Model gateway API key (optional) |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | API key for LLM (direct-provider alternative) |
| `OBSERVABILITY_LOG_EVENTS` | `1` | Forwarded from the CLI's `--verbose`/`-v` flag; streams each emit()'d event to this container's own `docker logs` (optional, only forwarded when observability is enabled) |

### Agent Container

| Variable | Example | Purpose |
|----------|---------|---------|
| `APPSCI_USERSIM_URL` | `http://usersim-xxx:8000` | Appsci agent user sim endpoint |
| `APPSCI_USERSIM_TOKEN` | `ab-xyz123...` | Bearer token for usersim |
| `TASK_WORKSPACE` | `o2c-workspace-1` | Workspace container name |
| `TASK_SPEC_PATH` | `/spec/spec.yaml` | Path to mounted spec |
| `ORCHESTRATOR_SUBMIT_URL` | `http://host.docker.internal:9000/submit` | Submission endpoint |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | API key for LLM |
| `AGENT_MODEL` | `claude-opus-4-8` | LLM model (optional) |
| `AGENT_MAX_TURNS` | `40` | Max turns (optional) |
| `APPSCI_LLM_TIMEOUT_S` | `120` | Per-request LiteLLM timeout, seconds (optional, default 120) |
| `OBSERVABILITY_LOG_EVENTS` | `1` | Forwarded from the CLI's `--verbose`/`-v` flag; streams each emit()'d event to this container's own `docker logs` (optional, only forwarded when observability is enabled) |

### Orchestrator Host Process (eval phase)

Step 7 runs the evaluator in the orchestrator's own process, not a container, so these are read from
the host environment.

| Variable | Example | Purpose |
|----------|---------|---------|
| `JUDGE_MODEL` | `anthropic/claude-opus-4-8` | Eval-side LLM judges (optional; falls back to `MODEL_NAME`) |
| `MODEL_NAME` / `MODEL_BASE_URL` / `MODEL_API_KEY` | `anthropic/claude-opus-4-8` | Shared default model + the credentials the judges use |
| `ARTIFACT_USERSIM_MODEL` | `openai/gpt-5.2` (default) | Eval-time **artifact** agent user sim, pinned to the user simulator the v1.0.1 airline submissions declare — see [evaluator_system_design.md](evaluator_system_design.md#model) |
| `OPENAI_API_KEY` | `sk-...` | Credentials for that sim's default model — read by LiteLLM directly, NOT inherited from `MODEL_API_KEY` |

### Python API

```python
import asyncio
from pathlib import Path
from appsci_bench.orchestrator import BenchmarkOrchestrator

async def run_benchmark():
    orchestrator = BenchmarkOrchestrator(
        tasks_dir=Path("tasks"),
        build_timeout=3600,  # 1 hour
    )

    result = await orchestrator.run(
        task_id="order_to_cash",
        persona_path=Path("tasks/order_to_cash/user_personas/l3.yaml"),
    )

    print(f"Status: {result.status}")
    print(f"Scores: {result.scores}")
    print(f"Error: {result.error}")

asyncio.run(run_benchmark())
```

### CLI

```bash
python -m appsci_bench.orchestrator.run \
    --task order_to_cash \
    --persona tasks/order_to_cash/user_personas/l3.yaml \
    --timeout 3600
```

**`-v` / `--verbose`** streams every observability event to this console as it's emitted (in
addition to the usual persisted trace + tracer UI). The orchestrator's and evaluator's own emits
(`build.*` / `eval.*` / judge model I/O) print here directly; the agent, mini-swe-agent, and
usersim containers' emits print to their own `docker logs` instead, via `OBSERVABILITY_LOG_EVENTS`
forwarded into each container (see the Environment Variables tables above). No effect without
`--enable-observability`.

**Observability port conflicts retry, then hard-stop.** If `--enable-observability` is passed
and the aggregator port (default 8080) is already bound by another process, the
orchestrator tries the next port (8081, 8082, ...) up to
`BenchmarkOrchestrator.AGGREGATOR_PORT_RETRY_LIMIT` (20) times, logging a warning with
whichever port it actually lands on -- observability still runs, it's just not
necessarily on the default port. This is deliberately different from silently
disabling observability: the run always ends up with a working, logged dashboard URL,
or it hard-stops. If every port in the range is occupied, the CLI exits immediately
with code `3` and a clear error -- it never falls back to running without
observability. `scripts/run_tasks.sh` treats exit code `3` specially: since a fully
exhausted port range is an environment-level problem that will recur for every
remaining row, it aborts the whole batch immediately rather than recording each
subsequent task as a separate failure.

## Submission Protocol

### Agent → Orchestrator

When the agent finishes building, the `complete` tool POSTs:

```
POST http://host.docker.internal:9000/submit
Content-Type: application/json

{
    "artifact_endpoint": "http://appsci-order_to_cash:8080"
}
```

### Orchestrator Response

```json
{
    "status": "accepted",
    "message": "Artifact endpoint registered. Evaluation will begin."
}
```

## Artifact Interface

The artifact endpoint should implement OpenAI completions format:

### Request
```
POST http://appsci-xxx:8080
Content-Type: application/json

{
    "messages": [
        {"role": "user", "content": "Check invoice INV-1234"}
    ]
}
```

### Response
```json
{
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "{\"flag\": true, \"fault_type\": \"price_mismatch\"}"
            }
        }
    ]
}
```

### Health Check

```
GET http://appsci-xxx:8080/health
→ {"status": "ok"}
```

## BenchmarkResult

```python
@dataclass
class BenchmarkResult:
    task_id: str
    status: str  # "completed", "failed", "timeout"
    scores: dict[str, Any] | None = None
    error: str | None = None
    agent_logs: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

## Known Limitations

1. **Orchestrator-to-evaluator wiring not exercised end-to-end** - the evaluator system itself
   (`appsci_bench/eval/`) is feature-complete and unit-tested; this orchestrator's call into it
   hasn't been run against a real Docker-built artifact in an automated test.

2. **Single task at a time** - The orchestrator runs one task. Parallel execution not implemented.

3. **Network assumption** - Assumes all containers can communicate on Docker network and host can reach containers via published ports (`docker port`). The reverse direction -- the agent container reaching the orchestrator's host-side submit/deploy server at `host.docker.internal` -- **is handled**: `core/agent_container.py` passes `--add-host=host.docker.internal:host-gateway` on every `docker run` for the agent (`HOST_GATEWAY_RUN_ARGS`). Docker Desktop on macOS/Windows injects that hostname implicitly, but Docker Engine on Linux does not, so without the flag the BUILD phase could not complete on Linux at all. The flag is passed unconditionally (verified equivalent on Docker Desktop) and needs Docker >= 20.10.

4. **No artifact manifest** - Current implementation passes artifact endpoint URL directly. The manifest-based approach documented earlier is not implemented.

5. **Artifact-container backstop cleanup assumes one run per network at a time** - The
   network-diff backstop (see "Step 8: Cleanup") removes anything new on
   `bench-network` since the agent started, once the orchestrator's own containers are
   already stopped. This correctly catches orphaned artifact containers even when
   `complete` was never called (crash/timeout/no_submission) -- but if two
   orchestrator runs are using the same network concurrently (not a supported mode per
   limitation #2, but not physically prevented either), one run's backstop could remove
   the other's in-flight artifact container. A fully robust fix would need each
   agent-deployed container labeled with its own run's trace_id at deploy time, so the
   backstop can prune by label instead of by "anything new" -- not implemented here.
