"""Tool: fetch an invoice LEFT JOINed to its PO line, to check the purchase."""
from __future__ import annotations

from typing import Any

from .db import row_to_dict

# PO-line columns are aliased with a ``po_`` prefix so they never collide with the
# invoice's own columns (po_number, item_id, item_number, unit_price, currency, ...)
# when both are selected into one flat row.
_QUERY = """
    SELECT
        i.*,
        pl.po_line_id   AS po_po_line_id,
        pl.po_number    AS po_po_number,
        pl.item_id      AS po_item_id,
        pl.item_number  AS po_item_number,
        pl.item_uom     AS po_item_uom,
        pl.quantity     AS po_quantity,
        pl.unit_price   AS po_unit_price,
        pl.received_qty AS po_received_qty,
        pl.currency     AS po_currency
    FROM invoices i
    LEFT JOIN po_lines pl ON i.po_line_id = pl.po_line_id
    WHERE i.invoice_number = %s
"""


def join_invoice_po_line(conn, invoice_number: str) -> dict[str, Any] | None:
    """Fetch the invoice LEFT JOINed to its PO line as one flat dict.

    Uses a LEFT (not INNER) join so a missing or dangling ``po_line_id`` yields
    ``po_*`` columns set to ``None`` rather than an empty result -- the agent can
    then report "PO line missing" as an extraction issue instead of silently
    dropping the invoice.

    Args:
        conn: an open psycopg2 connection.
        invoice_number: the invoice PK.

    Returns:
        A dict with the invoice columns plus ``po_*`` PO-line columns, or ``None``
        if the invoice itself does not exist. When the invoice exists but its PO
        line does not, the ``po_*`` values are ``None``.
    """
    with conn.cursor() as cur:
        cur.execute(_QUERY, (invoice_number,))
        return row_to_dict(cur, cur.fetchone())
