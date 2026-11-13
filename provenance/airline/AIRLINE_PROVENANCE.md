# tau2-bench airline — vendored seed (provenance)

These files are the **upstream source** the airline eval cases are converted from. Conversion is
OFFLINE (see `scripts/convert_tau2_airline.py`); this directory records exactly what was converted
so the result is auditable and reproducible.

| File | What it is |
|------|------------|
| `../../../../provenance/airline/tasks.json` | The 50 tau2 airline tasks (verbatim from upstream), the offline conversion source. Lives under top-level `provenance/airline/` (reference only; not read at run time). |
| `policy.md` | The airline agent policy, reframed as a build spec for the meta agent (ground-truth SOP source). |
| `tools_reference.json` | The 14 airline tools (action space) as OpenAI-standard function declarations (name, description, parameters with arg types + required), plus a `tool_type` (READ/WRITE/GENERIC) extension. Derived from `src/tau2/domains/airline/tools.py`. |
| `TAU2_COMMIT.txt` | The upstream release/commit the above were taken from, and why the current pin is a version stamp rather than a re-vendor. |

All 50 cast tasks are used as held-out **test** cases — we do not split into train/base (the
benchmark evaluates artifacts, it does not train on these), so the upstream `split_tasks.json` is
not vendored.

## Source
- Repo: https://github.com/sierra-research/tau2-bench
- Path: `data/tau2/domains/airline/` and `src/tau2/domains/airline/tools.py`
- Release: **v1.0.1** (commit `fc0055dc`, 2026-07-16) — see `TAU2_COMMIT.txt` for the full sha and for
  why moving to this tag changed no airline content.

### What is verified about this alignment

Checked against upstream rather than assumed, because "we vendored it once" is not the same as "we are
still aligned":

- `provenance/airline/tasks.json` is **byte-identical** to upstream's
  `data/tau2/domains/airline/tasks.json` at this release. That file has had two commits in its history
  (the 2025 release and `01e812d1`, the τ³ task-quality pass), so the τ³ corrections are included.
- The 50 converted eval samples carry upstream's reward inputs unchanged — `actions`,
  `communicate_info`, `nl_assertions`, `reward_basis` match leaf-for-leaf, and every scenario clause
  upstream defines (including the `unknown_info` present on 8 tasks) reaches the sample.
  `tests/tasks/airline_rebooking/test_sample_fidelity.py` asserts all of this, so a lossy
  re-conversion fails a test rather than quietly moving scores.
- The reward formula, the airline environment/tools semantics, and the premature-termination gate match
  this release's evaluator.

**The run configuration is a separate question from the vendored content, and it is where the real
divergence was.** The published airline submissions at this release declare
`user_simulator: "gpt-5.2"`; we had pinned tau2's `gpt-4.1-2025-04-14` code default, which is what the
v0.1.3-era rows used. See `appsci_bench/eval/artifact_agent_user_sim.py` and `docs/baselines.md`.

## DB not duplicated here
- `db.json` (300 flights / 500 users / 2000 reservations) is **not** vendored in this seed dir to
  avoid committing the same multi-MB data twice. Instead it is **materialized** as the agent-facing
  task environment fixture at `../../environment/db/db.json`, produced by
  `scripts/convert_tau2_airline.py --emit-db`. That materialized `db.json` is byte-equal to the
  upstream tau2 `db.json` (verified) and
  serves as both the fixture and the audit copy. Every eval case shares this one DB (not attached
  per-sample); the ground-truth DB-state check is the golden `actions` list carried in each sample's
  `expected_output.json`, replayed against this DB.

## How each tau2 field maps to our `eval_sample` (schema/dataset.schema.json `$defs/eval_sample`)
| tau2 field | eval_sample field |
|------------|-------------------|
| `id` | `data_point_id` (prefixed `tau2_airline_<id>`) |
| `user_scenario.instructions.{reason_for_call,known_info,unknown_info,task_instructions}` | `multi_turn_input.user_task` (composed) |
| (fixed pointer-back string) | `multi_turn_input.user_persona` |
| (upstream user-sim stop tokens, see below) | `multi_turn_input.user_stop_criteria` |
| `evaluation_criteria.{actions,communicate_info,nl_assertions,reward_basis}` | `expected_output.json` (full struct, JSON-encoded) |
| `evaluation_criteria.{nl_assertions,reward_basis}` | `eval_rubric` (NL, read by `policy_compliance`'s judge in `tasks/airline_rebooking/eval.py`) |

### The stop criterion is a fold-down, not a field move

Upstream's user sim ends a conversation by emitting one of three tokens (its
`data/tau2/user_simulator/simulation_guidelines.md`): `###STOP###` once the scenario goal is
satisfied, `###TRANSFER###` once the agent has clearly said it is handing the caller to a human, and
`###OUT-OF-SCOPE###` when the scenario provides nothing further to go on. Our sim judges ONE free-text
criterion instead of emitting tokens, so `convert_tau2_airline.py::USER_STOP_CRITERIA` folds all three
into one string. This is the only field in the cast that is a rewrite rather than a transcription --
in particular, treating a settled refusal as a resolved outcome is our reading, needed because 7 of
the 50 cases are correctly handled BY refusing, where the framework's default criterion ("the user
task has been successfully completed") never becomes true and the sim would argue until the ceiling.
That matters now that a truncated conversation scores 0 (see `eval.py::task_success`).

`task_success` and `policy_compliance` (`tasks/airline_rebooking/eval.py`) are the per-data-point
eval functions that score against this -- see that file's docstrings for exactly what each measures
(and, for `task_success`, why the DB half of the tau2 reward isn't measurable yet).
