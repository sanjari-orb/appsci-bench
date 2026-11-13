# CRM Insights — artifact

Answers a manager's plain-language question about accounts, cases and agents with a direct answer
plus the specific case records behind it.

## Shape

```
server.py     /health, /selftest, /respond  (the artifact contract)
agent.py      system prompt + tool loop; a rule-based router as fallback if no model is reachable
analytics.py  every operational policy, as SQL. The arithmetic lives here, never in the model
dates.py      quarter / season / "last N months" conventions
llm.py        Anthropic Messages client (stdlib only), one tool call per turn
db.py         read-only Postgres access, per-thread connections, guarded free-form SELECT
```

The split is the whole design: **the model reads the question, SQL computes the answer.** The model
never does arithmetic, never recalls a figure, and can only reach the database through tools that
have the policies already baked in. Every identifier in a reply comes from a tool result in the same
conversation, which is what makes the supporting case ids real rather than decorative.

If the model is unreachable, `agent.fallback_answer` routes the question with regexes over the same
policy-correct analytics, so the service degrades instead of failing. Spot-checked: it agrees with
the model path on handle-time, monthly-trend and transfer-count questions.

## Policies encoded (elicited from the stakeholder, not inferred from the schema)

- **Handle time** = closed_date − created_date, computed **only** on cases never transferred
  (exactly one `Owner Assignment` history row; more than one means transferred).
- **"Managed / handled N cases"** counts cases against the **first** agent assigned to them. A case
  transferred in does not count for the receiver; a case transferred away still counts for the
  original agent.
- **Transfer count** = reassignments away from an agent; a transfer from A to B counts for A. An
  agent in scope with no transfers counts as 0 (not absent).
- **Case routing** = issue expertise → product expertise → workload (fewest non-`Closed` cases), in
  that order.
- **Quarters** Q1 Jan1–Mar31, Q2 Apr1–Jun30, Q3 Jul1–Sep30, Q4 Oct1–Dec31.
- **Seasons** Winter Dec1–Feb28/29, Spring Mar1–May31, Summer Jun1–Aug31, Fall Sep1–Nov30. A season
  named with one year is the season *ending* in that year (Winter 2021 = 2020-12-01 → 2021-02-28).
- **Relative windows** are anchored to the question's own stated "Today's date", never to the real
  current date.

## Eligibility

A question that states no minimum case count imposes none: a state or agent with a single case can
win a "quickest" question, and the model is instructed never to invent a threshold (confirmed with
the stakeholder -- "if I didn't put a minimum in the question, don't invent one"). This matters more
than it looks: silently excluding thin groups to make a result look sensible changes the answer.

## Judgement calls the stakeholder did not specify

1. **"Past/last N quarters" is treated as a rolling window** of N×3 months back from the question's
   anchor date, not as the N most recently completed calendar quarters. Same rolling treatment as
   months, which is the reading the anchor-date convention suggests.
2. **Which date a period filters on follows the question's intent** (confirmed with the
   stakeholder, who has no fixed convention): a handle-time question, or anything about how quickly
   cases closed in the period, filters on `closed_date`; a volume question -- how many cases came in,
   which month got the most -- filters on `created_date`. `analyze` applies that default from the
   metric itself, so it cannot be forgotten at the call site.
3. **A date window on a transfer-count question scopes the cases**, not the moment the transfer was
   recorded. Stated in every answer's Method line.
4. **"Average" is the mean.** The stakeholder has no mean-vs-median rule; mean is the plain reading.
5. **Ties are reported, never broken.** The stakeholder asked for the tie surfaced honestly with
   records for each side rather than a silent pick, and routing ties are common in this data because
   per-agent-per-issue counts are small.
6. **Routing expertise is counted by the agent who closed the case** (its final owner), since the
   policy speaks of cases *closed* / *solved*.

## Answer format

No downstream schema is imposed (confirmed with the stakeholder), so the reply leads with the answer
and stays verifiable: a bold `Answer:` line naming the thing asked for (agent as name + id, state as
its 2-letter code, month by name, issue/product as name + id), the metric that makes it the answer,
up to 8 supporting case ids with the figures that matter, and one `Method:` line stating the window
used, the policy applied and any filter.

## Operating notes

- `GET /selftest` reports database and model reachability — the fastest way to tell a credential
  problem from a logic one.
- Typical answer takes 10–20s (one to three model turns plus SQL). That exceeds the 2s latency SLO
  in the task spec; correctness was the stakeholder's explicit priority over speed.
- Free-form SQL is restricted to a single `SELECT`/`WITH`, wrapped in a subquery with a row cap, with
  write and DDL keywords rejected.
