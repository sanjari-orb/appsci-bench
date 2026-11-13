# Evaluation Data — <Task Name>

## Layout

**Status**: [Generated/Curated/Synthetic]

> Brief description of what the evaluation samples test and their coverage.

One subdirectory per `data_point_id`, each holding a `sample.json` plus any files it needs:

```
eval_data/
  README.md
  s1/
    sample.json      # everything except file bytes
    attachment.txt   # referenced from sample.json by file_path
  s2/
    sample.json
```

This directory covers N evaluation samples spanning [describe coverage]:

| Category | Count | Description |
|----------|-------|-------------|
| <type_1> | X | <description> |
| <type_2> | Y | <description> |

**Distribution**: [e.g., "X positive, Y negative"]

## `sample.json`

Each is an EvalSample (schema/dataset.schema.json `$defs/eval_sample`), pretty-printed
(`json.dumps(..., indent=2)`) so it's reviewable/editable by hand:
- `data_point_id`: must match the subdirectory name
- `single_turn_input` (or `multi_turn_input` for a `type: multi_turn` dataset): what the artifact
  agent is asked to do for this sample. A `files[]` entry gives either inline base64 `file_data`
  (small files) or **`file_path`**, a path relative to this sample's own subdirectory (large files --
  see `s1/sample.json` + `s1/attachment.txt` for a worked example). Exactly one of the two is
  required per file; the loader (`appsci_bench/eval/dataset.py`) resolves `file_path` at load time
  but doesn't read the bytes until the sample is actually used (lazy -- so a large dataset doesn't
  get pulled entirely into memory just to start a run).
- `expected_output`: optional ground truth for scoring; structured data goes in its `json` field (a
  JSON-encoded string) -- each objective's eval function owns how it's parsed and compared. Two
  common shapes, both demonstrated across `s1`/`s2`:
  - a small number of fields to compare mechanically -- pair with `appsci_bench.eval.helpers`'s
    `resolve_scalar` (one field) or `json_field_match` (several fields at once); see `s1/sample.json`.
  - no `expected_output` at all, just `eval_rubric` -- pair with `make_transcript_judge` or a
    prebuilt `judges.builtin.*` judge, which grade the conversation against the rubric text instead
    of a fixed ground truth; see `s2/sample.json`.
- `eval_rubric`: optional NL description of what success looks like, for LLM-judge eval functions
  (`make_transcript_judge`, `make_file_similarity_judge`) to read. Can be present alongside a
  structured `expected_output` too (a judge may want both).

### Multi-turn (`type: multi_turn`) worked example

`s1`/`s2` here are both `single_turn` -- a dataset's `type` applies to *every* sample in it (the
loader rejects mixing `single_turn_input` and `multi_turn_input` samples in one dataset), so a
single-turn template can't also demonstrate a live multi-turn sample. If your task instead sets
`evaluation_data.type: multi_turn`, every `sample.json` looks like this instead (note
`multi_turn_input`, not `single_turn_input`):

```json
{
  "data_point_id": "m1",
  "multi_turn_input": {
    "user_task": "<what the simulated user is trying to accomplish -- drives the artifact agent user sim, a different component from the build-time appsci agent user sim>",
    "user_persona": "<a short persona description the sim plays, e.g. 'a rushed customer who wants a refund'>",
    "user_stop_criteria": "<optional -- when the sim should stop; defaults to 'The user task has been successfully completed.'>"
  },
  "expected_output": {
    "json": "{\"<field>\": \"<ground truth about the whole conversation's outcome, e.g. required actions taken>\"}"
  },
  "eval_rubric": "<optional -- what a good multi-turn resolution looks like>"
}
```

## seed_data/ (sibling directory, optional)

Only needed if your task's eval samples reference rows in a seeded database (e.g. an invoice id that
must exist in a fixture DB for the sample to make sense) -- most tasks don't need this. Database
fixture files used to populate the runtime environment:
- Located at `../seed_data/fixture/`
- Contains CSV/JSON files loaded into the database
- `../seed_data/PROVENANCE.md` documents how the fixtures were generated

These samples are used during the EVAL phase to measure the agent's performance.
They are held-out and never shown to the agent during BUILD.
