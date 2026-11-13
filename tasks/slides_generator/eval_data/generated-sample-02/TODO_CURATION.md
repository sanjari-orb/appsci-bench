# Curation TODOs — generated-sample-02

## (a) `gold.pptx` binary does not exist yet
`sample.json`'s `expected_output.files` references `gold.pptx`, but only the JSON content
(`expected_output.json`) was authored here — no binary `.pptx` was built. Someone needs to render
`expected_output.json`'s presentation into an actual `.pptx` on top of `template.pptx` (matching its
real layouts, extracted below) before this sample can be used with `template_adherence` /
`layout_quality` (the two vision judges render this file; without it they return `None`, not a
score). `functional_success`, `output_format_compliance`, and `constraint_satisfaction` also read
the *produced* deck at eval time, not this gold file, so those are unaffected — but authoring intent
review (does the gold content actually look right once laid out) still requires the binary.

## (b) `template.pptx` structure — inspected, not just guessed
Ran the repo's own extractor (`tasks.slides_generator.pptx_json.write_presentation_json`) against
`template.pptx`, producing `template_json.json` alongside it in this directory. Findings:
- The actual `.pptx` has only **2 slides**, both lorem-ipsum placeholder content, on layouts
  `Intestazione sezione` and `Titolo e contenuto`.
- The slide master defines **11 layouts** total: `Diapositiva titolo`, `Titolo e contenuto`,
  `Intestazione sezione`, `Due contenuti`, `Confronto`, `Solo titolo`, `Vuota`,
  `Contenuto con didascalia`, `Immagine con didascalia`, `Titolo e testo verticale`,
  `1_Layout personalizzato`. `sample.json`'s gold JSON uses six of these
  (`Diapositiva titolo`, `Titolo e contenuto` x2, `Confronto`, `Due contenuti`, `Solo titolo`) — all
  real layout names, none invented.
- Font is Calibri Light throughout; the only non-default color observed on styled text is
  `#44546A` (dark blue-gray); otherwise a stock Office theme palette. This is a plain,
  Italian-authored default PowerPoint template (culture_ID Combo_16), not a designed corporate
  brand template — flagged in EXPECTATIONS.md's Visual fidelity section so whoever grades
  `template_adherence` doesn't expect a distinctive brand look that isn't there.
