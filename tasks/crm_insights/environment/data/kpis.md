# CRM Insights — KPIs (customer success)

- **Answer correctness** (primary): does the reported answer match what a correct query against
  `crm_db` would produce? Most answers are exact identifiers (agent Id, Issue Id, state code) or a
  short exact value (month name) — there's no partial credit for "close" on these.
- **Grounding** (secondary, trust): does the response cite the actual CRM records (case ids) that
  justify the answer, rather than asserting a number with no traceable source? A manager needs to
  be able to click through and verify — an answer with no supporting records isn't actionable even
  if it happens to be right.
