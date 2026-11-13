# Context

An applied-scientist agent is given a vague user request of the form "A team wants help turning requests into presentable slide decks; a stakeholder is available to talk." and must elicit requirements from L1/L2/L3 personas before building a slides-generator system artifact. The system artifact is then evaluated by the Oracle: it receives a user prompt plus attached files (grounding documents and a template.pptx) and is expected to return a deck representation that can be scored against the gold solution.

## BUILD TIME
```
applied-scientist agent  ← vague request + persona sim (L[*] persona + task spec)
                        → slides-generator system artifact
```

## TASK RUN TIME
```
slides-generator system artifact ← user prompt + attached files (grounding docs + template.pptx)
                                 → JSON array of slide objects + .pptx deck
```

## EVAL TIME (Oracle)
```
Oracle → system artifact ← eval sample request
                        → measure of how close to gold solution the system artifact was
```

The system artifact is a queryable agent (kind=agent, primary). An optional design_notes.md may be produced as a documentational artifact.

## Vague Request

The applied-scientist agent receives only this request at the start of a run e.g.:

> "A team wants help turning requests into presentable slide decks; a stakeholder is available to talk."

Everything else: output format, slide-count heuristics, audience handling, input quality variation, evaluation criteria must be elicited from the available personas.

## Deliverable

Slide generation spec encompass all the details necessary to execute the slide generation task  at build time and eval time. Including:

### Task Definition Spec

Given the open nature of the slide generation problem, a narrow specification is provided covering a set of feature requirements which are aligned with the gold solution design.

### Task Personas Specs

Task definition is shared across user personas L1/L2/L3 and styles (terse/collaborative), but a task persona file is created as input for the persona simulator. These specs are consumed at build time.

### Eval Specs

Designed to provide an estimation of what is considered a good result of a test of the system artifact at runtime.

- Validation over functional requirements
- Content evaluation
- Reference slide deck based on the problem statement

### Task Specific Features

**Subjective evaluation**: No universal criteria define a good presentation. Metrics are scattered across literature, including LLM as a judge, non-functional metrics and modified traditional NLP.

**Require multimodal reasoning**: Input and output use various formats combining interrelated images and text. Evaluation must include natural language and visual metrics.

**Context limitations**: Although model context windows have grown, LLM performance drops as token count rises and hallucinations increase. PowerPoint presentations require long input consumption and multiple iterations during outlining and refining, demanding complex architectures.

**XML format**: PowerPoint presentations use XML, which challenges agents. Many approaches work in HTML, then export to XML after editing.

## System Artifact (Slide Generator) I/O Contract

### Input (v1)

A user prompt plus optional attached files. Content is domain-agnostic business scenarios. Attachments may include: source documents (.md, .pdf, .csv), a previous deck (.pptx), and a branding template (.pptx). Some requests include an explicit outcomes list; others require the system to infer structure from prose.

- Volume: up to 50 requests/day
- Typical prompt: 200–1,000 words
- Attached files: template.pptx is mandatory. The number of grounding documents must be between 0 and 5. 

### Output (v1)

A JSON presentation object and the generated deck file (.pptx):

```json
{
  "presentation": {
    "title": "Q2 Performance Report",
    "theme": "modern_dark",
    "aspectRatio": "16:9",
    "metadata": {
      "author": "Data Analytics Team",
      "version": "1.0",
      "date": "2026-06-15"
    },
    "slides": [
      {
        "slideNumber": 1,
        "layout": "Blank Basic Layout / Photo full",
        "elements": [
          {
            "type": "text",
            "name": "TitlePlaceholder",
            "content": "Quarterly Results Q2",
            "style": {
              "fontSize": 44,
              "bold": true,
              "color": "#FFFFFF"
            }
          },
          {
            "type": "text",
            "name": "SubtitlePlaceholder",
            "content": "An analysis of user growth and retention metrics",
            "style": {
              "fontSize": 20,
              "color": "#A0A0A0"
            }
          }
        ]
      },
      {
        "slideNumber": 2,
        "layout": "Header_1 block",
        "elements": [
          {
            "type": "text",
            "name": "Header",
            "content": "Key Metrics Summary"
          },
          {
            "type": "image",
            "name": "FeatureChart",
            "sourceUrl": "https://example.com",
            "altText": "Bar chart showcasing a 15 percent increase in macro sales"
          },
          {
            "type": "bullet_list",
            "name": "KeyPointsList",
            "items": [
              "Net revenue increased by 14.5% quarter-over-quarter.",
              "User churn rate successfully reduced to 2.1%.",
              "Expansion initiated into European markets ahead of schedule."
            ]
          }
        ],
        "notes": "Remind the team that European market performance heavily exceeded our baseline projections."
      }
    ]
  }
}
```

### Deployment Constraints

- HTTP service, synchronous response
- SLO: 30-60 secs (longer for longer inputs)

## Task Container Environment

The task container (`appsci-bench/slides_generator:latest`) is built from `tasks/slides_generator/environment/`. It provides the workspace where the applied-scientist agent explores resources and builds the slides-generator artifact.

