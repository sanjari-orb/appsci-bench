"""QTY_MISMATCH rule: flag when the invoiced quantity differs from the PO quantity.

The invoice's ``invoiced_qty`` is compared against the PO line's ordered
``quantity``. Any difference is flagged -- there is NO dollar materiality bar for a
quantity mismatch (README: "invoiced qty != PO qty (no threshold)").

The PO also records a ``received_qty`` (goods actually received). It is carried in
the facts as context for the reviewer, but the flag is driven purely by the ordered
quantity vs the invoiced quantity, matching the labelled examples (INV-2474:
invoiced 612 != PO 496, received 0).

All raw numbers are packed into ``facts`` so the reason layer states them plainly.
"""
from __future__ import annotations

from . import Decision

CATEGORY = "QTY_MISMATCH"


def check(record) -> Decision:
    """Decide whether the invoiced quantity differs from the PO ordered quantity.

    Extraction errors (including a wholly missing PO line or a null quantity) are
    handled upstream, so this rule assumes both quantities are present. If either is
    missing it cannot compare and returns not-flagged.

    Args:
        record: the assembled ``InvoiceRecord`` for one invoice.

    Returns:
        A ``Decision``. ``facts`` carries the invoiced quantity, the PO ordered and
        received quantities, and the difference so the reason needs no arithmetic.
    """
    invoiced_qty = record.invoiced_qty
    po = record.po_line or {}
    po_qty = po.get("quantity")

    facts = {
        "invoice_number": record.invoice_number,
        "invoiced_qty": invoiced_qty,
        "po_quantity": po_qty,
        "po_received_qty": po.get("received_qty"),
    }

    # Need both quantities to compare.
    if invoiced_qty is None or po_qty is None:
        return Decision(flagged=False, category=None, facts=facts)

    facts["qty_difference"] = invoiced_qty - po_qty

    if invoiced_qty != po_qty:
        return Decision(flagged=True, category=CATEGORY, facts=facts)

    return Decision(flagged=False, category=None, facts=facts)
