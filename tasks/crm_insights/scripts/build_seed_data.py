#!/usr/bin/env python3
"""Build crm_insights' build-time seed data: export CRMArena's real snapshot
(.ai-work/datasets/crmarena/crmarena_data.db, a byte-for-byte copy of CRMArena's own checked-in
local_data/crmarena_data.db -- see PROVENANCE.md) to JSONL fixtures for the Postgres environment.

Unlike compliance_review, there is no build/eval disjointness step here: the CRM data has no
hidden answer column (the answer is a computed aggregate the agent must derive itself), so the
full real snapshot is build-time-safe -- see PROVENANCE.md ("Why there's no disjoint split").

Usage:
    python3 build_seed_data.py --db .ai-work/datasets/crmarena/crmarena_data.db
"""
import argparse
import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK_DIR = HERE.parent
DEFAULT_DB = TASK_DIR.parents[1] / ".ai-work/datasets/crmarena/crmarena_data.db"

TABLES = ["Case", "Account", "User", "CaseHistory__c", "Order", "OrderItem", "Issue__c", "Product2"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    fixture_dir = TASK_DIR / "seed_data/fixture"
    fixture_dir.mkdir(parents=True, exist_ok=True)

    for table in TABLES:
        rows = [dict(r) for r in con.execute(f'SELECT * FROM "{table}"')]
        out_path = fixture_dir / f"{table.lower().replace('__c', '')}.jsonl"
        with out_path.open("w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        print(f"wrote {len(rows)} rows -> {out_path}")


if __name__ == "__main__":
    main()
