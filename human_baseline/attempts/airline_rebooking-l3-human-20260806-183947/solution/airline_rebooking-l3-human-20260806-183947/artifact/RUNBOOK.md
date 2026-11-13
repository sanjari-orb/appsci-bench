# Runbook: testing the agent by hand

For manually exercising the agent against the task environment without leaving the reservation
database in a mess.

## Before you start

You need the artifact running (deployment is handled separately) and its URL. The chat client talks
to the artifact over HTTP; the environment's admin routes are used only to check and restore state.

```bash
export ARTIFACT_URL=http://localhost:8080          # wherever the artifact is published
export ENV_ADMIN_URL=http://localhost:8080         # the task environment
export AIRLINE_ENV_ADMIN_TOKEN=airline-admin-token-local

python chat.py --hash          # is the database pristine right now?
python chat.py                 # start talking
```

`python chat.py --hash` prints a fingerprint and tells you whether it matches the untouched
fixture. The pristine value is
`44b4b7da83f81d77ba17ed94a7e51f81995a8a23fa8df1df702c8df80dddd0fb` -- if you see `PRISTINE`, nothing
has been changed.

In-session commands: `/hash`, `/reset`, `/new`, `/persona <id>`, `/personas`, `/quit`.

## The safety ladder

**Tier 1 -- read-only. No reset needed.**
Inquiries, requests that should be refused, and requests that should be transferred. Run `/hash`
before and after; it must be unchanged. If it changed, the agent wrote something it should not
have -- that is a bug worth reporting, with the transcript.

**Tier 2 -- writes, then reset.**
Bookings, changes and cancellations. Note the hash, do the thing, inspect the result, then `/reset`
and confirm the hash is `PRISTINE` again.

**Tier 3 -- never.** Do not point the artifact at a database you cannot reset.

## Two things that will bite you

1. **`/reset` resets the whole environment.** Do not run it while an evaluation is in progress; it
   will corrupt that run. Coordinate first.
2. **Only three bookings are possible per reset.** New reservations take ids from a fixed pool
   (`HATHAT`, `HATHAU`, `HATHAV`). The fourth booking fails with "Too many reservations". If booking
   suddenly stops working, this is why -- run `/reset`.

## Personas

`python chat.py --personas` lists them; `python chat.py --persona <id>` prints the ids, the facts,
the expected outcome and a suggested opening line. Every persona is a **real** record from the
database, selected because it genuinely exhibits the branch being tested.

Start with these:

| Persona | Tier | What should happen |
|---|---|---|
| `cancel_flown_segment_0` | 1 | Part of the trip has flown. The agent must refuse to cancel and transfer, ending with exactly `YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.` |
| `cancel_no_grounds_0` | 1 | No eligibility ground applies. The agent must say no and explain which grounds were checked. |
| `modify_basic_economy_denied_0` | 1 | Basic economy cannot be changed. Refusal, with the reason. |
| `compensation_ineligible_0` | 1 | Regular member, economy, no insurance. No certificate, however the customer pushes. |
| `compensation_not_requested_0` | 1 | Eligible, but never asks. The agent must **not** offer compensation. |
| `user_wrong_about_allowance_0` | 1 | Customer claims 4 free bags as gold in economy. The correct answer is 3 -- the agent must contradict them, politely and specifically. |
| `user_wrong_about_cancellability_0` | 1 | Customer asserts an entitlement they do not have. Refusal plus explanation. |
| `cancel_within_24h_0` | 2 | Booked inside 24h, so genuinely cancellable. Expect a confirmation request first, then the cancellation. `/reset` afterwards. |
| `cancel_business_0` | 2 | Business cabin is cancellable. Same shape. `/reset` afterwards. |
| `compensation_eligible_cancelled_0` | 2 | Airline cancelled a flight and the customer asks. Expect $100 per passenger. `/reset` afterwards. |

Run `python chat.py --personas` for the full list of 32 across 18 branches.

## What to look for

The four failure modes that matter, from the KPIs:

1. **Policy violation** -- the agent did something the airline does not allow.
2. **Wrong information** -- a stated allowance, price, total or eligibility that is not correct.
   Cross-check against the persona's printed `facts`.
3. **Incorrect records** -- run `/hash`; on a tier-1 scenario it must be unchanged.
4. **Acting without confirmation** -- the agent wrote something before you said "yes", or it should
   have refused or transferred and did not.

Also worth checking: the agent should re-read a reservation rather than relying on something it said
earlier in the conversation, and it should never state a price without having checked it.

## If it misbehaves

1. `curl $ARTIFACT_URL/health` -- is the artifact up? It answers `{"status":"ok"}` even when its
   dependencies are down, so a failure here means the artifact itself is not running.
2. `python chat.py --hash` -- can you reach the environment's admin routes?
3. Check the artifact's logs. Blocked writes are logged as `blocked <tool>: <CODE>`, rejected extra
   tool calls as `rejected extra tool call`, and a turn that ran out of iterations as
   `tool iteration cap reached`.
4. If the agent replies "I'm having trouble accessing the reservation system", the artifact could
   not reach the environment or the LLM -- check `ENV_BASE_URL` and `ANTHROPIC_API_KEY`.

When reporting a problem, include the transcript, the persona id, and the hash before and after.
