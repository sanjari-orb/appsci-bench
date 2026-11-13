#!/usr/bin/env python
"""Dev-only harness -- NOT on the serving path.

Runs `build_record` over transcripts so you can eyeball the records against the KPIs
(faithful: nothing invented; complete: nothing promised dropped; strict schema).

Sources, in order:
  - a transcript piped on stdin (one call), or
  - the seeded calls in Postgres (env creds; defaults match the task sandbox: host=db).

Requires ANTHROPIC_API_KEY for the live model path; without it every record comes back empty
(the safe fallback), which still lets you check the schema/plumbing.

Usage:
  python dev_eval.py                 # read all seeded calls from Postgres
  python dev_eval.py < a_call.txt    # score a single transcript from stdin
"""
from __future__ import annotations

import json
import os
import sys

from insights import LIST_KEYS, SENTIMENTS, build_record


def _contract_ok(record: dict) -> list[str]:
    """Return a list of contract violations (empty means valid)."""
    problems: list[str] = []
    if set(record) != {"next_steps", "risks", "commitments", "sentiment"}:
        problems.append(f"keys must be exactly the four; got {sorted(record)}")
    for key in LIST_KEYS:
        val = record.get(key)
        if not isinstance(val, list) or not all(isinstance(s, str) for s in val):
            problems.append(f"{key} must be a list[str]")
    if record.get("sentiment") not in SENTIMENTS:
        problems.append(f"sentiment must be one of {SENTIMENTS}")
    return problems


def _report(label: str, transcript: str) -> None:
    record = build_record(transcript)
    problems = _contract_ok(record)
    print(f"\n===== {label} =====")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    print("contract: " + ("OK" if not problems else "INVALID -> " + "; ".join(problems)))


def _load_seeds() -> list[tuple[str, str]]:
    """Fetch (call_id, transcript) rows from Postgres. Returns [] if unreachable."""
    try:
        import psycopg2
    except ImportError:
        print("psycopg2 not installed; skipping DB seeds", file=sys.stderr)
        return []
    try:
        conn = psycopg2.connect(
            host=os.environ.get("POSTGRES_HOST", "db"),
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ.get("POSTGRES_DB", "calls"),
            user=os.environ.get("POSTGRES_USER", "sandbox"),
            password=os.environ.get("POSTGRES_PASSWORD", "sandbox"),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"could not reach Postgres ({exc}); skipping DB seeds", file=sys.stderr)
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT call_id, transcript FROM calls ORDER BY call_id")
            return list(cur.fetchall())
    finally:
        conn.close()


def main() -> None:
    if not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            _report("stdin", piped)
            return

    seeds = _load_seeds()
    if not seeds:
        print("No transcripts to score (pipe one on stdin or start the DB).", file=sys.stderr)
        sys.exit(1)
    for call_id, transcript in seeds:
        _report(call_id, transcript)


if __name__ == "__main__":
    main()
