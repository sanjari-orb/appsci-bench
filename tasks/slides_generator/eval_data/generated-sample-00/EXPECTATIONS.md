# Expectations — Example 0: SEC Filing Executive Summary (Apple DEF 14A)

## Scenario

An investor relations analyst asks an agent to build a 5-6 slide executive summary from Apple's DEF 14A proxy filing, focused on business revenue and strategic priorities while explicitly filtering out compensation and governance details. The agent must:
1. Extract business-relevant facts from a complex, multi-section SEC document
2. Distinguish between material business content and off-topic governance/compensation data ("content selection")
3. Apply the template's visual style
4. Verify every specific claim against the source (groundedness)

This is the **primary groundedness + content-selection test**: a document where the agent must separate concerns (business vs. governance) and a complex format that rewards careful sourcing.

## What Good Output Looks Like

### Slide Count & Scope
- Exactly 5-6 slides, covering business overview → financials → strategy → either risks or forward outlook
- Each slide focuses on investor-relevant business content: segments, revenue, R&D, operational metrics, market position, strategic initiatives

### Content Selection: The Crux ✓ THE CRUX

The deck must demonstrate it understood the distinction between **business content** (in-scope) and **governance/compensation content** (out-of-scope):

**Out of scope (must be absent):**
- Director or officer compensation (salary, bonus, equity, incentives)
- Board committee structures or charters
- Voting procedures, proxy mechanics, or governance policies
- Executive biographies or background (beyond role title, if necessary)
- Compliance/regulatory process details

**In scope (should be present where the filing contains it):**
- Revenue and business segments
- Geographic footprint and market presence
- Financial metrics (profitability, margin, capital allocation)
- Strategic priorities and R&D focus areas
- Operational scale (employees, stores, facilities, if disclosed)
- Investor-relevant risks (supply chain, geographic concentration, regulatory, competitive)

If the deck includes compensation data, governance procedures, or board details anywhere, the content-selection expectation is **missed** — this is the primary differentiator this sample tests.

### Groundedness

Every specific claim — a number, name, date, or strategic statement — must be individually traceable to the DEF 14A. The agent must not:
- Invent or approximate numbers (e.g., rounding "approximately $80 billion" to "$80B" is acceptable; inventing "$75B" is not)
- Infer strategic directions beyond what the filing states
- Hallucinate segment names, metric definitions, or geographies

If a specific figure does not appear in the filing, it must be absent from the deck, not estimated.

### Template Adherence

- Colors applied consistently across slides
- Font family and sizing from the template respected
- Layout and white-space conventions matched
- No visual elements contradicting the template style
- Footer (if template includes one) applied consistently

## What This Example Uniquely Tests

1. **Content selection under ambiguity**: DEF 14A is a proxy filing containing both business and governance content. The agent must read carefully enough to separate them.
2. **Groundedness in a complex, unfamiliar format**: SEC filings have dense structure. Agents often over-generalize or fabricate when unsure.
3. **Handling business data precisely**: Geographic splits, segment names, financial figures must match exactly as stated.

## Failure Modes to Catch

- **Content-selection failure**: compensation, governance procedures, board details, or voting mechanics appear anywhere (automatic miss of the CRUX).
- **Groundedness failure**: a number or claim cannot be found in the filing; appears estimated or inferred.
- **Segment/region misname**: calling a business segment or geographic region by a name not in the filing.
- **Template not applied**: visual style diverges from the template (fonts, colors, layout).
- **Wrong slide count**: fewer than 5 or more than 6 slides.
- **Missing key business data**: if the filing clearly states revenue by segment or geography, the deck omits it.

## Grades Against

**Content selection (THE CRUX)** — Does the deck show compensation, governance, or policy details? If yes, it fails.

**Groundedness** — Every specific claim traceable to the DEF 14A, with no invented figures or inferences?

**Template adherence** — Do visual elements match the template's palette, fonts, and layout conventions?
