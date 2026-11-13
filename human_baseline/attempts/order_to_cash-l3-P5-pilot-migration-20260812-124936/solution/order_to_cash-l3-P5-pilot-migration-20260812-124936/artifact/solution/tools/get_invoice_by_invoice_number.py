"""Tool: fetch a single invoice by its invoice_number (primary key)."""
from __future__ import annotations

from typing import Any

from .db import row_to_dict


def get_invoice_by_invoice_number(conn, invoice_number: str) -> dict[str, Any] | None:
    """Fetch one invoice row as a dict, keyed by ``invoice_number`` (the PK).

    Args:
        conn: an open psycopg2 connection.
        invoice_number: the invoice PK, e.g. ``"INV-3046-DUP"`` (the ``-DUP`` suffix
            is part of the key and must be preserved by the caller).

    Returns:
        The invoice row as a column-name -> value dict, or ``None`` if no invoice
        with that number exists. Callers treat ``None`` as an "invoice not found"
        extraction error (which becomes part of the flag reason).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM invoices WHERE invoice_number = %s",
            (invoice_number,),
        )
        return row_to_dict(cur, cur.fetchone())
