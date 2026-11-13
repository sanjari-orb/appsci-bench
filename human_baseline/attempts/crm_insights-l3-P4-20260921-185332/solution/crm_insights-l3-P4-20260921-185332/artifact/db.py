"""Read-only access to the CRM database.

Everything the answer rests on is computed here or in analytics.py -- never by the model. The
model's job is to understand the question; the arithmetic is SQL.
"""
from __future__ import annotations

import os
import re
import threading

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("CRM_DB_URL", "postgres://sandbox:sandbox@db:5432/crm_insights")

_local = threading.local()


def _connect() -> psycopg.Connection:
    """One connection per thread -- a psycopg connection is not safe to share across threads."""
    conn = getattr(_local, "conn", None)
    if conn is None or conn.closed:
        conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
        _local.conn = conn
    return conn


def query(sql: str, params: tuple | dict | None = None, limit: int | None = None) -> list[dict]:
    """Run a SELECT and return rows as dicts, reconnecting once if the socket went away."""
    for attempt in (1, 2):
        try:
            with _connect().cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                return rows[:limit] if limit else rows
        except (psycopg.OperationalError, psycopg.InterfaceError):
            _local.conn = None
            if attempt == 2:
                raise
    return []


# ---------------------------------------------------------------- safe free-form SQL

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|"
    r"reindex|comment|call|do|set|begin|commit|rollback)\b",
    re.I,
)


def safe_select(sql: str, max_rows: int = 200) -> list[dict]:
    """Execute a single read-only SELECT/WITH statement, or raise ValueError."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("empty statement")
    if ";" in stripped:
        raise ValueError("only a single statement is allowed")
    if not re.match(r"^(select|with)\b", stripped, re.I):
        raise ValueError("only SELECT / WITH statements are allowed")
    if _FORBIDDEN.search(stripped):
        raise ValueError("statement contains a non-read-only keyword")
    return query(f"SELECT * FROM ({stripped}) AS _q LIMIT {int(max_rows)}")
