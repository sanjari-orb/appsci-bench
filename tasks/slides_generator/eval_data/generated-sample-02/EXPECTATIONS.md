# Expectations — Example: GENERATED_PROJECT_NAME bioenergy market survey (conflicting response
# count + claim substantiation)

## Scenario
A research-introduction document (`GENERATED_PROJECT_NAME_Research_Introduction.docx`) describing a
multi-country bioenergy survey states in prose that dissemination tracking put the total at
"approximately 500" completed responses. The actual response spreadsheet
(`BECoop_Potential for the Bioenergy Market Expansion.xlsx`) contains only **382
real response rows** (rows 6–387 of `Sheet0`; the header sits on row 5, two trailing rows are blank,
and no early row is a duplicated header — all excluded from the count). The two sources disagree on
the same checkable figure. The user's request never mentions this discrepancy — the deck must catch
it unaided from the two attached files.

The same document also states a specific claim: that cooperative members are highly receptive to
adding bioenergy to their existing renewable mix. The claim is stated qualitatively, without exact
figures — the deck's job is to substantiate it (or fail to) using the real breakdown in the
spreadsheet, not just restate the claim as given.

## What good output looks like

### The crux: the discrepancy itself must be shown to the user — reporting only "382" is not enough
The prompt explicitly asks "how many people responded," so a deck that never states a response
count at all is itself a failure — omitting the number is not a safe way to avoid the discrepancy.
But getting the number right is not sufficient either: a good deck **explicitly surfaces the
conflict between the two sources** — e.g. "the research introduction states approximately 500
responses; the response data provided contains 382" — so the project team sees that the two sources
disagree, not just which number the deck happened to pick.

A deck that silently reports "382 responses" with no mention that the introduction says otherwise
has picked the right number but still failed this item — the reader has no way to know a
discrepancy exists at all. Equally failing: stating "500 responses" (or "~500") as unqualified fact,
inventing a number that reconciles the two (e.g. "approximately 440"), or never stating a response
count anywhere.

### Substantiating the claim, not just restating it
The research introduction claims cooperative-member interest in bioenergy is high. A good deck
checks this against the actual data rather than repeating the claim as given:
- The bioenergy-interest question was answered **only by the 139 cooperative-member respondents**
  (the 243 policymaker/authority respondents were not asked it — all blank on that column in the
  spreadsheet). Of the 97 cooperative members who answered: Yes 38, Maybe 30, already use bioenergy
  24, No 5 — **92/97 ≈ 95%** positive.
- A good deck reports this scoped-correctly breakdown (among the members who answered) and connects
  it back to the claim ("this supports/substantiates..."), rather than either (a) treating all 382
  respondents as the denominator and reporting a large "not answered" bucket as if it undercut the
  claim, or (b) simply repeating the introduction's qualitative claim without ever deriving a number
  from the data at all.

### Structural fidelity
6 slides for a research-briefing-style deck (per L3's `slide_count_guidance` — research briefings
run 6-8 slides; this one is on the shorter end since the source is a single dataset + one
research-introduction document, not a long report): **Cover → Survey methodology → Response count
(the discrepancy slide) → Respondent profile (countries/languages/affiliation) → Bioenergy interest
(claim substantiation) → Closing/next steps**.

