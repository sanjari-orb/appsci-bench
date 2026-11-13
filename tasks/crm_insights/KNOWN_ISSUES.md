# Known Issues — CRM Analyst/Manager Insights

**Status: authored and verified end-to-end** (Mode A run completed 2026-08-07 with real,
non-degenerate scores — see "Not yet done" below). Owner: a contributor. See `seed_data/PROVENANCE.md`
for full data provenance.

## Fixed during the first E2E attempt

- **Missing "Today's date" anchor (real bug, now fixed).** The first Mode A run's agent built a
  working solver, but its own build report showed it reverse-engineering a single hardcoded
  "2021 Q4" anchor quarter for every relative-window question ("past N quarters/months") — because
  the sample text it was given had no reference date at all. CRMArena's original queries always
  ship with a per-query "Today's date" (in `metadata.required`), which
  `scripts/build_eval_data.py` was dropping when it built `single_turn_input.text` from just the
  bare `query` field. Fixed: `_query_text()` now appends `(Today's date: YYYY-MM-DD)` to every
  sample/example whose metadata has one — a relative-window question is genuinely unanswerable
  without it, for a human or an agent. The stable per-category policy text (Case Routing / Handle
  Time / Transfer Count Policy, identical across all 130 rows in each category, confirmed by hand)
  moved to `environment/data/schema_reference.md` as a shared resource instead of being repeated
  per-sample **(superseded 2026-09-02, see below — this text later moved again, into `user_sim_docs/`).**
  `eval_data/` and `examples.jsonl` were regenerated after this fix.
- **`grounding` missed cited evidence in plain-text responses (real bug, now fixed).** The same
  run's agent responded with a bare-text answer + "Supporting case records: ..." line rather than
  JSON. The original `eval.py` only read `evidence_case_ids`/`evidence` from a structured JSON
  field, so a plain-text response would score `grounding` as zero regardless of citation quality.
  Fixed: `grounding()` now also regex-scans the raw response text for id-shaped tokens.

## What's real vs. approximate

- `eval_data/`'s `answer` field is CRMArena's own real published ground truth (not invented) —
  trustworthy. `evidence_case_ids` (for `grounding`) is our own best-effort derivation and is NOT
  guaranteed to reproduce CRMArena's exact internal answer-derivation logic (their date-window and
  tie-break conventions aren't fully pinned down by the published policy text) — scored as a
  tolerant overlap for this reason, see `eval.py` + `PROVENANCE.md`.
- `case_routing`'s evidence is the weakest of the 6 categories: the query poses a new hypothetical
  case, and "most similar issue" is CRMArena's own semantic judgment, not a DB lookup — evidence
  falls back to the winning agent's full case load rather than issue-specific cases.
- `scripts/build_eval_data.py` only selects samples where `oracle.py` derives non-empty evidence;
  categories with harder-to-parse relative-date phrasing ("past N quarters" without an anchor date
  visible in the query itself) are underrepresented among the 130 candidates that qualified.

## Not yet done

