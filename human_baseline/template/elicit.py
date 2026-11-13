#!/usr/bin/env python
"""Chat with a task's user-sim persona for a human-baseline attempt, logging the transcript.

Generic replacement for human_baseline/pilot/elicit.py, which wraps a now-renamed script
(scripts/chat_user_sim.py -> scripts/chat_appsci_user_sim.py, see PR #41). This version calls the
current script directly and takes task/persona/attempt as arguments instead of hardcoding
order_to_cash.

Uses pty.spawn rather than subprocess+pipe: chat_appsci_user_sim.py's REPL calls input()/print()
directly against a real terminal, and piping its stdout would break interactive line buffering. A
pty gives it a real terminal while still letting us capture everything written to it.

Run from the appsci-bench repo root (needs scripts/chat_appsci_user_sim.py + real model credentials
via the APPSCI_USERSIM_* env vars) -- NOT from the standalone workspace. This step is fine to run
against the real repo: chat_appsci_user_sim.py talks to the live user-sim service, it does not read
eval_data/ or any other answer-key material.

Usage:
    python elicit.py --task order_to_cash --persona l3 --attempt otc-l3-P5-01
"""
from __future__ import annotations

import argparse
import os
import pathlib
import pty
import sys
from datetime import datetime, timezone

TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TEMPLATE_DIR.parents[1]
ATTEMPTS_DIR = TEMPLATE_DIR.parent / "attempts"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, help="Task id under tasks/ (e.g. order_to_cash)")
    parser.add_argument("--persona", required=True, help="Persona file stem (l1/l2/l3)")
    parser.add_argument("--attempt", required=True, help="Attempt id under human_baseline/attempts/")
    parser.add_argument("--model", default=None, help="Override the user-sim's model")
    args = parser.parse_args()

    transcript_path = ATTEMPTS_DIR / args.attempt / "logs" / "transcript.md"
    if not transcript_path.parent.exists():
        print(f"ERROR: no attempt at {ATTEMPTS_DIR / args.attempt} -- run new_attempt.sh first",
              file=sys.stderr)
        return 1

    started_at = datetime.now(timezone.utc).isoformat()
    log_fh = open(transcript_path, "ab")
    log_fh.write(
        f"\n## Session started {started_at}  (task={args.task} persona={args.persona})\n\n```\n".encode()
    )
    log_fh.flush()

    def _tee(fd: int) -> bytes:
        data = os.read(fd, 1024)
        log_fh.write(data)
        log_fh.flush()
        return data

    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "chat_appsci_user_sim.py"),
        "--task", args.task, "--persona", args.persona,
    ]
    if args.model:
        cmd += ["--model", args.model]

    docs_dir = REPO_ROOT / "tasks" / args.task / "user_sim_docs"
    if docs_dir.is_dir():
        os.environ["APPSCI_USERSIM_DOCS_DIR"] = str(docs_dir)

    exit_status = pty.spawn(cmd, _tee)

    ended_at = datetime.now(timezone.utc).isoformat()
    log_fh.write(f"\n```\n\n## Session ended {ended_at}\n".encode())
    log_fh.close()
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