### Visual fidelity (from `template.pptx`, Combo_16)
This is a plain, unbranded academic/workshop-style template (Italian-authored PowerPoint default
theme), not a designed corporate template:
- Fonts: Calibri Light for headings and body (the template's only font).
- Colors: dark blue-gray `#44546A` as the only distinguishing accent seen on styled text; otherwise
  default Office theme palette (`#5B9BD5`, `#ED7D31`, `#A5A5A5`, `#FFC000`, `#4472C4`, `#70AD47`).
  There is no bold brand color to "adopt" beyond matching this restrained, muted palette rather than
  introducing a generic default (e.g. plain black-on-white with no accent at all).
- Layouts available in the master: `Diapositiva titolo` (title slide), `Titolo e contenuto` (title
  and content), `Intestazione sezione` (section header), `Due contenuti` (two content), `Confronto`
  (comparison), `Solo titolo` (title only), `Vuota` (blank), `Contenuto con didascalia`,
  `Immagine con didascalia`, `Titolo e testo verticale`, `1_Layout personalizzato`. A good deck picks
  from these real layouts rather than inventing layout names.

### Grounded figures (every number must trace to the research introduction or the spreadsheet)
- Response count: **382** actual rows (or the discrepancy explicitly surfaced — see crux above).
- Survey languages: English plus six other languages — French, German, Greek, Italian, Polish,
  Spanish (from the research introduction).
- Actual respondent language breakdown (from the spreadsheet's `User Language` column, n=382):
  Polish 132, Greek 88, Spanish 52, English 45, German 43, Italian 17, French 5.
- Dissemination timeline: February to May 2021 (research introduction: "The dissemination phase ran
  from February to May 2021"); survey remained open until the end of the project.
- Funding: placeholder funding programme and grant number, as stated in the source documents.
- Target audience: cooperative members, authorities, and policymakers across Europe.
- Distribution channels: Social Media, email, other channels (e.g. WhatsApp); administered on a
  GDPR-compliant EU survey platform.
- Respondent affiliation breakdown (spreadsheet, n=382): Policy makers / Member of Local authority
  group / Energy Federation 243; cooperative members (affiliation label as it appears in the
  spreadsheet: "Member of Renewable Energy Source Cooperatives") 139.
- Respondent region breakdown (spreadsheet, n=382): Rural 197, Urban 152, Peri-urban 33.
- Bioenergy-interest breakdown (spreadsheet column "Would you be\ninterested in including bioenergy
  in\nyour RE Cooperative?", normalized for case/typo variants; **answered only by the 139
  cooperative-member respondents, n=97 who actually answered**): Yes 38, Maybe 30, "We already use
  bioenergy" 24, No 5. The remaining 285 respondents (243 policymakers/authorities + 42
  cooperative-members who left it blank) were not meaningfully asked this question and should not be
  reported as "no interest" or as a flat "not answered" share that implies the question applied to
  everyone.

Any deck that reports a country-count, an interest percentage, or a breakdown number NOT
recomputable from these figures is fabricating.

## What this example uniquely tests
- **Groundedness under a genuine cross-source conflict** — not omission or invention from a single
  source (tested elsewhere in this task's sample set), but choosing correctly between two sources
  that both look authoritative and disagree on the same fact.
- **Claim substantiation, not restatement** — the source document states a claim; the deck must use
  the data to check and support it (with correct scoping to who was actually asked the question),
  rather than either repeating the claim uncritically or reporting the data in a way that obscures
  whether it actually supports the claim.
- **Verification behavior**: does the agent treat a spreadsheet of raw rows as ground truth over a
  prose summary claim, or does it default to whichever source it read first / whichever number is
  more prominent (the response count appears early in the research introduction, a plausible reason
  for an agent to anchor on it).
- Maps to L3 `what_good_looks_like` (a) — "nothing you flagged as important got dropped, and it
  never pads with content you didn't provide" — the discrepancy itself is content the sources
  provide (implicitly, via contradiction); silently resolving it by picking a number is a form of
  padding with an unsupported claim. Also maps to `input_flexibility` — "agent extracts and
  synthesizes content" — since substantiating the claim requires synthesis across both sources, not
  just extraction from one.

## Failure modes to catch
- Reporting "500 responses" (or "~500") as fact anywhere in the deck (title, body text, or a KPI
  tile) without qualification.
- Reporting only "382 responses" with no mention that the research introduction says otherwise —
  the right number without the discrepancy still fails the crux; the mismatch itself must be shown.
- Never stating a response count anywhere in the deck — the prompt explicitly asks how many people
  responded, so omitting the number entirely is a failure, not a safe way to sidestep the conflict.
- Averaging or splitting the difference between 500 and 382 (e.g. "roughly 440 responses").
- Rounding 382 to "~400" or "nearly 400" when the exact figure is available and checkable.
- Fabricating a reconciliation explanation not present in either source (e.g. claiming some
  responses were later discarded, when neither file says this).
- Citing a language, country, or affiliation breakdown number not traceable to the spreadsheet.
- Treating the bioenergy-interest question as if it applied to all 382 respondents (e.g. reporting a
  low "interest rate" because most of the 382 didn't answer) rather than correctly scoping to the 97
  cooperative members who were actually asked and answered.
- Restating the research introduction's claim without ever checking it against the data, or
  reporting the data without connecting it back to the claim.
- Dropping the funding/grant reference or the dissemination timeline entirely (available, material
  context for a research briefing) — though these are lower-stakes than the response-count crux.
- Treating `template.pptx` as a content source (it is Combo_16's academic-workshop deck; nothing
  from its lorem-ipsum placeholder content belongs in the output).

## Visual scope
Native tables or a simple breakdown list for the language/affiliation/region/interest figures. No
charts required — a grounded table is sufficient and safer than a fabricated chart axis. A
discrepancy callout (a highlighted text box or a two-column "introduction says / data shows"
comparison) is a welcome way to surface the conflict, not a required element — the crux is being
caught and stated correctly, not any particular visual treatment.

## Grades against
**Groundedness (weighted highest here, 2.0)** · Content coverage (1.0) · Expectations satisfaction
(reads this file's eval_rubric) · Structure & clarity

## Note on synthetic content
The project name, author, and funding programme/grant number in this sample's source documents
(`GENERATED_PROJECT_NAME`, `GENERATED_FUNDING_PROGRAMME`, `GENERATED_GRANT_NUMBER`) are synthetic
placeholders authored for this benchmark sample and do not refer to a real project, person, or
grant. The underlying survey response data (countries, languages, affiliations, answers) is retained
unchanged from the original real-world dataset this sample was adapted from.
