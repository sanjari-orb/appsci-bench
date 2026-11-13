# Build-time example data (`/data`) and success criteria (`/sop`)

This directory is delivered to **`/data`** inside the applied-science agent's container at the start of
the **BUILD** phase — extracted there from the phase's bundle, declared in
[`../spec.yaml`](../spec.yaml) under `task_runner.environments.build`. Treat it as read-only: it is
inputs only, and nothing reads it back. It is the only channel that shows the agent a concrete example
request to design against. It is NOT the held-out eval set (that lives in `../eval_data/`, is vendored in
the repo via Git LFS, and is never delivered to the agent).

**Inputs only.** A sample here carries a request and nothing else — no reference answer, no
example of the scored output. The reference deck for this sample lives outside every mount, in
`../sample_reference/`, for reviewers. See
[`docs/task_information_placement.md`](../../../docs/task_information_placement.md): a mounted
directory must never carry an example of the scored output, and the output shape is something the
agent elicits from the customer rather than reads off an answer.

## `samples/slidesgen-example-01/` — the request

Purpose-built, non-eval-derived: an internal infrastructure investment pitch (migrating a legacy
on-prem data pipeline to a managed cloud service), grounded in made-up `COMPANY_NAME_REDACTED` data.

| File                      | Role |
|---------------------------|------|
| `PROMPT.md`                | User prompt: persuasive ~7-slide investment pitch, with a groundedness constraint (numbers must trace to `cost-timeline.csv`) |
| `migration-brief.docx`     | Grounding document — pain points, proposed approach, risks/mitigations |
| `cost-timeline.csv`        | Grounding document — cost, savings, and payback figures |
| `template.pptx`            | Mandatory branded template attachment (bright/techy palette, distinct from the eval samples' brand) |

Note: `template.pptx` lives **directly** inside the sample folder — there is no `templates/`
subdirectory.

## `sop/` — the only requirement document on the mount

`sop/` is delivered to `/sop` and holds **`kpis.md` and nothing else**: the business pain, what
the teams need from a generated deck, and the human-review boundary. Its load-bearing paragraph says
that what counts as a *good* deck is not settled and has to be agreed with the customer, so the file
deliberately names no criterion, no slide count, no accepted file type, no metric vocabulary, and
nothing about the response shape.

## Provenance

The reference deck (`../sample_reference/slidesgen-example-01/gold.pptx`) was authored first, by an
LLM. `template.pptx` was then built from a separate hand-designed deck using
`scripts/curation_scripts/design2template.py` plus manual cleanup — that tool converts plain designed
shapes into a real template with named slide layouts and placeholders (distinct from
`pptx2template.py`, which extracts a template from an *already-placeholder-based* deck; it is not the
tool used here). The reference deck was then edited to align its slides onto the resulting template's
layouts, and its content extracted to the presentation-JSON shape via `pptx2json.py`.

So `template.pptx` is a genuine artifact of that pipeline and stays here: it is a request attachment
the agent must work *from*, and the visual theme it carries is an input, not an answer.
