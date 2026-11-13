# Requirements — slides_generator L3 collaborative
# Attempt: slides_generator-l3-P1-20260909-173926
# Source of truth: user simulator transcript only (`logs/transcript.md`).
# This file is the product requirements the artifact must satisfy.
# Implementation notes live in `artifact_plan.md`; code lives in the standalone `artifact/`.
# Draft for review — polish as needed. Do not invent beyond what L3 said.

## Vague ask (starting brief)
A team wants help turning requests into presentable slide decks; a stakeholder is available to talk.

L3 restated the problem as: the team spends hours (sometimes days) turning a rough brief into a presentable deck. They want a system that takes a brief and produces slides that say what they meant, in a form they’d put in front of an audience — right content, right shape, looks like their deck.

## Elicited from L3 user sim

### Business context / pain
- Manual deck-building is slow and repetitive (recurring meetings, product pitches, research briefings).
- Content often overlaps previous decks; reformatting into the corporate template costs more time.
- Goal: save those hours — first draft should be **presentable**, not a rough scaffold a human finishes.

### Dual output (hard requirement)
Every request returns **both**, together, from the **same** request (not two calls / not one later):
1. An **editable `.pptx`** — real PowerPoint someone can open and edit. Never an image of slides, never flattened text.
2. The deck content as **structured data** (JSON) that their tooling reads afterwards (diff vs last quarter, pull figures, feed other tools).

Getting only one of the two is **not usable**.

### Structured JSON shape (fixed constraint — not our free choice)
Top-level object under `presentation`:
- `title`, `theme`, `aspectRatio`
- `metadata`: `author`, `version`, `date`
- `slides` (ordered list)

Each slide:
- `slideNumber`, `layout` (which of the **template’s** layouts it uses)
- `elements` (list), optional `notes` (speaker notes)

Each element has `name` + `type`:
| type | content fields |
|---|---|
| `text` | `content` |
| `bullet_list` | `items` |
| `table` | `rows` |
| `image` | `sourceUrl`, `altText` |

Rules L3 stated:
- Field names must come back every time.
- Slide order in JSON must match slide order in the `.pptx`.
- Every slide in the deck must be present in the data.
- Each slide needs at least one element with real content (`content` / `items` / `rows` / `sourceUrl`) — otherwise treated as empty.
- Empty / unreadable presentation object = failed request (prefer a clear error).

Example format L3 shared (use as the contract shape):

```json
{
  "presentation": {
    "title": "Q2 Business Review",
    "theme": "default",
    "aspectRatio": "16:9",
    "metadata": { "author": "Jane Doe", "version": "1.0", "date": "2026-01-14" },
    "slides": [
      {
        "slideNumber": 1,
        "layout": "title_and_content",
        "elements": [
          { "type": "bullet_list", "name": "Headline", "items": ["Point one", "Point two"] },
          { "type": "text", "name": "Body", "content": "Running paragraph text." },
          { "type": "table", "name": "Metrics", "rows": [["Q1", "Q2"], ["120", "145"]] },
          { "type": "image", "name": "Chart", "sourceUrl": "chart.png", "altText": "Growth chart" }
        ],
        "notes": "Optional speaker notes."
      }
    ]
  }
}
```

**Clarification (L3 corrected us):** this JSON is the **output**, not the input. Users must not be forced to pre-structure briefs into JSON.

### Inputs
Roughest → most structured:
- **Sparse notes** — expand sensibly; **flag big gaps**; do **not** invent filler/figures.
- **Markdown (`.md`) or plain text (`.txt`) prompt**.
- **Up to 5 uploaded documents**: `docx`, `pdf`, `pptx`, `csv`, or `xlsx` — extract and synthesize.
- Always attach **`template.pptx`** for look/branding (not content to put on slides). Everything else attached is content.

Must **not** require a rigid structured brief schema; accepting messier input is the value. More structured briefs are fine when offered, but not required.

