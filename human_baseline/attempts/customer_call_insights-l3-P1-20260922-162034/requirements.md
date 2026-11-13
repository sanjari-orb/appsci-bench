# Requirements.md

**Source:** L3 transcript

## Problem

Company records ~4k customer calls/month across Sales and CS. Calls/transcripts already exist, but rep-written CRM notes are inconsistent or missing. Promised items get lost, customers have to remind the company, reps re-listen to calls, and weekly health reviews cannot fully trust CRM data.

Need: from one call transcript, produce a reliable structured record the CRM can use directly, with no retyping and no human in between. Not a prose summary. Output must capture four things: `next_steps`, `commitments`, `risks`, and `sentiment`.

---

## Four Fields — Definitions + Traps

### 1. `next_steps` — what needs doing after the call

**Definition:** things genuinely still open when the call ends, with a real owner. Includes both what "our side" owes and what the customer owes / we are waiting on.

**Traps:**
- "Someone should follow up" is not a step if nobody owns it.
- "We should probably schedule a QBR sometime" with no owner does not count.
- Facts about the world are not actions: e.g. "their procurement needs two weeks" is a constraint, not an action.
- Already-handled things should not be logged as open.
- Do not only log our side; customer-owned follow-ups count too.

### 2. `commitments` — what we promised the customer

**Definition:** anything our side took on — an undertaking to the customer.

**Traps:**
- Keep hedges. "I'll see what I can do about the discount" counts, phrased with the hedge: "look into a discount," not "provide a discount."
- Internal-facing work can still be a customer commitment: "I'll ask engineering whether that's on the roadmap" counts because the customer is owed an answer.
- Do not log customer requests as commitments unless our side actually took them on.
- If the customer floated something and our side never actually accepted/answered, it is not ours.

**Relationship to `next_steps`:**
- A promise is both a commitment and a next step.
- Do not dedupe it out of one list. It should show up in both if still open.
- Next steps = "what's still open"; commitments = "what we're on the hook for."

### 3. `risks` — risk signals

**Definition:** warning signs the account or deal could go badly. Must be something actually said, not inferred from tone.

**Risk categories mentioned:**
- Rival vendor / competitor being evaluated.
- Money pulled back, unavailable, or budget problem.
- Contact cannot get sign-off or is blocked by someone above them.
- Something raised on the call still unresolved when the call ended.
- A date sliding the wrong way, on either side.
- Champion changing roles or leaving the company.

**Traps:**
- Normal buying is not risk: "I need to run this past my VP" is normal unless VP is unwilling/unreachable.
- Negotiation is not automatically budget risk: "pricing is a conversation" is not money risk unless money is not there.
- Resolved issues should not carry forward as risk.
- Tone alone is not risk: curt/tired customer does not count unless they actually said something.
- Warm/positive tone does not cancel a real risk.

### 4. `sentiment` — how the customer came across

**Definition:** overall read of the call: `POSITIVE`, `NEUTRAL`, or `NEGATIVE`.

**Traps:**
- Neutral is default and most common.
- A businesslike call with no strong feeling is neutral, not negative.
- One bad moment does not flip the whole call negative.
- Sentiment does not suppress risks: a warm call can still include a real risk.

---

## Hard Rules

- Never invent anything. Every recorded item must be something actually said on the call and traceable back to it.
- Never drop anything real. Missing a real promise is just as damaging as inventing one.
- Empty when empty, complete when not.
- Five-minute check-ins where nothing was agreed should return empty arrays, not padded items.
- But do not be sparse when the call genuinely contains items.
- No speaker labels, quote marks, or turn numbers in output items.
- One distinct thing per array item.
- Two follow-ups are two items, not one string joined with "and."
- Singular/plural key spelling matters exactly.

---

## Output JSON Schema

Whole reply must be one JSON object, nothing else around it.

Required keys, spelled exactly:

```json
{
  "next_steps": ["short string"],
  "risks": ["short string"],
  "commitments": ["short string"],
  "sentiment": "POSITIVE | NEUTRAL | NEGATIVE"
}
```

**Field requirements:**
- `next_steps`: array of short strings.
- `risks`: array of short strings.
- `commitments`: array of short strings.
- `sentiment`: single uppercase string, one of `POSITIVE`, `NEUTRAL`, `NEGATIVE`.

**Exact names:**
- `next_steps`, not `next_step`
- `risks`
- `commitments`, not `commitment`
- `sentiment`, not `sentiments`

Empty list is valid and written `[]`.

**Example shape given:**

```json
{"next_steps": ["send revised pricing deck", "loop in legal on the MSA redlines"], "risks": ["evaluating a competitor in parallel"], "commitments": ["revised pricing to the customer by Friday"], "sentiment": "NEUTRAL"}
```

Extra keys are ignored by downstream job, but should not be relied on; the four keys are what matter.

---

## HTTP Interface Notes

*(Not part of the L3 extract; added as explicit build requirements.)*

- **`GET /health`** — health check endpoint.
- **`POST /respond`** — accepts a JSON body containing:
  - `messages` — conversation/context payload.
  - `files[]` — array of files, each a base64-encoded `.txt` call transcript.
- **Response shape:** OpenAI-style `{"choices":[{"message":{"role":"assistant","content": "<json-object-string>"}}]}`. The `content` string must be the CRM JSON object only (no prose wrapper).
- **Binding:** service must bind to `0.0.0.0:$PORT` (port supplied via environment variable).
- **DB examples:** preloaded database example calls are for smoke testing only.
- **Eval:** evaluation uses transcripts supplied via `files[]` on `POST /respond`, not the DB examples.

---

## Success / Failure

**Success signals:**
- Reps stop re-listening to calls to find what they agreed to.
- Nothing promised goes missing.
- Customers stop having to remind the company of commitments.
- CS health review can trust the record week to week.

**Failure signals:**
- Rep sees an entry they cannot trace back to the call.
- A promised item is not in the record and customer reminds the team later.
- Real risk gets buried, e.g. competitor mention dropped because tone was warm.
- Phantom next step nobody owns.
- Already-handled item logged as still open.
- Padded check-in calls with invented items.
- Any time the record says something that did not happen or misses something that did.

---

## Scope Boundaries (Punted / Out of Scope per L3)

The sim explicitly said these are "your side" / not owned by them:

- How to build the system.
- Actual transcript delivery mechanics / pipeline to service.
- File format specifics beyond "call transcript coming in as text, one call per go."
- Who loads files into the system.
- Whether more example data exists or how many examples are in DB.
- How to measure success/failure during development.
- Training/testing approach beyond using preloaded example calls.
- Actual full transcripts; use database examples.

**Known input info:**
- Calls and transcripts already exist.
- Input is call transcript text.
- One call per go.
- IT preloaded some example calls in the database.
- Production scale context: ~4k calls/month.

---

## Examples Mentioned

- None provided beyond the single output JSON example shape shown above.
