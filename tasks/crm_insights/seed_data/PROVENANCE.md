# crm_insights eval seed — provenance

- substrate: CRMArena (Huang et al., "CRMArena: Understanding the Capacity of LLM Agents to Perform
  Professional CRM Tasks in Realistic Environment", arXiv:2411.02305), Salesforce AI Research —
  github.com/SalesforceAIResearch/CRMArena
- **queries + answers**: HF dataset `Salesforce/CRMArena`, config `CRMArena` (1,186 rows total, real
  ground truth, not something we compute) — `analyst_manager_queries.jsonl` (780 rows) is that
  dataset filtered to the 6 task strings owned by `crm_insights` (confirmed real strings from
  `run_tasks.py`'s `--task_category` choices; the other 3 — `named_entity_disambiguation`,
  `policy_violation_identification`, `knowledge_qa` — are CRMArena's service-agent tasks, which this benchmark does not include).
- **CRM data**: `crmarena_data.db` is a byte-for-byte copy of CRMArena's own
  `local_data/crmarena_data.db`, checked directly into their public git history (not something we
  snapshotted via their live Salesforce org, though we validated that path works too — see below).
  Confirmed identical row counts against the live org for every data-heavy table (Case 977,
  Account 201, Order 2069, OrderItem 7100, CaseHistory\_\_c 2983, Issue\_\_c 10) — strong evidence the
  org hasn't drifted from what the published answers were computed against; `User` differs (152
  local vs. 261 live, likely extra system/automation users on the live org not present in their
  static dump — irrelevant here since we only need agents that actually own cases).
- license: **CC BY-NC 4.0** (Creative Commons Attribution-NonCommercial 4.0), for both CRMArena's
  repo (its `LICENSE.txt`) and the Hugging Face dataset `Salesforce/CRMArena` (`license:
  cc-by-nc-4.0`). Redistribution needs attribution and is for non-commercial use only, and that
  applies to the fixtures derived from it that are committed under `seed_data/`. An earlier version
  of this note said MIT, which was wrong. The underlying data is synthetic, generated for CRMArena
  (a fictitious "Shoes & Clothings" retailer), not real customer data.
- local copies (gitignored, re-fetchable, not committed):
  `.ai-work/datasets/crmarena/crmarena_data.db` (from
  `raw.githubusercontent.com/SalesforceAIResearch/CRMArena/main/local_data/crmarena_data.db`),
  `.ai-work/datasets/crmarena/analyst_manager_queries.jsonl` (from `load_dataset("Salesforce/CRMArena",
  "CRMArena")`, filtered).

## Why there's no disjoint build/eval split (unlike `compliance_review`)

`compliance_review` had to split its corpus because the *answer itself* was a hidden label baked
into CUAD's clause-presence annotations — exposing it at build time would hand the agent the
answer key. Here there's no such column: every answer (`handle_time`'s agent Id, `best_region`'s
state code, ...) is a computed aggregate over ordinary CRM records the agent must derive itself by
querying `crm_db`, exactly as a real analyst would. So the full real snapshot is build-time-safe —
`crm_db` is the SAME data at build and eval time (a real CRM persists across time; there's nothing
to "hold out"). The only thing that stays held out is the *eval questions themselves* — the 30
picked for `eval_data/` never appear in `labeled_calibration_examples.jsonl` (the 6 calibration
examples are a disjoint set of rows from the same 780; also held out from the agent's mount entirely,
not just disjoint -- see the 2026-09-04 H2 entry in `KNOWN_ISSUES.md`).

## Live-org access (validated, not used as the running dependency)

CRMArena's `README.md` publishes shared, working credentials (see their repo's `.env.example` —
deliberately not reproduced verbatim here to avoid tripping our own secret scanners over a
credential that isn't ours to rotate) to a live Salesforce sandbox org, explicitly so the research
community can run the benchmark. We confirmed (2026-08-07) these still authenticate and that
`SELECT Id FROM Case LIMIT 3` succeeds. We deliberately don't depend on
this for the actual task environment — a third party's live org uptime/rate limits/credential
rotation is the wrong thing to depend on for a quarter of reproducible sweeps — but it's worth
knowing this path exists and works, e.g. to re-validate staleness in the future.

## The one genuinely self-authored piece: grounding evidence