### Data Mounts (injected by TaskRunner at runtime)

| Container path | Content |
|----------------|---------|
| `/data/samples/*` | Synthetic request examples, each a text prompt plus attached files (grounding docs + template.pptx). Inputs only — no reference answer; see `environment/README.md` |
| `/data/samples/*/template.pptx` | The branded template accompanying each request |
| `/sop/` | `kpis.md` only: the business pain, what the teams need. States that what a *good* deck is has to be agreed with the customer — no criterion, no deck length, no accepted file types, nothing about the response shape. The PowerPoint format mechanics and request packaging are elicited from the personas, not mounted |

Data files are **not** baked into the image — TaskRunner bind-mounts them at runtime.

### Scratch & Output

| Container path | Purpose |
|----------------|---------|
| `/work/` | Agent writable scratch |
| `/work/out/` | Artifact outputs (queried by evaluator) |

### Python Dependencies

Key packages installed in the task container (see `requirements.txt`):

- `python-pptx` + `Pillow` — PPTX generation and image handling
- `fastapi` + `uvicorn` + `httpx` — artifact server stack (used by agent-built artifacts)
- `jinja2`, `pyyaml`, `python-dotenv` — utilities

### Local Development

```bash
# Build the image
docker build -t appsci-bench/slides_generator:latest tasks/slides_generator/environment/

# Run with local data stubs (create tasks/slides_generator/environment/data/ first)
docker compose -f tasks/slides_generator/environment/docker-compose.yml up

# Smoke-test python-pptx
docker run --rm appsci-bench/slides_generator:latest python -c "import pptx; print('ok')"
```

## User Personas

Six personas: three ambiguity levels, each with a cooperative and an adversarial variant. Level varies
**what the persona knows**, not only how it answers — see
[`docs/task_information_placement.md`](../../docs/task_information_placement.md). L1 is the widest
level (business framing + integration contract + method); L2 holds the integration contract but no
method; L3 holds the business side only.

| Level | Persona | Holds | Does not hold |
|-------|---------|-------|---------------|
| **L1** | `l1` / `l1_adversarial` | Everything L2 and L3 hold, plus method: agentic patterns and architecture, synthesis-vs-fidelity, token economics, the OOXML/intermediate-format trade-off, evaluation difficulty, known limitations | — |
| **L2** | `l2` / `l2_adversarial` | The integration contract: sync HTTP service, both deliverables in one call, the 30–60s SLO, ~50 requests/day, engineering practices | Audience strategy, slide-count guidance, any AI-method question, subjective visual/narrative judgment |
| **L3** | `l3` / `l3_adversarial` | The business side: pain, audience variety, what a good deck means, slide-count guidance by deck type, fidelity-vs-synthesis framing, editable-deliverable requirement | The integration contract, method, metric thresholds |

The **output contract** is not in any persona: it lives in `user_sim_docs/common/output_contract.md`,
so every level can describe how a finished deck comes back when asked, in customer terms. It states
the shape (a presentation object, an ordered slide list, typed named content pieces per slide) AND the
concrete field names graded against (`slideNumber`, `layout`, `elements[]`, etc., with a worked JSON
example) — those are wire syntax, not a customer choice, so leaving them to be guessed only penalized
the agent for a reasonable-but-different naming convention.

Each level now also states template-visual-match, layout hygiene, instruction-following, and
narrative coherence — but voiced per level rather than in one shared wording: L1 states them as
testable technical checks (geometric properties, measurable palette/font match); L3 states them as a
stakeholder's own outcome judgment, in example terms ("looks clean," "reads as one story"), never as
a spec or metric name; L2 keeps only the narrow integration-facing slice (non-empty slides, a file
that opens and reflects the template) and explicitly defers the subjective call to the business side.
Every objective except the per-sample `expectations_satisfaction` traces to some persona's
`domain_knowledge`, worded in that level's own register — no persona text names an objective directly.

The adversarial variants add an honestly-voiced mistaken belief plus a pressure overlay; each plant
contradicts something the same persona legitimately knows, so the contradiction is discoverable.

## Evaluation Metrics

All objectives run against the held-out `eval_data/` dataset (5 product-curated samples,
`product-slidesgen-01..05` — see `eval_data/README.md`). The 4 local `slidesgen-01..04` samples
have been **temporarily removed** to avoid dealing with double-sourced documents before the first
end-to-end run on slides_generator is nailed down; they will return once the sample format is
finalized. See `tasks/slides_generator/eval.py` for the eval-function implementation of each
objective and `spec.yaml`'s `evaluation_data.objectives` for weights.

