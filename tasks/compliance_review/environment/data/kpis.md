# Success Criteria for the Compliance Review System

## What the Compliance Team Needs

A human reviewer today reads each customer-facing document by hand against internal policy before it
ships. They need a system that:

1. **Catches real violations** — don't let a policy violation reach a customer-facing document
2. **Doesn't waste their time** — too many false flags erodes trust and gets the tool ignored
3. **Justifies itself** — every flagged violation should point to the specific text and the rule it
   breaks, so a reviewer can act on it quickly

## Key Metrics

**Violation recall (critical)**
Missing a real violation is the worst outcome — it means a non-compliant document ships. The system
must catch nearly all real violations.

**Violation precision**
Flagging clean clauses wastes reviewer time and erodes trust. If too many flags are false alarms,
reviewers start ignoring them.

**Evidence quality**
When a violation is flagged, the cited text span should actually be the offending language — a
correct verdict with the wrong evidence doesn't help a reviewer act quickly.

## Human Review Stays in the Loop

The compliance team is the final reviewer. The system's job is to flag likely violations with a
reason, and route ambiguous clauses to a human — never to auto-approve or auto-reject a document on
its own.
