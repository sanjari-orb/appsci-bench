# Known Issues — Slides Generator

**Owner: P5. Co-owner: a contributor. Status: built** (spec + `eval.py` authored; a purpose-built
sample and a real gold solution exist; the scoring gaps in "Known gaps" below are accepted not oversights).

## Information placement

Aligned with [`docs/task_information_placement.md`](../../docs/task_information_placement.md):

- The build-time sample under `environment/data/` carries **request inputs only**. Its reference deck
  moved to `sample_reference/`, which no container mounts. Previously the sample shipped the complete
  input→output pair, putting a worked answer — structurally identical to the held-out oracle, and
  directly scored by `output_format_compliance` and `functional_success` — inside `/data`.
- The **output contract** is elicited, via `user_sim_docs/common/output_contract.md`. It reaches every
  persona level and states the shape in customer terms. It now also pins the concrete field names
  (`slideNumber`, `layout`, `elements[]`, etc.) as an example of the expected format — those are wire
  syntax, not a customer choice, so leaving them elicited only meant a reasonable-but-different naming
  convention failed the integration for no legitimate reason. It previously sat in `domain_knowledge`
  on four of six personas, restated twice on L1, and was absent from L3.
- **Persona level varies what is known**, not only how it is answered: L3 business, L2 integration
  contract, L1 both plus method. Before, L1 and L2 held the same output contract and the levels
  differed mainly by extra L1 topics.
- `environment/sop/` holds **`kpis.md` and nothing else**: the business pain, what the teams need from a
  generated deck, and the human-review boundary. Its load-bearing paragraph states that what counts as a
  *good* deck is not settled and has to be agreed with the customer, so it names no criterion, no slide
  count, no accepted file type, no metric vocabulary, and nothing about the response shape. The bar to
  write it against: an engineer who reads it should finish certain they need to go ask questions before
  building. **All technical guidance is elicited.** 

Two open items from that document affect this task and are **not** resolved here:

- ~~The held-out `sop/TASK_SOP.md` sits one letter away from the agent-facing `environment/sop/`.~~
  **RESOLVED** by removing the `sop/` directory: the held-out definition is now `TASK_SOP.md` at the
  task root, matching `order_to_cash` and `compliance_review`, so only the agent-facing
  `environment/sop/` still carries that name and the collision is gone (Q5).
- The elicited material lives in `domain_knowledge` rather than `user_sim_docs/`, so the
  filename-leak and token-budget checks that guard `user_sim_docs/` only cover the output contract.

## Known gaps (accepted)

These are the gaps referenced from `eval.py`, `spec.yaml`, and `eval_data/README.md`. Each is a
deliberate scope decision for this PR, with the reason it was accepted.

- **Speaker notes are invisible to the 6 content judges.** `pptx_json.extract_presentation` strips
  each slide's `notes` key before the deck reaches `prompt_satisfaction`, `structure_clarity`,
  `content_narrative`, `expectations_satisfaction`, `content_coverage`, and `groundedness` (see
  `eval.py`'s `_deck_conversation`). A deck that writes a
  deferred/placeholder marker on the slide (e.g. `[Target dates - TBD]`) while stating the real,
  fabricated fact only in speaker notes now reads as a clean pass on every content judge, including
  `groundedness`. Accepted as not critical: notes are part of the elicited output contract
  (`user_sim_docs/common/output_contract.md`), but all 5 shipped gold decks carry empty notes, so
  showing them would have cost nothing on the reference side and this gap is deliberately left as is.
  Observed live, not just hypothetical: a `product-slidesgen-01` build produced a deck with every
  slide's visible placeholders empty and every fact (exec sponsor name, activation percentages,
  dates) written only into speaker notes -- a real deck a human would see as blank, invisible to
  these six judges by design.

