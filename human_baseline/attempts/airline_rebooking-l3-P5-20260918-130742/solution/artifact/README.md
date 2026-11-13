# Airline Reservations Agent — Artifact

A contact-channel agent that books, changes, and cancels airline reservations, and handles refunds and
compensation, without ever breaking airline policy.

## The one idea worth knowing

**The task environment enforces none of the policy.** Its `cancel_reservation` takes only a reservation
id and will cancel an already-flown reservation without complaint; `update_reservation_flights` will
happily modify a basic-economy booking. Every rule lives with the agent.

So policy correctness is **not** left to the language model. Every rule is a pure, unit-tested Python
function in `policy/`, and every mutation is gated twice in `actions.py`:

1. a `DENY` or `TRANSFER` verdict makes the corresponding write **unreachable**, and
2. an `ALLOW` still cannot fire until the customer's explicit "yes" is found in the transcript.

The model chooses *what to attempt* and does the talking. It cannot choose to break a rule. That is the
structural answer to "a confident customer pushes and the agent over-agrees".

## Layout

| Path | Responsibility |
|---|---|
| `server.py` | HTTP contract only: `GET /health`, `POST /respond`. Thin by design. |
| `config.py` | Environment variables and the fixed clock (`2024-05-15T15:00:00`). |
| `logging_setup.py` | Structured JSON logs with one correlation id per request. |
| `env_client.py` | The 14 environment tools over HTTP. Distinguishes a 400 (business rejection) from a 500 (our own malformed call), and keeps `cancel_reservation` write-once. |
| `domain.py` | Typed read-only views over user/reservation/flight records. No I/O. |
| `policy/` | **The rules.** Pure functions returning `Decision(ALLOW/DENY/TRANSFER)`. No I/O, no model. |
| `actions.py` | Verifies facts, applies policy, and performs writes *only* after confirmation. |
| `agent/prompts.py` | System prompt and the model's tool schemas (no raw write tools are exposed). |
| `agent/orchestrator.py` | The turn loop. Enforces one tool call per step and the confirmation gate. |
| `tests/` | 185 tests: policy tables, action gating, turn discipline, adversarial personas, HTTP contract. |
| `tools/unwrap_sources.py` | Maintenance utility; strips a stray JSON envelope from a source file. |

### Reading order

`policy/decision.py` → `policy/cancellation.py` → `actions.py` → `agent/orchestrator.py`. The first two
show the vocabulary, the third shows the gating, the fourth shows the loop.

## The three buckets

The distinction the stakeholder cared most about:

* **ALLOW** — in policy and within our tools: do it, after confirmation.
* **DENY** — against policy: say no. Never dressed up as a transfer, because a human cannot approve what
  policy forbids. Basic-economy flight changes, passenger-count changes, adding insurance after booking,
  and out-of-policy cancellations are all denials.
* **TRANSFER** — a legitimate request no action of ours can carry out. The canonical case is a
  cancellation where part of the itinerary has already been flown.

A customer asking for a human is **not** a trigger; the nature of the request is.

## Rulings that are not in the task README

These four came from the product manager and are encoded in `policy/`:

1. Compensation via the *insurance* route also requires an insurance-covered reason (health or weather),
   mirroring the insurance cancellation rule.
2. Delay compensation ($50/passenger) requires the change or cancellation to have **actually succeeded**
   first. If that write is out of policy, the compensation is denied rather than paid.
3. A flown segment beats every other cancellation condition: flown → transfer, even for a business cabin
   or a booking made in the last 24 hours.
4. Identity is the stated `user_id`, verified to resolve; a reservation must also be verified as belonging
   to that user before any write.

## Running

```bash
pip install -r requirements.txt
PORT=8000 python server.py
```

Configuration, all optional except the LLM credential:

