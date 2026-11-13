# Generated-sample-00: Sample Creation Summary

## What was created

A new evaluation sample for the slides generator task that tests:
1. **Template adherence** — Apply brand styling to a generated deck
2. **Groundedness from complex formatted documents** — SEC filings have dense, multi-section structure
3. **Content selection** — Distinguish business-relevant content (segments, revenue, R&D) from out-of-scope content (compensation, governance, voting procedures)

## Files created

**In `/tasks/slides_generator/eval_data/generated-sample-00/`:**

- **PROMPT.md** — The user's request (concise, ~200 words): build a 5-6 slide executive summary from Apple's DEF 14A, focused on business performance and investor priorities, applying the template's visual style, excluding compensation/governance details, with every number grounded in the filing.

- **EXPECTATIONS.md** — What correct output looks like (abstract, criteria-focused, not prescriptive): A deck that clearly separates business content (in-scope) from compensation/governance content (out-of-scope), with every specific claim traceable to the DEF 14A, and visual style matching the template.

- **sample.json** — The evaluation data point in the standard format:
  - `data_point_id`: `generated-sample-00`
  - `single_turn_input`: References the PDF (Apple Inc. - DEF 14A.pdf) and template (template.pptx)
  - `eval_rubric`: Detailed scoring rubric for the three core expectations: content selection (THE CRUX), groundedness, and template adherence

- **Other files already present:**
  - `Apple Inc. - DEF 14A.pdf` (source document)
  - `template.pptx` (brand template to apply)

## Why this sample tests the target behaviors

### Content Selection (THE CRUX)
A DEF 14A proxy filing is deliberately ambiguous — it contains both business disclosure AND governance/compensation details in the same document. The agent must read carefully enough to filter out what is NOT investor-relevant business content (compensation, board details, voting procedures) while extracting what IS (segments, revenue, strategy, risks). This is a stronger test than working with pure business documents.

### Groundedness in Complex Formats
SEC filings have dense structure (Item numbers, sections, footnotes, financial statements). Agents often either:
- Over-generalize ("good growth in China" when the filing says "3% growth")
- Fabricate ("$50B services revenue" when the filing doesn't state this exact figure)
- Miss precision (reporting "margin" when the filing distinguishes "gross margin" vs "operating margin")

This sample rewards careful sourcing and penalizes guessing.

### Template Adherence
With a real, complex document (not a simple metrics dump), the agent must still apply the template's visual style correctly across 5-6 slides without letting complexity excuse visual sloppiness.

## How to grade it

The `eval_rubric` in sample.json captures three expectations:

1. **Content Selection (THE CRUX)** — If the deck includes compensation, governance procedures, board details, or voting mechanics, it FAILS this expectation. Otherwise, it PASSES if it covers business segments, revenue, R&D, and strategic priorities.

2. **Groundedness** — Every specific number, segment name, and strategic claim must be traceable to a specific location in the DEF 14A. No estimates, no inferences, no fabricated figures.

3. **Template Adherence** — Color palette, fonts, layout, and footer styling must match template.pptx.

The eval functions in `tasks/slides_generator/eval.py` will score these via:
- `expectations_satisfaction` (uses eval_rubric to score content selection)
- `groundedness` (source-reading judge checking DEF 14A for fabrication)
- `template_adherence` (vision judge comparing rendered PDFs)

## Next steps

1. Run a test build against this sample to verify the agent can:
   - Parse the PDF correctly
   - Extract business content without hallucinating
   - Apply the template visually
   - Stay within 5-6 slides

2. Create a gold.json and gold.pptx if a reference output is available.

3. Add to the spec.yaml if not already included.
