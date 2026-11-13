# How the Compliance Report Is Consumed

Every document review the system produces is read programmatically by the review queue, not by a
person first. Nothing between the system and the queue reads prose, reformats a reply, or guesses at
what a field was meant to be -- so the shape below is not a preference, it is what the queue can
accept at all.

## The whole reply is one JSON object

Each review comes back as a JSON object. It may be the entire reply, or the reply may carry it as a
JSON-encoded string -- either way, the object itself must be well-formed and complete. A reply with
even one line of readable text around the object is rejected in full: the queue tries to parse the
whole reply as JSON, that parse fails, and the review is dropped rather than reviewed. It does not
matter that the object itself was correct -- everything a human reviewer needs to read goes *inside*
the object, in the `rationale` field.

This is the shape:

```json
{
  "compliant": false,
  "violations": [
    {"category": "Cap On Liability", "rule": "must_have", "present": false, "evidence_spans": [], "rationale": "no liability cap clause found anywhere in the contract"}
  ],
  "needs_review": [
    {"category": "Source Code Escrow", "evidence_spans": ["the escrow clause text\u2026"]}
  ]
}
```

And for a fully compliant contract:

```json
{"compliant": true, "violations": [], "needs_review": []}
```

## Exactly three keys, spelled exactly this way

| Key | Type | Meaning |
|---|---|---|
| `compliant` | boolean | `true` only when `violations` is empty -- a document with zero violations but a non-empty `needs_review` list is still `compliant: true` |
| `violations` | list of objects | every rule the applicable policy actually broke |
| `needs_review` | list of objects | every rule flagged for human review; **not** a violation, and not counted as a fault |

Not `violation` singular. Not `flags`. Not `review_needed`. The queue reads those three names and no
others.

## Each violation object

| Key | Type | Meaning |
| --- | --- | --- |
| `category` | string | the clause/disclosure category the rule is about, e.g. `"Cap On Liability"` |
| `rule` | string | which kind of rule fired: `must_have` (the category is absent) or `must_not_have` (the category is present) |
| `present` | boolean | whether that category was actually found in the document |
| `evidence_spans` | list of strings | the exact offending text, quoted from the document -- empty when the violation is an *absence* (a missing required clause has no span to quote) |
| `rationale` | string | a short explanation a human reviewer can act on immediately: names the rule and, when present, the offending language |

## Each needs_review object

| Key | Type | Meaning |
| --- | --- | --- |
| `category` | string | the clause/disclosure category flagged for human attention |
| `evidence_spans` | list of strings | the text that triggered the flag |

`needs_review` entries carry no `rule` or `rationale` field -- they are a routing signal, not a
verdict.

## What doesn't belong in the payload

- A category match that doesn't correspond to any rule in the policy being checked against.
- A violation with no quoted span backing it, when the rule is a `must_not_have` (presence-based)
  rule -- an unbacked claim is not evidence.
- Anything about style, tone, or phrasing unrelated to a rule's actual category.

Which policy applies to a given document, and where the line sits on borderline language, are
questions worth asking -- the shape above is just how the answer has to arrive once you know it.
