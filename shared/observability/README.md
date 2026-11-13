# observability -- tracing for build + eval

A trace stitches together every process in a benchmark run -- the build agent, mini-swe,
the user-sim, the evaluator, and the orchestrator -- into one timeline the [tracer](../../appsci_bench/tracer/README.md)
dashboard renders. This README is the **guidance + contract**: how it works, how to instrument
a component, and what a submitter bringing their own agent framework has to satisfy to participate.

- Read side / dashboard: [`appsci_bench/tracer`](../../appsci_bench/tracer/README.md) -- first-time S3 setup, troubleshooting and the CLI reference

## Design goals

1. **Cross-process / cross-container.** The components run as separate processes -- some in
   separate Docker containers. The trace has to span the container boundary with no message bus.
2. **Zero overhead when off.** Instrumentation stays in the hot path; every hook no-ops when no
   publisher is active (local runs, tests, un-instrumented submitters).
3. **Never break a real run.** A trace-write failure is logged at debug and swallowed -- an LLM
   call or eval never fails because of observability.
4. **Lean stream, full I/O on demand.** Large LLM prompts/completions live out-of-band; the event
   timeline stays small and untruncated.

## How the boundary is crossed (the core idea)

The runs directory is a **host-absolute path bind-mounted into each container at `/runs`**. Each
writer process appends to **its own** file -- `runs/<trace_id>/events-<source>.jsonl` -- so *one
file has exactly one writer* and no locking or IPC is needed across processes or containers. The
host reader (tracer) merges those per-source files on read. The whole transport is **files on a
shared volume**; there is no network hop in the write path.

```
 agent container            usersim container         orchestrator / evaluator (host)
 ┌──────────────┐           ┌──────────────┐          ┌───────────────────────┐
 │ init_publisher│          │ init_publisher│         │ init_publisher         │
 │ emit(), model_│          │ emit(), model_│         │ build.* / eval.* emits │
 │ call_start/end│          │ call_start/end│         └───────────┬───────────┘
 └──────┬───────┘           └──────┬───────┘                      │
        │ append (own file)        │                               │
        ▼                          ▼                               ▼
  /runs bind mount  ─►  runs/<trace_id>/events-appsci_agent.jsonl
                                    events-usersim.jsonl
                                    events-orchestrator.jsonl
                                    events-evaluator.jsonl
                                    model_calls-<source>.jsonl   (full LLM I/O, out-of-band)
        │
        │  consolidate_trace()  -- merge, dedup by event_id, append-only, never reordered
        ▼
  runs/<trace_id>/events.jsonl  ─►  TracerServer (poll + SSE)  ─►  dashboard
```

## The agent tree (span model + render)

Every actor in a run -- the AppSci agent, mini-swe, the user-sim, the evaluator -- is treated as an
**agent** with a **role-label `name`**, and the trace is a **tree linked purely by `parent_span_id`**.
A parent agent *dispatches* a sub-agent by calling a tool; the sub-agent's spans nest under that
dispatch, to arbitrary depth (a dispatched agent may itself dispatch).

**Who parents what (the write-side contract that makes the tree):**

```
agent.turn_start                     span=TURN,  parent=None      name="AppSci agent"
├─ model.request/response            span=CALL,  parent=TURN      caller="agent_loop"
└─ agent.tool_invocation (+result)   span=TOOL,  parent=TURN      ← a dispatch
   ├─ [mini-swe] swe.task_start/complete   span=TASK, parent=TOOL   name="mini-swe"
   │   ├─ swe.bash_command / swe.bash_result / swe.file_write    parent=TASK
   │   └─ model.request/response (caller="mini_swe")  parent=TASK
   └─ [user-sim] usersim.request / usersim.response   parent=TOOL   name=<persona role>
       └─ model.request/response (caller="user_sim")  parent=TOOL
```

Key rules a component must follow so it slots into the tree:
- The **turn span** parents the turn's own agent-loop model call *and* its tool invocations. (Historically
  these were emitted parentless; `agent.py` now threads the turn span into both.)
- A tool that hands off to a sub-agent sets `context.parent_span_id = <tool_invocation span>` before
  calling the tool; the sub-agent parents its spans under it. Across a container boundary (user-sim) the
  span is forwarded as the `X-Obs-Parent-Span` header on `/ask`.
- A start/end pair (`swe.task_start`+`swe.task_complete`, `agent.tool_invocation`+`agent.tool_result`)
  **reuses one span_id**; a fresh emit gets a fresh span.

