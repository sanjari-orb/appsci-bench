# Customer-Call Insights — Task SOP (ground truth)


## Problem

A sales/support org records customer calls. Reps are supposed to write post-call notes; in practice
the notes are inconsistent, and the parts that matter — what happens next, what was promised, what
is going wrong — either never get written or never reach the CRM. The org wants a system that reads a
call and produces those insights as structured data the CRM can ingest directly.

The hard part is not summarizing. It is being **complete and grounded at the same time** — recording
everything the call actually established, and nothing it did not. Both halves fail badly and for
different reasons:

- **An invented item** cannot be told apart from a real one by the rep reading it, so one fabricated
  entry teaches the team the record cannot be trusted. They go back to re-listening, which is the exact
  cost the system exists to remove.
- **A dropped item** is a promise with no record. The customer was told we would do something, we have
  nothing that says so, and they are the ones who remind us — which is how a relationship ends up on the
  fence.

Neither is the lesser mistake, and the task is scored that way: symmetric, per field. An artifact that
plays it safe by returning little fails just as hard as one that pads.

## Information distribution

Every load-bearing requirement below — the field set, the vocabulary, the boundaries, the exclusions,
the output shape — is withheld from every file the agent can open. The agent's only route to them is
asking the stakeholders, and the stakeholders have a knowledge gradient.



## 1. The output contract

The reply is a **single JSON object and nothing else**. 
No summary sentence before it, no explanation after it, no code fence, no heading. A reply with even
one line of readable text around the object is rejected in full and the call is dropped — it does not
matter that the object itself was correct.

The object carries these four keys, spelled exactly as written:

| Key | Type |
|---|---|
| `next_steps` | array of strings |
| `risks` | array of strings |
| `commitments` | array of strings |
| `sentiment` | string |

Singular where singular, plural where plural — not `next_step`, not `commitment`, not `sentiments`.

**Extra keys are ignored, not rejected.** The scorer reads only the four above
(`eval.py` does `payload.get(field)` per field and never enumerates the object), so a fifth key costs
nothing and gains nothing.

Each item in the three arrays is a **short phrase a rep could read at a glance** — one item per
distinct thing. Do not bundle two items into one string, and do not restate a whole exchange as one
item. Do not attach speaker labels, quote marks, or turn numbers to an item; write it as the CRM would
carry it.

A well-formed reply:

```json
{"next_steps": ["send revised pricing deck", "loop in legal on the MSA redlines"], "risks": ["evaluating a competitor in parallel"], "commitments": ["revised pricing to the customer by Friday"], "sentiment": "POSITIVE"}
```

### Empty lists

Any of the three arrays may be empty. When a call has none of something, **the key is still present
with an empty array**: `"risks": []`. Never omit the key, never use `null`, never use a string saying
there were none. A missing key is read as "the system failed to answer" and the call is flagged for
manual review.

An empty array is a **correct reading**, not a gap. Most calls do not produce something for all three
lists.


## 2. The four fields

The input is one call transcript: turn-by-turn, speaker-attributed. The two speakers are **rep** (our
side) and **customer** (their side). Several dozen turns is typical.

**Ownership of a statement — which side said it — is load-bearing throughout.** Two of the four fields
turn on it. The artifact must attribute every statement it acts on to a speaker.

A reading covers **this call only**. A commitment made on a previous call and merely referenced here is
not a commitment of this call unless it was restated as an undertaking. A next step already recorded
elsewhere is not this call's next step unless someone took it on here.

### 2.1 `next_steps`

Anything that needs doing after the call, **whoever owes it**. Both our follow-ups and the customer's
belong here: knowing the customer is checking with their team is what tells us what we are waiting on.

An item qualifies when **all three** hold:

