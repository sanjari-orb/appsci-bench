# Evaluation Data — Slides Generator

## Layout

One subdirectory per `data_point_id`, each holding a `sample.json` plus any files it needs
(schema/dataset.schema.json `$defs/eval_sample`). Two sample families, 10 samples total:
`product-slidesgen-01..05` (product-curated) and `generated-sample-00..04` (built from open-source
documents). See `PROVENANCE.md` for exactly where each sample's source material came from.

### Product-curated (5): `product-slidesgen-01..05`

Real product examples, each testing a distinct generation challenge (template adherence against
a prior deck, groundedness under numbers, persuasive sequencing, synthesis + translate-down,
answered-vs-deferred grounding). **Files are committed to the repo via Git LFS** — the large binaries
(`*.pptx`) are LFS-tracked (see the top-level `.gitattributes`), the rest is plain git. A normal
clone with Git LFS installed pulls them automatically; if you only see 3-line pointer files, run:

```bash
git lfs install && git lfs pull
```

> This eval data is **held-out from the agent**: it is read on the **host** during the eval phase
> and is **never mounted to the agent** at build time. The agent's only build-time example is the
> placeholder under `../environment/data/samples/`. (Committing it to the repo does not change that
> boundary — the agent container only ever mounts `environment/data/`, not `eval_data/`.)

Each sample's `template.pptx` is derived from the same real anonymized
corporate template.