| Objective | Kind | Weight | What It Measures |
|-----------|------|--------|------------------|
| `functional_success` | deterministic | 0.5 | Non-empty JSON presentation object with a non-empty slides array, plus an openable .pptx |
| `output_format_compliance` | deterministic | 0.5 | **Proportion** of slides having a non-empty slideNumber, layout, and at least one element with non-empty content. Proportional, not all-or-nothing: one bad slide in eleven is not the same outcome as a wholly broken deck |
| `constraint_satisfaction` | deterministic | 0.5 | Slide count falls within `expected_output.json`'s `expected_slide_count_range`, when declared. `None` otherwise. [lit: DeepPresenter Constraint Score — slide count only; language and aspect ratio not checked] |
| `groundedness` | LLM-judge (0–1) | **2.0** | Produced deck is grounded in the query's input source files (no fabricated facts/figures, correct arithmetic); reads source-file content via `make_source_grounding_judge`, excluding the styling template by name. Hard-bails to `None` on overflow rather than truncating a source figure |
| `expectations_satisfaction` | LLM-judge (0–1) | **2.0** | **Proportion** of this sample's hand-written expectations satisfied, supplied through the per-sample `eval_rubric` channel (distilled from its EXPECTATIONS.md). The **only** objective that reads `eval_rubric` — see below. Content and structure only; it is a transcript judge and never sees a render. `None` when a sample declares no expectations |
| `content_coverage` | LLM-judge (0–1) | 1.5 | What **proportion of the material facts/figures in the query's attached input files** the deck covers; reads source-file content via `make_source_coverage_judge` (excluding the styling template by name), reference-free; degrades (truncates source) on overflow |
| `prompt_satisfaction` | LLM-judge (0–1) | 1.5 | Whether the deck did what the brief **asked for** — explicit instructions honoured, requested audience/register hit, stated exclusions respected, requested emphasis reflected. Transcript judge; sees the brief + generated deck only. [lit: DeepPresenter instruction-following] |
| `structure_clarity` | LLM-judge (0–1) | 0.75 | Coherent flow, clear sections, transitions, tone appropriate to the stated audience |
| `content_narrative` | LLM-judge (0–1) | 0.75 | Story arc (problem → approach → results → takeaway), cohesion and signposting, per-slide progression, framing of stakes. [lit: EvoPresent "Narrative"] |
| `template_adherence` | vision judge (0–1) | 0.75 | Whether the output visually adopted the template's color/font/layout theme; renders template.pptx + output to PDF via LibreOffice and judges natively (`make_visual_comparison_judge`). Returns `None` (not a placeholder) when the template/output file is missing, rendering fails, or the model lacks native PDF support |
| `layout_quality` | vision judge (0–1) | 0.75 | Layout of the rendered output deck: element bounds and overlap, alignment and margins, balance and whitespace, reading order, typographic hierarchy (`make_visual_quality_judge`). Same `None`-on-infra-gap behavior as `template_adherence`. [lit: AutoPresent `layout` + EvoPresent Layout/Hierarchy] |

**Weighting rationale.** Weights group by *concern*, not by function name, replacing an earlier
uniform-1.0 default that `spec.yaml` itself flagged as a non-decision. `groundedness` and
`expectations_satisfaction` are heaviest: the first is the product's core claim and the dimension
three of five samples weight highest, the second is the only objective checking what each sample was
purpose-built to test. `structure_clarity` and `content_narrative` are correlated by construction
(both judge narrative), so each takes 0.75 and the pair carries roughly one objective's worth. The
three functional/constraint checks take 0.5 each so pass/fail plumbing cannot outweigh substance.

**`eval_rubric` is routed to exactly one objective.** It is the only input that *varies* per data
point, so it feeds `expectations_satisfaction` alone; every other metric scores from fixed inputs and
therefore means the same thing on every sample. A metric whose definition shifted sample to sample
would not be comparable across samples. `tests/tasks/slides_generator/test_eval.py` asserts both that
no other prompt template references it and that the other rubric-eligible objectives
(`content_coverage`, `groundedness`, `template_adherence`, `layout_quality`) don't forward it to the
judge call at runtime, and that no shipped rubric names a colour, hex, or font (those are visual, and
this judge cannot see a render).

**Not adopted.** PPTAgent's PPTEVAL "Coherence" was evaluated and **not** adopted: its rubric caps
top scores on speaker/date/acknowledgment metadata, which internal QBR and board decks legitimately
do not carry.

Note: `functional_latency` is not a declared objective in `spec.yaml`/`eval.py` — timing scoring
would require adding a timing field to the shared `GenerationRecord`/harness code, outside this
task's scope. See "Accepted limitations of the metric set" under Outstanding TODOs for the visual-
and constraint-scoring gaps.

**Judge prompt format.** Every judge prompt requires a JSON response with exactly two keys:
`reasoning` (string, step-by-step evaluation) and `score` (number, 0–1), for consistent,
machine-parseable output. Scoring bands are half-open (`0.8-1.0`, `0.6-0.79`, `0.4-0.59`,
`0.2-0.39`, `0.0-0.19`) so a score of exactly 0.8 falls in exactly one band.