CRMArena's published rows carry `answer` but not *which records justify it*, and the `grounding`
objective (`spec.yaml`) needs that. `seed_data/oracle.py` derives a best-effort supporting-Case-ID
set per category by parsing the query's stated time window and re-deriving the aggregate against
`crmarena_data.db`.

**This is deliberately approximate, not a guaranteed reproduction of CRMArena's own answer-derivation
logic** — their exact date-window conventions (e.g. which calendar year "Winter 2021" spans) and,
for `case_routing`, their semantic "most similar issue" judgment are not fully pinned down by the
published policy text alone (confirmed by hand: re-deriving `handle_time`'s reported minimum-handle-
time agent under several plausible window/counting conventions did not reliably reproduce their
exact answer). Since we don't need to *recompute* the answer (it's already given), this doesn't
block anything — but it does mean:
- `answer_correctness` scores the real published `answer` (trustworthy).
- `grounding` scores against an approximate, best-effort evidence set — scored as a tolerant overlap
  (see `eval.py`), never exact-set equality, which is the right precision level for evidence we
  can't guarantee is CRMArena's exact original derivation.
- `case_routing` evidence is the weakest of the six: since the query poses a *new*, hypothetical
  case (not a real row in the DB) and "most similar issue" is CRMArena's own judgment call, there's
  no clean lookup at all. `oracle.py` falls back to the winning agent's full closed-case load as a
  defensible-but-approximate "why this agent" evidence set, documented as a known limitation in
  `KNOWN_ISSUES.md` rather than silently presented as precise.
- Only samples where `oracle.py` derived *some* non-empty evidence were selected for `eval_data/`
  and `labeled_calibration_examples.jsonl` (`scripts/build_eval_data.py` requires this) — categories
  with harder-to-parse
  phrasing (e.g. relative "past N quarters" wording without an explicit month/season) were more
  likely to be skipped in favor of a parseable sibling sample; this is a coverage bias worth knowing
  about, not a correctness bug.

## 20 self-generated eval cases (2026-09-04)

`eval_data/` now also holds 20 samples with ids like `crm_insights__handle_time__self01` (`self` in
the id, vs. CRMArena's bare numeric ids, so provenance is visually obvious at a glance). Unlike the
30 above, these are ours end to end: question text, answer, and evidence are computed by
`scripts/build_self_generated_eval_cases.py` directly against the real fixture data, using this
task's own documented policies (`user_sim_docs/domain_conventions.md`, `case_routing.md`,
`handle_time.md`, `transfer_count.md`) — not lifted from CRMArena's published query set at all.

Because the policy that defines "correct" here is our own document (also used for elicitation
scoring), there is no answer-derivation ambiguity of the kind flagged above for the CRMArena-derived
30 — `oracle.py`'s evidence for those is explicitly best-effort (their exact window/tie-break
conventions aren't fully pinned down by the published text). For the self-generated 20, policy →
answer is exact by construction: the script applies the documented rule directly to the real data,
so there's nothing to approximate.

Each scenario's parameters (which issue/product/window/agent-count-filter to use) were hand-picked to
resolve without a tie under the real policy, and to avoid the exact agent/product/issue/state
combination any of the existing 30 already exercises — genuinely new coverage, not near-duplicates.
Distribution: `handle_time` +4 and `best_region_identification` +4 (the two categories
`KNOWN_ISSUES.md` already flagged as weakest — 0/5 correct and thinnest evidence in the one real Mode
A run), the other four categories +3 each = 20.

The underlying CRM snapshot these 20 are computed against is still CRMArena's real data (same
`crm_db`/fixture, unchanged) — only the questions, answers, and evidence are self-generated, per the
scope decided for this pass (real environment data is fine; reusing CRMArena's own questions is not).

```
python3 tasks/crm_insights/scripts/build_self_generated_eval_cases.py
```
Deterministic and regenerable — same 20 cases every run, since the scenario parameters are fixed in
the script, not sampled.

## Regeneration

```
python3 tasks/crm_insights/scripts/build_seed_data.py --db .ai-work/datasets/crmarena/crmarena_data.db
python3 tasks/crm_insights/scripts/build_eval_data.py
```
The first exports `crmarena_data.db`'s 8 tables to `seed_data/fixture/*.jsonl` (loaded into
Postgres by `environment/db/02_load_data.py`). The second re-derives evidence and re-picks
eval/calibration samples deterministically (fixed selection order by `idx`) — will pick the same 36
rows given the same two source files.
