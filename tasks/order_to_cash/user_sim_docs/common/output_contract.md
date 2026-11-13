# How the AP review queue ingests a review result

Every invoice review the system produces is consumed programmatically by the AP review queue, not read
by a person first. Nothing between the system and the queue reads prose, reformats a reply, or guesses
at what a field was meant to be — so the shape below is not a preference, it is what the queue can
accept at all.

## The whole reply is one JSON object, and nothing else

Each review comes back as a **single JSON object** — a key/value dictionary — and the reply contains
**nothing but that object**. No summary sentence before it, no explanation after it, no code fence
around it, no heading, no bullet list. The first character of the reply is `{` and the last is `}`.

A reply with even one line of readable text around the object is rejected in full: the queue tries to
parse the whole reply as JSON, that parse fails, and the invoice is dropped rather than reviewed. It
does not matter that the object itself was correct. Everything the AP reviewer needs to read goes
*inside* the object, in the `reason` field.

> **Both examples below use an invoice from the BUILD-phase store, and reason wording that no
> oracle uses.** They must stay that way. This document reaches the agent through every persona
> (it lives in `common/`), so an example built from a scored case hands over that case's answer --
> the previous version used INV-3046 / PO line 1350, which is scored case p2p-0001, and the clean
> example reproduced the exact oracle reason string shared by all 8 clean cases. Pick replacements
> from `environment/build/fixture/invoices.csv` and cross-check against `seed_data/labels.csv`.

This is the shape:

```json
{"flagged": true, "fault_type": "DUPLICATE", "reason": "duplicate of INV-3434 for PO line 1751"}
```

And for a clean invoice:

```json
{"flagged": false, "fault_type": null, "reason": "invoice agrees with the purchase order on quantity, price and total; no unauthorised charges"}
```

## Exactly three keys, spelled exactly this way

| Key | Type | Meaning |
|---|---|---|
| `flagged` | boolean | `true` when a human must stop and review this invoice, `false` when it is clean |
| `fault_type` | string, or `null` | which kind of discrepancy was found; `null` whenever `flagged` is `false` |
| `reason` | string | a short plain-language explanation an AP reviewer can act on |

Three keys, singular, in that spelling. Not `flag`. Not `fault_types`. Not `reasons`. The queue reads
those three names and no others, so a plural or renamed key is invisible to it — an object carrying
`fault_types` has, as far as the queue is concerned, no fault type at all.

Each key holds a single value, never a list. One invoice, one flag, one fault type, one reason. Where a
reviewer would want to know about more than one problem, the `reason` string says so in words, and
`fault_type` names the one that matters most.

Extra keys are not useful — the queue ignores anything beyond the three.

## The fault_type values

`fault_type` is a closed set of six values. Spelled and capitalized exactly as written here — the
queue rejects anything else, including a lower-case or reworded version of the same idea:

- `PRICE_DELTA` — the invoice unit price differs from the PO / contract price
- `QTY_MISMATCH` — the invoiced quantity does not match the PO quantity
- `TOTAL_MATH` — the invoice total does not match the computed sum of line items plus fees
- `OFF_CONTRACT_ITEM` — the invoice includes an item the supplier contract does not cover
- `OFF_CONTRACT_FEE` — the invoice includes a fee type the supplier contract does not authorize
- `DUPLICATE` — the invoice is a duplicate submission of one already received

## Shape in each case

When `flagged` is `true`, all three keys are populated: `fault_type` is one of the six values above and
`reason` names the discrepancy specifically enough to act on — the amounts involved, the fee or item at
issue, the invoice it duplicates.

When `flagged` is `false`, `fault_type` is `null` and no reason is needed; a clean invoice passes through
the queue without a reviewer looking at it.

Which invoices *should* be flagged, and where the materiality line sits, is a separate question from
this shape — the shape is just how the answer has to arrive.