- `template_json.json` (the extractor's raw output) is left in this directory as a curation
  artifact; delete it if it should not ship alongside the sample, or keep it as a reference for
  whoever builds `gold.pptx`.

## (c) Recheck of my own spreadsheet extraction — IMPORTANT, changes the headline number
`GENERATION_SPEC.md`'s row for this sample states the spreadsheet has **383** real response rows.
My own extraction (via openpyxl, `Sheet0`) found:
- Header row is row **5** (not row 4 — I made an off-by-one error on a first pass and initially
  miscounted as 383; rechecked and corrected).
- Data rows run **6–387** (382 rows), all non-empty.
- Rows 388–389 are trailing blank rows.
- **No row in 6–387 is itself a stray duplicate header** (I checked this specifically — an earlier,
  incorrect row-6 anchor had picked up a duplicated header row, which is what produced the
  intermediate "383 nonempty" miscount before I found the correct header boundary).

**Net: I get 382, not the spec's 383.** This is a one-row discrepancy between my extraction and the
generation spec, not between the memo and the spreadsheet (that conflict — 500 vs. the true
row-count — is the intended test and is unaffected either way: 382 vs. 383 doesn't change that the
memo's 500 is wrong). I used **382** throughout `sample.json`/`EXPECTATIONS.md` because it's what I
verified directly. Whoever curates `gold.pptx` should re-run the extraction independently
(`openpyxl`, `Sheet0`, header at row 5, data rows 6–387) and confirm 382 vs. 383 before finalizing —
if 383 turns out correct, every occurrence of "382" in `EXPECTATIONS.md`, `sample.json`'s gold JSON,
and `sample.json`'s `eval_rubric` needs a one-number find/replace.

All other breakdown figures (language, affiliation, region, bioenergy-interest counts) were computed
from the same 382-row extraction and should be re-verified together with the row-count fix if it
changes.

## (d) Is the 500-vs-382(3) conflict a fair test, or too on-the-nose?
Judgment call: **fair, not too obvious**, for these reasons:
- The 500 figure sits inside a dense methodology paragraph (Activity Description) alongside many
  other true facts (channels, target audience) — it isn't flagged or set apart as suspicious in the
  memo's own text. An agent has to actually cross-check it against the spreadsheet rather than
  noticing it "looks wrong" on its own.
- The spreadsheet gives no shortcut count (no cell literally labeled "Total responses") — the agent
  has to count non-empty response rows itself, which requires actually processing the data rather
  than skimming for a summary statistic.
- Counter-argument for "too on-the-nose": once an agent decides to open the spreadsheet at all (which
  the prompt all but requires, since it asks what the responses show), a competent agent counts rows
  as a near-automatic first step, and 382 vs. 500 is a large, obvious gap (24% off) rather than a
  subtle rounding difference — so an agent that does look will not miss it. The interesting failure
  mode is an agent that trusts the memo's stated figure without ever deriving a count from the data
  at all, which this sample should catch reliably.
- On balance this matches the spec's own framing ("plausible misreport of a real, checkable
  number") — keeping it as authored.

## (e) 2026-09-13 — memo.txt replaced with a research-introduction docx; full real-name scrub; claim-substantiation framing added

Three changes made in this pass, at the repo owner's request:

1. **`memo.txt` → `GENERATED_PROJECT_NAME_Research_Introduction.docx`.** The old memo was a bare
   dataset-repository blurb (real public dataset description), too sparse to read as an actual
   research document and too thin a source to build a good presentation from. Replaced with a
   ~500-700 word docx (built via `build_docx.py`, kept alongside the sample as a record of how it
   was generated — not part of the repo's `curation_scripts/` pipeline, which only goes
   pptx→json/markdown/pdf, not docx authoring) with proper background/purpose, methodology, survey
   reach, and a stated finding sections. The 500-vs-382 response-count conflict is preserved
   (relocated into the new "Survey Reach" section, reframed as "the pilot teams' own tracking put
   the total... at approximately 500" — still unhedged/uncaveated in-document, so the test remains
   genuinely catchable rather than telegraphed).

2. **Full real-name scrub — scoped to the newly-authored synthetic content only.** The original memo
   carried real-world identifiers: project name "BECoop", author "Isabel Fróes" (with ORCID),
   funding programme "Horizon 2020", grant agreement number "952930". These belonged to a document
   we were replacing wholesale with new, authored prose, so the new docx uses synthetic placeholders
   instead: `GENERATED_PROJECT_NAME`, `GENERATED_FUNDING_PROGRAMME`, `GENERATED_GRANT_NUMBER`,
   `GENERATED_COOPERATIVE_TYPE` (this last one replacing "RESCoop" in the docx's own prose, since the
   docx is new text we're authoring, not the real dataset). No author byline was retained.
   References to the docx's filename/content in `PROMPT.md`, `sample.json`, `EXPECTATIONS.md`, and
   this sample's row in `GENERATION_SPEC.md` were updated to match.
   - **The spreadsheet itself is NOT touched by this scrub, and keeps its original filename**
     (`BECoop_Potential for the Bioenergy Market Expansion.xlsx`) **and internal `Alias` cell
     content.** This is real survey response data (not authored/synthetic content) — the "BECoop"
     name, the "RESCoop"/"Renewable Energy Source Cooperatives" affiliation label in column C, and
     every country/language/answer value are all part of the actual dataset being graded. Only
     newly-authored prose (the docx replacing the old memo) gets synthetic placeholders; the real
     spreadsheet — filename, Alias cell, and all 382 data rows — is retained exactly as it was.
     (I briefly renamed the xlsx and edited its Alias cell during this pass, then reverted both
     after confirming with the user that only synthetic content should carry `GENERATED_*` naming —
     re-verified afterward via openpyxl that the revert didn't disturb the 382 real data rows in
     `Sheet0` rows 6-387.)
   - `template.pptx` / `template_json.json` were checked and contain no real-world identifying names
     — left unchanged.

3. **Claim-substantiation framing added.** The new docx states a specific, checkable finding: that
   cooperative members are highly receptive to bioenergy. This is grounded in real data I derived
   from the spreadsheet: the bioenergy-interest question (`Sheet0` column H, "Would you be
   interested in including bioenergy in your RE Cooperative?") is **question-routed, not answered by
   everyone** — only the 139 cooperative-member respondents were asked it; all 243
   policymaker/authority rows are blank there. Of the 97 cooperative members who answered: Yes 38,
   Maybe 30, already-use-bioenergy 24, No 5 — **92/97 ≈ 95%** positive. The docx states this
   qualitatively (no exact numbers), so the deck's job is to derive and report the real breakdown,
   scoped correctly to the 97 who answered (not all 382), and connect it back to the stated claim.
   Updated `sample.json`'s gold JSON (slide 1 cover, slide 5 bioenergy-interest slide, slide 6
   summary) and `eval_rubric` (new item on claim-substantiation; corrected the interest-breakdown
   item to require correct denominator scoping) and `EXPECTATIONS.md` accordingly. This also
   corrects a minor pre-existing inaccuracy in the original sample, which reported "not answered:
   285" against all 382 respondents without noting the question was never asked of most of them.

4. **Unrelated pre-existing bug fixed while here**: `GENERATION_SPEC.md` stated "383" real response
   rows for this sample in two places; the correct, independently-re-verified figure is **382**
   (see (c) above) — fixed both occurrences.

**Still outstanding, unaffected by this pass**: `gold.pptx` binary still does not exist (see (a)) —
now also needs to reflect the updated cover/slide-5/slide-6 content and the renamed source files
once someone builds it. No json→pptx renderer exists in this repo yet (same gap shared with samples
`generated-sample-03` and `generated-sample-04`); building one is out of scope for this pass.

