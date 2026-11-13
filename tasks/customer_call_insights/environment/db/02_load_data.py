#!/usr/bin/env python3
"""Load the fixture transcripts into the `calls` table.

Run after 01_schema.sql. Reads only unannotated transcripts.
"""
import json
import os

FIXTURE_PATH = "/fixture/transcripts.jsonl"


def load_data(cursor):
    """Insert one row per call. Returns the number of records read."""
    count = 0
    with open(FIXTURE_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            cursor.execute(
                "INSERT INTO calls (call_id, account_id, account_name, occurred_at, transcript) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    rec["call_id"],
                    rec["account_id"],
                    rec["account_name"],
                    rec["occurred_at"],
                    rec["transcript"],
                ),
            )
            count += 1
    return count


if __name__ == "__main__":
    import psycopg2

    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "calls"),
        user=os.environ.get("POSTGRES_USER", "sandbox"),
        password=os.environ.get("POSTGRES_PASSWORD", "sandbox"),
    )
    try:
        with conn.cursor() as cursor:
            read = load_data(cursor)
        conn.commit()
        print(f"loaded {read} call(s) into the calls table")
    finally:
        conn.close()