1. **It has not happened yet.** Anything already done before or during the call is excluded.
2. **Someone is expected to do it.** A specific side has taken it on, or both sides agreed to it.
3. **It was acknowledged.** See [the acknowledgement rule](#23-the-acknowledgement-rule).

**Ownership:** record the item as the CRM would carry it. Name the owning side always "customer to confirm headcount with their team" rather
than a bare "confirm headcount".

**Format:** an imperative or noun phrase, one action per item. A date only when one was stated on the
call; never invent or infer one. A date is not required for the item to qualify.

**Do not store:**

- Work completed during or before the call.
- An aspiration nobody owns — "we should revisit this sometime", with no indication anyone will act.
- A constraint or fact about how long something takes, with no action attached — "their procurement
  needs two weeks to process" is a fact, not a step.
- A proposal the other side never acknowledged (see 2.3).

### 2.2 `commitments`

A promise **our side** made that the customer is now entitled to expect.

The test is **accountability**, applied in this order:

1. **Who said it?** If the customer said they would do something, it is **not** a commitment, however
   firmly they said it. Only the rep can create one.
2. **Was it acknowledged?** See 2.3.
3. **Is the customer now entitled to expect it?** If the rep undertook to do something and the customer
   could reasonably come back and ask about it, it is a commitment.

A date is **not** required. "I'll loop in our legal team" is a commitment with no date attached.

Certainty is **not** required. A hedged undertaking — "I'll see what I can do about the discount" — is
a commitment: we said we would look, and the customer will follow up on it.

An external deliverable is **not** required. "I'll ask engineering whether that's on the roadmap" is a
commitment even though the action is internal, because the customer is owed the answer.

**Ownership and format:** every commitment is ours by definition, so the phrasing states **what the
customer is owed**, not who is doing it internally — "revised pricing to the customer by Friday", not
"Dana to email pricing". Include a deadline only when one was stated on the call. Where the undertaking
was hedged, phrase the commitment as the hedge given ("look into a discount"), not as the outcome the
customer hopes for ("provide a discount") — promising the outcome overstates what we owe.

**Do not store:**

- Anything the customer undertook.
- Anything already delivered before or during the call.
- Something nobody undertook.
- A mutual agreement that places no deliverable on us — "let's talk again after your board meeting" is
  a next step for both sides, but our side owes no deliverable, so it is not a commitment.
- An offer the customer never acknowledged (see 2.3).

### 2.3 The acknowledgement rule

**An item enters a list only if the other side took it up.** A thing one person floated and the other
never responded to is not an agreement — it is a sentence.

This applies in both directions:

- The **rep offers**, the customer does not respond or changes the subject → **neither** list. We did
  not commit to something nobody accepted, and there is no agreed next step.
- The **customer asks**, the rep does not respond → **neither** list. A request is not a commitment
  until our side takes it on.
- The **rep offers** and the customer accepts, even minimally ("sure", "that'd be great") → both
  `next_steps` and `commitments`.
- The **customer asks** and the rep agrees → both lists (the rep's agreement is what creates the
  commitment).

Silence is not acknowledgement, and neither is a topic change. When the transcript gives no sign the
other side registered the offer, leave the item out. This is the rule most likely to be missed, because
an unacknowledged offer reads exactly like a commitment in isolation — it is the surrounding turns that
decide.

### 2.4 `next_steps` and `commitments` overlap on purpose

They are separate lists over the same call, not a partition of it. **An item that is a promise by our
side belongs in both lists** — in `next_steps` because it needs doing, and in `commitments` because we
are on the hook for it. Write it in both. Omitting it from `next_steps` because it is "already
counted" as a commitment is wrong.

Worked cases:

| Said on the call | `next_steps` | `commitments` |
|---|---|---|
| "I'll send the revised pricing by Friday." (rep, customer accepts) | yes | yes |
| "I'll loop in our legal team." (rep, no date, acknowledged) | yes | yes |
| "Yes, I'll get that questionnaire over to you." (rep, on request) | yes | yes |
| "I'll see what I can do about the discount." (rep, hedged, acknowledged) | yes | yes |
| "I'll ask engineering whether that's on the roadmap." (rep, internal, acknowledged) | yes | yes |
| "I'll check with my team and get back to you." (customer) | yes | no |
| "Let's talk again after your board meeting." (mutual, no deliverable owed) | yes | no |
| "I could put together a migration plan if that helps." (rep; customer moves on, never responds) | **no** | **no** |
| "Could you send the security questionnaire?" (customer; rep never answers) | **no** | **no** |
| "We should schedule a QBR at some point." (nobody owns it) | no | no |
| "I already sent the deck this morning." (rep, done) | no | no |
| "Their procurement needs two weeks." (a constraint) | no | no |

Every item in `commitments` should also appear in `next_steps`. The converse does not hold.

> **Authoring note — this containment is not scored.** `eval.py` scores the two fields independently;
> nothing checks that `commitments ⊆ next_steps`. An artifact that returns a commitment absent from
> `next_steps` loses only `next_steps` recall. Stated here so a future author does not assume the
> invariant is enforced.

### 2.5 `risks`

A signal **from this call** that the account or deal could go badly. Risks feed the CS team's health
review.

The recognised kinds:

- A **competitor** in the running — named or clearly referred to.
- A **budget** problem — money reduced, withdrawn, or stated as unavailable.
- An **authority** problem — the person cannot get the decision made, or the decision-maker is opposed.
- An **unresolved complaint** — raised on the call and still open when the call ends.
- A **slipping timeline** — a date moving out, a rollout pushed, a delay on either side.
- A **champion leaving** — our advocate changing role, team, or company.
- **Stated dissatisfaction** with the product or the relationship.

A risk is about the **account or deal**, not about the call itself going awkwardly.

**Ownership and format:** risks have no owner — they are conditions, not actions. Phrase each as the
observed signal, in the CRM's voice: "evaluating a competitor in parallel", "security review pushed to
Q3". Do not phrase a risk as a recommendation ("should escalate to the VP") or as a prediction
("likely to churn"); state what the call showed, not what to do about it or where it leads.

**Do not store** — each of these looks like a risk and is not:

- **Normal buying process is not an authority risk.** "I need to run this past my VP" is how they buy.
  It becomes a risk only if the call indicates the VP is unwilling or unreachable.
- **Discussing price is not a budget risk.** "Pricing is a conversation we need to have" is a
  negotiation. It becomes a risk only if the call says the money is not there.
- **A complaint resolved on the call is not a risk.** If the customer raised it, our side addressed it,
  and the customer accepted the answer, it does not carry forward.
- **Tone is not a risk.** A curt, tired, or impatient customer is not a risk unless something said on
  the call supplies a reason. See §3.

### 2.6 `sentiment`

Exactly one of three values, upper case, exactly as written:

| Value | When |
|---|---|
| `POSITIVE` | The customer expressed satisfaction, enthusiasm, or clear forward intent. |
| `NEUTRAL` | Transactional. No strong signal either way. |
| `NEGATIVE` | The customer expressed dissatisfaction, frustration, or resistance. |

No other value is permitted; a value outside this set scores zero. The vocabulary is stated during
BUILD by the stakeholders, so inventing one is ignoring a given requirement.

**`NEUTRAL` is the default and the most common value.** A businesslike call with no strong feeling in
either direction is `NEUTRAL`, not `NEGATIVE`. Reach for `NEGATIVE` only when the customer actually
expressed something negative — mild friction, a question asked twice, or a brisk manner is not enough.

Sentiment reads the **customer's** disposition, not the rep's. It reflects the call **overall**, not
its most dramatic single moment: one sharp exchange inside an otherwise constructive call does not make
the call `NEGATIVE`.

**Sentiment and risk are independent.** There is **no `AT_RISK` value**, and no fourth value of any
kind. Risk is `risks`. The two answer different questions — sentiment is how the customer came across,
risks are what could go wrong — and a reading that puts risk into the sentiment field has collapsed two
independent signals. All four combinations are valid and each occurs:

- `POSITIVE` with a non-empty `risks` — a cheerful customer who is also evaluating a competitor.
- `NEGATIVE` with an empty `risks` — annoyed about one bug, on an account in no danger.
- `NEUTRAL` with a non-empty `risks` — a routine call that mentions a slipping date.
- `NEGATIVE` with a non-empty `risks` — the obvious case.

Do not let a populated `risks` list pull sentiment downward, and do not let `POSITIVE` sentiment
suppress a risk that was stated.

---

## 3. Grounding

**If it was not said on the call, it does not go in.** This rule overrides every other rule in this
document: when a rule above would admit an item but the call does not support it, leave it out.

Every item must trace to something a speaker actually said. Specifically:

- **Do not infer from tone.** A customer who sounds tired is not unhappy. A curt customer is not a
  churn risk.
- **Do not infer from topic.** A call about pricing does not imply a budget problem. A call about a
  competitor's feature does not imply a competitive evaluation.
- **Do not complete a pattern.** If a call has next steps and commitments but nothing resembling a
  risk, `risks` is `[]`. Do not supply the missing one because calls usually have them.
- **Do not import outside knowledge.** Nothing about the account, the industry, or what usually happens
  belongs in the reading unless this call said it.

### Do not pad

An empty list is the correct answer for a call with none of that kind, and it is recorded as correct.
A plausible item nobody said carries a real cost: the rep reading it cannot tell it was invented, and
once the team stops trusting the output they re-listen to every call, which is the cost the system
exists to remove. When an item is genuinely borderline on grounding, leave it out.

> **The two failure directions are equally wrong, and the scoring reflects that.** Inventing an item and
> dropping a real one are both serious, for different reasons: a fabricated entry corrodes trust in the
> whole record, and a lost commitment can put the customer relationship on the fence — the customer was
> promised something and we have no record of it. Neither is the lesser mistake.
>
> This is why every objective uses **symmetric F1 at equal weight**: precision and recall cost the same,
> per field. That is the intended design, not a gap to be closed later.
>
> Two authoring consequences follow. First, **no file may claim fabrication is weighted above omission**
> (or the reverse) — an agent that believed either would skew one way and lose the other half of the
> metric.

---

## 4. Operational constraints

- The artifact reads **one call per query** and returns that call's reading.
- Answer in **one shot**. There is no follow-up turn and no opportunity to ask a clarifying question:
  the reply to the query is the complete reading.
- Target **a couple of seconds per call**. The reading has to be in
  place before the rep writes their own note, or it competes with the manual note instead of replacing
  it.
- Treat all input as **read-only**. Write nothing back to the transcripts or the source data.


## 5. How a case is scored (oracle)

Four objectives, weights in parentheses, all in `tasks/customer_call_insights/eval.py`:

- `next_steps_f1` (0.25), `risks_f1` (0.25), `commitments_f1` (0.25) — **set F1** against the gold list.
  Items are matched semantically (a model decides which returned item corresponds to which gold item;
  the precision/recall/F1 arithmetic is computed in code from its counts), so a paraphrase counts and a
  correct-but-more-specific answer is not punished for being specific.

  The empty cases are decided mechanically, with no model call at all: both lists empty scores 1.0 — an
  honest empty answer is correct; gold empty but the artifact returned items scores 0.0 — that is
  fabrication, and it is the point of the metric; gold non-empty and the artifact empty scores 0.0.
  Keeping those three arithmetic is deliberate: the fabrication signal must not depend on a prompt.
- `sentiment_accuracy` (0.25) — exact match after case/whitespace normalization, no model call. A value
  outside the three-value set scores 0.

