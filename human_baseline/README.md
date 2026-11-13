# Human Baseline

The appsci-bench team serves as the human applied-scientist baseline: team members perform the same
role as the meta-agent (elicit requirements, design, build, validate, iterate) against the same user
simulator and Oracle scoring pipeline used to grade agents. See [PROTOCOL.md](PROTOCOL.md) for the
full protocol — cross-assignment rules, cell prioritization, run procedure, and trajectory-parity
artifact requirements.

## Contents

- `PROTOCOL.md` — the full human-baseline protocol.
- `template/` — the current, task-agnostic harness. Use this for every new attempt, on any task.
- `attempts/` — per-attempt state (config, logs, reflection) created by `template/new_attempt.sh`.
  Gitignored; not committed.
- `pilot/` — **legacy**: the original reference implementation for the order_to_cash task (S2:
  Pilot), predating both the generic harness and the current artifact-deploy scoring contract. Kept
  for history; do not edit or extend it — see `template/README.md` instead.

See [template/README.md](template/README.md) to run an attempt.
