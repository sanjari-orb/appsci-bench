# [TASK_NAME] — Task SOP (ground truth)

Comprehensive definition of the task. **Not shown to the agent** — it is the source from which the
user-personas, environment, and oracle are authored. Personas reveal only audience-scoped subsets.

## Problem
[Describe the customer's operational challenge. Include:
- What the user does manually today
- Pain points or risks from current process
- Scale/volume if relevant
Example: "The AP team manually checks supplier invoices against POs and contracts, missing discrepancies that lead to overpayment. ~12k invoices/month."]

## Success Criteria
[Define what constitutes a successful outcome for the specific task. This should summarize the requirements of the expected system and any product leading metric. 
Example: "Flag for review if price delta > $500, quantity mismatch, or uncovered line item (any amount). Do not flag trivial roundings."]

## KPIs (customer success)
- [Primary metric — recall/precision/coverage]
- [Secondary metric — user trust, efficiency, cost impact, etc.]

**Note**: Detailed descriptions of these metrics (thresholds, calculation method, failure modes) should be included here and carried through to the eval_data/metrics section.

## Sections by audience
- **L3 (PM / [role]):** [What this audience cares about — business goal, success definition via examples, who reviews. 
  Does NOT speak to thresholds/architecture.]
- **L1 (applied-science manager):** [What this audience can discuss — metrics, class imbalance, approach trade-offs, 
  data constraints.]
- **L2 (technical partner / domain expert):** [Optional. Include if the task requires deep domain knowledge — 
  system architecture, API limitations, data availability, real-world constraints.]
