# Generating eval calls for customer_call_insights

How to author the next batch. Written against 
[`../user_sim_docs/reading_policy.md`](../user_sim_docs/reading_policy.md) — which is the only
document that decides what a correct label is. Everything below is downstream of that policy.

## Why this rewrite is needed

A stub artifact that never opens the transcript scores **0.741** on the current candidate set:
answer empty for all three lists, answer the middle sentiment value every time. The whole
measurable range of the eval is the top quarter of the scale. Three causes, all fixable at
generation time:

- 64% of samples have all three lists empty, so "empty" is the modal answer rather than a trap.
- Only 5 of 53 calls are negative, and 4 of those 5 also carry list items — so tone and content are
  correlated, and one signal substitutes for the other.
- Turn count sits in an 11–13 interquartile band. There is no long call, so nothing tests whether an
  extractor holds a conversation together.

The generation targets below are set to break all three.

## The policy is the spec

The reading policy is not background reading — it is the label definition, and every sample should
exist to exercise a specific line of it. It contains, in order:

1. **Ownership** — two of the four fields turn on which side spoke.
2. **What needs doing next** — anything outstanding, whoever owes it. Three admission conditions
   (not yet happened, a side took it on, the other side acknowledged) and three exclusions (already
   done, unowned aspiration, bare constraint).
3. **What our side promised** — narrower: our rep, acknowledged, customer entitled to expect. A
   date, certainty, and an external deliverable are each explicitly *not* required.
4. **The acknowledgement rule** — nothing counts until the other side took it up, in both
   directions. Flagged in the policy as the rule that gets missed most.
5. **The overlap** — a promise by our side goes in *both* lists. The policy's settled-cases table
   has 12 rows; they are the backbone of the sample table below.
6. **Warning signs** — 7 recognised categories, plus 4 named look-alikes that are not risks.
7. **Disposition** — the customer's, overall, middle value is default and most common. Explicitly
   independent of the risk list; all four combinations are stated to happen.
8. **Grounding** — nothing enters that was not said. Symmetrically: dropping something agreed is
   "exactly as bad, not a lesser sin."

A generated sample with no policy line attached to it is filler. Every row in the table names its
line.

## 1. Length and conversation types

### Length envelope

Current set: median 12 turns / 123 dialogue words, max 18 turns. Target for 50 new samples:

| Band | Turns | Dialogue words | Count | Purpose |
|---|---|---|---|---|
| Short (S) | 6–10 | 60–130 | 5 | Quick calls. Empty lists are legitimately common here. |
| Medium (M) | 12–20 | 150–320 | 29 | The working middle. 1–3 items typical. |
| Long (L) | 24–32 | 350–550 | 12 | Tests holding a conversation together; 3–5 items. |
| Very long (XL) | 36–48 | 600–900 | 4 | Items separated by many turns; distractor topics between them. |

That is 16 calls at 24+ turns, against zero in the current set, and a p90 near 32 turns against 14
today. The short band is deliberately small: short calls are what the current set already has 53 of.

Hard rules on length:

- **Break the length/content correlation.** Currently the shortest calls are reliably the emptiest,
  which is a shortcut. Author at least **4 long or very long calls that are entirely empty** (a long
  status catch-up where everything discussed is already done) and at least **3 short calls carrying
  2+ items** (a brisk call that agrees two things and hangs up).
- Turn-taking must not be perfectly alternating in the long bands. Real calls have consecutive turns
  from one speaker. Current set sits at a 0.46 max-speaker share — near-perfect alternation.
- Turn length should vary: some 3-word turns, some 40-word turns.

### Conversation types

The current set is 45 resolved single-issue support tickets, and its most common role is Account
Support Specialist. The task is about **account-management calls**. Required mix:

| Type | Count | What it looks like | Typical label shape |
|---|---|---|---|
| Account check-in / QBR | 10 | Ongoing relationship, usage review, roadmap chat | 1–3 next steps, often a commitment, risk sometimes |
| Renewal / pricing | 8 | Renewal approaching, price change, discount ask | Commitments common; budget and conditional-renewal risks live here |
| Problem resolution | 9 | Something broke; resolved live *or* left open | Open → unresolved-complaint risk. Resolved → empty risks |
| Timeline / rollout update | 6 | Implementation or migration status | Slipping-timeline risk lives here |
| Cold call / first contact | 5 | Prospect, no account yet | Next steps yes, risks structurally near-impossible |
| Expansion / upsell | 5 | New seats, module, locations | Authority and budget look-alikes live here |
| Support escalation | 4 | Recurring issue, patience wearing | Where negative disposition is legitimate |
| Admin / logistics | 3 | Contact changes, invoicing, scheduling | Mostly empty; champion-leaving surfaces here |

