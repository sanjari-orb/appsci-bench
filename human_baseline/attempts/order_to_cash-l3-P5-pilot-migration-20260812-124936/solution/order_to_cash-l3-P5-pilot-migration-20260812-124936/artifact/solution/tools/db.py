"""Shared Postgres connection helper for the DB tools.

Mirrors the connection pattern in ``checks/check_db_connection.py``: read the
``DATABASE_URL`` env var (set by docker-compose in the workspace container) and
open a psycopg2 connection. Callers own closing the connection.
"""
from __future__ import annotations

import os
from typing import Any


def get_connection():
    """Open a psycopg2 connection using ``DATABASE_URL`` from the environment.

    ``psycopg2`` is imported lazily here (not at module top) so that importing the
    agent/detector for offline unit tests does not require the driver to be
    installed -- only actually opening a connection does.

    Returns:
        A live ``psycopg2`` connection. The caller is responsible for closing it.

    Raises:
        RuntimeError: if ``DATABASE_URL`` is not set (i.e. not running inside the
            workspace container -- see ``run_setup.sh``).
    """
    import psycopg2

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set -- run inside the workspace container (see run_setup.sh)"
        )
    return psycopg2.connect(database_url)


def row_to_dict(cursor, row) -> dict[str, Any] | None:
    """Convert a single psycopg2 result row into a column-name -> value dict.

    Centralises tuple->dict conversion so tools return dicts rather than positional
    tuples. Type normalisation (e.g. Decimal -> float) is deferred to
    ``agent.record.assemble_record``.

    Args:
        cursor: the cursor the query was executed on (provides ``description``).
        row: a single row tuple from ``cursor.fetchone()``, or ``None``.

    Returns:
        A dict keyed by column name, or ``None`` if ``row`` is ``None``.
    """
    if row is None:
        return None
    columns = [col.name for col in cursor.description]
    return dict(zip(columns, row))
