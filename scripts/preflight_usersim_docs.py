#!/usr/bin/env python3
"""Preflight a batch run's sim-only document declarations, BEFORE any task starts (issue #76, D3).

Why before the loop: a declaration error is an authoring mistake, and finding it 40 minutes into a
batch -- after the first task has already produced results -- wastes a run and buries the cause. This
reports every bad task up front so they can be fixed in one pass.

Why it does not abort the batch: a bad declaration affects ONE task. Rows for other tasks are
unaffected and must still run. So this exits 0 even when tasks fail validation; the caller reads the
FAIL lines and skips just those rows (see scripts/run_tasks.sh). Exit 2 is reserved for "the preflight
itself could not run", which IS worth aborting on.

Usage:
    python scripts/preflight_usersim_docs.py <task_id> [<task_id> ...]
    python scripts/preflight_usersim_docs.py --all

Output, one line per task, parseable by the shell:
    OK   <task_id>
    WARN <task_id>: <message>
    FAIL <task_id>: <message>
"""
from __future__ import annotations

import argparse
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from appsci_bench.core.spec import TASKS_DIR  # noqa: E402
from appsci_bench.core.usersim_docs import validate_many  # noqa: E402


def _all_task_ids() -> list[str]:
    return sorted(
        p.name for p in TASKS_DIR.iterdir()
        if p.is_dir() and not p.name.startswith(("_", "."))
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tasks", nargs="*", help="task ids to check")
    ap.add_argument("--all", action="store_true", help="check every task in tasks/")
    args = ap.parse_args()

    task_ids = _all_task_ids() if args.all else list(dict.fromkeys(args.tasks))
    if not task_ids:
        print("preflight: no tasks given (pass task ids or --all)", file=sys.stderr)
        return 2

    try:
        results = validate_many(task_ids)
    except Exception as exc:  # noqa: BLE001 -- the preflight itself broke; that IS a blocker
        print(f"preflight: could not run validation: {exc}", file=sys.stderr)
        return 2

    failed = 0
    for result in results:
        for warning in result.warnings:
            print(f"WARN {result.task_id}: {warning}")
        if result.ok:
            print(f"OK   {result.task_id}")
        else:
            failed += 1
            for err in result.errors:
                print(f"FAIL {result.task_id}: {err}")

    if failed:
        print(
            f"preflight: {failed} of {len(results)} task(s) have invalid user-sim document "
            "declarations; those rows will be skipped. Other tasks are unaffected.",
            file=sys.stderr,
        )
    # Exit 0 regardless: per-task failures are the caller's to skip, not a reason to abort the batch.
    return 0


if __name__ == "__main__":
    sys.exit(main())