| Variable | Default | Purpose |
|---|---|---|
| `AIRLINE_ENV_URL` | `http://airline-workspace-1:8080` | Task environment base URL. Not published to the host, so it is addressed by container name on the shared docker network. |
| `AIRLINE_ENV_TOKEN` | `airline-tool-token-local` | Tool bearer token. |
| `ANTHROPIC_API_KEY` | — | Also accepts `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_API_KEY`, `LLM_API_KEY`. |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Override for a gateway. |
| `AGENT_MODEL` | `claude-sonnet-5` | Model id. |
| `AGENT_MAX_TOOL_STEPS` | `24` | Per-turn tool budget. |
| `LOG_LEVEL` | `INFO` | |

With no LLM credential the service still answers `/health` and returns a safe fallback from `/respond`,
logging the misconfiguration loudly at start-up. It never guesses: a wrong action is far worse than an
unhelpful turn.

## Tests

```bash
python -m pytest tests -q
```

The fixtures in `tests/db_snapshot.json` are a **real capture** from the running environment (5 users, 10
reservations, every referenced flight status, 149 route-date searches), so the tests exercise real record
shapes rather than hand-written guesses.

What the suite covers:

* **Policy tables** — every membership × cabin baggage cell, the payment limits, all four cancellation
  conditions, both compensation situations, and every modification prohibition.
* **Write gating** — the central guarantee. Each test asserts on `fake.writes`, so "nothing was mutated"
  is checked directly rather than inferred from the wording of a reply.
* **Turn discipline** — a model that emits several tool calls at once has all but the first dropped; prose
  emitted alongside a tool call is never shown to the customer.
* **Adversarial personas** (`tests/simulator.py`) — customers who lie about a cancellation, their
  membership, or their insurance, then escalate. They are run against a deliberately **over-agreeable**
  model that tries to do whatever was last asked. The gates, not the model, are what must stop it.
* **HTTP contract** — including malformed bodies, which must still return the documented shape.

No agent code references a sample id; the evaluation store has entirely different ones. Ids appear only
under `tests/`.

## Talking to the agent yourself

The task environment's port is not published to the host, so open a tunnel once:

```bash
tools/dev_proxy.sh up        # status | down | reset
```

Then either hold the conversation yourself, or let a scripted customer hold it for you:

```bash
python tools/chat.py                                   # you type, the agent answers
python tools/chat.py --persona lying_about_insurance    # a customer who lies, then pushes back
python tools/chat.py --persona all                      # every persona in turn
python tools/chat.py --list                             # what the personas are and expect
python tools/chat.py --dry-run                          # reads for real, blocks every write
python tools/chat.py --trace                            # print each tool call as it happens
```

Both modes drive the real agent against the real environment, so **writes really happen**. Each run
prints the database fingerprint before and after and tells you when it moved; `tools/dev_proxy.sh reset`
restores the pristine store. Use `--dry-run` to explore without touching anything.

Useful starting points from the development sample: `rafael_moreau_9821` (gold) with `TRN009`
(airline-cancelled, so cancellable and compensation-eligible), `owen_delgado_9137` with `TRN004`
(already flown → transfer), and `quinn_okafor_9503` with `TRN008` (booked inside the 24-hour window).

### If the model call fails TLS verification

A python.org framework build on macOS installs no CA certificates, so any HTTPS call raises
`CERTIFICATE_VERIFY_FAILED`. `chat.py` falls back to `certifi` automatically and reports the fix if even
that is missing, but to repair the interpreter itself, run the installer that shipped with it once:

```bash
/Applications/Python\ 3.14/Install\ Certificates.command   # match your version
```

## Environment traps worth knowing

Verified against the live container:

* `cancel_reservation` is **not idempotent** — each call appends another set of negated refund rows.
  `EnvClient` refuses a second cancel of the same reservation.
* A **missing argument returns HTTP 500**, not 400. Treated as our own bug and never surfaced to the
  customer.
* `search_onestop_flight` is a **stub** returning direct flights only, so connections (JFK↔LAX, SFO↔LAX)
  need two searches via the ORD hub.
* `book_reservation` computes the price itself and rejects the call unless the payment amounts sum to it
  **exactly**; the free-bag allowance is the agent's to derive.
* A cabin change re-prices **every** segment, because the tool only preserves a segment's original price
  when the cabin is unchanged.
* Leftover value on a travel certificate is never refundable, and a certificate can never fund an update.
