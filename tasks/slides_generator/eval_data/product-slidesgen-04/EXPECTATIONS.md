# Expectations — Example 4: Research → non-expert briefing (COMPANY_NAME_REDACTED)

## Scenario
A long COMPANY_NAME_REDACTED applied-research write-up (a detailed ~2,600-word technical report — abstract, background, objectives, full method, results, error analysis, threats-to-validity, discussion, recommendation, and three appendices) on **Automated QA accuracy** is compressed into a ~10-slide leadership briefing on the COMPANY_NAME_REDACTED template. That is a real ~5:1 compression: the agent must **select what matters and drop the rest**, not restate the whole document. The distinctive tests are **synthesis under genuine compression**, **translate-down** (expert → layperson), and **faithfulness to caveats** — the mirror image of Example 2's translate-up. All figures are synthetic.

## What good output looks like

### Structural fidelity
~10 slides, "so what" first: **Headline finding → recommendation → why we looked → what we did → results → where it struggles → what it doesn't tell us yet (limitations) → next steps.**
- One acceptable structure, **not a required title list** — grade the *so-what-first ordering* and that each beat is present (especially that limitations survive), not exact slide titles or count.

### Visual fidelity (from the COMPANY_NAME_REDACTED template)
- Colors: Data Stream teal `#179DAB`, dark gray `#4C4E56`, white; accents sparingly
- Fonts: Lato Bold/Medium headings, Lato Regular body
- Footer carries the confidential marker + briefing context — e.g. `© COMPANY_NAME_REDACTED 2026 · Confidential — Applied Research · Auto-QA Pilot` (illustrative — grade the tokens, not exact wording or position)

### Plain-language translation of the jargon
- "88% exact agreement at the item level" → "the assistant gave the same score a human did on about 88% of scorecard items"
- "Compliance recall 96%" → "it caught 96% of the compliance problems human reviewers found"
- "Coverage" → "share of calls actually reviewed" (3% by hand vs 100% automated)

### Every headline number preserved exactly
100% vs ~3% coverage; 88% item-level agreement; 93% within one point; 96% compliance recall; ~70% less analyst scoring time; 14,000 calls; 1,200-call human sample; 8 weeks; 3 teams.

### Caveats survive into the deck (the crux)
The limitations slide must keep: **English-only / single region; one org-specific scorecard; no downstream impact (CSAT/AHT/coaching) measured; human ground truth itself ~85% (a ceiling on agreement); 8-week window.** The error concentration (subjective soft-skills items ~78%; poor-audio/cross-talk; multi-intent calls) is represented. Note that in the long source the **~85% human-agreement ceiling is stated inside the method and discussion prose, not in the tidy "Limitations" list** — a faithful deck still surfaces it. The "don't convert the ~70% time saving into a dollar/headcount figure" caution from the discussion is respected.

### Drops the low-level detail (synthesis, not restatement)
A leadership deck keeps findings, plain-language meaning, and caveats — and **leaves out** the implementation weeds the long source carries: the model build/version and hyperparameters (Appendix B), ASR word-error-rate internals, Cohen's κ / confidence-interval statistics, and the per-team breakdown numbers. Their absence is correct, not a coverage gap.

### Recommendation stated as the authors framed it
Controlled expansion to two more teams **in shadow mode**, humans still in the loop — **not** "replace human QA" or "make Auto-QA the agent's official score."

## What this example uniquely tests
- High compression ratio (~2,600 words / multi-section report → ~10 slides) without dropping a finding — genuine selection, not restatement.
- Lowering reading level while keeping numbers and meaning intact.
- Resisting the "make it a success story" pull: a faithful deck stays appropriately hedged.
- Finding a caveat that is **not** in a labelled list (the ~85% ceiling lives in the method/discussion prose).

## Failure modes to catch
- Dropping or burying the limitations (presenting it as a slam-dunk).
- Missing the ~85% human-agreement ceiling because it isn't in the tidy "Limitations" section — it's woven into the methodology and discussion.
- Overstating the recommendation (e.g., "automate QA," "retire manual QA").
- Inventing a CSAT/AHT/ROI figure to fill the obvious gap — the study explicitly didn't measure downstream impact, and explicitly declines to turn the ~70% time saving into a cost/headcount number.
- Lifting low-level methodology into the exec deck (model build/version, hyperparameters, ASR WER, Cohen's κ / CIs, per-team tables) — detail a leadership audience doesn't need.
- Leaving jargon (agreement, recall, coverage) untranslated for a non-technical audience.

## Visual scope
One native "Auto-QA vs human/manual baseline" results table is natural and welcome. Optional KPI tiles for the headline deltas. **No charts**, no diagrams.

## Grades against
**Coverage + Groundedness (caveat fidelity weighted highest here)** · Structure & clarity · Template adherence
