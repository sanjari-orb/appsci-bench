"""TOTAL_MATH rule: flag invoices whose stated amount doesn't match the computed
total (qty * unit_price + fees).

The invoice's ``amount`` is compared against ``invoiced_qty * unit_price`` plus the
sum of ``extra_fees`` amounts. The invoice is flagged when the difference exceeds
the $500 materiality bar (README: "off by > $500"), matching the PRICE_DELTA bar.

All the raw numbers are packed into ``facts`` so the reason layer can state them
plainly -- the LLM is never asked to do arithmetic.
"""
from __future__ import annotations

from . import Decision

CATEGORY = "TOTAL_MATH"

# Materiality bar. Impacts at or below this are not flagged (README: "over $500").
MATERIALITY_USD = 500.0


def _fees_total(extra_fees) -> float:
    """Sum the ``amount`` of each fee dict in ``extra_fees``; ignores bad shapes."""
    total = 0.0
    for fee in extra_fees or []:
        if isinstance(fee, dict) and isinstance(fee.get("amount"), (int, float)):
            total += fee["amount"]
    return total


def check(record) -> Decision:
    """Decide whether the invoice's stated amount differs materially from the
    computed total (qty * unit_price + fees).

    Extraction errors are handled by an earlier rule, so this rule assumes a
    gathered record. If the numbers needed for the math are missing (qty, price, or
    amount is ``None``) it cannot compute a total and returns not-flagged.

    Args:
        record: the assembled ``InvoiceRecord`` for one invoice.

    Returns:
        A ``Decision``. ``facts`` carries the invoice amount, the computed total,
        and the difference so the reason layer can spell them out without
        arithmetic.
    """
    qty = record.invoiced_qty
    inv_price = record.unit_price
    amount = record.amount

    facts = {
        "invoice_number": record.invoice_number,
        "invoiced_qty": qty,
        "invoice_unit_price": inv_price,
        "invoice_amount": amount,
        "extra_fees": list(record.extra_fees or []),
        "threshold": MATERIALITY_USD,
    }

    # Need qty, price, and amount to compute the expected total.
    if qty is None or inv_price is None or amount is None:
        return Decision(flagged=False, category=None, facts=facts)

    fees_total = _fees_total(record.extra_fees)
    computed_total = qty * inv_price + fees_total
    facts["fees_total"] = fees_total
    facts["computed_total"] = computed_total

    impact = abs(amount - computed_total)
    facts["impact_vs_computed"] = impact
    facts["max_impact"] = impact

    if impact > MATERIALITY_USD:
        return Decision(flagged=True, category=CATEGORY, facts=facts)

    return Decision(flagged=False, category=None, facts=facts)
