# Expectations — Template-outranks-prior-deck's-own-branding (SECreTour / Campina de Faro)

## Scenario
A research team asks the agent to re-style their existing SECreTour/Campina de Faro deck
(`prior-deck.pptx`, 9 slides) into their new template (`template.pptx`). This is explicitly a
**pure re-template**, not a content update: same slides, same content, same order — only the
visual styling changes. The catch: `prior-deck.pptx` is itself visibly built on a *different*
branded look than `template.pptx` — a plausible real-world situation (the team switched templates
since the last deck was built). The user never says "ignore the prior deck's styling" — the agent
must infer this itself from the standing rule that `template.pptx` is *always* the one to style
from, never whatever a prior attachment happens to look like. This is the **primary
template-adherence test** in this batch.

## What good output looks like

### Visual fidelity (from `template.pptx`, NOT from `prior-deck.pptx`)
- Colors: dark navy `#00102C`, white `#FFFFFF`, blue-gray `#44546A`, light gray `#E7E6E6`, accent blue
  `#3092D0`, accent teal `#6CCCD3`, accent magenta `#AE538E`, neutral grays `#555A5C`/`#737474` — the
  template's actual scheme, applied consistently across every slide shown.
- Fonts: Arial throughout (the template's major/minor font), not whatever face `prior-deck.pptx` used.
- Layout conventions: title placement, header-block structure, and spacing follow `template.pptx`'s own
  layouts, not the prior deck's layout geometry.
- None of `prior-deck.pptx`'s own colors, fonts, or decorative styling should appear anywhere in the
  output — that deck is a content/structure reference only.

### Content and structure carried forward exactly (from `prior-deck.pptx`)
- Same number of slides (9), same slide order, same content on each slide. This is a re-style, not
  a rewrite: no slides added, removed, merged, or reordered; no content rewritten, summarized, or
  updated with new material.
- Text content should be substantively the same as the prior deck's — the agent may reflow text to
  fit the new template's layouts, but should not paraphrase away specific figures, names, or claims.

### What should NOT appear
- No slide should present `template.pptx` itself as a subject of the deck's content — it is a style
  source only.
- No content changes, additions, or updates relative to `prior-deck.pptx` — this sample does not test
  groundedness against any new source; there is no new source material in scope.

## What this example uniquely tests
- **Template-vs-attachment disambiguation with zero content-update pressure**: a prior deck that must
  be used for structure/content carry-forward *in full* (not just "where it still makes sense") and
  must NOT be used for visual styling, despite being a real, complete-looking deck with its own
  coherent (but wrong) branding. Every visual cue in the request pulls toward "keep it looking the way
  it already does" — the agent has to resist that and apply `template.pptx`'s scheme instead, purely
  because of the `template.pptx` naming convention, never stated explicitly in the prompt.
- **Faithful reproduction under a restyle**: because there is no new content to fold in, any drift in
  slide count, order, or substance is a pure failure mode, not a defensible restructuring choice.

## Failure modes to catch
- Applying `prior-deck.pptx`'s own color palette, fonts, or layout instead of `template.pptx`'s — this
  is THE CRUX failure mode this sample exists to catch.
- Treating `prior-deck.pptx` purely as a style reference (copying its look) rather than a content/
  structure reference, inverting the two attachments' actual roles.
- Changing the slide count, order, or content — adding, dropping, merging, or rewriting slides beyond
  what re-styling requires.

## Visual scope
Whatever native shapes (bullet lists, tables, stat callouts) best fit each slide's existing content,
restyled to `template.pptx`'s layouts and color scheme. No new visual elements beyond what the prior
deck already has.

## Grades against
**Template adherence (weighted highest here, carries the styling CRUX)** · Expectations satisfaction
(content fidelity to `prior-deck.pptx` only — see `sample.json`'s `eval_rubric`, which does not
re-test styling) · Groundedness