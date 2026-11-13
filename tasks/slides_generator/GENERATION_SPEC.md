# Generating eval samples for slides_generator


This is a **spec-only** document. It defines the scenario, inputs, and grading target for each new
sample in enough detail that someone can later author its `PROMPT.md`, `EXPECTATIONS.md`,
`sample.json`, `template.pptx`, and gold files. No sample files or `eval_data/` directories are
created by this document.

Use ids `generated-sample-00` through `generated-sample-04` — a prefix deliberately disjoint from the
existing `product-slidesgen-*` samples, so provenance is visible from the id alone.

## Why this batch is needed

The existing samples cover several scenarios:

- `00` — content-selection under ambiguity (business vs. governance in an SEC filing) + groundedness
  in a dense, unfamiliar format.
- `01` — template-outranks-the-request (the prompt text asks for the *wrong* look) + structural
  carry-forward from a prior deck + numeric delta computation.
- `02` — numeric groundedness under a tables-heavy financial translate-**up**; exact figures, no
  rounding, correct variance direction.
- `03` — persuasive sequencing/ordering rather than faithful restructuring; grounding under pressure
  to "sell" (resisting invented ROI numbers).
- `04` — long-document (~2,600 words) synthesis under real 5:1 compression, translate-**down**
  (expert → layperson), caveat survival buried in prose.
- `05` — sparse input, ask-then-build-with-placeholders loop; explicit-vs-deferred fact handling.

Every existing sample's prompt already states an explicit or approximate slide count ("5-6 slides
exactly," "~8 slides," "~10-slide," "~6 slides"), and `sample.json`'s `expected_slide_count_range` is
bound tightly enough to each deck type's `slide_count_guidance` band that `constraint_satisfaction` is
already meaningfully exercised across the set — not a gap.

But a few things the scoring function checks are barely, or never, exercised:

- **Template-as-content-source confusion** — L3 is explicit that `template.pptx` "is there for the
  look, not as material to put on the slides," but no sample plants that confusion via an attached
  file (only `01` plants it via the request text).
- **Disagreeing multi-source input** — every existing sample's sources are mutually consistent (one
  document, or one CSV plus one commentary that agrees with it). Nothing tests `groundedness`/
  `content_coverage` against two sources that contradict each other.
- **Exclusion under direct pressure** — `what_good_looks_like` says a good deck "never pads with
  content you didn't provide," and `03` tests this only as an implicit temptation (sell harder).
  Nothing tests it against an explicit instruction to add something the brief states isn't available.
- **Multi-document synthesis into one deck** — nothing tests combining two separate source documents
  about the same initiative into a single coherent review, as opposed to translating one document
  (`02`, `04`) or carrying one forward (`01`).
- **Audience register beyond expert→layperson within one company** — `04` already tests
  research→non-expert framing for an internal leadership audience; nothing tests the same
  expert-source-to-business-audience move for a genuinely business-facing deck (paper/technical
  write-up in, business-audience deck out), a register shift `audience_variety` names but no sample
  exercises.

The targets below are set to close these gaps, each cited to a specific L3 fact and a specific scored
objective.

## The persona is the spec

Every sample below cites one or more of these L3 `domain_knowledge` keys (`user_personas/l3.yaml`) —
no L1 or L2 fact is used anywhere in this document, since L1/L2 hold the method and integration
contract, not the business reality a sample is meant to simulate:

1. **`business_pain`** — decks are slow to build manually and content overlaps across decks.
2. **`what_good_looks_like`** — six sub-points: (a) covers everything asked, never pads with content
   not provided; (b) a stranger to the topic follows it unaided; (c) matches the template's
   letterhead feel, not a generic deck plus logo; (d) visually clean, nothing crammed or off-edge;
   (e) respects inclusions *and* exclusions, pitched for who's in the room; (f) flows as a narrative,
   not just slides in order.
3. **`input_flexibility`** — sparse notes → prompt text → up to 5 uploaded documents (docx, pdf, pptx,
   csv or xlsx); unreadable attachments must be flagged, not silently dropped.