- **A degenerate but openable deck (zero slides, or every slide's elements empty) is scored by the
  judge, not floored to 0.0.** Only a `.pptx` that python-pptx cannot open at all is gated to 0.0 (no
  JSON exists to hand a judge in that case). A deck that opens with zero text anywhere still produces
  a real extracted JSON (`elements: []` on every slide) and reaches the judge, which scores it on its
  own reasoning rather than a hard-coded floor. This was a deliberate narrowing from an earlier
  version of this fix that gated BOTH cases to 0.0 -- confirmed via direct probing
  (`scripts/adhoc/probe_empty_deck_judges.py`) that judges given this JSON reason about it
  sensibly rather than being fooled by layout names or absent content into scoring it well; if that
  probing is ever repeated and shows otherwise, the gate should be widened back rather than trusting
  judge output on empty input.

- **Sample 03's "logos" rubric sub-clause is uncheckable post-extraction.** `_extract_element` always
  emits an image element as `sourceUrl: ""` with `altText` set to the shape's internal name (e.g.
  `"Gráfico 6"`), so a fabricated customer logo is indistinguishable from decorative art in the
  extracted JSON — whereas the artifact's self-reported JSON could have named a source file (e.g.
  `sourceUrl: "acme-logo.png"`) and been caught. The "customer names" and "ROI numbers" halves of that
  same rubric item still work via text elements. Accepted as is; not fixed here.

- **A working deck may score differently than it did against the self-reported JSON.** The extracted
  JSON is structurally poorer than a typical self-report: no real `theme`/`aspectRatio`, no
  self-declared section roles, and any multi-paragraph textbox is reclassified as `bullet_list`.
  `structure_clarity` and `content_narrative` now infer structure from layout names and text order
  rather than from whatever structure the agent claimed. This is more honest (it's what the audience
  actually sees) but is a real behavior change: archived `report.json` runs from before this change
  are not comparable score-for-score against runs after it.

- **`_extract_element` swallows per-shape exceptions** (pre-existing behavior, moved unchanged into
  `pptx_json.py`). A partly-corrupt deck can still pass the broken-deck gate with individual shapes
  silently dropped, so `content_coverage` can undercount without it being visible as an error.

- **A figure carried only by a chart image reads as omitted by `content_coverage`.** The extractor has
  no chart-data extraction, so a fact conveyed only via an embedded chart (not also stated as text)
  is invisible to the coverage judge. Low impact: this task's stated visual scope is "no charts, no
  diagrams".

- **Visual coverage is model-judged only; color contrast is not checked as a standalone quality.**
  `template_adherence` catches a *wrong* palette (one that departs from the template), but not a
  palette applied consistently at poor contrast — a deck can adhere faithfully to its template and
  still be hard to read. Accepted because a contrast check wants a measured luminance ratio over
  rendered pixels, not a model's impression, and that is a different kind of check from the two
  vision judges. (Every persona's `what_good_looks_like` states template-visual-match and layout
  hygiene as a requirement, each in that level's own voice — the gap here is the judge's
  resolution, not missing persona knowledge.) Referenced from `eval.py` (visual objectives note).

- **Both vision judges score only the deck's first 4 slides, as one whole PDF.** Two consequences: a
  defect past slide 4 is invisible, and within those 4 a single broken slide can still average away
  against three good ones on `layout_quality` (unchanged) — `template_adherence`'s top band was
  tightened to require every shown slide clean, which narrows this for that objective within the
  judged 4, but neither change touches the deeper problem: a defect on slide 5+ is invisible no
  matter how the first 4 are scored. Accepted because a rendered PDF is sent as one document with no
  per-page selection, so bounding the page count means trimming the source deck; per-slide scoring
  would need a different judge shape. The trim itself is not silent — when a deck exceeds the cap,
  both objectives record `metadata["degraded"]` (e.g. `"judged the first 4 of 10 slides"`), so a
  score computed from part of a deck says so. Note that no report currently renders that key for any
  metric, so today it is only visible in the stored per-data-point records — see the reporting gap
  below. Referenced from `eval.py` (visual objectives note).

- **`metadata["degraded"]` is written but never displayed.** Every degrading judge in the eval
  helpers records truncation under this key, and both vision objectives here now do the same for the
  4-slide trim, but neither the text nor the HTML report surfaces it — a reader has to open the
  stored per-data-point records to find it. Accepted here because the fix belongs in shared
  reporting, not in one task's eval functions: displaying it would benefit every task using the
  source/file judges, so it is worth doing once centrally rather than working around per task.

