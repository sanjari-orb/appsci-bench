# tracer -- the read side of observability

A local dashboard for benchmark runs: one timeline stitching together the build agent, mini-swe, the
user-sim, the evaluator and the orchestrator, plus the eval report and the produced artifact.

It reads runs from a directory (`--runs-dir`, default `runs/`) **and** from the shared S3 mirror, so
you can open a run someone else produced without them sending you anything.

## Native harness history

For Codex and Claude Code runs, the **Build** tab also shows **Harness history**: the initial
benchmark prompt, assistant messages, tool calls, their responses and exit status, file changes,
and native lifecycle/errors. Tool responses link back to their call IDs; Claude subagent messages
retain their parent tool ID. Long output is expandable. Benchmark service events remain underneath.
The build outcome comes from the orchestrator, not from whether the CLI printed a final message
(successful submission can stop the CLI before it emits its own completion event).

The worker follows Docker output into `harness/native.log` during the build. Capture redacts known
environment credentials before writing, caps individual records at 1 MiB and the log at 50 MiB,
and marks omitted output. The tracer reads this persisted file only: no Docker access, new service,
or model calls. History loads automatically in successive bounded pages, then refreshes every three
seconds while the Build tab is visible. A loading indicator distinguishes a backlog from caught-up
history; **Jump to latest** goes to the newest loaded record. Histories above 200 records render only
the visible window plus a small buffer. Long output stays collapsed until expanded, and expansion
state and tool-call links survive scrolling. Content is rendered as text, never agent-supplied HTML.

Existing S3/local runs are supported without rerunning the agent. Old logs lack timestamps and
were saved only at cleanup, so they cannot supply live history retrospectively. Upgrade/restart
the tracer to view saved logs; update workers for live capture on new runs. No CLI image change is
needed for this feature. `GET /api/harness/<trace_id>?offset=0&limit=100` provides paginated history
using byte cursors (up to 200 native records per page), including explicit missing/truncated-log
warnings. Files and content are bounded; symlinked inputs are refused.

These are native CLI messages, not complete model requests or token/cost accounting. Native usage
totals remain marked incomplete. Unsupported event types are shown as notices rather than silently
inventing an AppSci turn or dropping the event. Claude parsing follows the assistant/user content
blocks in the [Claude Code SDK message format](https://code.claude.com/docs/en/agent-sdk/typescript).

- Write side / event contract: [`../../shared/observability`](../../shared/observability/README.md)
- Full S3 mirror design: [S3 mirror section](../../shared/observability/README.md#s3-mirror-sharing-runs)

## Quickstart

```bash
python -m appsci_bench.tracer --port 8479          # serves ./runs
open http://localhost:8479
```

Then either click a run in the list, or **load any run by id**:

```
http://localhost:8479/?trace_id=airline_rebooking_20260730_160833
```

`?trace_id=` is the single entry point. If that run isn't on your machine yet it is pulled from the
mirror first, then rendered exactly like a local one -- so the URL is the same whether the run is
yours or a teammate's, and it can be pasted into Slack, bookmarked, or hand-edited. The search box on
the run list and the "load another run" box in the dashboard header are just forms against that param.

**You need no AWS setup to view your own local runs.** Everything below is only for reading runs
someone else produced (or for your runs to be uploaded so others can read them).

## The run list is a window

The list shows the **last 2 days** of runs, not every run on the host. Rendering a row is not free:
it merges that run's per-source logs and parses the merged one, so a page that listed everything
took as long as the host's whole history was long. On the jobs box, before this, that was ~6.9s per
render, repeated every 10 seconds by the page's own auto-refresh.

Nothing is hidden permanently. Three ways to anything older:

| You want | Do this |
|---|---|
| A run you can name part of (task, model, date) | Type it in the **search box**. It matches ids across *every* run on the host, ignoring the window. |
| More history in the list | **2d / 7d / 30d / all** above the table. One page renders at most 200 rows whatever you pick. |
| One specific run, local or mirrored | Paste its id in "load any run by id", or edit `?trace_id=` in the URL. |

The window is chosen from directory timestamps alone, before anything is read, which is what makes
it cheap. An eval trial's BUILD run is pulled in even when it falls outside the window, so a
replicate group is never split across the boundary.

Two caches sit behind it, both keyed on each file's size and mtime rather than a TTL: a finished
run's log never changes, so it is parsed once, while a live run's log changes on every append and is
re-read. Nothing here is a storage policy -- **no file is ever deleted from S3**, and the window
changes only what one page reads.

## Configuration

Two separate flags control the two directions, on two different commands — they are independent and
neither implies the other:

| | Command | Flag |
|---|---|---|
| Stop **this tracer reading** the mirror | `python -m appsci_bench.tracer` | `--no-s3-reads` |
| Stop **runs being uploaded** to the mirror | `python -m appsci_bench.orchestrator.run` | `--no-upload-runs-to-s3` |

| Setting | Flag | Env | Default |
|---|---|---|---|
| Runs directory | `--runs-dir` | -- | `runs/` |
| Port | `--port` | -- | `8080` |
| Bucket | `--s3-bucket` | `APPSCI_S3_BUCKET` | `<your-bucket>` |
| Key prefix | `--s3-prefix` | `APPSCI_S3_PREFIX` | `runs` |
| AWS profile | `--aws-profile` | `APPSCI_S3_PROFILE` | boto3 default chain |
| Region | -- | `APPSCI_S3_REGION` | `us-east-2` |
| Disable mirror **reads** (tracer) | `--no-s3-reads` | -- | reads enabled |
| Credential redaction in captured LLM I/O | -- | `OBSERVABILITY_REDACT` | on |

## A note on access

The tracer has **no authentication** and binds `0.0.0.0` by default — both pre-existing. What the
mirror changes is *reach*: previously the server could only expose runs already on that host, and now
`/?trace_id=<id>` will fetch **any** run in the bucket using your AWS session, after which the
existing file/eval/model-I/O routes serve it. Anyone who can reach the port inherits your read access
to the whole mirror.

If that matters in your setting, bind it to loopback (`--host 127.0.0.1`) or run with
`--no-s3-reads`. Putting real auth in front of the tracer is out of scope here and tracked as a
follow-up.

## Module map

- `server.py` -- the FastAPI app: run list, dashboard, SSE event stream, eval + artifact routes, and
  `resolve_trace()` (the local-or-mirror resolution behind `?trace_id=`).
- `__main__.py` -- the standalone CLI (`python -m appsci_bench.tracer`).
- `db_reader.py` -- thin read adapter over the consolidated `events.jsonl`.
- `file_server.py` -- serves the artifact snapshot (and, for a live run, files out of a container),
  with traversal guards.
- `templates/dashboard.html` -- the whole dashboard (Alpine.js): timeline tree, Eval tab, Artifact
  browser, raw event stream.
- `static/styles.css` -- styling.