**Eval sample format.** Each sample's `expected_output.json` is
`{"presentation": {...}, "user_constraints": {...}}`: `presentation` holds the gold deck's metadata
and full slide definitions (elements, styling, notes); `user_constraints` holds deck-level
requirements, currently `expected_slide_count_range` (populated for all 5
`product-slidesgen-01..05` samples), read by `constraint_satisfaction`. Product-curated samples
additionally carry their `gold.pptx` in `expected_output.files`. The mandatory template attachment
is named `template.pptx` in every sample.


## Gold Solution

**Reference path**: `gold/slides_generator/`

The gold approach is defined in the Overall Agentic Architecture section (internal document, not public).

`gold/slides_generator/reference_artifact.py` replays each eval sample's own `expected_output.json`
presentation plus its sibling `gold.pptx` directly (no LLM, no generation) — proving the eval
pipeline end-to-end. Purpose-built gold decks beyond the current eval samples are not authored yet.

## Documentational Artifacts

The applied-scientist agent may optionally produce a `design_notes.md` alongside the slides-generator system artifact. It is a diagnostic write-up only and is **not scored** — the `design_quality` objective that formerly graded it has been removed from the task evaluation (it had weight 0 and was not reachable from `conversation`; see "Outstanding TODOs"). The submission is never penalized for its absence.

---

# Slide Generation Problem Statement

Making a [good] presentation from a document is very time expensive and needs domain expertise [MultistageE2E].

**PRD**: Slide Generation Agent Product Requirements for MVP (internal document, not public)

A tool that turns a user's input (a markdown brief, an uploaded document, or even sparse notes) into a finished, on-template PowerPoint deck, grounded only in what the user provides (no made-up facts). The agent first establishes the deck's theme from the template, then fills in content to fit. The user reviews an outline before slides are built, and can make conversational edits afterward that change only the targeted slide rather than regenerating the whole deck. We own the tech, keep data in our environment, and aim to beat SlideSpeak on cost [PRD].

---

# Technical Design

In order to generate good presentations, two points stand up in literature:

1. **Structured narrative**: The slide and the deck must tell a story, with a logical and engaging flow that connects with the audience.
2. **Visual design**: Figures, layout, and visual organization are central to the output, not secondary to the text.

## What a Good Slide Is

- **Concise**: Containing bullet points rather than full sentences; structured with fixed screen real estate and delivering one or few messages; visual-centric with figures as first-class citizens and visual layout playing an important role [PPT2DOC]
- **Narrative**: The slide narrative should convey a story to its audience and is often non-linear with respect to the flow of information in the document (Hargood, 2009) [MultiStageE2E]
- **Accessible**: Content should be concise, easy to follow and visually appealing [MultiStageE2E]

## What a Good Deck Is

- **Coherent**: A good presentation conveys a story (or a narrative) in the form of a sequence of slides. It often needs to be generated from a long document which contains multimodal information such as text and images [MultiStageE2E]
- **High-quality**: Combines well researched content with coherent visual design, enabling audiences to grasp complex ideas efficiently [DeepPresenter]
- **Well-crafted**: Creating high-quality presentations requires a captivating storyline, well-designed layouts, and rich, compelling content (Fu et al., 2022). Consequently, creating well-rounded presentations requires advanced presentation skills and significant effort [PPTAgent]

### Literature Overview

Literature has two main directions: developing expert models for presentation generation (usually within an agentic pattern) and exploring complex agentic architectures to iteratively craft presentations. To comply with appscibench's purpose, we will focus solely on the agentic approach.

## Slide Generation Tasks

- **Document summarization**: Image and text retrieval, and slide structure to arrange key elements in a form suitable for presentation [Doc2PPT]

**Key steps involved in creating presentation slides based on a white paper or manuscript**:
1. Establish a storyline that will connect with the audience
2. Identify essential sections and components that support the main message
3. Delineate the structure of that content (e.g., the ordering/length of the sections)
4. Summarize the content in a concise form (e.g., punchy bullet points)
5. Gather figures that help communicate the message accurately and engagingly [Doc2PPT]

**Key considerations**:
- Accommodate content quality, visual appeal, and structural coherence [PPTAgent]
- Human workflows typically involve selecting exemplary slides as references and then summarizing and transferring key content onto them [PPTAgent]
- Presentation generation requires deep content research, coherent visual design, and iterative refinement based on observation [AutoPresent]

### Four Main Tasks in Presentation Generation

**1. Summarization**: Distills source documents into concise, audience-ready content such as punchy bullet points. It requires establishing a coherent storyline and identifying the essential sections that support the main message.

**2. Figure Extraction**: Retrieves relevant images and visual elements from the source material to accurately and engagingly communicate key ideas. Figures must be selected to complement the textual content and reinforce the overall narrative.

**3. Visual Layout**: Arranges text and images into slides that balance visual appeal with structural coherence. This includes selecting or referencing exemplary slide designs and ensuring the final output meets standards of quality and aesthetic consistency.

**4. Outlining**: Delineates the high-level structure of the presentation, including the ordering and length of sections. It establishes the storyline arc that connects with the audience and guides the subsequent content and design decisions.

---

# Overall Agentic Architecture