**Read side (`tracer/templates/dashboard.html`).** `rebuildBuildTree()` indexes every event by
`span_id`, collapses each `model.request`/`model.response` pair into a `model_call` node, classifies
each span (`turn` / `dispatch` / `subagent` / `model_call` / `event`), links children by
`parent_span_id`, and flattens the tree to one indented pre-order row list. A **single recursive row
template** renders all kinds, so nesting is unlimited and there is one `.mcall` card, not four copies.
`agentName(node)` shows the recorded `name`, falling back to a source→label map for old traces. It is
rebuilt from scratch each SSE batch, so it is robust to child-before-parent arrival; an unresolved
parent makes the node a root (old parentless traces degrade to a flat-but-grouped list).

This model is why a dispatched sub-agent renders **once** as "→ dispatched `<name>`" with its Q&A /
steps nested underneath (rather than as separate agent-side + sub-agent-side rows), and why a turn's
one-line digest is its own agent-loop reasoning rather than a neighbouring turn's echoed tool result.

## Module map

- `events.py` -- the `ObservabilityEvent` dataclass (the wire record) + the `EventType` / `Source`
  enums (the fixed vocabulary). `(str, Enum)` members serialize to their `.value` (`"build.start"`,
  not `"EventType.BUILD_START"`).
- `publisher.py` -- `EventPublisher` (one per writer; appends one flushed JSON line per event to its
  per-source file) + a module-level singleton. `init_publisher(source=...)` reads config from args or
  env vars; `emit(...)` / `get_publisher()` are safe to call anywhere and **no-op when uninitialized**.
  `span()` is a context manager that emits a start/end pair.
- `model_io.py` -- full LLM I/O capture. `model_call_start()` / `model_call_end()` wrap a call: they
  write a `ModelCall` record to `model_calls-<source>.jsonl` and emit only lightweight
  `MODEL_REQUEST` / `MODEL_RESPONSE` events (a preview + a `ref` = the `call_id`). `model_call_context(**tags)`
  is a `ContextVar` that stamps every call in scope (e.g. the eval case) and propagates across thread pools.
- `storage.py` -- path helpers + `consolidate_trace()` (merge per-source logs into append-only
  `events.jsonl`, dedup by `event_id`; line number = stable monotonic `id`) + `read_events(since_id=...)`
  (the polling / SSE cursor) + `list_traces()`.
- `redact.py` -- scrubs credential-shaped env values out of captured LLM I/O **at write time**, so a
  secret the agent echoed into its own output (an `env` dump in a shell command, say) never reaches
  disk or the mirror. On by default; `OBSERVABILITY_REDACT=0` disables it. Write time rather than
  upload time keeps the mirror a byte-for-byte copy of the local run.
