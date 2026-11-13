"""Tool: fetch the contract row (agreed price + allowed fees) for supplier+item."""
from __future__ import annotations

from typing import Any

from .db import row_to_dict


def get_contract_price(conn, supplier_id: int, item_id: int) -> dict[str, Any] | None:
    """Fetch the contract row for a ``(supplier_id, item_id)`` pair.

    The contract carries the ``agreed_unit_price`` the invoice's unit price is
    checked against (PRICE_DELTA), plus ``allowed_fees`` (used by later fault
    types). One row per authorised item; a missing row means the item is
    off-contract for that supplier.

    Args:
        conn: an open psycopg2 connection.
        supplier_id: the invoice's supplier id.
        item_id: the invoice's item id.

    Returns:
        The contract row as a column-name -> value dict, or ``None`` if there is no
        contract for that supplier/item (i.e. the item is off-contract).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM contracts WHERE supplier_id = %s AND item_id = %s",
            (supplier_id, item_id),
        )
        return row_to_dict(cur, cur.fetchone())
