# Appsci Agent User Sim — Service Overview & HTTP API

Audience: developers building an **applied-science agent** that appsci-bench evaluates.
This is the wire contract between your agent and the benchmark's appsci agent user sim service.

> **Naming note:** this is the **appsci agent user sim** — it talks to your agent while it *builds*
> the artifact (requirements elicitation), described below. It is an entirely separate component
> from the **artifact agent user sim** (eval-time, drives the artifact your agent produces once
> served — see `docs/evaluator_system_design.md`'s "Artifact Agent User Sim" section). They share nothing,
> including their models: this build-time sim runs on Claude (`APPSCI_USERSIM_MODEL`, defaulting to the
> shared `MODEL_NAME`), while the eval-time one is pinned to tau-bench's user-simulator model
> (`ARTIFACT_USERSIM_MODEL`).

For benchmark mechanics overall, see [OVERVIEW.md](OVERVIEW.md). For the persona files that
configure the service per run, see [task_spec_reference.md](task_spec_reference.md).

---

## 1. Overview

The **appsci agent user sim** is the benchmark's stand-in for the human stakeholder your agent is
building for. It runs as a **separate backend service in its own container**, on the same private
network as
your agent's container. Your agent talks to it over HTTP — it is a microservice, not a library you
import.

```
  +-------------------------+            +-----------------------------+
  |   agent container       |            |   appsci-agent-user-sim container        |
  |  (your applied-science  |  HTTP      |  backend service            |
  |   agent under test)     | ---------> |  exposes /health /ask       |
  |                         | <--------- |  (one session, implicit)    |
  +-------------------------+            +-----------------------------+
            ^                                       ^
            | APPSCI_USERSIM_URL, APPSCI_USERSIM_TOKEN            | startup params (task, persona
            | (injected by orchestrator)           |  level L1/L2/L3, model, ...)
            |                                       | + open/close the session
            +------------------ orchestrator -------+
```

**Who controls what.**
- The **orchestrator** starts the appsci-agent-user-sim service and fixes its configuration at startup: which task,
  which persona **level (L1 / L2 / L3)**, the backing model, etc. It also opens and closes
  the conversation **session** out-of-band, when it spins the service up and down for a run. Your agent
  does **not** choose or see any of this — it is the run's hidden difficulty setting.
- The orchestrator injects the service address and a per-run token into your agent's container as env
  vars (§2.1). From your agent's side there is **exactly one, already-open session** — there is no
  session to create or manage. The API is the entire surface: ask questions, read answers.

**What the service is for.** appsci-bench scores the *process* of turning a vague customer ask into a
working agent: clarify -> design -> build -> validate. The requirements your agent needs are
deliberately **not** all in the task description — they live with the simulated stakeholder, and your
agent must **interview** for them via this API.

**Some requirements exist only here.** A task may give the stakeholder documents — a policy, eligibility
rules, thresholds — that are mounted into the simulator's container and into **no other container**
(`tasks/<id>/user_sim_docs/`, listed per persona as `known_docs`; see
[`tasks/README.md`](../tasks/README.md#documents-only-the-user-sim-can-read) for the task-authoring side,
and startup env var `APPSCI_USERSIM_DOCS_DIR`, default `/user_sim_docs`). There is no file for your agent
to find and no path to read: asking is the only channel. The stakeholder answers from that material as
someone who has read and internalized it — briefly, one question at a time, quoting exact numbers and
thresholds — so expect to interview across several turns rather than receiving a document.

**Behavioral contract of the answers** (this shapes how you should query it):
- **It answers; it does not lead.** Your agent drives. The service will not ask you questions back,
  propose next steps, hand you a requirements list, or tell you when to stop. Silence gets you nothing.
- **It answers only what its persona knows.** Each persona has a closed fact set. Out-of-scope
  questions get "I don't know / would have to check" — it will **not** fabricate a number, name, or
  value. "I don't know" is a correct, expected answer.
- **It stays in its lane (the level is the difficulty knob).**

  | Level | Role | Speaks to | Deflects |
  |-------|------|-----------|----------|
  | **L1** | Applied-Science / ML Manager | Requirements, methods, metrics, trade-offs, technical guidance | Doing the build for you |
  | **L2** | Senior SWE / Data Engineer | Infra, data, schemas, integration, deployment | ML/modeling choices |
  | **L3** | Product Manager | Business goals, users, outcomes, "good" by example | Anything technical |

  Your agent is **not told the level** — infer it from the answers.
- **It refuses to do your work.** It will not draft your doc, write your SQL, run your query, or
  produce any deliverable artifact. It will review something you bring it. Delegation is redirected.
- **It will not reveal identity/personal details.** No backstory to mine; don't spend turns there.
- **It never ends the conversation.** Termination is owned by *your* agent (decide you have enough) and
  by a benchmark-side **turn ceiling** (§2.6). The service keeps answering until one of those stops it.

---

## 2. HTTP API

All endpoints are JSON over HTTP. Base URL and token are injected by the orchestrator (§2.1).
The public surface is two routes: `GET /health` and `POST /ask`. There is no session to open or
close — the orchestrator manages the single session's lifecycle out-of-band (§1).

> Status: contract is stable; the service implementation is landing now. Until a live run wires it in,
> the env vars below may be unset — detect that and no-op gracefully.

### 2.1 Connection

The orchestrator injects into your agent's container:

| Env var | Example | Meaning |
|---------|---------|---------|
| `APPSCI_USERSIM_URL` | `http://usersim:8000` | Base URL of the appsci-agent-user-sim service on the private network. |
| `APPSCI_USERSIM_TOKEN` | `run_abc123` | Per-run bearer token. Send on every request. |

Authentication: **every** request carries `Authorization: Bearer ${APPSCI_USERSIM_TOKEN}` (including
`/health`). A missing or wrong token returns `401`. The token scopes you to **this run's** service
instance.

### 2.2 `GET /health`

Readiness/liveness probe. Poll until `200` before your first `/ask` (the service may still be warming
up when your container starts).

```
GET /health
Authorization: Bearer ${APPSCI_USERSIM_TOKEN}
```
Response `200`:
```json
{ "status": "ok" }
```
Requires the bearer token, like every route; a missing or invalid token returns `401`.

### 2.3 `POST /ask` — ask the stakeholder a question

The one call you make, repeatedly. Send a question; get the stakeholder's reply. The conversation is
implicit and continuous — the service remembers everything you've asked this run (§2.4), so you send
only the question.

```
POST /ask
Authorization: Bearer ${APPSCI_USERSIM_TOKEN}
Content-Type: application/json
```
Request body:
```json
{
  "question": "What does success look like, and what must we not miss?"
}
```

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `question` | `string` | yes | Your agent's question this turn. Non-empty. |

Response `200`:
```json
{
  "message": "We can't miss late-payment risk flags. Marking a paying customer as a risk is worse than missing one.",
  "turn": 1
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `message` | `string` | The stakeholder's reply, plain text. This is the answer your agent acts on. |
| `turn` | `integer` | 1-based index of this exchange in the run. Lets you track against the ceiling (§2.6). |

### 2.4 State model

The conversation is **server-side stateful and implicit**. There is exactly one session per run; the
orchestrator opened it before your agent started and will close it after. The service remembers every
prior turn, so `/ask` carries only the new `question` — never a transcript or a session id.
Consequences:
- Every `/ask` continues the same conversation; the stakeholder "remembers" earlier answers.
- There is no way to reset or branch the conversation — it is one continuous interview for the run.
- If you need your own transcript (for logging / your agent's reasoning), keep it client-side; the API
  does not return prior turns.

### 2.5 Errors

| Status | When | Body |
|--------|------|------|
| `400` | Malformed JSON, or missing/empty `question`. | `{"error": "..."}` |
| `401` | Missing or invalid bearer token. | `{"error": "unauthorized"}` |
| `409` | No session is open (the orchestrator has not started, or has closed, the run's session). Not something your agent can fix — the run is not in an askable state. | `{"error": "no active session"}` |
| `500` | Internal failure (e.g. backing model returned something unparseable after retries). | `{"error": "..."}` |

`409` and `500` are benchmark-internal faults, not normal turn outcomes; the run surfaces them. Treat
`400`/`401` as your agent's bug to fix.

### 2.6 Turn budget

Each task has a build-phase **turn ceiling** (e.g. `order_to_cash` allows 40). The service does **not**
enforce or reveal it — but the benchmark stops handing your agent execution once the ceiling is hit,
finished or not. The `turn` field in `/ask` responses is your running count. Spend turns on specific,
in-scope, answerable questions.

### 2.7 End-to-end example

```bash
# 1. wait for readiness
curl -s "$APPSCI_USERSIM_URL/health" -H "Authorization: Bearer $APPSCI_USERSIM_TOKEN"
# {"status":"ok"}

# 2. ask (no session to open — there is exactly one, already running)
curl -s -X POST "$APPSCI_USERSIM_URL/ask" \
  -H "Authorization: Bearer $APPSCI_USERSIM_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question":"What does success look like?"}'
# {"message":"...","turn":1}
```

Python:
```python
import os, httpx

base, token = os.environ["APPSCI_USERSIM_URL"], os.environ["APPSCI_USERSIM_TOKEN"]
h = {"Authorization": f"Bearer {token}"}

with httpx.Client(base_url=base, headers=h, timeout=60) as c:
    c.get("/health").raise_for_status()

    def ask(q: str) -> str:
        r = c.post("/ask", json={"question": q})
        r.raise_for_status()
        return r.json()["message"]

    success = ask("What does success look like, and what must we not miss?")
    edge    = ask("What's an example of an output that would be unacceptable?")
    # ... keep asking specific, in-scope questions until YOU decide you have enough.
```

### 2.8 Not in the API (by design)

- **No session management.** There is exactly one implicit session per run, opened and closed by the
  orchestrator. You never create, pass, or close one.
- **No termination signal.** The service never says "we're done." Stopping is your agent's call plus
  the turn ceiling (§2.6).
- **No persona/level introspection.** You cannot query which level you got or its hidden facts —
  inferring that is the benchmark's difficulty.
- **No configuration on your side.** Model, task, and persona are fixed by the
  orchestrator at startup so runs are comparable across agents.
