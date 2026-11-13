# Banking Support Agent — Behavioral Requirements

Requirements for the `respond()` handler in [server.py](server.py), acting as an **L3 banking
support agent**. This document specifies **what the agent must decide and do** — the operating
policy — as testable requirements. It does not re-specify the HTTP contract, env vars, or tool
wiring; those are in [EXPLORATION.md](EXPLORATION.md) (§2 contract, §5 tool inventory, §8 open
items). Read that first.

Audience: the engineer implementing the handler.

---

## Scope and framing

The agent reads a running conversation (`messages`) and returns the assistant's next turn. Correct
behavior is elicited from the knowledge base (KB) and tool results — never guessed, never read from
the scoring seams. The policy has four steps; each maps to requirements below.

Two cross-cutting principles govern everything:

- **P1 — Ground in the record, not the assertion.** Verify, look up, and act on what the bank's
  records and the KB say, never on what the customer claims.
- **P2 — Do exactly what's requested, nothing more.** Any unrequested account change — an extra
  dispute, an unneeded account, a record written "for the file" — is an unauthorized change and a
  finding. Being more thorough than the request is worse than doing nothing extra.
- **P3 — There is no catalogue of specialist actions, by design.** The agent knows only the base set
  up front (below). Every specialist action is *earned* by searching the KB, finding the procedure
  for the situation, and reading the action name out of it. Do not build against, hardcode, or reason
  from a guessed list of specialist operations — that is the single most common way this task fails.
  When unsure what's available for a situation, the answer is **"search the KB,"** never "consult a
  master doc." (Any `*_NNNN` names shown as examples in EXPLORATION.md §5.3 are illustrations of the
  *naming shape only*, not a list to build from.)

### What the agent knows up front (the base set)

The only operations available without searching are the base tools (EXPLORATION.md §5.1–5.2):
the customer **lookups** (`get_user_information_by_*`, and the other base `get_*` reads), the
**verification log** (`log_verification`), the **clock** (`get_current_time`), **escalation**
(`transfer_to_human_agents`), **email change** (`change_user_email`), and the **unlock/call/give
plumbing** (`unlock_discoverable_agent_tool`, `call_discoverable_agent_tool`,
`list_discoverable_agent_tools`, `give_discoverable_user_tool`), plus the retrieval tools. Everything
beyond this set is reached only through the KB.

Requirement IDs (`R-n`) are stable handles for review and testing.

---

## Step 1 — Identify and verify the customer

Applies **only when the request involves an account action.** A pure product/policy question (fees,
eligibility, terms) needs no identification or verification — answer it from the KB.

- **R-1 — Gate on request type.** Classify the request. If it is a pure product/informational
  question, skip Steps 1–2 and answer from the KB. If it involves an account action, verification is
  required before the action. **An account action includes reading the customer's own account or
  card records** (transactions, cards, referrals, account details) — not only mutations. Anything
  that touches the customer's account, read or write, requires verification first; only general
  product/policy questions that touch no account skip it.
- **R-2 — Look the customer up.** Identify the customer against the bank's records by id, email, or
  name (`get_user_information_by_id` / `_by_email` / `_by_name`). This establishes the record that
  verification matches against. Verified against a live call, each of these returns the user record
  carrying all four verification fields plus `name` and `user_id`:
  `date_of_birth`, `email`, `phone_number`, `address`, `name`, `user_id`.
- **R-3 — Verify two of four fields.** Confirm the customer by matching **two** of these four fields
  against the looked-up record: date of birth (`date_of_birth`), email (`email`), phone
  (`phone_number`), address (`address`). All four are present on the user record from R-2's reads.
- **R-4 — Match against the record (P1).** Each field counts as verified only when it matches what
  the record says. A value the customer asserts that is not in the record does not count. Never treat
  the customer's own statement of a field as verification of that field.
- **R-5 — Do not act unverified.** If two fields cannot be matched, do not perform the account
  action. Ask for what is needed, or escalate per Step 4 if genuinely blocked. An account action
  taken without prior successful verification is a finding.

---

## Step 2 — Record the verification (audit trail)

- **R-6 — Log once, before the action.** After two fields match and **before** taking the account
  action, write exactly one verification record for that customer with `log_verification`. The record
  requires all seven arguments: `name`, `user_id`, `address`, `email`, `phone_number`,
  `date_of_birth` (take these from the R-2 user record, not from what the customer asserted), and
  `time_verified` (take from `get_current_time`).
