#!/usr/bin/env python
"""Run the real final scoring pass for a human-baseline attempt.

Replaces human_baseline/pilot/score_final.py's NOT_AVAILABLE placeholder. Deploys the attempt's
artifact dir and scores it against the task's FULL held-out eval set via
`appsci_bench.orchestrator.eval_artifact eval --task <id> --artifact-dir <dir>` (no --limit) --
the exact same evaluate_deployed() call the real BenchmarkOrchestrator makes after an agent's
deploy_artifact + complete, so this score is directly comparable to an agent run on the same
task/persona. Never used for mid-run checkpoints -- see checkpoint.py for that (which passes
--limit deliberately, so it never reproduces this number by accident).

Run from the appsci-bench repo root (needs appsci_bench installed + tasks/<task_id>/spec.yaml).

Usage:
    python score_final.py --task order_to_cash --attempt otc-l3-P5-01 \\
        --artifact-dir ~/Documents/baseline-work/otc-l3-P5-01/artifact
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

sys.path.insert(0, str(TEMPLATE_DIR))
from gateway_env import build_eval_subprocess_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", required=True, help="Task id under tasks/ (e.g. order_to_cash)")
    parser.add_argument("--attempt", required=True, help="Attempt id under human_baseline/attempts/")
    parser.add_argument("--artifact-dir", required=True, type=pathlib.Path,
                         help="Path to the attempt's artifact/ dir (Dockerfile + server.py)")
    args = parser.parse_args()

    attempt_dir = ATTEMPTS_DIR / args.attempt
    final_report_path = attempt_dir / "logs" / "final_report.json"
    if not attempt_dir.exists():
        print(f"ERROR: no attempt at {attempt_dir} -- run new_attempt.sh first", file=sys.stderr)
        return 1

    artifact_dir = args.artifact_dir.expanduser().resolve()
    cmd = [
        sys.executable, "-m", "appsci_bench.orchestrator.eval_artifact", "eval",
        "--task", args.task, "--artifact-dir", str(artifact_dir),   # 0 = no limit, run on full held-out eval set
    ]
    # Stream stdout/stderr live (interleaved) instead of subprocess.run's capture_output, which
    # buffers everything until the process exits -- eval_artifact.py logs generation/scoring
    # progress (e.g. "scoring: 12/50") as it runs, and capture_output was silently swallowing it
    # until the whole (potentially long) full-set run finished.
    with subprocess.Popen(cmd, cwd=REPO_ROOT, text=True, env=build_eval_subprocess_env(),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1) as proc:
        for line in proc.stdout:
            print(line, end="")
        proc.wait()
    returncode = proc.returncode

    # eval_artifact.py writes report.json/report.html/conversations/ into
    # <artifact_dir>/../eval/ (see its own docstring) -- point there rather than duplicating scores.
    eval_dir = artifact_dir.parent / "eval"
    report: dict = {}
    report_json = eval_dir / "report.json"
    if report_json.exists():
        report = json.loads(report_json.read_text())

    final_report_path.parent.mkdir(parents=True, exist_ok=True)
    final_report_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_id": args.task,
        "attempt_id": args.attempt,
        "eval_artifact_returncode": returncode,
        "final_score": report.get("final_score"),
        "per_metric": report.get("views", {}).get("per_metric"),
        "eval_dir": str(eval_dir),
    }, indent=2) + "\n")

    print(f"\nWrote {final_report_path}")
    print(f"Full report: {eval_dir / 'report.html'}")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