### Template / look
- Deck must **obey the attached template every time** (colors, fonts, overall feel) — not a generic deck with a logo slapped on.
- Lock visual theme from the template first, then fit content into it.
- Parameters they care about for look: mainly **slide count** + **which template**; theme comes from the template.

### Audiences
- Execs, clients, board, internal teams — different depth/tone.
- A board deck shouldn’t read like a status update.
- Whether audience is an explicit param vs inferred from the brief = **our design call**; often implied by the brief. System must get tone/depth right somehow.

### Slide length defaults (when user doesn’t set a count)
User can set an explicit count; otherwise sensible defaults by deck type:
- Recurring QBRs / status reviews: **~8**
- Product pitches: **8–12**
- Research briefings: **6–8**
- Sparse status updates: **3–5**

No rigid must-have slide list, but flow: beginning (why it matters) → middle (makes the case) → end (lands the point).

### What “good” / presentable means
- Covers what was asked; doesn’t drop flagged-important content; doesn’t pad with content they didn’t provide.
- Someone unfamiliar with the topic can follow the story; every slide earns its place.
- Looks like the team’s letterhead (template obedience).
- Clean layout: nothing overlapping, spilling, crammed, or oddly spaced.
- Pitched for the audience in the room.
- Flows as a narrative, not just ordered slides.

Human review is a **safety net** (gaps, judgment calls, tweaks) — not assembly work. If people still spend hours fixing, we haven’t solved the problem.

### Hard nos / failures (must not tolerate)
- **Silent attachment failure** — if an attach can’t be read, tell the human clearly; never quietly build as if it weren’t there.
- **Inventing content** when input is sparse.
- **Dropping** something they flagged as important.
- **Ignoring the template**.
- Returning **only** `.pptx` or **only** JSON.
- Unopenable / non-editable `.pptx`, or empty/unreadable structured data — prefer a **clear error**.

Error categorization / exact flagging UX = our design call; L3 only requires those failures not be silent.

### Scope / priorities (v1)
- **MVP focus: business decks** (QBRs, pitches, research briefings, status updates). Other deck types not the priority.
- **#1 thing to get right:** content fidelity — says what was asked; doesn’t invent; doesn’t miss the point. Looks can be polished a bit by hand if needed; wrong content cannot.
- **Don’t optimize yet:** speed/latency/efficiency at the expense of output richness/quality.

### Fidelity vs synthesis
System must judge when to mirror source structure vs reframe for the audience (board deck ≠ raw source doc). Real judgment call, not just formatting.

### Human-in-the-loop
No formal HITL process from L3. Practically: human brings brief + attachments in; human reviews before an audience. Gaps/attachment errors surface there.

### Environment resources L3 pointed at
- Sample requests: **`/data/samples/`** — synthetic examples (prompt + attachments + `template.pptx`). L3 hasn’t counted them; open and look.
- Also mentioned success framing lives in a way that points agents at **`/sop/kpis.md`** in the resource list (in sim setup); check the env rather than inventing KPIs beyond what L3 stated.

### Deferred / our design decisions (L3 would not specify)
- Exact error taxonomy / labels.
- Formal HITL workflow.
- Whether audience is an explicit parameter.
- How we surface attachment failures (as long as not silent).
- API / transport packaging beyond: both outputs together + that JSON shape + editable pptx.

## Open questions (for us / optional follow-up elicit)
- Exact HTTP `/respond` packaging for returning `.pptx` + JSON together (L3 cares about both existing; transport is ours).
- How layouts are named beyond the example `title_and_content` — inspect `template.pptx` / samples.
- Whether to schedule a short “draft requirements review” with L3 (they offered).

## Notes for artifact build
- Implement a queryable service: `GET /health`, `POST /respond`.
- Input path: brief + attachments + `template.pptx` → synthesize deck.
- Output path: editable `.pptx` **and** the JSON shape above, same turn; orders must match.
- Prefer failing loudly on unread attachments / empty decks over silent wrong decks.
- Use `/data/samples/` to ground behavior once env is up (after Git LFS pull).

## Transcript
- `logs/transcript.md` (session ~2026-09-09T15:42:58Z → 16:21:25Z)
