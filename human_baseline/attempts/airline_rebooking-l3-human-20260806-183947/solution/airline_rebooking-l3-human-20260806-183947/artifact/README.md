# Airline reservation agent

A conversational agent that handles airline reservation requests end to end -- booking, changing,
cancelling, refunds and compensation -- without breaking airline policy.

## The contract

The artifact is an HTTP service with two routes:

| Route | Behaviour |
|---|---|
| `GET /health` | `{"status": "ok"}`. Touches no dependency, so readiness never fails on a transient blip. |
| `POST /respond` | `{"messages": [{"role", "content"}, ...]}` -> `{"choices": [{"message": {"role", "content"}}]}` |

`/respond` is **stateless**: the caller sends the whole conversation and receives the next assistant
turn. Everything the agent needs to do -- reading records, checking policy, writing changes --
happens inside that single call, because the caller only ever observes the final assistant text.

A consequence worth knowing: tool results do not survive between turns. The system prompt therefore
instructs the agent to re-read any record before reasoning about or writing to it, rather than
quoting a price or allowance from memory.

## Why policy lives in two places

The task environment's tools enforce **no policy at all**. Verified by executing them:
`cancel_reservation` will cancel a fully-flown trip, `update_reservation_baggages` will *decrease*
the bag count, `send_certificate` checks no eligibility, and `book_reservation` ignores the
five-passenger and payment-composition limits. The policy document says as much itself: "The API
does not check these for the agent."

So the work is split:

* **`policy.py` -- deterministic Python.** Every rule that is a pure function of
  `(tool, arguments, records, clock)`. These are unit-tested and cannot be talked out of by a model
  having a bad day. A blocked call never reaches the environment.
* **`prompt.py` -- the system prompt.** What genuinely needs judgement: gathering facts, obtaining
  explicit confirmation, wording a refusal, correcting a mistaken customer, and the three rules no
  code can decide (no proactive compensation; cabin uniformity, which is structurally impossible to
  violate; and insurance not being addable after booking, for which no tool exists).

When the guard blocks a call it returns a `BLOCKED (CODE): explanation` message to the model rather
than a canned string to the customer. The database is protected deterministically; the conversation
is still composed by the model, which is what makes the refusal an accurate explanation instead of
an error code.

## Modules

| File | Purpose |
|---|---|
| `server.py` | FastAPI app: `/health` and `/respond`. Thin adapter over `Agent.run`. |
| `agent.py` | The internal tool-calling loop: turn-shape enforcement, guard invocation, termination. |
| `policy.py` | The deterministic rules. No network, no LLM -- the unit-testable heart. |
| `env_client.py` | HTTP client for the environment's `/tools/{name}`, plus read helpers for the guard. |
| `prompt.py` | The system prompt. |
| `tools.py` | Builds the LLM tool definitions from `tools_reference.json`. |
| `tools_reference.json` | The authoritative 14-tool schema (copy of `sop/tools_reference.json`). |
| `chat.py` | Interactive client for manual testing. See `RUNBOOK.md`. |

## Configuration

Environment variables, with defaults that are verified to work:

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(none)* | Required. Startup fails with a clear message without it. |
| `MODEL` | `claude-sonnet-4-5` | |
| `ENV_BASE_URL` | `http://workspace:8080` | The task environment's tool API. |
| `ENV_TOOL_TOKEN` | `airline-tool-token-local` | Bearer token for the tool API. |
| `MAX_TOOL_ITERS` | `20` | Internal tool calls per turn before a safe close-out. |
| `MAX_REPEATED_VIOLATIONS` | `3` | Same blocked rule this many times ends the turn. |
| `TRUST_STATED_REASON` | `false` | See below. |

### `TRUST_STATED_REASON`

Policy permits cancellation when the customer has travel insurance *and* the reason is health or
weather. No tool can verify that reason. The default (`false`) transfers to a human instead of
acting on an unverifiable claim, which is the safer reading of "zero policy violations" -- at the
cost of transferring insured customers who have a genuine reason. Set it to `true` to take the
customer at their word (still restricted to health/weather wording). Flip it once real traffic shows
which error is more expensive.

## Turn-shape rules enforced by structure, not by prompting

Two policy rules are guaranteed by the loop rather than requested of the model:

* **One tool call at a time.** Extra `tool_use` blocks are rejected unexecuted, with feedback.
* **Never a tool call and a message together.** Only a text-only turn ends the loop, so a mixed
  turn can never reach the caller.

## Tests

```bash
# Pure logic and loop mechanics: no API key, no container, no network.
pytest tests/test_policy.py tests/test_agent_loop.py tests/test_tools.py

# Adding the no-mutation layer needs the environment package, so run it in the test image:
docker build -t airline-artifact-test -f Dockerfile.test .
docker run --rm -v "$PWD":/artifact -w /artifact airline-artifact-test pytest tests/ -q
```

| Layer | File | What it proves |
|---|---|---|
| Rules | `tests/test_policy.py` | Every rule, both sides. All nine baggage cells. The pricing formula. |
| Loop | `tests/test_agent_loop.py` | Turn shape, iteration cap, and that a blocked write never reaches the environment. |
| Schema | `tests/test_tools.py` | All 14 tools exposed; the guard's write list matches the reference. |
| No mutation | `tests/test_no_mutation.py` | Against real records: every forbidden write is blocked, and judging a request changes nothing. |
| End to end | `tests/harness.py` | Drives the real agent over mined cases. Needs an API key; not in the default run. |

The pricing function is not merely unit-tested against my reading of the tool -- it was
cross-validated against the real `update_reservation_flights` on 121 actual reservations across
three target cabins each, and agreed on all of them.

### Test data

There are no transcripts of past requests, so `tests/mine_cases.py` derives cases from the real
fixture: for each policy branch it finds reservations whose records genuinely exhibit it.
Regenerate with

```bash
docker run --rm -v "$PWD":/artifact -w /artifact airline-workspace \
  python tests/mine_cases.py --out /artifact/tests/cases.json
```

`tests/cases.json` is committed so the suite needs neither the container nor the miner.

## Adding a rule

1. If it is decidable from `(tool, arguments, records, clock)`, add a check to `policy.py` and a
   test to `tests/test_policy.py` covering both the allow and the deny side. Wire it into the
   relevant `check_*` function.
2. If it needs judgement or conversation, add it to `prompt.py` -- and note in the `policy.py`
   docstring that it is prompt-owned, so nobody later assumes it is enforced in code.

## Known limits

* `search_onestop_flight` in the environment returns direct flights only, so connecting itineraries
  cannot be found or booked. Out of scope.
* New bookings draw reservation ids from a fixed pool of three (`HATHAT`, `HATHAU`, `HATHAV`), so
  only **three bookings are possible** before the environment must be reset.
* The default test suite does not exercise the assembled agent's conversational judgement -- only
  the rules and the loop. Use `tests/harness.py` with an API key for that.
