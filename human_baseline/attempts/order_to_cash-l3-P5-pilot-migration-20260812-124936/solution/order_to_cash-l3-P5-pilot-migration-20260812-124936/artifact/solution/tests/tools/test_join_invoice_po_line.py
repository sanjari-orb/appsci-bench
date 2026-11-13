"""DB-backed tests for join_invoice_po_line (skipped without DATABASE_URL)."""
from __future__ import annotations

from solution.tools.join_invoice_po_line import join_invoice_po_line


def test_join_returns_po_fields(db_conn):
    """A real invoice joins to its PO line, exposing po_* columns."""
    row = join_invoice_po_line(db_conn, "INV-3046-DUP")
    assert row is not None
    assert "po_po_line_id" in row
    # The duplicate example shares po_line_id 1350 with its PO line.
    assert row["po_po_line_id"] == 1350


def test_join_missing_invoice_returns_none(db_conn):
    """A bogus invoice number yields None (invoice absent)."""
    assert join_invoice_po_line(db_conn, "INV-DOES-NOT-EXIST") is None