- `s3_store.py` -- the **S3 mirror** (see [below](#s3-mirror-sharing-runs)): `upload_trace()` /
  `download_trace()`, plus `S3Config` and the credential handling that survives STS rotation.
  boto3 is imported lazily, so this module is safe to import with no AWS setup.

## The wire contract (language-neutral)

Everything above the file layer is Python convenience. The **contract a component must satisfy** is
just files + env vars, so a non-Python or third-party agent can participate without importing this package.

**Config, via environment (injected into agent/usersim containers by the orchestrator):**

| Env var | Meaning |
|---|---|
| `OBSERVABILITY_RUNS_DIR` | Base runs dir as this process sees it (`/runs` inside containers). Absent = observability off. |
| `OBSERVABILITY_TRACE_ID` | Per-run correlation id, e.g. `order_to_cash_20260720_023648`. |
| `OBSERVABILITY_TASK` | Task id, denormalized onto every event. |
| `OBSERVABILITY_AGENT_NAME` | Optional. Human-readable **role label** of the agent this process is (e.g. `AppSci agent`, the user-sim persona role). Stamped as `name` on every event this writer emits; a per-emit `name` can override it (mini-swe, running in the agent process, tags its own events `mini-swe`). Absent = `name` is null and the dashboard falls back to a source→label map. |
| `OBSERVABILITY_LOG_EVENTS` | Optional. Truthy (`1`) = the publisher logs one INFO line per emitted event so the stream is watchable in the console / docker logs; unset = emits log at DEBUG only. The orchestrator's `--verbose` flag sets it for the orchestrator process and forwards it into the agent + user-sim containers. |

When `OBSERVABILITY_RUNS_DIR` / `OBSERVABILITY_TRACE_ID` are unset, do nothing -- that is how "off" is signalled.

**Event file:** append newline-delimited JSON to `<runs_dir>/<trace_id>/events-<source>.jsonl`, one
writer per file, flush per line. Each line:

```json
{"source":"appsci_agent","event_type":"agent.turn_start","trace_id":"<id>",
 "task_id":"<task>","event_id":"<uuid>","timestamp":"<ISO8601 UTC>",
 "span_id":"<uuid>","parent_span_id":"<uuid|null>","name":"<role label|null>","payload":{...}}
```

`span_id` / `parent_span_id` give the hierarchy (this is the OpenTelemetry trace/span shape); see
[The agent tree](#the-agent-tree-span-model--render) for how they must be wired. `event_id`
must be unique -- the merge dedups on it. `name` is the optional role label (§ above).

**Model I/O (optional but recommended):** append the full call to
`<runs_dir>/<trace_id>/model_calls-<source>.jsonl` with fields
`call_id, source, caller, model, messages, content, tool_calls, usage, duration_ms, span_id,
parent_span_id, timestamp, context`, and emit `model.request` / `model.response` events whose
`payload.ref` equals the `call_id` (plus a short `preview`). The dashboard loads the store on expand.

**Source vocabulary:** `appsci_agent`, `usersim`, `mini_swe_agent`, `task_env`, `orchestrator`,
`evaluator`, `artifact_usersim` (the eval-time user sim that drives the finished artifact -- a
separate source from the build-time `usersim`, and from the `evaluator` whose process it shares, so
its model calls can be counted and priced on their own). **Event types:** `agent.*`, `usersim.*`,
`swe.*`, `build.*`, `eval.*`, `model.*`
(see `events.py`). Unknown sources and event types are **still merged and rendered generically** --
only known types get bespoke UI.

## Instrumenting a component (Python)

```python
from shared.observability.publisher import init_publisher, emit
from shared.observability.events import EventType, Source
from shared.observability.model_io import model_call_start, model_call_end, model_call_context

init_publisher(source=Source.APPSCI_AGENT, name="AppSci agent")   # reads OBSERVABILITY_* env; returns None if off

emit(EventType.AGENT_TURN_START, payload={"turn": n})   # no-op if uninitialized; `name` inherited

# a sub-agent running in this process tags its own events (source + name override) and parents them
# under the dispatch span so the tree nests correctly:
emit(EventType.SWE_BASH_COMMAND, payload={...}, source=Source.MINI_SWE_AGENT,
     name="mini-swe", parent_span_id=task_span)

mc = model_call_start(messages, model=model, caller="agent_loop", parent_span_id=turn_span)   # None if off
resp = llm(messages)
model_call_end(mc, content=resp.text, tool_calls=resp.tool_calls, usage=resp.usage)

with model_call_context(data_point_id=dp, repeat_index=i):   # tags every call in scope
    ...
```

Reference call sites: `appsci_agent/core/agent.py`, `appsci_agent/tools/mini_swe_agent.py`,
`appsci_bench/eval/eval_system.py` (evaluator), and the user-sim.

## Extensibility to other agent frameworks

**The core is framework-agnostic.** The wire format is JSON lines and the transport is a shared
filesystem + three env vars, so any process in any language can participate by writing the files
above. The merge/read side keys only on `event_id` / `timestamp` / `source` -- it does not care who wrote them.

**Current couplings (why it is not yet turnkey for arbitrary submitters):**

1. **Instrumentation is hand-placed Python.** The rich trace comes from explicit calls inside
   `appsci_agent`. A different framework (LangGraph, CrewAI, a non-Python agent) gets nothing
   automatically -- it must call these hooks or reimplement the JSONL writer against the contract above.
2. **Fixed event taxonomy + bespoke UI** for `agent.*` / `swe.*` / `usersim.*`. A framework's own
   concepts (planner nodes, sub-agents, retrieval) have no first-class type; they render generically.
3. **Model I/O assumes an OpenAI-ish shape** (`messages` + `content` / `tool_calls`).
4. **Transport assumes a shared `/runs` volume**, i.e. the agent runs as a container the orchestrator
   controls. A **remote / hosted endpoint** (which the "bring your own framework, hand back an
   executable endpoint" submission model allows) has no shared filesystem -- the file transport does not reach it.

**To make it turnkey**, the additions would be: a documented language-neutral emitter for non-Python
agents (this contract is the start); an **HTTP / OTLP push transport** for remote endpoints that cannot
share `/runs`; a generic/extensible event taxonomy; and ideally an auto-instrumentation shim (wrap the
LLM client) so submitters get model I/O for free. Because the schema is already OpenTelemetry-shaped
(`trace_id` / `span_id` / `parent_span_id`), mapping onto **OTel GenAI semantic conventions** would
provide framework-agnostic auto-instrumentation *and* a remote transport with far less bespoke code.