4. **`audience_variety`** — executives, clients, board, internal teams each expect different depth,
   tone, and detail.
5. **`fidelity_vs_synthesis_trade_off`** — balancing mirroring the source structure against reframing
   for the audience.
6. **`slide_count_guidance`** — QBRs ~8, product pitches 8-12, research briefings 6-8, sparse status
   updates 3-5; an explicit user count overrides the default band.
7. **`template_expectations`** — `template.pptx` is for look only, never a content source; everything
   else attached is content.

## Sample targets

| id | scenario | inputs | template (combo) | primary objective(s) stressed | L3 fact cited |
|---|---|---|---|---|---|
| `generated-sample-00` | Content-selection under ambiguity in a dense SEC filing (business vs. governance) | `Apple Inc. - DEF 14A.pdf` + `template.pptx` | `Combo_11_culture_ID_20231024_UNIVIE_RDM_DSTraining` | `expectations_satisfaction`, `groundedness`, `template_adherence` | `what_good_looks_like` (a), (e) |
| `generated-sample-01` | A prior deck is attached alongside `template.pptx`, but the prior deck is itself visibly built on a *different* branded look | Prior deck (manually extracted from a differently-styled source PDF; different colors/fonts than the assigned template) + `What makes a place to visit_.pdf` (new content to update the deck with) + `template.pptx` | `Combo_12_culture_ID_20240425_FM_GraspOS_SSH_RRA` | `template_adherence` (0.75), `groundedness` (2.0) | `template_expectations` — "it comes in as template.pptx, and it is there for the look, not as material to put on the slides. Everything else we attach is content" |
| `generated-sample-02` | Two source files disagree on the same figure — a research-introduction document states a survey reach of ~500 responses and a specific claim about cooperative-member bioenergy interest, while the raw response spreadsheet contains 382 actual rows and must be used to substantiate (not just restate) the claim | `GENERATED_PROJECT_NAME_Research_Introduction.docx` (synthetic research introduction; all project/author/funding identifiers are `GENERATED_*` placeholders) + `BECoop_Potential for the Bioenergy Market Expansion.xlsx` (382 real response rows, underlying data retained from the original dataset) + `template.pptx` | `Combo_16_culture_ID_2_Arianna_Martinelli_Maynooth_Recreating Europe Workshop 10 December 2021` | `groundedness` (2.0), `content_coverage` (1.0) | `what_good_looks_like` (a) — "nothing you flagged as important got dropped, and it never pads with content you didn't provide" — plus `input_flexibility` — "agent extracts and synthesizes content" |
| `generated-sample-03` | Request explicitly asks to add something the same brief states is unavailable (e.g. a historical/longitudinal trend the source document does not track) | `DVAM_Digital_Value_Alignment_Model (2).pdf` (no historical/longitudinal data) + prompt with the explicit ask + `template.pptx` | `Combo_17_culture_ID_4-4_Hermans_OAI13` | `groundedness` (2.0), `expectations_satisfaction` (2.0) | `what_good_looks_like` (a), inverse clause — "never pads with content you didn't provide" |
| `generated-sample-04` | Two related but separate documents about the same initiative must merge into one requested deck — a real earnings release plus a spreadsheet of figures extracted from it | `google.pdf` (Alphabet Q2 2026 earnings release) + `Alphabet_Q2_2026_Financials.xlsx` (same quarter's figures extracted from it) + single prompt asking for one combined review + `template.pptx` | `Combo_02_culture_ID_2021-02_IBEC2021_Presentation_AC` | `content_coverage` (1.0), `structure_clarity` (0.5) | `input_flexibility` — "Up to 5 uploaded documents ... agent extracts and synthesizes content" |

Each of `generated-sample-01` through `-05` is assigned a distinct template combo from
[`eval_data/templates/`](eval_data/templates/) — one combo per sample, no combo reused across samples,
so no sample's template also appears as another sample's content or prior-deck material.