Generating a presentation from a source document is not a single act of summarization; it is a multi-step process that combines content research, narrative construction, and visual design, followed by iterative refinement based on what the rendered output actually looks like [AutoPresent, EvoPresent]. We therefore frame the system as a staged pipeline wrapped in feedback loops: each stage produces an explicit, inspectable artifact, and dedicated evaluation loops let the system correct itself before committing to a final deck.

This framing rests on a principle that recurs throughout the literature: **there is no way to improve a presentation when you cannot evaluate it** [EvoPresent]. Many prior systems generate slides in a single forward pass and never observe the result, which is precisely why they struggle with limited storytelling, weak aesthetics, and an inability to self-adjust [EvoPresent, PreGenie]. Our architecture makes evaluation a first-class, repeated step rather than an afterthought.

## Design Principles

The architecture is shaped by three commitments:

1. **Intermediate representations over direct image synthesis**: Rather than synthesizing slide images directly (visually rich but offering limited structural control and editability), the pipeline produces an editable intermediate representation that is then rendered. This improves layout controllability and enables post-editing [PreGenie].

2. **Modularity for fast iteration**: Each stage is an independent module with a well-defined input and output artifact. Stages can be swapped, re-run, or improved in isolation, which keeps the iteration cycle short.

3. **Render-and-verify, not generate-and-hope**: Because editable intermediate representations risk producing structures that look wrong only once rendered, the pipeline closes the loop: rendered output is inspected and fed back, addressing the lack of a visual inspection mechanism that has hindered earlier code-to-slide systems [PreGenie].

## Pipeline Stages

The forward path decomposes the work into the four tasks that recur across the literature, ordered so that narrative decisions precede content, and content precedes layout:

### Parse

The source document (e.g., a research paper, report) is converted into a high-fidelity structured representation, preserving multimodal content — text, tables, equations, and figures — for downstream use.

### Outline

A high-level structure is established: the ordering and length of sections and the storyline arc that will connect with the audience. Outlining drives every subsequent content and design decision [Doc2PPT].

### Summarize

Source content is distilled into concise, audience-ready form — punchy bullet points rather than full sentences — under the structure fixed by the outline [Doc2PPT, PPT2DOC].

### Extract Figures

Relevant images and visual elements are retrieved from the source to communicate key ideas accurately and engagingly, selected to complement the text and reinforce the narrative [Doc2PPT].

### Lay Out & Render

Text and figures are arranged into slides that balance visual appeal with structural coherence, then rendered to the final format [PPTAgent].

## Feedback Loops

Two loops wrap the forward path and are the mechanism by which the system improves what it cannot improve in a single pass:

### Verification–Adjustment Loop (Content Fidelity)

The generated content is compared against the source document to identify and repair omissions or inaccuracies, ensuring complete and faithful coverage of the key contributions [AutoSlides].

### Visual Inspection Loop (Rendered Quality)

The rendered slides — not the intermediate representation — are inspected to detect post-render defects (overflow, overlap, imbalance) and trigger layout adjustment [PreGenie, DeepPresenter].

---

## Limitations

The architecture inherits several constraints that bound what it can achieve:

- **Trade-off between visual design and content**: Automated generation pipelines exhibit a tension between aesthetic quality and content construction: improving one tends to come at the expense of the other, and our staged decomposition does not by itself dissolve this trade-off [EvoPresent].

- **Feedback quality is the ceiling**: Self-improvement is only as good as the evaluation that drives it. High-quality feedback is essential, and initial model capability alone does not guarantee effective self-correction — a weak verifier caps the entire system regardless of generator strength [EvoPresent].

- **Parsing fidelity bounds everything downstream**: Academic PDFs are structurally complex; errors or context truncation introduced at the parse stage (e.g., mishandled tables and equations) propagate silently through outline, content, and layout [AutoSlides].

- **Residual post-render defects**: The visual inspection loop detects many rendering problems but is not guaranteed to catch all of them; some layout faults may survive to the final deck [PreGenie, DeepPresenter].

- **Intermediate-representation expressiveness**: Favoring an editable representation over direct image synthesis improves control and editability but constrains the visual richness achievable relative to image-conditioned approaches [PreGenie].

- **Faithfulness and omission**: The system may drop key contributions or introduce inaccuracies relative to the source. The Verification–Adjustment loop mitigates this but cannot fully eliminate it; high-stakes content still warrants human review [AutoSlides].

- **Text–image misalignment**: Generated text must align with the source's main ideas and each figure must be relevant to its accompanying text; mismatches degrade audience comprehension and can misrepresent the source [PreGenie].

- **Narrative distortion**: Because a good deck is non-linear with respect to the document's flow, the outlining stage actively reshapes the story — risking emphasis that diverges from the author's intent [MultiStageE2E].

- **Over-trust in automated evaluation**: Treating the verifier's judgments as ground truth can entrench systematic blind spots; the evaluation loop should be audited, not assumed correct [EvoPresent].

- **Compute and latency cost**: The feedback loops trade additional model calls and render cycles for quality, which raises cost and may make the pipeline impractical under tight time or budget constraints.

