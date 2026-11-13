# Success Criteria for Invoice Review System

## What AP Needs

The accounts payable team processes ~12,000 invoices per month. They need a system that:

1. **Catches real problems** - Don't let material discrepancies slip through
2. **Doesn't waste their time** - Too many false flags erodes trust
3. **Explains itself** - When flagging, give a reason AP can act on

## Key Metrics

**Flag Accuracy**
Overall accuracy of flag decisions - how often does the system correctly decide whether to flag or not flag an invoice? This combines both catching real issues and avoiding false alarms.

**False Negative Rate (Critical)**
Missing a faulty invoice is the worst outcome - it means we pay for something we shouldn't. The system must have a very low false negative rate. Every material discrepancy should be caught.

**False Positive Rate**
Flagging clean invoices wastes AP's time and erodes trust in the system. If too many flags are false alarms, reviewers start ignoring them. Keep false positives low while still catching real issues.

**Fault Type Classification Accuracy**
When flagging an invoice, the system should correctly identify the type of problem (price issue, quantity mismatch, off-contract charge, duplicate, etc.). Accurate classification helps AP understand and resolve issues faster.

## Processing Speed

Invoice reviews should complete quickly - AP can't wait around. Target response time is under 2 seconds per invoice.

## Important Constraints

- The system flags and explains - it does NOT auto-pay or auto-reject
- AP makes the final call on flagged invoices
- Better to flag something questionable than miss a real issue
- But excessive false positives will cause AP to ignore the flags
