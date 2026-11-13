# Evaluation Data — Airline Reservations Agent

## Layout

One subdirectory per `data_point_id` (e.g. `tau2_airline_0/`), each holding a `sample.json`
(schema/dataset.schema.json `$defs/eval_sample`). 50 cases cast offline from tau2-bench's airline
domain (`scripts/convert_tau2_airline.py`): each sample drives the artifact agent user sim (built
and wired into `runner.py`'s eval phase -- see `appsci_bench/eval/README.md`) rather than a single
query.

The vendored upstream material lives outside `eval_data/` (see below): the policy and tool reference in
`user_sim_docs/common/`, and the provenance record + commit pin in `provenance/airline/`.

## `sample.json`

- `data_point_id`: the case id (e.g. `tau2_airline_0`), matching the subdirectory name
- `multi_turn_input.user_task`: WHAT the caller wants -- why they're calling, what they don't know,
  and how they'll pursue it (negotiating stance, constraints) -- composed from tau2's
  `reason_for_call`/`unknown_info`/`task_instructions` fields.
- `multi_turn_input.user_persona`: WHO is calling -- tau2's `known_info` field verbatim
  (name/user id/confirmation or reservation number, whatever the case includes). Previously a
  generic pointer-back string ("the caller described in user_task, played faithfully"): the
  original concern was that only 31/50 samples' COMPOSED monologue opened with a cleanly
  regex-parseable "You are `<Name>`..." pattern, so splitting the already-joined string would be
  silently wrong for the other 19. That concern doesn't apply to reading `known_info` directly --
  it's tau2's own already-separated field (non-empty for all 50 samples), not a re-parse of the
  monologue, so the split is a full mechanical field move now, not an NL heuristic. (tau2's own
  `user_scenario.persona` field is null for every airline task, so `known_info` is the only real
  persona signal upstream provides -- there's nothing else to add here.)
- `expected_output.json`: the old `oracle.data` verbatim (`actions`, `communicate_info`,
  `nl_assertions`, `reward_basis`, `purpose`)
- `eval_rubric`: the old `success_criteria` plus `nl_assertions`, joined into one NL string for
  `policy_compliance`'s judge to read
- `user_stop_criteria`: when the eval-time user sim should end the conversation -- upstream's three
  stop tokens (goal met / transferred to a human / nothing left to go on) folded into the single
  free-text criterion our sim judges. See `provenance/airline/AIRLINE_PROVENANCE.md` for why this one field is
  a rewrite rather than a transcription.

No per-sample files -- every case's DB context is the shared `airline_db` environment resource
(`environment/eval/data/`), not an attachment.

## Objectives (`tasks/airline_rebooking/eval.py`)

- `task_success`: tau2's reward = DB x COMMUNICATE, both halves measured (the DB half needs
  `evaluation_data.reset_env_per_sample`, which is on; without it the function falls back to
  COMMUNICATE-only and says so via `db_component: "not_measurable_yet"` rather than faking DB=1).
  Gated on the conversation having actually ended: a trace cut off by the `max_turns` ceiling scores
  0 before either half is evaluated, matching upstream's own premature-termination gate. Without that
  gate the 7 cases needing no writes and no communications score 1.0 for any trace that simply never
  wrote anything, finished or not.
- `policy_compliance`: a real `make_transcript_judge`, replacing the old inline `criteria` string.
  Declared at **`weight: 0`** -- it has no upstream counterpart (all 50 cases set `reward_basis` to
  DB + COMMUNICATE only), so it stays computed and reported per-metric while `final_score` remains
  exactly `task_success`, the one figure comparable to a published tau2 number.

## Why the vendored material is not under `eval_data/`

`policy.md` / `tools_reference.json` / the provenance docs are source material, NOT eval samples. They
were moved out because the dataset loader treats every subdirectory of `eval_data/` as a sample -- a
stray `seed/` here broke loading with a "missing sample.json" error. They now live where each is
actually consumed: the policy and tool reference in `user_sim_docs/common/` (read by the personas, and
by the gold reference), the commit pin and field mapping in `provenance/airline/`.
