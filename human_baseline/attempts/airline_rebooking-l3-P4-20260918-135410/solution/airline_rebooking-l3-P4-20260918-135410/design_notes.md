# Design notes — airline reservations agent

Optional diagnostic artifact (`spec.yaml` declares `design_notes` as `required: false`). Human-baseline
attempt `airline_rebooking-l3-P4-20260918-135410`, L3 collaborative.

## The problem, as it actually is

The tool API performs whatever it is told. It will cancel a reservation that policy forbids cancelling,
book a flight that is not bookable, and issue a certificate nobody is entitled to. Nothing downstream
checks. So the entire policy surface is the artifact's responsibility, and the stakeholder was explicit
about the ranking: a policy violation or a wrongly-mutated record is unshippable, and *getting the
right outcome the wrong way is worse than failing to complete a request*.

That ranking is what the architecture is for. It is cheap to build something that usually complies. It
is harder to build something whose compliance does not depend on the model having a good day.

## Shape: the model converses, code decides

```
  customer turn
       |
  server.py   agent loop, one tool call per model turn, returns exactly one message
       |
  gate.py     <-- every tool call passes through here
       |          reads the live records, asks policy.py, and only then calls the tool
  policy.py   pure predicates. no I/O, no model.
       |
  tools.py    transport only. refuses nothing.
```

The model owns conversation, information gathering and phrasing. It does not own a single policy
decision. Where the two disagree, code wins and the tool is never called — the model is told the
denial is final and is given the reason to relay.

The split is deliberate about testability too: because `policy.py` touches nothing, all forty-two rule
assertions in `check_policy.py` run offline in about a second, against records copied out of the live
store. Every rule change is verified before a container is rebuilt.

## Three things that are less obvious than they look

**The clock is fixed and real time is two years wrong.** The store's "now" is 2024-05-15 15:00 EST.
Every time-relative rule — the 24-hour cancellation window, whether a segment has departed — is judged
against it. A single `datetime.now()` in a policy path silently inverts the 24-hour rule for every
reservation in the store. All of it goes through `clock.py`; nothing else reads a clock.

**A stateless endpoint plus a stateful tool loop will replay writes.** `/respond` receives the whole
customer conversation each call, but the internal tool calls are not in it. Re-deriving the loop from
the visible text would re-issue a booking or a cancellation that already happened — the worst class of
bug available in this task, since it corrupts the store rather than merely failing a case. So an
in-process cache keyed by a hash of the customer-turn sequence holds the internal transcript, an
identical conversation replays its stored reply rather than re-running, and on a cache miss the loop
resumes from text alone and never speculatively repeats a write.

**One tool call per turn has to be enforced by rewriting history, not by ignoring extras.** The API is
asked for a single call (`disable_parallel_tool_use`, which this gateway honours), and the code
enforces it regardless. The subtlety: a tool-use block with no matching result in the following turn
makes the *next* request invalid, so silently dropping the extras breaks the conversation one turn
later rather than immediately. The assistant turn is therefore rewritten in our own transcript to hold
exactly one call before the result is appended.

## Making an unverifiable claim verifiable

Two write schemas carry a field the real API does not have, stripped before the call:

| tool | added field | why |
|---|---|---|
| `cancel_reservation` | `cancellation_reason` | eligibility can depend on *why* the customer is cancelling (the insurance condition), and the real tool takes only a reservation id |
| `send_certificate` | `justification_reservation_id`, `justification_ground` | compensation is permitted on exactly two grounds; the real tool takes only a user id and an amount, so without naming the reservation there is nothing to check a ground against |

The model must state its justification; the gate then checks that justification against the store
rather than believing it. Compensation amounts are computed in code — $100 or $50 per passenger — and
never taken from the model.

## Deny and transfer are different outcomes

They are scored differently and the distinction is easy to collapse:

- **Deny** — the airline does not allow it. A basic-economy flight change, a change to the number of
  passengers, an ineligible cancellation, compensation outside the two grounds. A human agent cannot
  override any of these, so offering a transfer is wrong.
