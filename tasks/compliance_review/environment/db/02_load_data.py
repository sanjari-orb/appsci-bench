#!/usr/bin/env python3
"""Load fixture contracts.jsonl into the compliance review database.

Run after 01_schema.sql has created the tables.
"""
import json
import os


def load_data(cursor):
    fixture_path = "/fixture/contracts.jsonl"
    with open(fixture_path, "r") as f:
        for line in f:
            rec = json.loads(line)
            cursor.execute(
                "INSERT INTO contracts (contract_id, title, full_text) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (rec["contract_id"], rec["title"], rec["full_text"]),
            )


if __name__ == "__main__":
    import psycopg2

    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "compliance"),
        user=os.environ.get("POSTGRES_USER", "sandbox"),
        password=os.environ.get("POSTGRES_PASSWORD", "sandbox"),
    )
    try:
        with conn.cursor() as cursor:
            load_data(cursor)
        conn.commit()
        print("Data loaded successfully")
    finally:
        conn.close()
