# Expectations — Example 5: Project kickoff — sparse input → ask, then build with placeholders (COMPANY_NAME_REDACTED)

> **Eval-wiring note:** `clarifying-questions.md` and `clarification-answers.md` (below) describe the
> authored scenario, but this task has no live clarification round-trip at eval time — the agent
> only ever sees one static input. So the Q&A they describe is delivered as a single attached file,
> `clarification_qa.md`, merging both into one document that the agent reads like any other source
> file (and that `content_coverage`/`groundedness` grade against, same as any source file). The
> "ask" half of the expand-vs-ask loop is therefore not exercised or scored by this sample — nothing
> currently checks whether the agent asks anything before building. What's still graded is whether
> the deck correctly incorporates the *answered* items and placeholders the *explicitly deferred*
> ones rather than inventing them, per the crux rubric below.

## Scenario
A handful of scattered notes for a Monday alignment meeting on an internal COMPANY_NAME_REDACTED engineering effort — no attachments beyond the template. This is the deliberately under-specified sample, and it tests the **full expand-vs-ask loop**: the agent should ask a few targeted clarifying questions, then build a deck that fills what it has learned and uses **explicit placeholders for what it still doesn't know** — inventing nothing.

## The interaction this models
1. **User pastes sparse notes** (`PROMPT.md`).
2. **Agent asks clarifying questions** (`clarifying-questions.md`) — targeted at the material gaps; does not invent answers, and does not ask back things already provided.
3. **User answers some, defers others** (`clarification-answers.md`).
4. **Agent generates a ~6-slide deck** that fills the now-known facts and renders **blank fillers / placeholders** for the still-missing ones.

## What good output looks like

### The clarifying-questions step
- ~5–7 questions, each tied to a real gap (timeline, owners, Finance scope, success metrics, GPU cost, migration approach, decision needed).
- Does **not** ask about facts already in the notes (the three pipelines are named; the goals are stated).
- Concise — a short list, not an interrogation.

### The generated deck — fill what's known
~6 slides on the COMPANY_NAME_REDACTED template: **Why now → Goals → Scope → Owners → Risks → Next steps.**
- **Why now / Goals / Scope:** expanded from the notes + answers (three pipelines; phased migration, batch first and partner last; backfill sequenced after cutover; Finance out of kickoff scope).
- **Owners:** filled from the answers (NAME_REDACTED — Speech/ML, NAME_REDACTED — Platform, NAME_REDACTED — Analytics).
- **Risks:** seeded from the two stated worries (migration risk; GPU inference cost).

### The generated deck — placeholders for what's still unknown (the crux)
The following must appear as **explicit placeholders / blank fillers**, clearly marked (e.g., `[Target dates — TBD]`), never invented:
- **Timeline / milestone dates** — no firm dates were given.
- **Success-metric targets** — directional goals only; no numbers set.
- **GPU inference-cost figure** — explicitly not yet known.

### Visual fidelity (from the COMPANY_NAME_REDACTED template)
- Colors: Data Stream teal `#179DAB`, dark gray `#4C4E56`, white. Fonts: Lato.
- Footer carries the confidential marker + project context — e.g. `© COMPANY_NAME_REDACTED 2026 · Confidential — Project Keystone Kickoff` (illustrative — grade the tokens, not exact wording or position)
- Placeholders styled so they read as intentional "to-fill" fields (e.g., muted / bracketed), not as finished content.

## What this example uniquely tests
- Knowing when to **ask** vs **expand** — the core anti-hallucination behavior.
- **Incorporating** provided answers correctly (not ignoring them).
- Using **placeholders** for missing facts instead of fabricating them.

## Failure modes to catch
- Inventing dates, targets, a cost figure, or owners instead of asking / using placeholders.
- Not asking at all — producing a confident, fully-populated deck with made-up specifics.
- Asking back things already provided (the pipeline names, the goals) — over-asking.
- Ignoring the answers that were given and leaving everything blank.
- Placeholders that are invisible or look like real content (e.g., a date like "Q3 FY26" presented as fact when it was never provided).

## Visual scope
Mostly text. A simple **phases box row** (e.g., Batch → Real-time → Partner, or Now → Migrate → Cutover as plain rectangles) is an acceptable light accent **only if** it carries no invented dates. Placeholders are bracketed text fields. No diagrams, no charts.

## Grades against
**Groundedness + the ask / placeholder judgment (weighted highest here)** · Structure & clarity · Coverage · Template adherence