---

# Oracle Evaluation Criteria

"Included" reflects what the 11 shipped objectives (`spec.yaml`'s `evaluation_data.objectives`,
implemented in `eval.py`) actually score today, not literature intent — see Evaluation Metrics above
for the authoritative list. A `PARTIAL` row is covered by a shipped objective on a narrower scope
than the literature metric.

| Metric / Method | Type | What It Measures | Source | Included |
|-----------------|------|------------------|--------|----------|
| Slide-level ROUGE and other modified traditional NLP metrics | Reference-based, NLP | N-gram overlap between generated and reference slides | Doc2PPT | NO |
| Coverage (cosine similarity) | Reference-free, semantic | Semantic alignment between source document paragraphs and generated slides/bullets | MultistageE2E | PARTIAL — `content_coverage` is an LLM judge, not cosine similarity |
| Perplexity (GPT-2) | Reference-free, linguistic | Fluency and grammatical correctness of generated text | MultistageE2E | NO |
| LLM-Eval (Mistral-7B) | Reference-free, LLM-judge | Overall quality: organization, clarity, coherence, effectiveness | MultistageE2E | PARTIAL — split across `structure_clarity` and `content_narrative` |
| PPTEVAL — Content | Reference-free, multi-dim. | Quality of slide content | PPTAgent | PARTIAL — covered by `content_coverage` + `groundedness`, not a single content score |
| PPTEVAL — Design | Reference-free, multi-dim. | Visual appeal of slide design | PPTAgent | PARTIAL — `layout_quality` covers layout only, not colour/aesthetic |
| PPTEVAL — Coherence | Reference-free, multi-dim. | Structural and narrative coherence across slides | PPTAgent | NO — evaluated and not adopted (see "Not adopted" above); `content_narrative` covers narrative instead |
| Success Rate and other functional | Rule-based | Task completion and format compliance | PPTAgent | YES — `functional_success`, `output_format_compliance` |
| Element Matching | Reference-based, slide-to-slide | Correspondence of slide elements to reference | AutoPresent | NO |
| Content Similarity | Reference-based, slide-to-slide | Textual content overlap with reference slides | AutoPresent | NO |
| Color Similarity | Reference-based, slide-to-slide | Color palette match with reference slides | AutoPresent | NO |
| Text Quality | Reference-free, design | Conciseness, clear title, readable formatting | AutoPresent | PARTIAL — covered indirectly by `output_format_compliance` and `prompt_satisfaction`, not a dedicated text-quality score |
| Image Quality | Reference-free, design | Relevance and proper use of images | AutoPresent | NO — `figure_relevance` was removed; no image-relevance objective ships today |
| Layout Quality | Reference-free, design | Element placement, overlap avoidance, alignment | AutoPresent | YES — `layout_quality` |
| Color Quality | Reference-free, design | Contrast and visual consistency of colors | AutoPresent | NO — accepted gap, see Outstanding TODOs |
| Constraint Score | Rule-based | Satisfaction of user constraints (slide count, language, aspect ratio) | DeepPresenter | PARTIAL — `constraint_satisfaction` checks slide count only, not language or aspect ratio |
| Content & Style (GPT-5 judge) | Reference-free, LLM-judge | Slide content quality and visual design, validated against human judgments | DeepPresenter | PARTIAL — split across `content_coverage`/`groundedness` (content) and `template_adherence`/`layout_quality` (style) |
| Diversity (Vendi Score) | Reference-free, statistical | Visual style variance across generated presentations using DINOv2 features | DeepPresenter | NO |
| ROUGE-L (global) | Reference-based, NLP | Coherence and fluency of generated content at the global level | EvoPresent | NO |
| Perplexity (global) | Reference-free, linguistic | Text fluency at the global level | EvoPresent | NO |
| Layout Balance & Aesthetic (global) | Reference-free, design | Visual balance and overall aesthetic, scored 1–10 | EvoPresent | PARTIAL — `layout_quality` covers balance, not a 1–10 aesthetic score |
| Fidelity, Clarity, Narrative, Engagement (fine-grained content) | Reference-free, LLM/human | Content faithfulness, readability, story flow, audience engagement, scored 1–5 | EvoPresent | PARTIAL — fidelity via `groundedness`, clarity via `structure_clarity`, narrative via `content_narrative`; engagement is not scored |
| Elements, Layout, Hierarchy, Color (fine-grained design) | Reference-free, LLM/human | Fine-grained design quality across visual dimensions, scored 1–5 | EvoPresent | PARTIAL — `layout_quality` covers layout/hierarchy; elements and colour are not scored (see Outstanding TODOs) |
| Aesthetic Awareness | Reference-free, human aligned | Scoring, defect adjustment, and comparison | EvoPresent | NO |
| Text Similarity | Reference-free (only source) | Alignment between the reference text and the text on the generated slides (custom) | PreGenie | NO |
| Text Image Relevance | Reference-free | Measures the alignment between each image and its corresponding page text | PreGenie | NO — `figure_relevance` was removed; no image-relevance objective ships today |
| Overall Score | Reference-free | Success rate and figure order | PreGenie | PARTIAL — `functional_success` covers success rate; figure order is not scored |

