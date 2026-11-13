#!/usr/bin/env python
"""Build an isolated, task-agnostic standalone workspace for a human-baseline attempt.

INHERITS THE TASK-ENV FROM THE REAL ORCHESTRATOR INSTEAD OF REBUILDING IT:

Earlier versions of this script copied environment/, rewrote docker-compose.yml's build context,
and hand-mirrored each Dockerfile's COPY sources into the standalone dir so the task-env could be
built there. That duplicated -- badly, with a new bug per task shape -- what
`appsci_bench.task_runner.TaskRunner` already does correctly for every task in production:
`TaskRunner.start(task_id)` runs `docker compose` straight from the task's own spec.yaml paths
(repo-rooted, build context untouched) and bundles task_runner.bundle's files into the container
itself via a tar.gz (see task_runner/bundle.py, containers.py) -- there is no "relocate the compose
file" step to reproduce.

`appsci_bench.orchestrator.eval_artifact` already wraps exactly this for CLI use:
    eval_artifact.py up   --task <id>   ->  TaskRunner(tasks_dir).start(task_id)
    eval_artifact.py down --task <id>   ->  TaskRunner(tasks_dir).stop(env)
    eval_artifact.py eval --task <id> --artifact-dir <dir> [--attach]
        -> deploys <dir> as the artifact and scores it with the same evaluate_deployed() the real
           BenchmarkOrchestrator uses.

So the task-env is run FROM THE REPO (this script's caller), not copied anywhere. What actually
needs to be outside the repo -- so Claude Code / an editor rooted there cannot see eval_data/,
evaluators/, gold/<task_id>/, or answer-key columns like labels.csv -- is only the workspace the
human authors their solution in: an `artifact/` scaffold plus environment/sop/ (the same docs the
real agent gets at /sop). This script builds exactly that, nothing more.

Usage:
    python setup_standalone.py --task order_to_cash --dir ~/Documents/baseline-work/otc-01
    python setup_standalone.py --task airline_rebooking --dir ~/Documents/baseline-work/air-01

Run from the appsci-bench repo root. The printed next-steps use `eval_artifact.py up/down/eval`
(also run from the repo) to manage the task-env, and root Claude Code / your editor at --dir to
author the artifact.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = Path(__file__).resolve().parent


def _load_spec(task_id: str, tasks_dir: Path) -> dict:
    spec_path = tasks_dir / task_id / "spec.yaml"
    if not spec_path.exists():
        raise FileNotFoundError(f"spec.yaml not found at {spec_path}")
    return yaml.safe_load(spec_path.read_text())


def build_standalone(task_id: str, dest_dir: Path, tasks_dir: Path) -> None:
    if dest_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing {dest_dir}")

    _load_spec(task_id, tasks_dir)  # fail fast if the task id / spec.yaml is bad

    dest_dir.mkdir(parents=True)

    sop_dir = tasks_dir / task_id / "environment" / "sop"
    if sop_dir.exists():
        shutil.copytree(sop_dir, dest_dir / "sop")

    shutil.copytree(
        TEMPLATE_DIR / "artifact_scaffold", dest_dir / "artifact",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    print(f"Standalone workspace ready: {dest_dir}")
    print(f"  artifact/  -- author Dockerfile + server.py here (see tasks/{task_id}/spec.yaml's "
          "interface_design)")
    if sop_dir.exists():
        print("  sop/       -- agent-visible SOP docs")
    print(f"Elicit requirements any time with: python {TEMPLATE_DIR / 'elicit.py'} --task {task_id} "
          "--persona <l1|l2|l3> --attempt <attempt_id>")
    print("See human_baseline/template/README.md for the full lifecycle.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", required=True, help="Task id under tasks/ (e.g. order_to_cash)")
    parser.add_argument("--dir", required=True, type=Path, help="Destination standalone workspace dir")
    parser.add_argument("--tasks-dir", default=REPO_ROOT / "tasks", type=Path,
                         help="Path to the tasks/ directory (default: repo's tasks/)")
    args = parser.parse_args()

    dest_dir = args.dir.expanduser().resolve()
    try:
        build_standalone(args.task, dest_dir, args.tasks_dir.resolve())
    except FileExistsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
