# Banking EVAL-phase store

The task's **full transactional store** — the records the 97 held-out cases are drawn from, and the only
records the artifact is scored against. Nothing was authored here: the build phase's share is carved out
of this file and this file is unchanged, which is what makes "an artifact must score the same before and
after the carve" a usable acceptance test.

Kept beside `data/` rather than inside it, for the same reason the build phase's README is: a phase
declaration bundles its data directory into the container, and documentation about the split does not
belong in a bundled directory.

| File | What it is |
|------|------------|
| `data/db.json` | The whole store: 17 collections, 768 records, each collection `{data, notes}`. Byte-equal to the upstream tau2 `banking_knowledge` store (commit in `../../seed_data/TAU2_COMMIT.txt`). |
| `data/expected_counts.json` | Per-collection record counts, asserted by `../setup/setup.sh` after the load. Written by `scripts/generate_banking_build_share.py` from the store itself. |

## Why the path matters more than it looks

`db.json` is what the environment package resolves by default (`EVAL_STORE_PATH`), and the scoring code
loads a base environment with no path argument and replays each case's golden actions against it on the
host. If that default resolved to the build share instead, the replay would compute expected states from
8 records while the container held 768: every state check would fail, with no error, no warning and
nothing in the score to say why. Moving or renaming this file means repointing that constant in the same
change.

## It must keep everything

34 of the 97 cases depend on base-store records their own starting state does not seed — verified by
replaying every case against the full store, against the build share, and against an empty store, where
the last two give identical results. Reducing this store to seeds-only would break a third of the suite.