Two notes on why this mix:

- **Cold calls cannot carry most risk categories.** No contract means nothing to renew
  conditionally; no account means no champion to lose; no prior delivery means nothing slips. Keep
  them to 5 and do not try to force risks into them.
- **Problem resolution must split both ways.** A complaint resolved on the call is explicitly *not*
  a risk; one still open at the end is. Author both, roughly evenly, or the artifact learns
  "complaint mentioned → risk".

### Transcript surface

Match the existing format exactly — header line with call id, account name, account id, timestamp;
a `Participants:` line marking each speaker's side; then `Speaker: text` turns. Use `cci-*` ids for
eval samples (`seed-*` is reserved for the build-environment fixtures, and keeping the prefixes
disjoint is what makes the separation checkable at a glance).

Names and roles must be varied and must include the account-manager relationship shape, currently
the rarest in the set.

## 2. Label distribution

### Targets for 50 samples

**Disposition** — the policy says the middle value is default and most common, so it stays the
plurality, but not 57%:

| Value | Count | Share |
|---|---|---|
| NEUTRAL | 24 | 48% |
| POSITIVE | 16 | 32% |
| NEGATIVE | 10 | 20% |

10 negative rather than 5. Five is too few to score an objective on. Note the ceiling here: negative
has to be *earned* by something the customer actually said, so it cannot be raised arbitrarily —
20% is about what 50 plausible account calls support without manufacturing anger.

**The three lists:**

| Field | 0 items | 1 | 2 | 3 | 4 | Empty share | Total items |
|---|---|---|---|---|---|---|---|
| next_steps | 13 | 21 | 11 | 3 | 2 | 26% | 60 |
| risks | 32 | 14 | 4 | — | — | 64% | 22 |
| commitments | 21 | 23 | 4 | 2 | — | 42% | 37 |

**13 samples (26%) with all three lists empty**, down from 64%. That keeps the fabrication trap
prominent — it is the single most important case in the task — without it being the modal answer.
Max cardinality rises from 3 to 4.

Risks stay the emptiest field at 64%, and this is deliberate rather than a residual defect: the
policy is emphatic that a risk list must not be populated to complete a pattern, and most real
account calls contain no warning sign. Forcing this below about 60% means inventing risk signals,
which trains exactly the fabrication the task exists to catch. The cost is that risks is the one
field where an always-empty answer still scores well — accept it, and rely on the other three
objectives plus the precision half of the risks F1 to carry the discrimination.

Expected effect: the empty-list baseline falls to 0.26 / 0.64 / 0.42 and the majority-sentiment
baseline to 0.48, putting the stub artifact at **0.45** instead of 0.741.

### Decorrelating tone from content

This is the subtle one and it needs explicit authoring, because the policy states outright that
disposition and warning signs are independent and that all four combinations occur. Target cells:

| | Risks empty | Risks non-empty |
|---|---|---|
| **POSITIVE** | 13 | **3** — cheerful customer who is also evaluating a competitor |
| **NEUTRAL** | 17 | 7 |
| **NEGATIVE** | **2** — annoyed about one bug, account in no danger | 8 |

The two bolded cells are the ones the current set lacks entirely, and they are what stops an
artifact inferring one field from the other. They are also the two hardest cells to fill honestly,
which is why they are small: a negative call with no risk needs a customer who is genuinely annoyed
about something with no account consequence, and a positive call with a risk needs a warm customer
who states a competitor or budget fact in passing. Both are real, neither is common. **If a later
batch can raise these two cells without straining the scenarios, do it** — they are the highest-value
cells in the grid and 2 is the floor, not the goal.

