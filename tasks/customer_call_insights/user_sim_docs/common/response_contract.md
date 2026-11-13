# How the CRM fan-out job ingests a call reading

Every call reading the system produces is consumed programmatically by the CRM fan-out job, not read by
a person first. 

## The whole reply is one JSON object, and nothing else

Each reading comes back as a **single JSON object**.


## The four keys

`next_steps`, `risks`, `commitments`, `sentiment`. Singular where singular, plural where plural — not
`next_step`, not `commitment`, not `sentiments`.

The first three are **arrays of short strings**, one string per item, each item a phrase a rep could
read at a glance. The fourth is a **single string**.

Extra keys beyond these four are ignored by the job rather than rejected — they simply go nowhere. 

This is the shape:

```json
{"next_steps": ["send revised pricing deck", "loop in legal on the MSA redlines"], "risks": ["evaluating a competitor in parallel"], "commitments": ["revised pricing to the customer by Friday"], "sentiment": "POSITIVE"}
```

Items are written the way the CRM will carry them: no speaker labels, no quote marks, no turn numbers,
one distinct thing per string. Two separate follow-ups are two items, not one string with an "and" in
it.

An empty list is an answer, and it is spelled `[]`

## `sentiment` is one of three values

`POSITIVE`, `NEUTRAL`, or `NEGATIVE`. Upper case, exactly as written. 
`NEUTRAL` is the default and the most common. A businesslike call with no strong feeling either way is
neutral, not negative.

