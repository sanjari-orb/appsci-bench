# Expectations — Example 1: Recurring QBR (COMPANY_NAME_REDACTED)

## Scenario
A COMPANY_NAME_REDACTED CSM builds the Q2 QBR for **CUSTOMER_NAME_REDACTED** by handing the agent: the previous quarter's deck, the COMPANY_NAME_REDACTED branded template, a sparse set of update notes, and a metrics dump. The agent must carry forward the prior deck's structure, apply the COMPANY_NAME_REDACTED template's visual rules, and update all content for Q2. This is the workhorse case and the **primary template-adherence + groundedness test**.

## What good output looks like

### Structural fidelity (from previous deck)
Identical 8-slide skeleton: Title → Agenda → Executive Summary → Adoption & Usage → Value Delivered → Support & Health → Roadmap → Asks & Next Steps. Same section names, same table formats, same flow.

### Visual fidelity (from COMPANY_NAME_REDACTED template)
- Colors: Data Stream teal `#179DAB`, Neutrality dark gray `#4C4E56`, Clarity white `#FFFFFF`; secondary palette as accents
- Fonts: Lato Bold/Medium for headings, Lato Regular for body
- Cover slide: dark navy/teal background with COMPANY_NAME_REDACTED logo prominent
- Content slides: white or near-white background, `#4C4E56` body text
- Footer: `Confidential — CUSTOMER_NAME_REDACTED QBR · Q2 FY26 · COMPANY_NAME_REDACTED` bottom-left; slide number bottom-right
- Tables: COMPANY_NAME_REDACTED-styled header row; alternating body rows
- Health chip: RAG pill rendered as rounded rectangle (green this quarter)

### Numbers updated from the dump, with deltas surfaced
| Metric | Q1 FY26 | Q2 FY26 | Delta |
|---|---|---|---|
| Monthly active agents | 1,240 | **1,510** | +22% |
| Licenses activated | 1,180 / 1,500 (79%) | **1,395 / 1,500 (93%)** | +14pp |
| Conversation Intelligence adoption | 68% | **74%** | +6pp |
| Auto-QA adoption | 41% | **58%** | +17pp |
| Coaching & Insights adoption | 12% | **19%** | +7pp |
| CSAT | 4.3 | **4.6** | +0.3 |
| Support tickets | 47 | **31** | -34% |
| P1 incidents | 2 | **1** | — |
| SLA attainment | 98.2% | **99.1%** | +0.9pp |
| Health | Amber | **Green** | ✓ |

### Every update note lands somewhere
- **SSO shipped** → under **Value Delivered** (was a Q1 open ask, now closed)
- **May P1 API latency escalation** → under **Support & Health**, honest + brief, RCA noted
- **Exec sponsor change** (NAME_REDACTED → **NAME_REDACTED, VP Contact Center Operations**) → Title slide / intro
- **EU expansion (~500 licenses)** → **Asks & Next Steps**, framed as a conversation to have, not a committed deal
- **Q3 renewal** → called out in Asks & Next Steps

### Roadmap rolls forward
Q1's "ship SSO" moves from planned to done. New Q2/Q3 roadmap items populate from update notes.

## What this example uniquely tests
- Reproducing *structure* from a prior deck while applying a *separate* COMPANY_NAME_REDACTED brand template for visuals — two sources of fidelity at once.
- Carry-forward + diff: merging a prior deck, a metrics table, and free-text notes into one consistent result; computing deltas correctly.
- Tone discipline on bad news (the P1 escalation): present, acknowledged with RCA, not buried and not over-dramatized.
- Keeping an expansion conversation framed correctly — a "discussion to have," not a closed deal.

## Failure modes to catch
- Applying the prior deck's Helio visual style instead of the COMPANY_NAME_REDACTED template (wrong colors/fonts).
- Mis-computed or invented deltas; pulling a number from neither the dump nor notes.
- Dropping an update (sponsor change, escalation, expansion ask — all three are easy to miss).
- Turning the EU expansion into a committed deal.
- Footer not updated from Q1 FY26 → Q2 FY26.

## Visual scope
Native tables for Adoption & Usage and Support & Health. Health rendered as a RAG **pill** (rounded rectangle). Delta arrows on metrics are a fine accent. No diagrams or charts — if content wants a chart, a grounded table is the correct fallback.

## Grades against
Coverage · Structure & clarity · Groundedness · **Template adherence (weighted highest here)**
