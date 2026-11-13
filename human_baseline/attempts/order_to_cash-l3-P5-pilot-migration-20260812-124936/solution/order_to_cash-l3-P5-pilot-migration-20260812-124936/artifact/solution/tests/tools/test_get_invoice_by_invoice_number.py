"""DB-backed tests for get_invoice_by_invoice_number (skipped without DATABASE_URL)."""
from __future__ import annotations

from solution.tools.get_invoice_by_invoice_number import get_invoice_by_invoice_number


def test_known_duplicate_invoice(db_conn):
    """A known duplicate example returns a row whose duplicate_of points at the original."""
    row = get_invoice_by_invoice_number(db_conn, "INV-3046-DUP")
    assert row is not None
    assert row["duplicate_of"] == "INV-3046"


def test_missing_invoice_returns_none(db_conn):
    """A bogus invoice number returns None."""
    assert get_invoice_by_invoice_number(db_conn, "INV-DOES-NOT-EXIST") is None
