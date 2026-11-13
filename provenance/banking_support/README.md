# banking_support — upstream provenance (from tau2 banking_knowledge domain; reference only)

Verbatim source material from **tau2-bench** (https://github.com/sierra-research/tau2-bench), kept for
audit/reproducibility. These files are NOT read at task run time.

| File | What it is |
|------|------------|
| `tasks.json` | The 97 tau2 banking_knowledge tasks, verbatim from upstream `data/tau2/domains/banking_knowledge/tasks.json`. The offline conversion source for `tasks/banking_support/eval_data/` (converter added in a later phase; see `scripts/convert_tau2_banking.py`). Byte-equal to upstream (verified). |

Upstream commit: see `tasks/banking_support/seed_data/TAU2_COMMIT.txt` (`3b6a28ed…`).
Full field mapping and confirmed task shape (reward basis, discoverable-tools mechanic, per-sample
initial_state, user-held write tools): `tasks/banking_support/seed_data/PROVENANCE.md`.

The knowledge corpus (698 docs) and transactional DB are materialized as the agent-facing fixture
under `tasks/banking_support/environment/` (not duplicated here).

## Refreshing
To re-vendor from a newer upstream commit: clone `sierra-research/tau2-bench`, copy
`data/tau2/domains/banking_knowledge/tasks.json` here and the corpus/DB into
`tasks/banking_support/environment/`, update `TAU2_COMMIT.txt`, and re-run the converter.
