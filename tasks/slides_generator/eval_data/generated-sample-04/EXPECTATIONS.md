# Expectations — Two agreeing documents merged into one review deck

## Scenario
The user attaches two real, mutually-consistent documents about the same event — Alphabet's Q2 2026
earnings release (`google.pdf`, a dense 12-page SEC-style press release with prose, a CEO quote, and
several financial tables) and `Alphabet_Q2_2026_Financials.xlsx`, a spreadsheet of the same quarter's
headline figures the user extracted directly from that release. The prompt asks for **one** combined
quarterly business review deck, not two source-by-source sections, and explicitly asks that the
headline numbers make clear how much of net income is real operating performance versus one-off
items. Because the spreadsheet and the PDF agree on every figure, there is nothing to reconcile — the
test is **synthesis**, not conflict detection (that is `generated-sample-02`'s job). `input_flexibility`
(L3) names exactly this case: "Up to 5 uploaded documents ... agent extracts and synthesizes content."

## What good output looks like

### One coherent narrative, not two disjoint sections
The gold deck's narrative through-line is **operating strength vs. non-operating distortion**, directly
answering the prompt's ask to make clear how much of net income is real operating performance versus
one-off items: headline KPIs frame revenue/margin growth as genuinely strong while flagging that the
net-income headline is mostly a non-operating gain; segment performance contrasts Services' scale with
Cloud's acceleration and Other Bets' widening loss; Services and Cloud each get their own slide pairing
that segment's numbers with the release's narrative color for it (including, for Cloud, the AI-adoption
metrics behind the growth); the net-income headline is revisited and explicitly re-contextualized as
largely non-operating; capital raises, debt, and dividends are covered together; and a closing
synthesis restates what improved, what drove it, and what to watch. A deck with a "from the PDF" half
and a "from the spreadsheet" half — even if every number is correct — fails the structural intent here,
as would a deck that drops the requested operating-vs-non-operating framing and reports net income at
face value.

### Grounded, exact figures (both sources agree, so exactness is achievable)
- Consolidated revenue: $119.8B, up 24% YoY (23% in constant currency)
- Consolidated operating income: $40.8B, up 30%; operating margin 34% (up 2 points from 32%)
- Other income (expense), net: $98.0B (vs. $2.66B prior year) — driven by unrealized gains on equity
  securities; this is what drives most of the net-income headline, not core operating performance
- Net income available to common stockholders: $112.1B; diluted EPS $9.11, up 294%
- Google Services revenue: $94.5B, up 15% YoY — Search & other +17%, subscriptions/platforms/devices
  +15%, YouTube ads +13%
- Google Cloud revenue: $24.8B, up 82% YoY
- Segment operating income: Google Cloud $8.8B (vs. $2.8B prior year — more than 3x; operating margin
  ~35.6%), Other Bets operating loss $1.8B (vs. $1.2B prior year)
- Capital actions in the quarter: $49.6B raised via stock + mandatory convertible preferred issuance;
  $20.3B raised via senior unsecured notes; $40.0B ATM program authorized (unused as of 6/30/26); new
  $0.22/share common dividend and $12.15/share preferred dividend declared
- Cash, cash equivalents & marketable securities: $242.5B (up from $126.8B at year-end 2025);
  long-term debt $98.2B
- CEO framing (Sundar Pichai, from the release): AI investment driving the quarter — Cloud growth tied
  to enterprise AI infrastructure/solutions demand, Gemini Enterprise adoption near 90% of the Fortune
  100, Gemini API processing 22 billion tokens/minute, Gemini App at 950M monthly active users,
  AI features driving Search query growth

Figures not stated in either source (e.g. a Cloud "backlog" dollar figure) must not appear — see
Failure modes below.

### Segment commentary paired with its number, not listed separately
The release's narrative color (why Cloud accelerated, what drove Services growth, the CEO's AI framing,
the Gemini adoption/usage metrics) should sit next to the segment figures it explains — concretely, the
Gemini Enterprise/API-throughput/App-MAU figures belong on the same slide as Cloud's revenue and margin,
not stranded in a separate "quotes" slide disconnected from the numbers.

### Business-review register, not analyst deep-dive
This is a recurring quarterly review for internal stakeholders (per `slide_count_guidance`, QBRs run
~8 slides) — not an investor research note. Balance-sheet detail should be summarized at the
headline level (cash position, debt, dividend actions) rather than reproducing the full balance
sheet line by line.

### Respects the template's look
The template (`Combo_02` — a French-language corporate deck: `Accueil` title layout, `Défaut avec
contenu` content layout, `Remerciements` closing layout) is for visual style only — its own lorem-ipsum
placeholder text and French labels are not content to carry into the deck.

## What this example uniquely tests
- Synthesizing two consistent sources about one initiative into a single narrative deck, rather than
  translating one document (`02`, `04` in `product-slidesgen-*`) or carrying one deck forward (`01`).
- Using the spreadsheet as the numeric backbone while still pulling qualitative context (CEO quote,
  segment drivers, AI-adoption metrics) from the PDF — an accurate deck can't be built from either
  source alone.
- Correctly distinguishing operating performance from a one-off non-operating gain, rather than taking
  the net-income headline at face value.
- Keeping a dense 12-page earnings release compressed to a business-review-appropriate slide count
  without losing the segment-level and capital-structure story.

## Failure modes to catch
- Two-part deck: a "from the release" section followed by a "from the spreadsheet" section, instead of
  one blended narrative.
- Reporting the $112.1B net-income headline without flagging that it is mostly a non-operating
  equity-securities gain rather than operating performance.
- Dropping segment-level detail (Services vs. Cloud vs. Other Bets) in favor of only the consolidated
  headline numbers.
- Dropping the Cloud/AI-adoption context (Gemini Enterprise Fortune-100 adoption, API throughput, App
  MAU) that explains what's driving Cloud's acceleration.
- Treating the spreadsheet as the only source and ignoring the release's context/quote entirely (loses
  the "why," turning the deck into a bare numbers table).
- Rounding or altering any of the headline figures (e.g., "24%" becoming "roughly a quarter," "$119.8B"
  becoming "$120B" if the deck is expected to preserve exact figures elsewhere).
- Inventing a figure not present in either source (e.g. a Cloud "backlog" dollar amount, or a
  securities-gain sub-total that doesn't reconcile to the reported other-income figure).
- Reproducing the full balance sheet or every line item from the source tables — over-detailing past
  what a quarterly business review needs.
- Bleeding the template's French placeholder text or lorem ipsum into any slide.
- A slide count far outside the QBR-appropriate ~8-10 band the prompt requested.

## Visual scope
Native tables for the headline financial summary and the segment breakdown are natural and welcome.
KPI-tile treatment for the top-line deltas (revenue growth, operating margin, EPS) is used on the
headline slide. No charts required — the template has no native chart placeholders to adopt.

## Grades against
**Content coverage (1.0)** · **Structure & clarity (0.5)**
