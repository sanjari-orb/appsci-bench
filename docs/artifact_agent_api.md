# Artifact-Agent Session API (contract for the produced artifact)

The applied-scientist agent's deliverable is a **served artifact agent**. The benchmark evaluates it
by acting as a client over a small session API. Your artifact MUST implement these three endpoints
exactly. Machine-readable schema: [`schema/contracts/session_api.schema.json`](../schema/contracts/session_api.schema.json)
(the contract is a **versioned registry** — each version is a self-contained file
`schema/contracts/session_api.v<N>.json`; the evaluator broadcasts the latest and can load an older
one to negotiate compatibility).
Full design: [`docs/evaluator_system_design.md`](evaluator_system_design.md).

## Endpoints

| Endpoint | Input | Output |
|----------|-------|--------|
| `create_session` | optional reserved `{ context?: object }` (currently always empty — **accept and ignore it**) | `session_id` — a non-empty string |
| `query_session`  | `{ session_id: string, query: ArtifactAgentQuery }` | `ArtifactAgentOutput` |
| `delete_session` | `{ session_id: string }` | `{ success: boolean }` |

A single-turn case is one `query_session`; a multi-turn case is several within one session, driven by
the artifact agent user sim, up to the dataset's `max_turns`.

## Payloads

- **ArtifactAgentQuery** — `{ text?: string, json?: string, files?: File[] }`
- **ArtifactAgentOutput** — `{ text?: string, json?: string, files?: File[] }`
- **File** — `{ file_type: pdf|docx|pptx|csv|xlsx|jpg|png|txt|md, file_name: string, file_data: bytes }`

`text` and `json` are exposed to the artifact agent user sim; **file bytes are not** (the sim sees only
`file_type` + `file_name`). Framing is **application/json** — a non-null `json` field must be valid
JSON. File bytes are base64-encoded when serialized (see the transport section below).

## Transport: how this maps onto the deployed `POST /respond`

A deployed artifact serves `GET /health` + `POST /respond` (what `deploy_artifact` builds and
health-checks). The evaluator owns session state and projects the session API onto that single
endpoint: `HttpArtifactClient` keeps the running message history and replays it on each call.

Request — `files` carries `ArtifactAgentQuery.files`, and the key is **omitted entirely** when the turn
has no attachments (so a text-only artifact sees exactly the body it always did):

```json
{
  "messages": [{"role": "user", "content": "Build the deck from the attached files"}],
  "files": [{"file_type": "csv", "file_name": "metrics.csv", "file_data": "<base64>"}]
}
```

Response — `choices` is required; `json` and `files` are optional channels for a structured
deliverable and produced files respectively:

```json
{
  "choices": [{"message": {"role": "assistant", "content": "Generated 8 slides."}}],
  "json": "{\"presentation\": {...}}",
  "files": [{"file_type": "pptx", "file_name": "deck.pptx", "file_data": "<base64>"}]
}
```

`json` must be a JSON-encoded **string**, not an object. Produced files must go in the `files` channel
and structured output in the `json` channel — the evaluator reads them from nowhere else, so anything
base64-embedded in the prose `content` scores as if it were missing.

Attachments belong to the turn that carried them: the replayed history is text-only, so a multi-turn
artifact is expected to retain its own attachments across turns rather than receive them again.

The contract carries a top-level `version` (currently `1`); it bumps on any shape change so the
evaluator and artifact can negotiate compatibility. The `create_session` `context` arg is **reserved**
for per-case starting state — empty today, but accepting it now keeps that addition non-breaking.

## Conformance

Before scoring, the evaluator runs a **conformance probe**: `create_session` → `query_session` (a
benign query; the output must be a well-formed `ArtifactAgentOutput` with valid JSON framing) →
`delete_session` (must return a boolean). A non-conformant artifact **fails the run with a clear
error** rather than being scored — so implement the contract exactly.

## Reaching the task environment: read your environment, never hardcode it

The address and credentials of the task environment arrive as environment variables on the artifact
container (the task's `task_runner.agent_env`, e.g. `AIRLINE_TOOLS_URL` / `AIRLINE_TOOLS_TOKEN`). Read
them at run time. They are **not stable across containers**, and an artifact that copies either value
into its source is broken in a way its author will not notice locally.

The reason is that eval may run several COPIES of the task environment at once, so a task whose store
is reset per case is not forced to score one case at a time. Each copy has its own container name and
its own tool token, and the harness runs one artifact container per copy, handing each the values for
the copy it is paired with. An artifact that hardcoded the URL reaches a copy it was not assigned; one
that hardcoded the token is refused with a 401. Both fail loudly, which is deliberate — the
alternative, every copy transacting against one store, is a wrong score that looks like a real one.

The probe runs against every copy, so a mismatch is found before the dataset is scored rather than
one case at a time.
