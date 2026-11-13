# Customer-Call Insights — environment resources

Where the customer's resources live inside the task container (agent-facing; distinct from the hidden
held-out eval set in `eval_data/`).

> This file is **host-only** — it is never mounted into the agent container, which sees `/sop`, `/spec`,
> `/work` and `/workspace` and nothing else. It is the author's map of the environment. Anything the
> agent must actually be told has to reach it through `spec.interface_design` or a file under
> `environment/sop/`.

| Resource | Type | Location in container | Notes |
|----------|------|-----------------------|-------|
| Call transcripts | database | `postgres://sandbox:sandbox@db:5432/calls` | Table: `calls (call_id, account_id, account_name, occurred_at, transcript)`. `transcript` is plain text. **No insight labels attached.** |
| Success KPIs | kpi | `/sop/kpis.md` | What the team means by this working |

## Transcripts — unlabeled, on purpose

Each row in `calls` is one recorded call. `transcript` holds the call as plain text -- a header line,
a participants line, then the dialogue turn by turn as `Speaker: text`, one speaker per line, blank
line between turns. Same shape as what the evaluator attaches at eval time (see below), on purpose --
an agent that parses `turns` as JSON would build the wrong reader for eval's flat-text attachments.

The table has no column for what a call means, and nobody has gone through and marked up the right
answer per call — the reps' existing notes are the inconsistent artifact this project exists to replace,
so they are not a training signal. What a good reading of a call looks like is a question for the
customer, not something to infer from the data.

## Access

- `DATABASE_URL` is injected as an env var; treat the database as read-only.
- `/sop` is mounted read-only. Scratch space is `/work` (writable); file deliverables go in `/work/out`.
- No external API for this task.

## Expected artifact architecture

The artifact is an HTTP service:

- `GET /health` -> `{"status": "ok"}`
- `POST /respond` with `{"messages": [...]}` -> OpenAI-style
  `{"choices": [{"message": {"role": "assistant", "content": ...}}]}`

The request body also carries a top-level `files` array beside `messages`, holding one attachment per
request as `{"file_type": "txt", "file_name": ..., "file_data": "<base64>"}`. **The call to analyze is
that attachment** — the user message only names the call. This is what the evaluator really sends
(`HttpArtifact.respond`), and it is stated for the agent in `spec.interface_design`; an earlier revision
of both claimed the transcript arrived in the user message, and the artifact built against that claim
never read a single call. The service must bind `0.0.0.0:$PORT` (the port is injected; do not hardcode
it).

Eval-time calls arrive only this way. They never enter the `calls` table, which holds the seeded
build-time calls alone.
