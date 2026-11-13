# Requirements (from L3 elicit)

Synthesized from `logs/transcript.md` for the build phase. Not ground truth beyond what the
persona stated in the you/sim dialogue.

## Problem / users / success

- Today every customer-facing document is read by hand against policy/regulatory rules before it ships.
- Volume: hundreds of documents per month. Review is both **leaky** (real violations slip through) and a **bottleneck** (slow end-to-end reads).
- Users: the **reviewers** who do that manual work today.
- Desired system: read a document and **flag** likely violations — offending text, which rule, short rationale — for a human. It does **not** approve or reject; the reviewer stays the final decision-maker.
- Success: reviewers trust the flags, real violations stop shipping, false alarms stay low enough that reviewers don’t tune the tool out.
- Worst failure: a real violation ships. False alarms also hurt (reviewers stop trusting the tool). Catch real ones first, but don’t drown reviewers in noise.

## Input

- A **customer-facing document** (contracts/agreements that go out to customers), checked against the **policy that applies** to it.
- Document/deal types mentioned: licensing / IP transfer; purchasing/sourcing; general vendor/supplier; high-liability engagements. Each type maps to a different standard.
- **Punted by L3:** file formats and other technical/data layout — look at what the team env actually provides.
- **Open / tricky:** how to know which policy applies to a given document (sim flagged this as still open and important).

## Output JSON contract

The review queue reads the reply **programmatically**. Whole response = **one JSON object**, nothing else (no prose around it or the queue drops it).

Exactly three top-level keys, spelled exactly:

| Key | Type | Meaning |
|-----|------|---------|
| `compliant` | boolean | `true` only when `violations` is empty. Non-empty `needs_review` does **not** make it non-compliant. |
| `violations` | list | Pass/fail findings for the applicable policy. |
| `needs_review` | list | Routing signals for a human look — **not** faults. |

### Each `violations[]` object (five keys)

| Key | Type | Meaning |
|-----|------|---------|
| `category` | string | Clause/disclosure category, e.g. `"Cap On Liability"`, `"Most Favored Nation"`. |
| `rule` | string | `must_have` (required category **absent**) or `must_not_have` (prohibited category **present**). |
| `present` | boolean | Whether that category was found in the document. `must_have` violation → `false`; `must_not_have` → `true`. |
| `evidence_spans` | list of strings | Exact offending text quoted from the document. **Empty** for missing required clauses (nothing to quote). **Required** for prohibited-language flags — no span ⇒ don’t report. |
| `rationale` | string | Short reviewer-facing explanation naming the rule and (when present) the offending language. |

### Each `needs_review[]` object (two keys)

| Key | Type | Meaning |
|-----|------|---------|
| `category` | string | Category that triggered the human-look route. |
| `evidence_spans` | list of strings | Text that triggered the flag. |

No `rule` / no `rationale` on needs_review — routing signal, not a verdict.

## Violations vs needs_review vs noise

**Real violation** = maps to an actual rule in the **policy being checked**:

1. `must_have` — required clause/category **missing** → `present: false`, `evidence_spans: []`.
2. `must_not_have` — prohibited language **present** → `present: true`, must quote exact span(s).

**needs_review** — if the category is present, surface it for a human. Not a fault, not in `violations`, does not flip `compliant`. Examples called out: Source Code Escrow, Change Of Control, Covenant Not To Sue, Volume Restriction.

**Noise (never report as violations):**

- Category match that isn’t a rule in the applicable policy.
- Prohibited-clause “violation” with no quoted span.
- Style/tone/phrasing complaints not tied to a rule’s category.
- Counting a needs_review item as a violation.

## Standards / policies

Sim said “five standards” / “that’s all five,” but only **four** named standards were listed. Recorded as stated; do not invent a fifth.

### Vendor Contract Standard — general vendor/supplier agreements

- **Required:** Cap On Liability; Governing Law.
- **Prohibited:** Uncapped Liability; Most Favored Nation.
- **Flag for review:** Change Of Control.

Glosses: Cap On Liability = must limit each party’s total liability. Governing Law = must name governing jurisdiction. Uncapped Liability = language that removes/overrides a liability cap. Most Favored Nation = terms at least as good as any other customer. Change Of Control = acquisition clause → needs_review, not a violation alone.

### Risk-Averse Standard — high-liability-exposure engagements

- **Required:** Cap On Liability; Audit Rights; Insurance.
- **Prohibited:** Uncapped Liability; Liquidated Damages.
- **Flag for review:** Covenant Not To Sue.

Glosses: Audit Rights = right to audit the counterparty. Insurance = counterparty must carry insurance. Liquidated Damages = pre-agreed damages for breach. Covenant Not To Sue = waiver of right to sue → needs_review.

### Procurement Clean Standard — purchasing/sourcing agreements

- **Required:** Audit Rights.
- **Prohibited:** Most Favored Nation; Non-Compete; Exclusivity; Minimum Commitment.
- **Flag for review:** Volume Restriction.

Glosses: Non-Compete = restrictions on who else we can do business with. Exclusivity = exclusive-dealing. Minimum Commitment = minimum purchase/volume. Volume Restriction = caps on purchase volume → needs_review.

### IP-Protective Standard — licensing / IP transfer

- **Required:** License Grant; Ip Ownership Assignment.
- **Prohibited:** Joint Ip Ownership; Irrevocable Or Perpetual License.
- **Flag for review:** Source Code Escrow.

Glosses: License Grant = must state what’s being licensed. Ip Ownership Assignment = who owns resulting IP. Joint Ip Ownership = shared ownership of resulting IP. Irrevocable Or Perpetual License = never-revocable / never-expires. Source Code Escrow → needs_review.

## Priorities

1. Catch real violations (miss that ships = worst outcome).
2. Keep false alarms low enough that reviewers keep trusting flags.

## Open questions (from elicit)

- **Which policy applies** to a given document (mapping left open; L3 said it’s genuinely tricky).
- Sim’s **“five standards”** wording vs **four** named lists.
- **File formats** / how documents are delivered in the env (L3 punted to technical/data side).
- Borderline language judgment — sim offered to review tagged examples (doc + output) later.
