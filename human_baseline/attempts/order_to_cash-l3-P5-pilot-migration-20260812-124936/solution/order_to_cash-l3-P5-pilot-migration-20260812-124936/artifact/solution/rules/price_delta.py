"""PRICE_DELTA rule: flag material differences between the invoice unit price and
the PO line's agreed unit price.

The invoice's ``unit_price`` is compared against the PO line's ``unit_price`` -- the
price this purchase order actually agreed. The dollar impact is
``|invoice_price - po_price| * invoiced_qty``; the invoice is flagged when that
impact exceeds the $500 materiality bar (README: "over $500 total impact").
Sub-$500 variances and trivial rounding are left unflagged.

The contract ``agreed_unit_price`` is carried in the facts for context but does NOT
drive the flag: in this data it is a standing/list price that legitimately differs
from the per-PO negotiated price even on clean invoices, so comparing against it
would flag every invoice. The PO line price is the correct per-transaction
reference.

All the raw numbers are packed into ``facts`` so the reason layer can state them
plainly -- the LLM is never asked to do arithmetic.
"""
from __future__ import annotations

from . import Decision

CATEGORY = "PRICE_DELTA"

# Materiality bar. Impacts at or below this are not flagged (README: "over $500").
MATERIALITY_USD = 500.0


def check(record) -> Decision:
    """Decide whether an invoice's unit price differs materially from the PO price.

    Compares ``record.unit_price`` against the PO-line price; the impact is the price
    gap times ``invoiced_qty``. Flags when the impact exceeds ``MATERIALITY_USD``.

    Extraction errors are handled by an earlier rule, so this rule assumes a gathered
    record. If the numbers needed for the math are missing (no PO price, or invoice
    price/qty is ``None``) it cannot compute an impact and returns not-flagged --
    another rule or the extraction path is responsible for that case.

    Args:
        record: the assembled ``InvoiceRecord`` for one invoice.

    Returns:
        A ``Decision``. ``facts`` carries every extracted number (invoice qty/price/
        amount, PO qty/received_qty/price, contract price for context, and the
        computed impact) so the reason layer can spell them out without arithmetic.
    """
    inv_price = record.unit_price
    qty = record.invoiced_qty
    po = record.po_line or {}
    po_price = po.get("unit_price")
    agreed_price = record.agreed_unit_price

    # Facts are always built (flagged or not) so the reason can state the numbers.
    facts = {
        "invoice_number": record.invoice_number,
        "invoiced_qty": qty,
        "invoice_unit_price": inv_price,
        "invoice_amount": record.amount,
        "po_quantity": po.get("quantity"),
        "po_received_qty": po.get("received_qty"),
        "po_unit_price": po_price,
        "contract_agreed_unit_price": agreed_price,
        "threshold": MATERIALITY_USD,
    }

    # Need the invoice price, quantity, and PO price to compute an impact.
    if inv_price is None or qty is None or po_price is None:
        return Decision(flagged=False, category=None, facts=facts)

    # Dollar impact = price gap vs the PO line * invoiced quantity.
    impact = abs(inv_price - po_price) * qty
    facts["impact_vs_po"] = impact
    facts["max_impact"] = impact

    if impact > MATERIALITY_USD:
        return Decision(flagged=True, category=CATEGORY, facts=facts)

    return Decision(flagged=False, category=None, facts=facts)