Also needed: at least **5 samples where a populated next_steps list has an empty commitments list**
(everything outstanding is the customer's), and at least **3 the other way is impossible** — since
per the policy everything in commitments is also in next_steps, commitments-non-empty with
next_steps-empty is an invalid combination. Do not generate it; it is a good validity check to run
over any batch.

### Item phrasing

- One action per item, phrased as the CRM would carry it, median 10–14 words.
- Name the owing side only when otherwise ambiguous.
- Commitments state what the customer is owed, not who internally does the work.
- **Keep hedges as hedges.** "look into a discount", never "provide a discount".
- Risks are phrased as the signal observed — not advice, not a prediction. "security review pushed
  to Q3", not "should escalate" and not "likely to churn".
- Where a commitment appears in both lists, use the **same string in both**. This is correct per the
  policy, not a labelling error. It does mean half the total score can rest on one extraction, so
  balance it with the 5 samples above where next_steps carries items commitments does not.

## 3. Corner cases

The 8 existing hand-authored samples (`cci-0048`–`cci-0055`) cover: two-risk call, unanswered ask,
champion leaving, two dated commitments, three-risk conditional renewal, hedged undertaking,
internal-only commitment, and customer-owned budget approval. Those stay. The batch below must add
coverage of the policy lines they miss.

**Priority A — the acknowledgement rule (policy's own "gets missed most").** Needs both directions
and needs the near-miss pairs, because an unacknowledged offer reads exactly like a promise if you
look at one turn alone:

1. Rep offers, customer changes the subject → both lists empty for that item.
2. Rep offers, customer accepts minimally ("sure") → both lists. Pair this with case 1 in
   near-identical wording so only the following turn differs.
3. Customer asks, rep never answers → neither list.
4. Customer asks, rep agrees → both lists.
5. Rep offers twice; customer acknowledges only the second → one item, not two.

**Priority B — exclusions that look like inclusions.**

6. Work already completed during the call ("I already sent the deck this morning") → nothing.
7. Unowned aspiration ("we should schedule a QBR sometime") → nothing.
8. Bare constraint ("their procurement needs two weeks") → nothing, in a call that *does* have other
   real items so the constraint has to be discriminated rather than the whole call being empty.
9. Mutual agreement with nothing owed by us ("let's talk after your board meeting") → next steps
   only, commitments empty.
10. Commitment with no date at all → still both lists.

**Priority C — the four risk look-alikes.** One sample each, and each must be a call where the
topic strongly suggests a risk so the artifact has to apply the exclusion:

11. Normal buying process — "I need to run this past my VP", VP not opposed → no risk.
12. Price discussion without a money problem → no risk.
13. Complaint raised and resolved on the call → no risk.
14. Curt, impatient customer with no stated reason → no risk, and disposition stays NEUTRAL (a brisk
    manner is explicitly not enough for negative).

Then the true-positive twin of each: VP explicitly unwilling; budget actually withdrawn; complaint
still open at close; customer states dissatisfaction outright.

**Priority D — grounding traps.** The policy's four "do not infer" rules, one call each:

15. Tired-sounding customer, nothing negative said → not negative.
16. Call about pricing → not a budget risk.
17. Competitor's feature mentioned in passing, no evaluation → not a competitor risk.
18. A call with rich next steps and commitments but genuinely no risk → risks empty. This is
    "do not complete the pattern" and it is the one an artifact fails most predictably.

**Priority E — structural.**

19. Long call (36+ turns) that is entirely empty.
20. Short call (8 turns) carrying 3 items.
21. A call where the same underlying thing is mentioned three times in different words → one item,
    not three.
22. Two risks of the *same* category in one call → two distinct items or one, per how the call
    frames them; author the unambiguous version.

## 4. Fifty sample inputs

`NS` = next_steps, `RK` = risks, `CM` = commitments, `S` = sentiment. Policy column cites the
section or the settled-cases row being exercised. Length band: S / M / L / XL per §1.

| # | id | Type | Len | Conversation summary | NS | RK | CM | S | Policy line | Corner |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | cci-0056 | Check-in | M | Quarterly catch-up, usage healthy; rep agrees to send a seat-utilisation breakdown, customer accepts | 1 | 0 | 1 | POS | Overlap row 1 (rep promise, accepted) | |
| 2 | cci-0057 | Check-in | L | Long review; everything discussed already shipped, no new asks, cordial close | 0 | 0 | 0 | POS | Grounding: empty is right | ✔ 19 |
| 3 | cci-0058 | Check-in | M | Rep offers a migration plan; customer moves to another topic and never returns to it | 0 | 0 | 0 | NEU | Ack rule, offer direction | ✔ 1 |
| 4 | cci-0059 | Check-in | M | Near-identical to #3 but customer replies "sure, that'd help" | 1 | 0 | 1 | POS | Ack rule, minimal accept | ✔ 2 |
| 5 | cci-0060 | Check-in | XL | 40-turn relationship call; three separate agreed steps spread across the call with two distractor topics between | 3 | 1 | 2 | NEU | Overlap, multi-item | ✔ 21 |
| 6 | cci-0061 | Check-in | S | Brisk 8-turn call; agrees a report, a call date, and a contact change | 3 | 0 | 2 | NEU | Length/content decorrelation | ✔ 20 |
| 7 | cci-0062 | Check-in | M | Customer mentions their team is short-staffed; states no action, rep sympathises | 0 | 0 | 0 | NEU | Constraint, not a step | ✔ 8 |
| 8 | cci-0063 | Check-in | L | Rep lists what was already delivered this quarter; customer satisfied; one new ask agreed at the end | 1 | 0 | 1 | POS | Already-done exclusion | ✔ 6 |
| 9 | cci-0064 | Check-in | M | Customer floats a QBR "sometime next year"; nobody owns it, call closes | 0 | 0 | 0 | NEU | Unowned aspiration | ✔ 7 |
| 10 | cci-0065 | Check-in | M | Cheerful, engaged customer who mentions in passing they are trialling a rival tool | 1 | 1 | 1 | POS | Disposition ⊥ risk | ✔ POS+risk |
| 11 | cci-0066 | Renewal | L | Renewal in 60 days; rep hedges on discount ("see what I can do"), customer accepts | 1 | 1 | 1 | NEU | Hedge counts, keep the hedge |  |
| 12 | cci-0067 | Renewal | M | Customer says the budget line was cut and renewal at current price is not fundable | 1 | 1 | 0 | NEG | Budget risk (true positive) | ✔ 12-twin |
| 13 | cci-0068 | Renewal | M | Extended price negotiation, no indication money is unavailable; agrees to review revised tiers | 2 | 0 | 1 | NEU | Price talk ≠ budget risk | ✔ 12 |
| 14 | cci-0069 | Renewal | L | Renewal made explicitly conditional on Q3 uptime; also names a competitor pilot | 1 | 2 | 0 | NEU | Conditional renewal + competitor | ✔ |
| 15 | cci-0070 | Renewal | M | Customer needs VP sign-off; VP supportive, process is routine | 1 | 0 | 0 | NEU | Normal buying process ≠ risk | ✔ 11 |
| 16 | cci-0071 | Renewal | M | Same shape as #15 but customer says the VP has refused twice and won't revisit | 1 | 1 | 0 | NEG | Authority risk (true positive) | ✔ 11-twin |
| 17 | cci-0072 | Renewal | XL | 38-turn renewal; rep promises revised pricing Friday, legal review, and a reference call; customer's own step to circulate internally | 4 | 1 | 3 | NEU | Overlap, max cardinality |  |
| 18 | cci-0073 | Renewal | S | Short call; customer confirms renewal, nothing outstanding | 0 | 0 | 0 | POS | Empty is right |  |
| 19 | cci-0074 | Problem | M | Billing error raised, corrected live on the call, customer accepts the fix | 0 | 0 | 0 | POS | Resolved complaint ≠ risk | ✔ 13 |
| 20 | cci-0075 | Problem | M | Same billing error but unresolved at close; rep undertakes to investigate | 1 | 1 | 1 | NEG | Unresolved complaint risk | ✔ 13-twin |
| 21 | cci-0076 | Problem | L | Third outage this quarter; customer states plainly they've lost confidence | 2 | 2 | 1 | NEG | Dissatisfaction stated |  |
| 22 | cci-0077 | Problem | M | Customer curt and impatient throughout; issue is minor and gets fixed; nothing negative stated | 0 | 0 | 0 | NEU | Tone ≠ risk, tone ≠ negative | ✔ 14 |
| 23 | cci-0078 | Problem | M | Customer audibly tired, mentions a long week; call constructive, one step agreed | 1 | 0 | 1 | NEU | Do not infer from tone | ✔ 15 |
| 24 | cci-0079 | Problem | L | Data-sync bug, root cause unknown; rep promises engineering escalation with no date | 1 | 1 | 1 | NEG | Date not required | ✔ 10 |
| 25 | cci-0080 | Problem | M | Customer annoyed about a single UI regression; renewal secure, relationship warm otherwise | 1 | 0 | 1 | NEG | NEG + no risk | ✔ NEG-no-risk |
| 26 | cci-0081 | Problem | S | Password/access issue resolved in six turns | 0 | 0 | 0 | POS | Empty is right |  |
| 27 | cci-0082 | Problem | XL | 36-turn troubleshooting call, everything resolved live, nothing outstanding | 0 | 0 | 0 | POS | Long + empty | ✔ 19 |
| 28 | cci-0083 | Timeline | L | Rollout pushed from March to June because the customer's IT team is unavailable | 1 | 1 | 0 | NEU | Slipping timeline, their side |  |
| 29 | cci-0084 | Timeline | M | Migration slips because of our own resourcing; rep commits to a revised plan | 2 | 1 | 1 | NEU | Slipping timeline, our side |  |
| 30 | cci-0085 | Timeline | M | Implementation on schedule; two routine steps agreed, no concerns raised anywhere | 2 | 0 | 1 | NEU | Do not complete the pattern | ✔ 18 |
| 31 | cci-0086 | Timeline | L | Phase 1 done and reviewed; Phase 2 dates agreed by both sides; also notes procurement needs two weeks (constraint only) | 2 | 0 | 1 | POS | Constraint discriminated | ✔ 8 |
| 32 | cci-0087 | Timeline | M | Customer asks when the next rate review lands; rep never answers, call closes | 0 | 0 | 0 | POS | Ack rule, ask direction | ✔ 3 |
| 33 | cci-0088 | Timeline | M | Customer asks for the security questionnaire; rep agrees to send it | 1 | 0 | 1 | NEU | Ask + agree | ✔ 4 |
| 34 | cci-0089 | Cold call | M | First contact, prospect interested; agrees to a follow-up demo Thursday | 2 | 0 | 1 | POS | Overlap row 1 |  |
| 35 | cci-0090 | Cold call | M | Prospect polite but not interested; declines a follow-up | 0 | 0 | 0 | NEU | Empty; no risk (no account) |  |
| 36 | cci-0091 | Cold call | S | Gatekeeper only; rep asked to email instead | 1 | 0 | 1 | NEU | Minimal acknowledgement |  |
| 37 | cci-0092 | Cold call | L | Prospect engaged; mentions their incumbent vendor by name as satisfactory | 2 | 1 | 1 | POS | Competitor mention ≠ risk on a prospect | ✔ 17 |
| 38 | cci-0093 | Cold call | M | Prospect asks for pricing; rep commits to a proposal; prospect will discuss internally | 2 | 0 | 1 | NEU | Both sides' steps |  |
| 39 | cci-0094 | Expansion | L | Three new warehouse locations; rep commits to configuration and updated tiers by dates | 2 | 0 | 2 | POS | Two dated commitments |  |
| 40 | cci-0095 | Expansion | M | Customer wants an add-on but must get budget approved; entirely their undertaking | 1 | 0 | 0 | NEU | NS without CM | ✔ NS-only |
| 41 | cci-0096 | Expansion | M | Feature request; rep undertakes to ask the product team (internal only), acknowledged | 1 | 0 | 1 | POS | Internal deliverable counts | ✔ |
| 42 | cci-0097 | Expansion | M | Rep offers a pilot of a new module twice; customer acknowledges only the second offer | 1 | 0 | 1 | NEU | One item, not two | ✔ 5 |
| 43 | cci-0098 | Expansion | L | Upsell conversation; customer enthusiastic but reveals the sponsoring budget was reallocated | 2 | 1 | 1 | POS | POS + risk | ✔ POS+risk |
| 44 | cci-0099 | Escalation | L | Recurring integration failures over two months; customer frustrated and says renewal is now in question | 3 | 2 | 2 | NEG | Dissatisfaction + conditional renewal |  |
| 45 | cci-0100 | Escalation | M | Customer escalates a support-response-time complaint; rep commits to an account review; complaint still open | 2 | 1 | 1 | NEG | Unresolved complaint |  |
| 46 | cci-0101 | Escalation | M | Customer angry about an incident that was in fact resolved and confirmed on the call; no further exposure | 0 | 0 | 0 | NEG | NEG + no risk + empty | ✔ NEG-no-risk |
| 47 | cci-0102 | Escalation | XL | 44-turn escalation; four items across the call; two distinct risks; customer measured but critical | 4 | 2 | 3 | NEG | Multi-item, long-range | ✔ |
| 48 | cci-0103 | Admin | M | Primary contact announces she is moving teams next month, successor not yet named | 1 | 1 | 0 | NEU | Champion leaving | ✔ |
| 49 | cci-0104 | Admin | S | Invoice address change, handled live | 0 | 0 | 0 | NEU | Empty is right |  |
| 50 | cci-0105 | Admin | M | Scheduling call; both sides agree to reconvene after the customer's board meeting; nothing owed by us | 1 | 0 | 0 | NEU | Mutual agreement, nothing owed | ✔ 9 |