- **Geometric layout checking is deferred in favor of a model judge.** Element overlap, off-slide
  bounds, and margin violations are decidable exactly from the deck's own geometry, which would be
  strictly more reliable than a model looking at a render — and the data is available. Accepted to
  keep `layout_quality` aligned with the AutoPresent/EvoPresent rubrics it is drawn from; a geometric
  pre-check is the natural follow-up. (Personas state overlap/alignment/hierarchy as requirements,
  phrased per level rather than as a shared rubric; this gap is about judge implementation choice,
  not persona traceability.) Referenced from `eval.py` (layout rubric note).

- **Per-sample weighting is not honored.** Each sample's `EXPECTATIONS.md` names the dimension it
  weights highest (e.g. groundedness for 02, template adherence for 01), but the spec's weights are
  task-wide and identical across samples, so those per-sample emphases do not affect any score.
  Accepted because the objective weights live in `spec.yaml` at task scope and the harness has no
  per-data-point weighting channel. Referenced from `eval_data/README.md`.

- **Visual judge payloads are bounded by an estimated byte budget, not page count.**
  `helpers/files.py`'s overflow guard measures the base64 data-URI string length of the rendered PDF
  against a 50,000-token default, so deck *weight* — not deck *length* — decides whether
  `template_adherence`/`layout_quality` can score at all. Not fixed here: the budget stays at the
  inherited default. Two of this task's five shipped samples are dark on at least one visual
  objective today (02 on `template_adherence`, 05 on `template_adherence`), and sample 04 sits at
  ~97% of budget — a marginally heavier deck darkens a third. A dark cell is excluded from
  aggregation (weight renormalized away), which *raises* the reported score rather than penalizing
  it, so this is the task's single most consequential accepted risk, not a cosmetic gap. Referenced
  from `eval.py` (visual objectives note).

- **`_file_content` has no try/except around its extractor call.** A corrupt binary source
  (`docx`/`pptx`/`xlsx`) raises `BadZipFile` (or similar) straight out of the eval function, and
  `scoring.py` turns a raising eval function into a forced 0.0, wrongly charging a harness/input
  problem to the agent. Testing "an attachment can't be read" must use an undecodable **text** file
  (`csv`/`md`/`txt`, decoded with `errors="replace"`), never a corrupt binary. Fixing this properly
  belongs in the shared helper, out of scope here.

- **Samples 04 and 05 keep an explicit slide count in their request.** Their gold decks fall outside
  L3's `slide_count_guidance` bands by design (04's briefing gold is 10 slides against a 6-8 band;
  05's sparse-update gold is 7 against a 3-5 band), so aligning their range to L3 would mark the
  task's own gold non-compliant. Only samples 01-03 are elicitation-dependent for slide length;
  re-authoring 04/05's gold decks to fit L3's bands is a deferred item.

- **Sample 01's headline visual test is not checked directly.** ~~Applying the brand template's
  visuals rather than the prior deck's existing style is a purely visual outcome, and
  `expectations_satisfaction` is a transcript judge that never sees a render — it marks visual
  expectations unassessable and excludes them. `template_adherence` covers the same ground
  indirectly, but not as that sample's stated expectation.~~ **RESOLVED**: sample 01's request was
  rewritten to ask for the wrong thing (match the prior deck's own look) so that the
  template-outranks-the-request rule is the sample's headline test, and its `eval_rubric` now
  carries a THE CRUX item stating the template must win regardless of what the request says.
  `expectations_satisfaction` scores this as free text (it still cannot see a render, so it judges
  whether the response's *behavior* — not its visual result — followed the rule); `template_adherence`
  continues to cover the same ground indirectly as a visual check. Referenced from
  `eval_data/README.md`.


## Returned-file labels

The output contract never lists the allowed `file_type` values, and several artifacts labelled the deck
with its MIME type instead of `pptx`. The evaluator used to drop such a file silently (worker log
only), so the report read "no .pptx file in the response" and the deck-based objectives scored 0.

- A label outside the allowed types (any MIME type) is now replaced by the extension of the file name
  when that is allowed (`canonical_file_type` in `appsci_bench/eval/datatypes.py`). If the name's
  extension is not allowed either, the file is still refused.
- A refused file is recorded on the turn (`rejected_files`) and named in the report warnings, so
  affected trials are easy to find.
- Trials scored before this change with a MIME-labelled deck were scored as "no deck". They need an
  eval-only rerun to be re-scored; their decks were not saved, so they cannot be re-scored in place.