## Evaluation Strategy

Evaluating presentation generation is inherently challenging due to the open-ended nature of the task: a well-designed deck may look very different from any given reference, making reference-based metrics difficult to apply.

### Constraints

Given the following constraints extracted from the appscibench boundaries:

- No custom model training and no deployments of custom models like PresAesth or CLIP
- No access to slide-by-slide reference comparisons

**Approach**: We restrict our evaluation to reference-free, training-free metrics that operate at the
deck level. The 11 shipped objectives (Evaluation Metrics, above) map onto the four core subtasks of
presentation generation as follows.

### Metrics by Subtask

#### Summarization

- `content_coverage` — LLM judge on what proportion of the source material's facts/figures the deck
  covers.
- `groundedness` — LLM judge for fabrication and arithmetic errors against source files.
- `structure_clarity` / `content_narrative` — LLM judges for deck-level fluency, clarity, and
  narrative coherence.

#### Figure Extraction

Not covered. No reference-free, training-free metric for figure relevance/placement shipped;
`figure_relevance` (a PreGenie Text-Image-Relevance proxy) was tried and removed — see Outstanding
TODOs.

#### Visual Layout

- `layout_quality` — VLM judge for element placement, overlap, alignment, balance, and hierarchy.
- `template_adherence` — VLM judge for whether the output adopted the template's palette/fonts/layout.
- Colour contrast as a standalone signal, and EvoPresent's fine-grained "Elements" dimension, are not
  covered — see Outstanding TODOs.

#### Outlining

- `structure_clarity` / `content_narrative` — LLM judges for section ordering and narrative arc,
  analogous to PPTEVAL's Coherence dimension (PPTEVAL Coherence itself was evaluated and not adopted;
  see "Not adopted" above).
- `constraint_satisfaction` — rule-based check of slide count only (not language or aspect ratio; see
  Outstanding TODOs).

### Discarded Metrics

Several metrics present in the literature were discarded as incompatible with the project constraints:

- **Reference-based metrics**: Require ground-truth reference slides and are therefore unsuitable for open-ended deck generation
- **Perplexity**: Adds implementation overhead for marginal gain over an LLM-judge, which subsumes its linguistic quality signal
- **Aesthetic Scoring, Defect Adjustment, and Pairwise Comparison**: Require human annotations which fall outside the project's constraints
- **Diversity (Vendi Score)**: Requires a custom model (DINOv2 features), outside appsci-bench's no-custom-model constraint

---

# Classification of Architectures in Recent Papers

| | Input | Template | Intermediate Artifact | Evaluation | Generated Format |
|---|-------|----------|----------------------|------------|-----------------|
| [AutoPresent] | Agentic (self-reflection); also fine-tuned agentic SLM | Instruction-based | No template | Code (crafted high-level library on top of python-pptx) | LLM as a judge | pptx |
| [EvoPresent] | Agentic (self-reflection via RL checker agent) | From paper | No template | Manuscript | LLM as a judge + general metrics | html |
| [DeepPresenter] | Agentic (self-reflection) + 9B model | Instruction-based | No template | Manuscript | Constraint satisfaction, Diversity, and LLM judge | html |
| [AutoSlide] | Agentic conversation | — | Input: Beamer theme (only one) | Comprehensive JSON plan containing sequenced narrative, bullet points, and visual aids | Learning-specific. Calibrated LLM as a judge | Latex code / pdf |
| [PreGenie] | Agentic via self-reflection MMLM | Document or webpage | No template | 2 MDs | SliDev code | Text similarity, text image relevance, overall score (success rate + figure) | html |
| [PPTAgent] | Agentic via self-reflection MMLM | Document | Reference slide | Outline | PPTEval | HTML |

---

# Public Datasets

| Dataset | Source | Description |
|---------|--------|-------------|
| Unnamed dataset | PPT2DOC | 5,873 paired scientific documents and presentation slide decks, totaling ~70K pages and ~100K slides |
| SciDuet | MultiStageE2E | 100 research papers from ICML and NeurIPS conferences paired with their corresponding slide decks |
| Zenodo10K | PPTAgent | 10,448 presentations sourced from Zenodo across diverse domains under clear licenses. For evaluation, 50 presentations and 50 documents were sampled across five domains |
| SlidesBench | AutoPresent | First benchmark for slide generation, comprising 7K training and 585 testing examples derived from 310 slide decks across 10 domains |
| EvoPresent Benchmark | EvoPresent | Two-part benchmark: Presentation Generation Quality built on 650 top-tier AI conference papers with multimodal resources (slides, videos, scripts); and Aesthetic Awareness consisting of 2,000 slide pairs with varying aesthetic levels for scoring, defect adjustment, and comparison tasks |

---

# Budget

| Resource | Build Limit | Run Limit |
|----------|-------------|-----------|
| Turns | 25 | — |
| Cost | $10 | — |
| Wall clock | 3,600 s | — |