`expected_output.json` is `{"presentation": {...}, "user_constraints": {...}}` — `presentation` is
the gold deck content, `user_constraints` holds deck-level requirements (currently just
`expected_slide_count_range`, populated for every sample across both families — see
`tasks/slides_generator/eval.py`'s `constraint_satisfaction` for the objective that reads it).
`expected_output.files` additionally carries each sample's `gold.pptx`. Per the design's own caveat
(still true): this is an AUTHORING/sanity aid, not a slide-by-slide reference target — no objective
does reference-based slide or pptx matching against it.

### Generated (5): `generated-sample-00..04`

Built from public, open-source documents rather than product-simulated content — closes coverage
gaps the product-curated set left thin (see `tasks/slides_generator/GENERATION_SPEC.md` for the
per-sample rationale, each cited to a specific L3 persona fact and objective). Each sample's
`template.pptx` comes from the Zenodo10K dataset (sourced via PPTAgent; see
`tasks/slides_generator/README.md`'s Public Datasets table), and each gold deck was produced from
that template via Microsoft Copilot in PowerPoint followed by manual curation. `.pptx` files are
Git LFS-tracked like the product-curated set.

| Sample | Tests | Source document(s) |
|---|---|---|
| `generated-sample-00` | Content-selection under ambiguity (business vs. governance) in a dense SEC filing, groundedness | Apple Inc. DEF 14A (SEC EDGAR) |
| `generated-sample-01` | Template-as-content-source confusion — a prior deck with its own distinct visual style is attached alongside the assigned `template.pptx` | Zenodo record 17256797 (SECreTour), exported as `prior-deck.pptx` |
| `generated-sample-02` | Disagreeing multi-source input — a narrative document and a data spreadsheet state different figures for the same claim | Zenodo record 8262919; `.docx` is generated narrative, `.xlsx` is real response data |
| `generated-sample-03` | Exclusion under direct pressure — the request asks for content the source explicitly lacks | Zenodo record 21871990 |
| `generated-sample-04` | Multi-document synthesis into one deck from two related sources | Alphabet Q2 2026 earnings release (SEC EDGAR), split into a PDF and a companion spreadsheet |

No customer or confidential data is used in this family; see `PROVENANCE.md` for exact record IDs,
filing dates, and any cropping/alteration applied to each source.

> `generated-sample-02` currently has stray Office lock files (`~$...`) checked into its directory —
> worth cleaning up before treating that sample as final.

### Local, real, immediately runnable (`slidesgen-01..04`) — temporarily removed

The 4 local `slidesgen-01..04` samples (output of `curation_scripts/main.py`'s pipeline over real
reference decks) have been **temporarily removed** to avoid dealing with double-sourced documents
before the first end-to-end run on slides_generator is nailed down. They will return once the
sample format is finalized; the curation pipeline that produced them is still in
`tasks/slides_generator/scripts/`.

## `eval_rubric` — this sample's expectations

Every sample carries an `eval_rubric` string: a hand-distilled checklist of the concrete expectations
that sample was built to test, taken from its `EXPECTATIONS.md`. It is consumed by **exactly one**
objective, `expectations_satisfaction`, which scores the proportion of the checklist the deck
satisfies.

Three rules govern this field. Breaking any of them silently degrades the metric, so
`tests/tasks/slides_generator/test_eval.py` enforces all three:

1. **Only `expectations_satisfaction` reads it.** `eval_rubric` is the one input that *varies* per
   data point. Routing it into a single dedicated objective keeps every other metric's definition
   identical across samples — a metric whose criteria shifted sample to sample would not be
   comparable across samples. Do not add `{{ eval_rubric }}` to another prompt.
2. **It is per-sample intent, never a metric definition.** A metric's own scoring criteria are fixed
   by the task designer and live in `eval.py`'s prompt constants. `EvalSample.eval_rubric`'s docstring
   in `appsci_bench/eval/datatypes.py` spells out the distinction; it is easy to violate.
3. **Content and structure only — no visual expectations.** `expectations_satisfaction` is a
   *transcript* judge: it sees the JSON **extracted from the produced `.pptx`** (speaker notes
   stripped; see `tasks/slides_generator/pptx_json.py` and `eval.py`'s `_deck_conversation`) and
   **never a render**. So a rubric line may assert content, figures, arithmetic, caveat survival,
   ordering and narrative beats, placeholder markers as text, footer text, and whether a `table`
   element exists — but nothing about colour, font, palette, or visual appearance, and nothing that
   depends on speaker notes (invisible to this judge, by design — see `KNOWN_ISSUES.md`). Visual
   expectations belong to `template_adherence`, which does see the render.

Two consequences worth knowing (both specific to the anonymized `product-slidesgen-*` family):

- Each `EXPECTATIONS.md`'s "Visual fidelity" section is therefore **out of scope** for this metric.
  Sample 01's headline test — apply the brand template's visuals rather than the prior deck's Helio
  style — is purely visual and is not checked directly by any objective (see `KNOWN_ISSUES.md`).
- Anonymization replaced company/customer/person names with `*_REDACTED` in both `EXPECTATIONS.md`
  and the gold decks, so two expectations are omitted as unverifiable: sample 02's "do not name the
  at-risk account" and sample 01's exec-sponsor carry-forward. Everything else survived redaction —
  the brand palette, `Lato`, every figure, and structural tokens like `Q2 FY26`.
- **Every sample's footer did not survive anonymization.** Each `EXPECTATIONS.md` describes a footer
  carrying the confidential marker + context (sample 01 additionally claims a positioned
  bottom-left/bottom-right footer with a page number); across all 5 gold decks, that content instead
  ended up as plain body text on slide 1, and sample 01's has no footer at all. `template_adherence`
  therefore can't be checked against a real footer example in any gold deck — treat the footer
  language in each `EXPECTATIONS.md` as illustrative of intent, not a reproduced gold example.

**A sample shipped without an `eval_rubric` silently drops a weight-2.0 objective** (the metric
returns `None`, which is excluded from aggregation rather than charged as a 0.0). A test asserts every
shipped sample declares one; keep it that way when adding samples.

## Objective applicability

Not every objective applies to every sample — see `tasks/slides_generator/eval.py`'s docstrings for
the authoritative per-objective detail, and `spec.yaml` for weights. Six of these objectives
(`prompt_satisfaction`, `structure_clarity`, `content_narrative`, `expectations_satisfaction`,
`content_coverage`, `groundedness`) score the JSON **extracted from the produced `.pptx`**, not the
artifact's self-reported JSON — a `.pptx` that cannot be opened at all scores `0.0` on all six, never
`None` (there is no JSON to hand a judge). A `.pptx` that opens but is degenerate (zero slides, or
every slide's elements empty) still produces a real extracted JSON and is scored by the judge like
any other deck — it is not pre-empted by a hard-coded floor. See `eval.py`'s module docstring.
- `functional_success`, `output_format_compliance`, `prompt_satisfaction`, `structure_clarity` and
  `content_narrative` apply to all samples. `output_format_compliance` is the **proportion** of
  compliant slides, not a pass/fail, and is the one objective that keeps reading the self-reported
  JSON as a deliberate choice (see `eval.py`) — it is the contract check on the CLAIM.
- `constraint_satisfaction` is `None` (not applicable) when a sample declares no
  `expected_slide_count_range`.
- `expectations_satisfaction` is `None` when a sample declares no `eval_rubric` (see above); this is
  checked before the unopenable-`.pptx` gate, so it stays `None` (not `0.0`) even when the produced
  deck can't be opened at all.
- `content_coverage` and `groundedness` are real source-reading judges
  (`make_source_coverage_judge` / `make_source_grounding_judge`): both read the query's input
  source-file content and check it against the produced deck's extracted JSON, returning a 0–1
  score, or `None` when a sample has no non-template source files to check against (a legitimate
  `None` distinct from the `0.0` an unopenable `.pptx` gets). `content_coverage` measures what
  proportion of the source facts the deck covers (and degrades — truncates source — on overflow);
  `groundedness` flags fabrication (and hard-bails to `None` on overflow). Sample 05's only
  non-template source is `clarification_qa.md` (the merged clarifying-questions/answers exchange,
  some answered, some explicitly deferred) — both judges read it like any other source file. This is
  distinct from
  `prompt_satisfaction`, a transcript judge measuring whether the deck followed the prompt's explicit
  instructions.
- `template_adherence` and `layout_quality` are real vision judges
  (`make_visual_comparison_judge` / `make_visual_quality_judge`): both render the relevant deck(s)
  to PDF via LibreOffice and judge natively. They return `None` (not an unconditional placeholder)
  when the template/output file is missing, rendering fails, or the model lacks native PDF support.
  Both see only the **first 4 slides** of a deck.
- Removed: `figure_relevance` and `layout_color_quality`. `outcome_coverage` was renamed to
  `prompt_satisfaction` and retargeted onto instruction-following. `design_quality` remains out.
  Archived `eval_report/report.json` files predate all of these changes.

## scripts/curation_scripts/ (`tasks/slides_generator/scripts/`, not `eval_data/`)

The pipeline that produced the `slidesgen-01..04` samples' `presentation.pdf`/`.md`/`input_brief.md`/
`presentation_template.pptx`/`presentation_json.json` from an original reference `presentation.pptx`. Kept in a sibling
`scripts/` directory, not inside `eval_data/` itself -- the dataset loader treats every
subdirectory of `eval_data/` as a sample, so tooling can't live there. Not needed at eval time.
