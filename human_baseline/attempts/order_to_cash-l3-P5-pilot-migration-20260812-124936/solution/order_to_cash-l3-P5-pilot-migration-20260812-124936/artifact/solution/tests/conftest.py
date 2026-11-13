"""Shared pytest fixtures and builders for the agent tests.

The suite runs fully offline. DB-backed tests use the ``db_conn`` fixture, which
skips them when ``DATABASE_URL`` is not set (i.e. outside the workspace container),
so the pure-logic tests still run anywhere.
"""
from __future__ import annotations

import os

import pytest

from solution.agent.record import ExtractionError, InvoiceRecord


@pytest.fixture
def db_conn():
    """Yield a live psycopg2 connection, or skip if no DB is configured.

    Skips (rather than fails) when ``DATABASE_URL`` is unset so the offline suite
    still runs; inside the workspace container the DB-backed tests execute for real.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set -- DB-backed test skipped (run in container)")
    from solution.tools.db import get_connection

    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def make_record(**overrides) -> InvoiceRecord:
    """Build an InvoiceRecord with sensible defaults, overridable per field."""
    fields = {
        "invoice_number": "INV-0001",
        "duplicate_of": None,
        "po_line": {"po_line_id": 1},
    }
    fields.update(overrides)
    return InvoiceRecord(**fields)


def make_extraction_record(detail: str = "invoice INV-0001 not found in the database") -> InvoiceRecord:
    """Build an InvoiceRecord carrying an extraction error."""
    return InvoiceRecord(
        invoice_number="INV-0001",
        extraction_error=ExtractionError(kind="invoice_not_found", detail=detail),
    )
