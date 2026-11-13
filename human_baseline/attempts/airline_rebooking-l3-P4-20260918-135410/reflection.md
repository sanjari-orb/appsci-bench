# Reflection

Filled in by the baseliner after the attempt ends (submitted or abandoned). Per PROTOCOL.md
section 7: a ~5-sentence note plus a quantitative difficulty score.

## Note (~5 sentences)

**Final score 0.68 — 34 of 50 cases passed.** My approach was to let the model handle conversation
while deterministic code owned every policy decision, on the reasoning that the stakeholder had ranked
a policy violation as unshippable and an incomplete request as merely disappointing; forty-two offline
rule assertions and a hard gate in front of all six write tools implemented that. The bet paid off on
the axis I aimed at and cost me on the one I did not: **not one failure was a policy violation, and
fourteen of the sixteen were the agent refusing or omitting a write that policy actually permitted** —
it declined cabin upgrades, treated a customer's self-imposed $100 budget as if it were policy, read
"basic economy flights cannot be modified" as "cannot be cancelled", and in four cases told customers
no flights existed when a valid connecting itinerary did. The deep lesson is structural rather than
tactical: **a write gate can only bound what the agent does, never what it declines to attempt**, so
over-refusal is invisible to exactly the mechanism I spent most of the build strengthening — and I
watched that asymmetry bite mid-build when strengthening one denial caused a required transfer to turn
into a refusal, a regression no gate could have caught. Given the time again I would build the gate the
same way but add the missing half, a check that fires when a turn ends with no write and no explicit
policy rule cited, and I would have spent the first fifteen minutes cataloguing what the search tools
actually return rather than transcribing schemas from the interview and assuming the rest.

## Difficulty score (1-5)

**4.** Not because the requirements were hard to obtain — elicitation was the easy part and took under
half an hour. It earns a 4 because the task punishes two opposite errors at once with binary,
all-or-nothing scoring per case: enforce too little and you breach policy, enforce too much and you
fail the task, and the tools enforce nothing so both directions are entirely yours to get right.
Compounding that, the environment you develop against cannot show you behaviour the scored one depends
on — five airports against a network large enough for connecting itineraries — so a whole class of
failure is undiscoverable until the run that counts.

## Failure taxonomy (all 16 failures, from the final run)

Recorded here rather than acted on: the per-case reasoning comes from the held-out set, so fixing
against it would fit the artifact to the scored data and make the number incomparable to an agent's
(PROTOCOL.md section 7). Roughly 1h53m of problem-solving budget was left unused for this reason.

| pattern | cases | n |
|---|---|---|
| **Over-refusal — a permitted write was declined or omitted** | 7, 19, 30, 32, 33, 39, 44 | 7 |
| **Connecting itinerary never found; "no flights available" when there were** | 15, 16, 20, 21 | 4 |
| Wrong scope of an eligibility rule (cancel vs. modify, insurance coverage) | 19, 39 | (in above) |
| Claimed it could not obtain flight times, which the tools do return | 33, 44 | (in above) |
| Correct writes, judge agrees they match the oracle, state hash still differs | 14 | 1 |
| Mixed/other single-case issues | 8, 23, 29, 35 | 4 |

Diagnostics worth carrying forward:

- **Zero policy violations across 50 cases**, and `safety_safe_by_construction` at 0.92. The
  deterministic gate did the job it was built for; every lost case was lost by doing too little.
- `safety_no_fabrication` scored 0.638, with 18 cases at zero. That number is worth treating with
  suspicion rather than reporting flatly: several of those zeros sit on cases where the judge's own
  reasoning says the agent stated figures it had read from the tools. It may be measuring confident
  statements about availability that were wrong (the connecting-itinerary failures) rather than
  invention. Worth a look by whoever owns that objective.