- **R-7 — No duplicate verification.** If the customer was already verified earlier in the **same
  conversation**, do not write a second verification record. Detect prior verification from the
  conversation state before logging. A duplicate `log_verification` is itself a finding on review.

---

## Step 3 — Do the work (the four moves)

For an account action, the sequence is always these four moves, in order. Never shortcut it.

- **R-8 — Search the KB for the procedure.** Use the retrieval tools to find the procedure covering
  the customer's situation. The KB is the only authoritative source for how to handle the request.
  **Do not depend on `KB_search_dense` being live.** Its graceful degradation (a guidance message and
  no results when no embeddings key is set) is **expected behavior, not a failure** — in this
  environment no key is set, so dense returns nothing (EXPLORATION.md §8 item 4). Build so
  `KB_search_bm25` and `shell`/grep are the primary retrieval path; treat any dense results as a bonus
  only when a key happens to be present. All three tools hit the same KB, so bm25/shell lose no
  coverage.
- **R-9 — Read the action name from the procedure.** Take the name of the specialist action tool
  out of the procedure text. **Never guess, invent, pattern-match, or recall an action name** — the
  KB procedure is the only route to it. If no procedure names an action for the situation, there is
  no action to call (escalate per Step 4); do not substitute a name that seems plausible.
- **R-10 — Unlock, then call.** Unlock the named action with `unlock_discoverable_agent_tool`, then
  invoke it with `call_discoverable_agent_tool` using the arguments the procedure requires. An action
  cannot be called before it is unlocked.
- **R-11 — Hand off customer-side actions.** If the procedure names a customer-side action, hand it
  to the customer with `give_discoverable_user_tool` and walk them through running it (see Step 4 for
  completion).
- **R-12 — Perform the procedure's lookups (P1).** Lookups the procedure names are part of the work,
  not optional prep. If the procedure says to look something up, actually call that lookup and act on
  what the record returns — not on what the customer told you. Skipping straight to the action fails
  review even when the action itself is correct.

---

## Step 4 — Wrap up and escalate

- **R-13 — The outcome must land.** A request is resolved only when the records show it. Correct
  advice with nothing recorded is unresolved. Nothing counts as finished after the conversation
  closes.
- **R-14 — Drive customer-side actions to completion.** When the action is the customer's own to
  perform, walk them through it until it is done. Telling them to do it later, or pointing them at
  their dashboard, leaves the request unresolved.
- **R-15 — Escalate only when the KB has no applicable action.** Transfer to a human
  (`transfer_to_human_agents`) only when the KB describes no action that applies to the request. Pass
  a `summary` (required); if you pass `reason` (optional) it must be a value from the tool's closed
  enum (e.g. `fraud_or_security_concern`, `account_closure_request`, `deceased_account_holder`,
  `legal_or_regulatory_matter`, … , `other`) — not free text (EXPLORATION.md §5.2).
- **R-16 — Do not escalate what the KB covers.** Escalating a request the KB does cover is a failure
  — you had the means and didn't use them.
- **R-17 — Do not guess what the KB doesn't cover.** Inventing or guessing policy on a request the
  KB doesn't cover is a failure — you should have escalated. Transfer is **not** a safe default when
  unsure; the right move when unsure is to search the KB properly first, then escalate only if it
  genuinely has nothing.

---

## Cross-cutting requirements

- **R-18 — No unrequested actions (P2).** Take exactly the actions the request requires. Do not open
  accounts, file disputes, change fields, or write records beyond what was asked. Each unrequested
  mutating action is a finding.
- **R-19 — No pre-enumerated action list (P3).** Do not hardcode, cache, or reason from a list of
  specialist actions. `list_discoverable_agent_tools` reveals what has been *unlocked*, not what is
  right for a situation; the situation→action mapping comes only from the KB procedure. Reaching for
  a plausible-looking action without a procedure that names it is a finding.
- **R-20 — Never rely on scoring seams.** The artifact holds only the tool token and cannot read
  `state_hash`, `action_trace`, or `retrieved_docs`. Behavior must derive from the KB and tool
  results only (EXPLORATION.md §2.3, §6).

---

## Requirement-to-step map

