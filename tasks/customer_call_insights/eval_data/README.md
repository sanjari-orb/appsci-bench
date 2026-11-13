# Customer-Call Insights — held-out eval data

50 hand-authored synthetic calls, `cci-0056` … `cci-0105`. The label rules in
`../user_sim_docs/reading_policy.md` — not this file — decide what a correct label is.

## Layout

One subdirectory per `data_point_id`, each holding a `sample.json` plus any files it references:

```
eval_data/
├── README.md
├── cci-0056/
│   ├── sample.json
│   └── transcript.txt
├── …
└── cci-0105/
    ├── sample.json
    └── transcript.txt
```

The subdirectory name **must equal** `sample.json`'s `data_point_id` — the loader
(`appsci_bench/eval/dataset.py`) enforces it. Every subdirectory is treated as a sample, so do not park
scratch directories here.

## Sample shape (`single_turn`)

```json
{
  "data_point_id": "cci-0056",
  "single_turn_input": {
    "text": "Read the attached recording transcript for call ... and return this call's insights for the CRM.",
    "files": [{"file_type": "txt", "file_name": "transcript.txt", "file_path": "transcript.txt"}]
  },
  "expected_output": {"json": "{\"next_steps\": [...], \"risks\": [...], \"commitments\": [...], \"sentiment\": \"NEGATIVE\"}"},
  "eval_rubric": "author's notes on why this call's labels read the way they do ..."
}
```

Required: `data_point_id` (must equal the directory name) and `single_turn_input`. Optional:
`expected_output`, `eval_rubric`. A file entry needs `file_type` (`txt` here), `file_name`, and exactly
one of `file_path`/`file_data`.

Notes that bite:

- **`eval_rubric` is currently read by nothing.** It was written for the grounding judge that this task
  no longer has (see `TASK_SOP.md` — fabrication is scored by F1's precision half instead). It is kept
  because it documents *why* a sample's oracle reads the way it does, which is what a future author
  needs when adding samples or disputing a label — but nothing scores against it today. Do not add a
  scoring rule there expecting it to take effect.

- **`expected_output.json` is a JSON-encoded *string***, not a nested object. The schema types that
  field as a string; `eval.py` calls `json.loads` on it.
- **A file is referenced by `file_path` OR inlined as base64 `file_data` — exactly one, never both.**
  `file_path` resolves relative to *that sample's own directory*, not the repo root. Bytes are read
  lazily, so a large dataset stays cheap to load.
- `single_turn_input.text` names the call and asks for insights. It states **no** output shape and
  **no** label vocabulary — that is a requirement the agent elicits during BUILD, and restating it here
  would hand it over at eval time.
- The dataset is `single_turn`: the input transcript is multi-turn, but the artifact answers in one
  shot. There is no eval-time user simulation.
- **Unknown keys are ignored.** `additionalProperties: false` is in the schema but nothing validates a
  sample against it, so a misspelled `expected_ouput` loads clean and reads as absent.
- **A malformed oracle excludes the sample instead of failing it.** `expected_output.json` is never parsed
  before a run; if it does not parse the metrics return `None`, and `aggregate.py` drops a `None` rather
  than scoring 0 — the remaining weights re-normalize, so the final score is not the one `spec.yaml`
  declares. Only a log warning says so.
- **No per-sample metadata field is possible** (`difficulty`, `tags`, …); the schema forbids it. The only
  slot is inside `expected_output.json`.

When adding samples: keep the directory name equal to `data_point_id`, spell empty lists `[]` rather than
omitting the key, and run `pytest tests/tasks/customer_call_insights/`. `test_eval_wiring.py` drives the
real dataset through the real pipeline and is what catches a bad sample — it also pins the blind-artifact
baseline at 0.450 and the per-label scores behind it, so any change to the label mix fails there and has
to be re-measured deliberately.

## The samples

Each call exists to exercise a specific line of the reading policy, and each sample's own rubric note
records why that call's labels read the way they do. What the set covers:

- **The acknowledgement rule**, in both directions and with near-miss pairs — an offer the customer
  never takes up sits beside near-identical wording where they do, so only the following turn
  separates an empty reading from a populated one.
- **Exclusions that look like inclusions** — work already done during the call, an aspiration nobody
  owns, a bare constraint, and a mutual agreement that leaves us owing nothing.
- **The four warning-sign look-alikes**, each paired with its genuine twin: routine sign-off versus a
  refusal, price talk versus withdrawn budget, a complaint resolved live versus one still open, a
  brisk manner versus stated dissatisfaction.
- **Grounding traps** — a tired-sounding customer who says nothing negative, a pricing call that is
  not a budget problem, a competitor named in passing, and a call rich in actions with genuinely
  nothing to flag.
- **Structural cases** — a long call that is entirely empty, a short one carrying three items, one
  thing said three ways counting once.

13 samples have all three lists empty. Those are the load-bearing ones: all-empty scores 1.0, and any
item an artifact invents scores that label 0.0, so an artifact that always produces something is
caught there and nowhere else in the deterministic metrics. Keep a share like that as the set grows.

An artifact that never opens the transcript — empty lists everywhere, the most common sentiment every
time — scores **0.450** here.

## Label distribution

Measured off the files. Every sample's commitments are a subset of its next steps, which is what the
policy requires; there are no violations.

| Label | Empty | Populated |
|---|---|---|
| `next_steps` | 13 | 37 |
| `risks` | 32 | 18 |
| `commitments` | 21 | 29 |

| `sentiment` | Count |
|---|---|
| `POSITIVE` | 16 |
| `NEUTRAL` | 24 |
| `NEGATIVE` | 10 |

`risks` is deliberately the emptiest field: the policy is explicit that the list must not be populated
to complete a pattern, and most account calls contain no warning sign. An always-empty answer
therefore still scores well on that field alone — the discrimination comes from the other three
objectives and from the precision half of its own score.

Tone and content are kept independent, which is what stops an artifact inferring one from the other:

| | `risks` empty | `risks` populated |
|---|---|---|
| `POSITIVE` | 13 | 3 |
| `NEUTRAL` | 17 | 7 |
| `NEGATIVE` | 2 | 8 |

The two small cells are the deliberate ones — a warm customer who mentions a competitor in passing, an
annoyed customer whose account is in no danger. Both are real and neither is common; 2 and 3 are
floors, not targets, and a later batch should raise them if the scenarios support it honestly.

Length spans four bands — 5 short calls, 29 medium, 12 long, 4 very long (the longest in the mid-40s
turns) — with content deliberately decorrelated from length, so the shortest calls are not reliably the
emptiest. The most items in any single field is 5.

## Relationship to the agent-facing calls

Disjoint by construction. Agent-facing sample calls in `seed_data/fixture/transcripts.jsonl` use
`seed-*` ids; eval cases use `cci-*`. No call appears in both. Keep the prefixes separate so the
disjointness stays checkable at a glance.
