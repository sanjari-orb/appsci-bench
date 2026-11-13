#!/usr/bin/env python
"""Log a real progress checkpoint (1h / 2h / 3h30 / end marks per PROTOCOL.md section 7).

Replaces human_baseline/pilot/checkpoint.py's NOT_AVAILABLE placeholder: appsci_bench.orchestrator
.eval_artifact is now fully wired, so this deploys the attempt's current artifact dir and scores it
against a small sample (--limit) via the SAME evaluate_deployed() routine the real orchestrator uses
after an agent's deploy_artifact + complete. Uses --attach against a task-env brought up once (see
README.md) so repeated checkpoints don't pay the environment-setup cost each time.

Run from the appsci-bench repo root (needs appsci_bench installed + tasks/<task_id>/spec.yaml).

Usage:
    python checkpoint.py --task order_to_cash --attempt otc-l3-P5-01 \\
        --artifact-dir ~/Documents/baseline-work/otc-l3-P5-01/artifact --mark 1h --limit 5
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TEMPLATE_DIR.parents[1]
ATTEMPTS_DIR = TEMPLATE_DIR.parent / "attempts"
VALID_MARKS = ("1h", "2h", "3h30", "end")

sys.path.insert(0, str(TEMPLATE_DIR))
from gateway_env import build_eval_subprocess_env  # noqa: E402


def _run_eval(task: str, artifact_dir: pathlib.Path, limit: int | None, attach: bool) -> dict:
    cmd = [
        sys.executable, "-m", "appsci_bench.orchestrator.eval_artifact", "eval",
        "--task", task, "--artifact-dir", str(artifact_dir),
    ]
    if limit is not None:
        cmd += ["--limit", str(limit)]
    if attach:
        cmd += ["--attach"]

    # Stream stdout/stderr live (interleaved) instead of subprocess.run's capture_output, which
    # buffers everything until the process exits -- eval_artifact.py logs generation/scoring
    # progress (e.g. "scoring: 12/50") as it runs, and capture_output was silently swallowing it
    # until the whole run finished.
    lines: list[str] = []
    with subprocess.Popen(cmd, cwd=REPO_ROOT, text=True, env=build_eval_subprocess_env(),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1) as proc:
        for line in proc.stdout:
            print(line, end="")
            lines.append(line)
        proc.wait()

    return {
        "returncode": proc.returncode,
        "stdout_tail": "".join(lines)[-4000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", required=True, help="Task id under tasks/ (e.g. order_to_cash)")
    parser.add_argument("--attempt", required=True, help="Attempt id under human_baseline/attempts/")
    parser.add_argument("--artifact-dir", required=True, type=pathlib.Path,
                         help="Path to the attempt's artifact/ dir (Dockerfile + server.py)")
    parser.add_argument("--mark", required=True, choices=VALID_MARKS)
    parser.add_argument("--limit", type=int, default=5,
                         help="Score only the first N eval samples (default 5; a checkpoint is a "
                              "progress signal, not the final score -- see score_final.py for that)")
    parser.add_argument("--attach", action="store_true",
                         help="Reuse a task-env already brought up with "
                              "'python -m appsci_bench.orchestrator.eval_artifact up --phase eval "
                              "--task <id>' instead of standing one up and tearing it down for this "
                              "checkpoint alone. It must be the EVAL phase's env -- scoring against "
                              "the build one is refused, because it holds a different set of records")
    args = parser.parse_args()

    checkpoints_path = ATTEMPTS_DIR / args.attempt / "logs" / "score_checkpoints.jsonl"
    if not checkpoints_path.parent.exists():
        print(f"ERROR: no attempt at {ATTEMPTS_DIR / args.attempt} -- run new_attempt.sh first",
              file=sys.stderr)
        return 1

    eval_result = _run_eval(args.task, args.artifact_dir.expanduser().resolve(), args.limit, args.attach)

    row = {
        "mark": args.mark,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "limit": args.limit,
        "eval_artifact_returncode": eval_result["returncode"],
    }
    with checkpoints_path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")

    print(f"\nLogged checkpoint '{args.mark}' to {checkpoints_path}")
    return eval_result["returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
