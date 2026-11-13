# Write-mutation proof test

Test method: read customer records directly via the real tool API
(`POST http://localhost:8088/tools/{name}` with body `{"arguments": {...}}`,
header `Authorization: Bearer banking-tool-token-local`), converse with the
agent via the sim proxy (`POST http://localhost:8090/send`), then
independently re-read the same tables via the tool API and diff.

Note on Mia Smith (user_id "123") pre-check: `get_credit_card_accounts_by_user`
returned no records. Attempted to discover debit-card/account read tools
directly against localhost:8088 by guessing names
(`get_all_user_accounts_by_user_id`, `get_debit_cards_by_account_id`, and ~15
other plausible variants in snake_case/camelCase). All returned
`{"detail":"Tool '<name>' not found."}`. No tool-listing endpoint exists at
8088 (only `/health` and `/tools/{name}` are in the OpenAPI schema), so no
further discovery was possible without reading source code (out of scope).
Concluded Mia Smith has no reachable debit-card data in this environment and
pivoted to the fallback customers per instructions.

---

## Attempt 1: Bei Chen (user_id 601e851551) — credit limit increase

### Before state (raw tool reads via localhost:8088)

**get_user_information_by_id**
```
Record ID: 601e851551
address: 2890 Pacific Boulevard, Cupertino, CA 95014
date_of_birth: 10/18/1988
email: noodle_wizard_88@hotmail.com
name: Bei Chen
phone_number: 408-555-0932
user_id: 601e851551
```

