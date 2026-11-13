# compliance_review — L3 domain knowledge (agent-facing extract)

Role: Compliance program owner (Legal/Compliance Ops)

## current_pain

Reviewers read every customer-facing document by hand against internal policy and regulatory rules
before it ships; issues get missed and reviews are slow.

## document_volume

Hundreds of customer-facing documents move through review each month.

## final_reviewer

A human reviewer stays the final decision-maker -- the system surfaces likely violations with the
offending span, the rule, and a rationale; it does not auto-approve or auto-reject a document.

## success_signal

Reviewers trust the flags and stop missing real violations; false alarms stay low enough that
reviewers don't tune the tool out.

## what_counts_as_problem

A finding is a real violation only when it maps to a rule in the policy being checked:
- A required clause/disclosure is missing (must_have rule)
- Prohibited language is present (must_not_have rule) -- must be backed by the exact offending span
A category flagged needs_review, when present, is NOT a violation -- it's routed to a human, not
counted as a fault.

## what_doesnt_count

These should NOT be reported as violations (they create noise):
- A span-level match that doesn't correspond to any rule in the policy being checked
- A "violation" with no quoted span from the document to back it
- Stylistic or paraphrase concerns unrelated to the rule's category

## validation_rules

For each rule in the applicable policy, in order:
1. must_have -> violation if the clause/category is absent from the document
2. must_not_have -> violation if the clause/category is present; evidence = the offending span(s)
3. needs_review -> if the category is present, add to needs_review (not a violation)
Verdict: compliant = true only if the violations list is empty.

## rule_types

Rule type enum values (per policy rule):
- must_have: violation if the clause/category is ABSENT
- must_not_have: violation if the clause/category is PRESENT (evidence = offending span)
- needs_review: not pass/fail; surfaced to a human reviewer if the clause/category is PRESENT

## priority

Catching real violations matters more than a spotless report -- a missed violation that ships is
the worst outcome. But too many false alarms burn reviewer trust.
