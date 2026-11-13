"""OFF_CONTRACT_ITEM rule: flag invoices whose item is not on the supplier contract.

The contract table holds one row per authorised ``(supplier_id, item_id)`` pair.
The gather step looks the contract up by the invoice's supplier and item; when no
row is found the invoice's ``agreed_unit_price`` is left ``None`` -- meaning the
supplier has no contract covering that item, i.e. it is off-contract.

Off-contract items are flagged regardless of amount (README: "flag regardless of
amount"). No price or quantity logic is applied here -- that is other rules' job.

The message states the invoice's claimed item plainly so the reviewer sees exactly
what is not covered.
"""
from __future__ import annotations

from . import Decision

CATEGORY = "OFF_CONTRACT_ITEM"


def check(record) -> Decision:
    """Decide whether the invoice's item is covered by the supplier's contract.

    The contract is keyed by ``(supplier_id, item_id)`` and its ``agreed_unit_price``
    is ``NOT NULL``; therefore ``record.agreed_unit_price is None`` means the gather
    step found no contract row for this supplier/item -- the item is off-contract.

    Args:
        record: the assembled ``InvoiceRecord`` for one invoice.

    Returns:
        A ``Decision``. ``facts`` carries the invoice's supplier and claimed item so
        the reason layer can state which item is not covered.
    """
    facts = {
        "invoice_number": record.invoice_number,
        "supplier_id": record.supplier_id,
        "item_id": record.item_id,
        "item_number": record.item_number,
    }

    # A missing contract row (agreed_unit_price None) means the supplier's contract
    # does not cover this item -> off-contract.
    if record.agreed_unit_price is None:
        return Decision(flagged=True, category=CATEGORY, facts=facts)

    return Decision(flagged=False, category=None, facts=facts)