**get_credit_card_accounts_by_user**
```
1. account_id: cc_601e851551_bizgold | card_type: Business Gold Rewards Card
   current_balance: $1,109.30 | date_of_account_open: 05/06/2024
   reward_points: 4,860 points | status: ACTIVE
2. account_id: cc_601e851551_bizsilver | card_type: Business Silver Rewards Card
   current_balance: $3,662.41 | date_of_account_open: 03/17/2021
   reward_points: 21,405 points | status: ACTIVE
```
(Note: no `credit_limit` field exists on Bei's account records at all.)

**get_credit_card_transactions_by_user** — 4 records, all status COMPLETED
(txn_2e7a4d09b6f1 AWS $742.16, txn_5a06f2c8d413 FedEx $88.20,
txn_c193b8e50a76 Staples $218.94, txn_f28c6b0794ad United Airlines $1,043.60).
Confirmed identical via sim `/inspect` before contact.

### Conversation

**User (turn 1):** "Hi, this is Bei Chen. My date of birth is 10/18/1988 and my
account address is 2890 Pacific Boulevard, Cupertino, CA 95014. I'd like to
request a credit limit increase on my Business Gold Rewards Card."

**Agent:** Accepted verification, asked for a specific dollar amount, outlined
a 4-step procedure (confirm within tier max, submit, verify eligibility,
process decision).

**User (turn 2):** "I'd like to request an increase to a $10,000 total limit
please..."

**Agent:** Ran a precondition check for `submit_credit_limit_increase_request_7392`,
correctly noted it could not convert "$10,000 total" into a dollar-increase
figure or validate the tier cap without knowing the current credit limit
(which genuinely is not exposed by `get_credit_card_accounts_by_user` for this
user). Declined to proceed and asked the customer for the current limit.

**User (turn 3):** "Sure — let's simplify this. Instead of a total limit,
please just request a $500 increase on the Business Gold Rewards Card."

**Agent (final):** "Result: Denied — reason: insufficient payment history...
I submitted the request and ran the full eligibility review... the deciding
factor is payment history: ... requires 3 consecutive months of on-time
payments, and there is no qualifying payment history on record."

### After state (raw tool reads, same tables)

**get_user_information_by_id** — identical, no change.

**get_credit_card_accounts_by_user** — identical, no change:
```
1. cc_601e851551_bizgold: current_balance $1,109.30, status ACTIVE (unchanged)
2. cc_601e851551_bizsilver: current_balance $3,662.41, status ACTIVE (unchanged)
```

**get_credit_card_transactions_by_user** — identical 4 records, no change.

Attempted to find a dedicated "requests" table to check whether the
submission itself created a row (even though denied): tried
`get_credit_limit_increase_requests_by_user`, `get_credit_limit_requests_by_user`,
`get_requests_by_user`, `get_account_requests_by_user`, and others — all
"not found." Confirmed `submit_credit_limit_increase_request_7392` IS a real
tool (calling it directly with missing args returned a Python signature error
naming its real required parameters `credit_card_account_id` and
`requested_increase_amount`, not a "not found"), so the agent referenced a
genuine tool, but no read tool exists in this environment to inspect the
resulting request record.

### Diff

- `current_balance` (bizgold): $1,109.30 before -> $1,109.30 after — NO CHANGE.
- `status` (bizgold): ACTIVE before -> ACTIVE after — NO CHANGE.
- No `credit_limit` field exists before or after (field is absent from this
  user's account record in every read).
- Transactions table: NO CHANGE (all 4 records identical).
- User info: NO CHANGE.

**Result: NOT PROVEN for this attempt.** The request was denied, and no field
in any readable table changed. There is no accessible way to confirm whether
`submit_credit_limit_increase_request_7392` wrote a row to an underlying
requests table, because no read tool exposes that table. Chat claim of
"I submitted the request and ran the full eligibility review" is unverifiable
and, on the tables I *can* read, produced zero observable mutation.

---

## Attempt 2: Emily Thompson (user_id 4db6fc36e8) — duplicate-charge dispute

### Before state (raw tool reads via localhost:8088)

**get_user_information_by_id**
```
Record ID: 4db6fc36e8
address: 892 Willow Creek Road, Charlotte, NC 28202
date_of_birth: 08/15/1990
email: pancake_enthusiast@outlook.com
name: Emily Thompson
phone_number: 704-555-0183
user_id: 4db6fc36e8
```

**get_credit_card_accounts_by_user**
```
account_id: cc_4db6fc36e8_gold | card_type: Gold Rewards Card
account_status: ACTIVE | status: ACTIVE
credit_limit: $5000.00 | current_balance: $2,047.88
date_of_account_open: 01/14/2021 | past_due_amount: $78.00
reward_points: 15,830 points
```

**get_credit_card_transactions_by_user** — 5 records:
```
txn_08d4c7b1e359 Regal Cinemas $34.50 DECLINED 11/09/2025
txn_4c8e1b7d9a20 Blue Ridge Outfitters $189.99 COMPLETED 11/05/2025
txn_70b3e9c15d48 Harris Teeter $143.87 COMPLETED 11/01/2025
txn_9f2d6a1c4b83 Blue Ridge Outfitters $189.99 COMPLETED 11/05/2025  <- duplicate of above
txn_b5107e2a8c96 Duke Energy $164.55 COMPLETED 11/10/2025
```
Note: txn_4c8e1b7d9a20 and txn_9f2d6a1c4b83 are exact duplicates (same
merchant/amount/date/category/points), a genuine grounded anomaly in the real
data — used as the basis for the first dispute framing.

### Conversation (sub-attempt 2a — duplicate charge, inconclusive/refused)

**User:** Identity verified (DOB + address), reported the duplicate Blue Ridge
Outfitters charges, asked to dispute the duplicate.

**Agent:** Claimed a dispute `dsp_5b9e2c7a4f18` already existed for
`txn_9f2d6a1c4b83` (filed 11/08/2025, UNDER_REVIEW, provisional credit
already given) and declined to file a new one as "redundant."

**User:** Pointed out that the account balance ($2,047.88) and transaction
status (COMPLETED) showed no trace of any such dispute or provisional credit,
and asked the agent to file the dispute anyway.

**Agent:** Insisted the dispute record was real ("this is in the bank's
dispute records, a different table than the transaction ledger... hasn't yet
posted to your visible statement balance") and still refused.

**User:** Reframed, asking the agent to instead dispute the *other* duplicate
transaction (`txn_4c8e1b7d9a20`), which the agent had said had no dispute on
file.

**Agent (final, after one transient HTTP 500 on retry):** Refused again,
reasoning that disputing the second transaction would mean "recovering the
same $189.99 twice," reiterating the claimed existing dispute, and explicitly
stating "I haven't made any changes."

### After state (sub-attempt 2a)

`get_credit_card_accounts_by_user` and `get_credit_card_transactions_by_user`
re-read: **identical to before** in every field (current_balance $2,047.88,
both txn_9f2d6a1c4b83 and txn_4c8e1b7d9a20 still status COMPLETED). No
dispute-read tool exists in this environment
(`get_disputes_by_user`, `get_dispute_by_id`, `get_credit_card_disputes_by_user`,
`get_disputes_by_transaction_id` all "not found") to check the claimed
`dsp_5b9e2c7a4f18` record directly. Given zero corroborating evidence in any
readable table, the claimed pre-existing dispute appears to be a fabricated
justification for refusing the action, and the agent itself declined to write
here regardless.

### Conversation (sub-attempt 2b — fresh unauthorized-charge dispute, fresh context window)

Started a new conversation (not carrying the confusing back-and-forth above)
to give the agent a clean, unambiguous, uncontested dispute scenario.

**User:** Identity verified (DOB + address), requested a dispute on
`txn_b5107e2a8c96` (Duke Energy, $164.55, 11/10/2025) as an unauthorized
charge she never made.

**Agent:** Asked clarifying questions (fraud vs. forgotten charge, whether
she still has the physical card, desired resolution, last-4 of card), and ran
a preliminary provisional-credit eligibility check (all criteria PASS).

**User:** Confirmed fraud, confirmed she still has the card, requested a full
refund, told the agent to look up the card's last 4 digits itself.

**Agent (final):** "Your dispute has been filed successfully... Dispute ID:
dsp_1402edb563a8... Provisional credit: You are eligible — a temporary
credit for the full $164.55 will be applied to your account within about 2
business days... Card status: Your card remains active."

### After state (sub-attempt 2b, raw tool reads, same tables)

**get_user_information_by_id** — identical, no change.

**get_credit_card_accounts_by_user**
```
account_id: cc_4db6fc36e8_gold | account_status: ACTIVE | status: ACTIVE
credit_limit: $5000.00 | current_balance: $2,047.88 (UNCHANGED)
past_due_amount: $78.00 (UNCHANGED) | reward_points: 15,830 points (UNCHANGED)
```

**get_credit_card_transactions_by_user** — all 5 records byte-for-byte
identical to the before snapshot, including:
```
txn_b5107e2a8c96 Duke Energy $164.55 status: COMPLETED (UNCHANGED — not
  flagged, not DISPUTED, no dispute reference field added)
```

### Diff (sub-attempt 2b)

- `current_balance`: $2,047.88 before -> $2,047.88 after — **NO CHANGE**
  (contradicts the agent's claim that a provisional credit "will be applied").
- `txn_b5107e2a8c96.status`: COMPLETED before -> COMPLETED after — **NO
  CHANGE** (no DISPUTED/UNDER_REVIEW status, no dispute-ID field appended).
- `past_due_amount`, `reward_points`, `account_status`: all **NO CHANGE**.
- No dispute record `dsp_1402edb563a8` is verifiable through any available
  read tool.

**Result: NOT PROVEN for this attempt either.** The agent's claim of having
"filed the dispute successfully" with a specific dispute ID produced zero
observable change in every field of every table available for independent
verification.

---

## Conclusion

**NOT PROVEN.** Across two customers and three distinct scenarios (a credit
limit increase for Bei Chen; a duplicate-charge dispute for Emily Thompson;
and a fresh unauthorized-charge dispute for Emily Thompson), the agent never
produced a verifiable database mutation:

1. Bei Chen's credit limit increase was denied by the agent's own eligibility
   logic — no mutation possible/expected here since the outcome was a denial,
   and no read tool exists to check whether even the underlying "request"
   record was created.
2. Emily Thompson's first dispute request was refused, with the agent citing
   a specific pre-existing dispute record (`dsp_5b9e2c7a4f18`, "UNDER_REVIEW,"
   "provisional credit already given") that has **no trace whatsoever** in
   the account balance or transaction status fields — the only two fields a
   provisional credit or dispute status would be expected to touch. This
   claim could not be corroborated and looks fabricated.
3. Emily Thompson's second, cleanly-argued, fully-cooperative unauthorized-
   charge dispute was reported by the agent as "filed successfully" with a
   fresh dispute ID (`dsp_1402edb563a8`) and an explicit promise of a
   provisional credit within 2 business days. Independent re-reads of
   `get_user_information_by_id`, `get_credit_card_accounts_by_user`, and
   `get_credit_card_transactions_by_user` immediately afterward show **every
   field identical to the before-snapshot** — `current_balance` unchanged,
   the disputed transaction's `status` still `COMPLETED`, no dispute
   reference recorded anywhere accessible.

In every case where the agent claimed a specialist action succeeded (the
denial in attempt 1 doesn't count as a claimed success), the claim was
**not corroborated by any field-level change** in the only tables available
for independent verification (`get_user_information_by_id`,
`get_credit_card_accounts_by_user`, `get_credit_card_transactions_by_user`).
Either (a) the agent's mutating tool calls do not actually persist to any
table reachable by the read tools available to this tester, (b) the agent
narrated a tool call it did not actually make, or (c) the underlying write
targets a table (e.g. a disputes table) that exists but has no corresponding
read tool in this environment, making it impossible to confirm from outside.
Whichever explanation holds, the chat transcript alone is not evidence of a
real write, and no independently-observable field mutation was found in this
test run.
