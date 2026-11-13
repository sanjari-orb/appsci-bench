# Airline — upstream provenance (reference only)

Verbatim source material from **tau2-bench** (https://github.com/sierra-research/tau2-bench), kept for
audit/reproducibility. These files are NOT read at task run time.

| File | What it is |
|------|------------|
| `tasks.json` | The 50 tau2 airline tasks, verbatim from upstream `data/tau2/domains/airline/tasks.json`. The offline conversion source for `tasks/airline_rebooking/eval_data/samples.jsonl` (see `scripts/convert_tau2_airline.py`). |

Upstream release: **v1.0.1** (commit `fc0055dc`) — see `TAU2_COMMIT.txt`.
`tasks.json` here is byte-identical to upstream's at that release; the converted eval samples are checked
against this copy by `tests/tasks/airline_rebooking/test_sample_fidelity.py`.
Full field mapping (tau2 → our eval samples): `AIRLINE_PROVENANCE.md`.
