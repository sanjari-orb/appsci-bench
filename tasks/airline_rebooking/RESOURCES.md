# Airline Reservations Agent — environment resources

Where the customer's resources live inside the `appsci-bench/airline` task environment (agent-facing;
distinct from the hidden held-out eval set in `eval_data/`).

This task's environment is an in-memory airline reservation store (`flights` / `users` / `reservations`)
plus a fixed tool layer. The internal provenance record lives at `provenance/airline/`.

| Resource | Type | Location | Notes |
|----------|------|----------|-------|
| Airline DB | database | `environment/{build,eval}/data/` | In-memory JSON-backed store, **not** a Postgres/SQL server; the agent reaches it only through the tools — see below |
| Airline tools | api | (in-process tool interface) | 14 read/write/transfer tools over the DB — see below |

> **There are no production traces.** A `prod_traces` resource was declared in `spec.yaml` from this
> task's first commit and never materialized — no traces file exists in the repo, nothing is bundled
> into either run phase, and no endpoint serves them. The entry is gone from `spec.yaml` and from the
> L1/L2 personas' `known_resources`. Do not re-add it without a fixture the agent can open.

> **Neither the policy nor the KPIs are listed here, because neither is published to the agent.** The
> policy and the KPIs live only in `user_sim_docs/`, which is mounted to the user-sim and never to the
> agent; the agent's copies are gone, and so are the `rebooking_policy` and `kpis`
> resource entries in `spec.yaml` — a resource entry would have named something the agent cannot reach.
> See below. This doc stays about *where resources live*.

## The policy is elicited, not published
The agent is given **no policy document at all**. Every operating rule — the confirmation gate, one
tool call at a time, the refusal and transfer duties, the domain vocabulary, and the booking, modify,
cancel and refund/compensation rules — reaches all three user-sim personas as a sim-only document and
has to be elicited in conversation. The seed stays the authoring ground truth and the user-sim copy is
byte-identical to it.

## Airline DB — in-memory, JSON-backed (not Postgres)
Unlike the SQL/Postgres task environments we first sketched, this DB is an in-memory JSON store: three
JSON collections (`flights`, `users`, `reservations`). Each RUN PHASE has its own store --
`environment/build/data/db.json` (the carved share the agent explores) and
`environment/eval/data/db.json` (the full store the eval cases are drawn from). At task-run time the
environment **loads `db.json` into an in-memory object** that the tools read and mutate; it is not a
networked database server and there is no `DATABASE_URL`. Each run starts
from a fresh copy of the fixture (the eval-time DB-state oracle is replayed against the same fixture).

## Airline tools — the in-process API the agent drives
The agent does not query the DB directly; it acts through a fixed **tool interface** (the 14
airline tools), the action space the artifact exposes/consumes:
- **Read:** `get_user_details`, `get_reservation_details`, `search_direct_flight`,
  `search_onestop_flight`, `list_all_airports`, `get_flight_status`, `calculate`.
- **Write:** `book_reservation`, `update_reservation_flights`, `update_reservation_baggages`,
  `update_reservation_passengers`, `cancel_reservation`, `send_certificate`.
- **Transfer:** `transfer_to_human_agents`.

These are invoked in-process against the in-memory DB. The submitted artifact reaches them over the
airline tool-API (`AIRLINE_TOOLS_URL`) and exposes its own behavior via the fixed HTTP completions
interface (`POST /respond`) described in `spec.yaml`. Per-tool argument lists are not published to
the agent -- they are held by the user-sim personas and have to be elicited in conversation.

## Important: policy is NOT enforced by the tools
The write tools apply changes blindly — they do **not** validate eligibility or policy. The artifact
must check the rules itself **before** any write, and must list the action + obtain explicit user
confirmation first. Note that **no policy document is published to the agent** (see above), so all of
those rules have to be elicited. The environment's reference "now" is fixed at
**2024-05-15 15:00:00 EST** (the policy's time-relative rules are evaluated against this clock).

## Access
- DB/tool detail is gated to the L1/L2 personas; L3 speaks only to business outcomes.
- The **whole policy** reaches all three personas, since the agent has no other route to it. Per-level
  scoping of that material is a deferred follow-up.
- The success KPIs are held by L1 and L3 only.
- The agent under test is never shown the held-out eval set or the per-case oracle.

## Expected artifact architecture
The artifact the agent builds is an HTTP service:
- `GET /health` → `{"status":"ok"}`; `POST /respond` with `{"messages":[...]}` →
  OpenAI-style `{"choices":[{"message":{"role":"assistant","content":...}}]}`.
- It reaches the 14 airline tools as a **tool-API** at `AIRLINE_TOOLS_URL` (bearer `AIRLINE_TOOLS_TOKEN`):
  `POST {AIRLINE_TOOLS_URL}/<tool_name>` with `{"arguments": {...}}` → `{"result": ...}`.
- Reference implementation: `gold/airline_rebooking/local_server/server.py` (deployed via
  `gold/airline_rebooking/local_artifact_agent.py`).