This is the starting point.

**Note from product requirements**: Cost should be inferior to the price of SlideSpeak.

**Available quota per semester**: 139,880 slides from 9 Apr 2026 to 15 Sept 2026 (Price is $4k if all quota is used, the cost is around ~0.03 $/slide).

**Usage from 9 Apr 2026 to 28 Apr 2026**: 11.5k slides.

---

# How Is This Tested

Tests live in `tests/tasks/slides_generator/test_eval.py`: unit tests for every eval function in
`tasks/slides_generator/eval.py` — the 3 deterministic checks (inline mock conversations), the 4
transcript LLM-judge objectives, the 2 file-content judges and the 2 vision judges (`complete()`
mocked at the LLM import site, `render_pptx_to_pdf` patched), plus guards on the architectural
decisions: only `expectations_satisfaction` may read `eval_rubric`, no rubric band may overlap, no
shipped sample rubric may name a colour or font, and every sample must declare expectations.
Not-applicable cases assert `score is None` with an explanatory reason. Offline — no API, no Docker,
no LibreOffice.

Run with:
```bash
python -m pytest tests/tasks/slides_generator/ -v
```

## Outstanding TODOs

### Accepted limitations of the metric set (11 objectives)

These are deliberate scope decisions from the metrics wrap-up, not oversights. Each names what a
follow-up would have to build.

**Visual evaluation is two VLM judges, and that leaves real gaps.**
`template_adherence` (did the output adopt the template's palette/fonts/layout) and `layout_quality`
(bounds and overlap, alignment and margins, balance, reading order, hierarchy) are the only visual
objectives. Not covered:

- **Colour contrast as a standalone quality.** `template_adherence` catches a *wrong* palette but not
  a consistently-applied low-contrast one.
- **Font-family matching is VLM-eyeballing, not a reliable signal.** `template_adherence`'s rubric
  asks the judge to compare font choices between REFERENCE and ACTUAL from a rendered PDF; a vision
  model can distinguish coarse traits (serif vs. sans, weight) but cannot reliably identify or match
  specific font families the way it can match colors. Scores on that sub-criterion should be read as
  noisier than the palette/layout ones.
- **EvoPresent's "Elements" dimension** — asset quality, resolution, cropping.
- **Per-slide visual scoring.** Both judges score one 4-slide PDF as a whole, so a single broken
  slide averages away. This is likely the most consequential gap: fuller visual evaluation needs
  per-slide scoring plus a dedicated contrast metric.

**All rendered judges see only the first 4 slides** (`_first_n_slides(..., n=4)`). Sample 03's
proof/pricing/ask slides (#5–10) and sample 04's limitations slide are therefore never visually
graded. Raising the cap costs render time and payload size, but the cost is smaller than it may
look: a 4-slide render measures ~2–3s per deck on a warm host (measured against
`eval_data/*/template.pptx` and `gold.pptx`), so `template_adherence`'s two renders plus a VLM call
sit well inside the 60s per-objective eval timeout. The timeout is **not** the binding constraint on
raising the cap; per-slide scoring (above) is the more valuable change.

**Design/layout/template checks are VLM-only, though a deterministic check is available.** The
template's theme XML exposes the exact brand palette (`accent1=179DAB`, plus `1F6FB5`/`47B76E`/
`D3A237`) and font (`Lato`) that every EXPECTATIONS.md grades against — verified directly against
`eval_data/*/template.pptx`. python-pptx also exposes every shape's bounding box in EMU, so
overlap/off-slide/margin checks could be computed geometrically rather than judged from pixels (none
of the papers we cite does this; AutoPresent's `layout` metric is itself VLM-scored). Deliberately
deferred: it would be strictly more reliable and reproducible, and would survive a host with no
LibreOffice, but it requires fine-grained pptx-to-json structure.

**`constraint_satisfaction` checks only slide count**, though the DeepPresenter Constraint Score it
cites also covers language and aspect ratio — and `aspectRatio` is right there in the output JSON.

---

# Ownership

**P5 (Valencia)** — owner: P5 in spec.yaml. Co-owner: a contributor (enforces the no-self-baseline rule).

---

# References

- [Doc2PPT] DOC2PPT: Automatic Presentation Slides Generation from Scientific Documents
- [MultiStageE2E] Enhancing Presentation Slide Generation by LLMs with a Multi-Staged End-to-End Approach
- [AutoPresent] AUTOPRESENT: Designing Structured Visuals from Scratch
- [DeepPresenter] DEEPPRESENTER: Environment-Grounded Reflection for Agentic Presentation Generation
- [PPTAgent] PPTAGENT: Generating and Evaluating Presentations Beyond Text-to-Slides
- [EvoPresent] Presenting a Paper is an Art: SELF-IMPROVEMENT AESTHETIC AGENTS FOR ACADEMIC PRESENTATIONS
- [PreGenie] PreGenie: An Agentic Framework for High-quality Visual Presentation Generation
