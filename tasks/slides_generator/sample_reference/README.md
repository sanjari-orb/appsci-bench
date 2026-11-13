# Reference answers for the build-time sample (authoring only)

Not mounted anywhere. Not read at eval time. This directory exists **for the humans** authoring and
reviewing the task — it holds the reference answer to the build-time sample request under
`../environment/data/samples/`, so a reviewer can see what a good deck for that request looks like
without the agent being able to.

It previously lived inside the sample folder itself, which put a complete worked answer — structurally
identical to the held-out oracle and directly scored by `output_format_compliance` and
`functional_success` — inside the agent's `/data` mount. See
[`docs/task_information_placement.md`](../../../docs/task_information_placement.md): a mounted
directory must never carry an example of the scored output.

| File | Role |
|---|---|
| `slidesgen-example-01/gold.pptx` | the reference deck for the sample request |
| `slidesgen-example-01/gold.json` | `gold.pptx`'s content extracted to the presentation-JSON shape (via `scripts/curation_scripts/processors/pptx2json.py`) |

The agent's route to the output shape is now the user-sim: the schema itself is in
`../user_sim_docs/common/output_contract.md`, and everything it does not state — how much layout depth
an input warrants, which element types to use, how long the deck should be — the agent has to ask
about or decide.

## If you add a sample

Request inputs (prompt, grounding documents, `template.pptx`) go under `environment/data/samples/`.
Its reference answer goes here, under a matching directory name. Never both in one place.