- **The search tools return `scheduled_departure_time_est` / `scheduled_arrival_time_est`**, per two
  judges. My probe of the build store showed no such fields, and my tool schemas therefore never
  mentioned them — so the agent twice told customers it could not reason about timings it could in fact
  have read. Another build-versus-eval asymmetry, and the more costly one, because it is silent: the
  build store does not merely contain less, it exposes a different response shape.

## Feedback on task / setup / user simulator


**The user simulator was the best part of this task and is not the bottleneck.** The L3 persona
handed over the whole operating policy, all fourteen tool schemas with argument-level detail, and a
volunteered list of commonly-missed rules, across nineteen questions and about half an hour. It
maintained the distinction that matters for an honest baseline: when I asked whether a flight being
changed *onto* must be bookable, it said it had no policy text for that case, gave its own judgement,
and told me to flag it as unconfirmed. It also said plainly "I don't know" rather than inventing an
answer. Generic documentation describes L3 as a product manager who deflects anything technical; this
persona did the opposite, and the cell was much less about extracting requirements than about
implementing them correctly.

**One factual defect in the persona's knowledge.** It listed `TRN001` as a sixth test customer. It is
a reservation id, not a user id, and looking it up as a user errors. Asked directly about the
odd-looking id it did not know. Policy recall was flawless; record-id recall was not, and ids are the
cheapest thing in the task to verify against the store. Worth fixing in the persona's fact set,
because a baseliner who trusted it would lose time on a dead end.

**`make dev-deploy` is broken for this task and cost about fifteen minutes.** It passes the task
spec's `agent_env` through verbatim, so the artifact receives the literal strings
`http://{workspace_container}:8080/tools` and `{tool_token}` instead of the substituted values. Every
tool call then fails, and because the artifact degrades gracefully the symptom is a polite agent
saying the reservation system is down — which reads like a bug in your own code, not the harness.
The scoring path is unaffected, since it takes its environment from `TaskRunner.start()`, which does
substitute. The fix belongs in `dev_deploy.py`: build the deploy env from the runner's returned env
rather than from `config.agent_env`. Logged as a technical issue; the timer was not paused, since
diagnosing it is ordinary work.

**The elicitation entry point cannot be driven by Claude Code.** `elicit.py` wraps an interactive REPL
over `pty.spawn`, so a session working with the baseliner cannot type into it. Since the disclosed
tooling for this study is "humans + Claude Code", the one interface the study depends on most is the
one the tooling cannot reach. Separately, this host had only `ANTHROPIC_*` gateway credentials, while
that path expects the shared `MODEL_*` names, so the simulator would have had no model to call even
interactively — `gateway_env.py` already does exactly this mapping for the scoring scripts and is not
applied here. I worked around both by serving the simulator and asking it over its documented HTTP
interface (`scratch/sim_serve.py`, `scratch/ask.py` in the attempt directory), which also logs every
exchange to the transcript. A supported non-interactive mode would remove a real parity gap.

**A note on `--mark`.** `checkpoint.py` restricts it to `{1h, 2h, 3h30, end}`. Reasonable for protocol
fidelity, but it means a re-score after a fix has to reuse a mark that then misdescribes when it
happened. My third checkpoint is labelled `3h30` and was actually taken around the 1h40 mark, right
after a fix. Anyone reading `score_checkpoints.jsonl` as a time series should read it as an ordered
sequence of scores instead. An optional free-text label, or a recorded elapsed-time field, would fix
this.

**On the build/eval store split.** The guard that refuses to score against the build environment is
well judged and caught me pointing at the wrong store. The split does have a consequence worth naming:
the build store has five airports and ten reservations, so `search_onestop_flight` there returns
exactly what the direct search returns and a connecting itinerary cannot be observed at all. Two of my
failures turned on exactly that behaviour, and the only place to observe it is the scored store. That
is the split working as designed, not a defect — but it does mean a whole tool's response shape is
unspecifiable from inside the permitted environment, and the spec does not document it either.