- **Transfer** — a legitimate need this agent cannot service. The one case policy spells out:
  cancelling a reservation with an already-flown segment. A human genuinely can help; saying otherwise
  is wrong.

This produced the only regression of the build, and it is worth recording because it exposes a real
limit of the architecture. After strengthening the prompt's "not even a human can change the passenger
count" language, the flown-segment cancellation started being **denied** rather than transferred — the
emphasis bled across. The gate could not catch it: **the gate only fires on an attempted write, and a
model that refuses pre-emptively never reaches it.** Code-enforced guardrails bound what the agent
*does*; they cannot bound what it declines to attempt. That half stays with the prompt, which now
contrasts the two cases explicitly and tells the model not to pre-judge a write that policy would
decide.

## The turn ceiling shapes the loop

Twenty customer exchanges per case, and hitting the ceiling scores zero — strictly worse than giving up
cleanly. So internal tool iterations are capped per turn; exhaustion still produces a message (asked
for with tools withheld) rather than silence; no path returns empty or raises, and `/respond` never
returns a 500 because a graceful turn scores better than a dropped one. The prompt also forbids
narration — a message that only says "let me check that" spends a turn to convey nothing, which cost a
turn in testing before it was banned.

## Reads are id-addressed, and that is not a choice

Nothing in the tool set lists users or reservations; every read takes an id the caller already holds.
That forces the right design anyway: the development store holds ten reservations and the scored store
holds two thousand, so anything that scanned would survive development and fall over under scoring.
Nothing is cached across conversations either, since another case may have written to the same record.

## What is deliberately not enforced

One rule is missing on purpose. Whether a flight a customer is moved *onto* must itself be `available`
is **not** in the written policy — the stakeholder said so plainly, offered their own judgement that it
should apply, and asked for it to be flagged as judgement rather than policy. It is allowed but
de-preferred: the two firm rules bordering it (only `available` is bookable; a departed segment blocks
a change) are enforced separately, so permitting it cannot breach a confirmed rule, while enforcing it
on an unconfirmed reading could block a legitimate change and lose a case outright.

The cost of that choice showed up immediately: the prompt's booking-side "only available can be
booked" leaked into the change path and the agent told customers no flights existed when they did.
The prompt now scopes that restriction to new bookings explicitly. A gate that is permissive and a
prompt that is strict is a mismatch that produces confident wrong answers, which is worse than either
setting alone.

## Audit trail

Every blocked and performed write is written to stdout as a JSON line (`AUDIT ...`), carrying the rule
that fired and the record data behind it. The purpose is diagnostic and specific: after scoring it
separates *my elicited rule was wrong* from *the model misbehaved*. Those are a task-difficulty finding
and a build defect respectively, and without the trail they are indistinguishable from a score alone.

## Known limitations

- **Booking totals are not verified in code.** Per-segment prices are only obtainable from a search,
  and the booking arguments carry flight numbers without their routes, so the expected total cannot be
  recomputed from the arguments alone. Payment-method identity, counts, per-method balances and the
  baggage arithmetic *are* all checked. Totals rest on the model plus the `calculate` tool.
- **`search_onestop_flight`'s response shape is unverified.** In the build store it returns exactly
  what the direct search returns, because that store has five airports and nothing to connect through.
  Its description no longer claims a shape it cannot support. This is the likely root of two remaining
  failures, and the only environment that would settle it is the scored one — so it was left unsettled
  rather than investigated.
- **The conversation cache is in-process.** Fine for the one-conversation-per-environment-copy shape of
  scoring; it would need externalising for more than one replica.
- **The confirmation gate is textual.** It requires a preceding recap and an affirmative latest
  customer turn, matched against a broad affirmative pattern with negation handling. A customer who
  agrees in a form the pattern misses is asked to confirm again — a wasted turn, deliberately preferred
  over an unconfirmed write.