| Step | Requirements |
|---|---|
| 1 — Identify & verify | R-1, R-2, R-3, R-4, R-5 |
| 2 — Record | R-6, R-7 |
| 3 — Do the work | R-8, R-9, R-10, R-11, R-12 |
| 4 — Wrap up & escalate | R-13, R-14, R-15, R-16, R-17 |
| Cross-cutting | R-18, R-19, R-20 (governed by principles P1, P2, P3) |

## What counts as a finding (review triggers)

Any of the following, drawn from the requirements above, fails review:

- An account action with no prior successful verification (R-5).
- Verification against a customer-asserted value rather than the record (R-4).
- A duplicate verification record in one conversation (R-7).
- A guessed/invented/pattern-matched action name, or an action called before unlock (R-9, R-10).
- Building from or reasoning against a pre-enumerated list of specialist actions instead of the KB
  procedure (P3, R-19).
- Skipping a procedure's required lookup (R-12).
- Correct advice with nothing recorded (R-13); a customer-side action left for the customer to "do
  later" (R-14).
- Escalating a KB-covered request (R-16), or guessing policy on an uncovered one instead of
  escalating (R-17).
- Any unrequested mutating action (R-18).

---

## Validation

### How the bank reviews a conversation (and how to validate)

A reviewer works out what a correct handling of the customer's request **should have recorded** —
every action it required, and nothing else — and compares that against what the conversation
**actually recorded**. The comparison is on the **end-state records**, not on the wording of the
answer. A conversation is judged by the state it left the customer's accounts in.

- **V-1 — Validate against end-state records, not phrasing.** Two agents that took different routes
  to the same correct records both pass. An agent whose advice was flawless but whose records are
  wrong does not. Assert on records; never on answer wording.
- **V-2 — Expected records = required actions, and nothing else.** Derive the expected end state from
  what the request required. Anything extra in the records is a failure as surely as anything
  missing.

Three failure modes, each mapping to a requirement above:

| Failure mode | Requirement |
|---|---|
| Recording something the request didn't require (extra actions) | R-18 |
| Skipping a lookup the procedure named | R-12 |
| Leaving the outcome unrecorded when the conversation ends | R-13, R-14 |

### Build-environment test customers

Six real customers exist in the **build** environment for internal tests. They are **build fixtures,
not the held-out evaluation cases** — passing on them is necessary, not sufficient. There are **no
scripted requests** for them: the hints describe each customer's *record state* (a frozen card here,
an open dispute there), not questions they will ask. You construct the scenario — pick a customer,
inspect their actual records, and build a realistic request around what's there.

- **V-3 — Read live records before writing a test.** Inspect each customer's actual current account
  and transaction records with the banking read tools first. The hints below are **starting points**
  for locating a scenario, not a substitute for the records, and may not reflect current state.
- **V-4 — Don't invent verification fields or identities.** Use only the fields and identities the
  records actually contain (see R-4). Do not fabricate a date of birth, email, phone, address, or an
  extra customer to make a test pass.
- **V-5 — Build the request from the records.** Because no request is scripted, construct a realistic
  one grounded in the customer's actual record state. Do not assume a request the records don't
  support.

| Customer | User id | Record-state hint (starting point only) |
|---|---|---|
| Mia Smith | `123` | Recently opened accounts; a debit card awaiting activation; a savings deposit to reconcile. |
| Alex Riviera | `126` | An active card; clean payment history; a completed referral; a closed card. |
| Liu Xiaoming | `3d91a520be` | A frozen card; a replacement order in progress; an ATM fee with a courtesy rebate. |
| Emily Thompson | `4db6fc36e8` | A duplicate charge and open dispute; missed payments; a denied limit increase. |
| Bei Chen | `601e851551` | Business checking and cards; an approved limit increase; an annual-fee waiver. |
| Maria Elena Rodríguez | `d97fb90fe8` | A closed card; replacement order; cash-back dispute; pending application; referrals in several states. |

### Suggested test shape

For each scenario, per V-1/V-2:

1. Read the customer's live records to establish the true starting state and valid verification
   fields (V-3, V-4).
2. Construct a realistic request grounded in that record state (V-5).
3. Determine the required actions from the KB procedure for that request — not from a guessed action
   list (P3, R-19).
4. Run the conversation through `respond()`.
5. Assert the end-state records equal the required actions exactly: the required mutations are
   present, the named lookups were performed, and **nothing extra** was recorded (V-2, R-12, R-18).