- **First real Mode A run completed successfully** (2026-08-07):
  `status: completed`, `{'answer_correctness': 0.6, 'grounding': 0.508, 'final_score': 0.554}`.
  Per-category breakdown (5 samples each, best_region had 4/5 with derivable evidence):
  `case_routing` 5/5 correct, `monthly_trend_analysis` 4/5, `top_issue_identification` 3/5,
  `transfer_count` 3/5, `best_region_identification` 2/4, `handle_time` 0/5. Non-degenerate,
  no suspiciously pinned values — a real signal from a real agent build, not a scoring artifact.
  `handle_time` at 0/5 is worth another look (either the artifact's window logic is off, or
  `handle_time`'s policy is genuinely the hardest of the six to calibrate from 1 example) but that's
  an agent-under-test finding, not evidence of a task-authoring bug — see
  `docs/crm_insights_e2e_instructions.md` to reproduce.
- **L2 persona not authored** — no deep domain/systems axis was judged necessary beyond what L1
  already covers (schema + metrics); revisit if red-teaming surfaces a need for it. **Superseded
  2026-09-02: this reasoning conflated "domain knowledge" with L2's actual defined role** (per
  `schema/user_persona.schema.json`: L2 = "non-AI technical partner, engineering/integration only,"
  a different competence axis from L1's AI/ML-technical framing, not more of it). Authored `l2.yaml`
  — data engineer who owns `crm_db`'s infra (read-only Postgres, schema, freshness), matching the
  `order_to_cash`/`compliance_review` L2 convention. It does not know the withheld category rules
  (case routing/handle time/transfer count) — same status as L1, since those are business-domain
  facts an infra owner wouldn't hold either; only `l3` (the persona actually asking the question)
  does.
- **Adversarial persona variants authored 2026-09-02** (`l1_adversarial`, `l2_adversarial`,
  `l3_adversarial`) — same convention as `compliance_review`: base `domain_knowledge` verbatim +
  one intentionally-mistaken belief (L1: season-year anchoring inverted; L2: falsely claims
  `CaseHistory__c` has a precomputed transfer-count column; L3: inverts speed-vs-correctness
  priority) + role-scoped `adversarial_pressures` (L1/L2 defend the eval, no `scope_contraction`;
  L3 — the business owner who can actually say "skip the evidence" — gets it). Loaded by path for
  red-team runs only, not in `spec.yaml`'s `user_personas`. `_check_adversarial_consistency` passes
  clean (l3_adversarial's `known_docs` matches l3's). No red-team trace run against them yet — see
  `docs/adversarial_personas.md` for the mechanism.
- **a contributor's semi-automated labeling tool** (referenced in the Q2 plan doc) does not exist in-repo
  yet — this task was authored by hand instead, same posture `compliance_review` took with its own
  placeholder tooling gap.
- **Coverage strengthened 2026-09-04**: was 5 eval samples per category (30 total, all CRMArena's
  own published queries) + 1 calibration example per category — thin, and not self-generated.
  Added 20 self-generated cases (`scripts/build_self_generated_eval_cases.py`, see PROVENANCE.md) —
  `handle_time`/`best_region_identification` +4 each (the two categories flagged weakest below),
  the other four +3 each. Now 50 total, mixed CRMArena-native (30) + self-generated (20); calibration
  examples unchanged at 6 (see 2026-09-04 H2 entry below — no longer mounted to the agent).

## 2026-09-04 — H2 migration: labeled calibration examples held out

`environment/data/examples.jsonl` mounted 6 fully-labeled (query, answer, evidence) examples, one per
category, directly to the agent's `/data`, and every persona (`l1`/`l2`/`l3` and their adversarial
variants) listed `examples` in `known_resources`. Same failure shape PR #89 fixed for `order_to_cash`
and the H2 migration already fixed for `compliance_review` (see its `KNOWN_ISSUES.md`): a labeled
example resolving the withheld window/tie-break/first-agent-filter conventions teaches the agent the
decision rule directly, letting it pattern-match instead of eliciting/reasoning about the policy from
`l3`'s `known_docs` -- the thing the 2026-09-02 reshape above put those docs in `user_sim_docs/` to
force in the first place. This task never got the H2 fix applied to it even though the convention
already existed elsewhere in the repo.

Moved to `seed_data/labeled_calibration_examples.jsonl` (held out, not mounted); the `examples`
resource is removed from `spec.yaml` and every persona's `known_resources`. `RESOURCES.md` updated to
match.

## Agent-facing docs convention (`/sop` and `/data`)

`environment/sop/` is unused here (kept as a placeholder, same as `compliance_review`) — agent-facing
docs live under `environment/data/` (`schema_reference.md`, `kpis.md`), mounted read-only at `/data`.
The 6 labeled calibration examples are held out at `seed_data/labeled_calibration_examples.jsonl`
instead (see 2026-09-04 H2 entry above) — not mounted. See `tasks/compliance_review/` for the
reference layout this mirrors.

## 2026-09-02 — reshaped in response to the "is this text-to-SQL?" ticket

**What this task now measures that a text-to-SQL benchmark over the same schema wouldn't: whether
the agent elicits the hidden operational definitions from the persona posing the question, before
trusting its own reading of the schema to compute an answer.** Concretely: `schema_reference.md`
(mounted, agent-readable) now states only genuinely-discoverable schema facts (table/column names,
what a column means); the quarter/season window rules, the Case Routing 3-tier tie-break order, and
the Handle Time / Transfer Count first-assigned-agent filter are held out in `user_sim_docs/` and only
reachable by asking `l3` (`known_docs`). A new `elicitation_rubric.yaml` (5 items) scores whether the
agent actually asked, separate from `answer_correctness`/`grounding`. Ties to `answer_correctness`:
getting these facts wrong (assuming the wrong quarter boundary, the wrong tie-break order, or the
wrong filter direction) produces a demonstrably wrong answer against CRMArena's real published ground
truth — this isn't a rubric bolted on top of an unrelated query task, the same facts gate whether the
SQL an agent writes is even the right SQL. Ticket closed as reshaped, not dropped — see PR #87.

**Correction (same day):** an earlier version of this pass replaced `l3` with six category-scoped
personas (one per CRMArena category), reasoning that the old merged persona was itself "the
giveaway." Wrong: a build run talks to exactly one persona for its entire duration, and the resulting
artifact is scored against the full eval dataset across all 6 categories regardless of which persona
built it (`task_category` in the eval samples is descriptive only, never filtered on) — so splitting
meant every one of the 7 personas could only ever surface 1/6 of the rules needed, guaranteeing
failure on the other 5/6 categories no matter what the agent asked. Reverted to a single `l3` persona
holding all 3 category docs via `known_docs` — the actual fix (moving the rules out of the mounted
doc) didn't require splitting the persona to work.
