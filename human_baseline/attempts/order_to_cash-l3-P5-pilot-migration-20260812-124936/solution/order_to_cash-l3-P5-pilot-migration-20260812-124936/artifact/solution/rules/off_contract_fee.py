"""OFF_CONTRACT_FEE rule: flag invoices carrying a fee the contract does not allow.

The contract's ``allowed_fees`` is a JSON array of permitted fee labels; the
invoice's ``extra_fees`` is a JSON array of ``{label, amount}`` objects. Any fee
whose label is not in the contract's allowed set is off-contract and is flagged
regardless of amount (README: "flag regardless of amount").

Labels are compared case-insensitively and whitespace-trimmed so trivial
formatting differences ("Fuel Surcharge" vs "fuel surcharge") do not slip through
or misfire. All the offending labels are packed into ``facts`` for the reason.
"""
from __future__ import annotations

from . import Decision

CATEGORY = "OFF_CONTRACT_FEE"


def _normalise(label) -> str:
    """Lower-case and strip a fee label for tolerant comparison; None -> ''."""
    return str(label).strip().lower() if label is not None else ""


def check(record) -> Decision:
    """Decide whether the invoice carries a fee not permitted by the contract.

    Compares each ``extra_fees`` label against the contract's ``allowed_fees`` set
    (both normalised). Any label not in the set flags the invoice. An empty
    ``allowed_fees`` means no fees are permitted, so any fee is off-contract.

    Args:
        record: the assembled ``InvoiceRecord`` for one invoice.

    Returns:
        A ``Decision``. ``facts`` carries the offending fees (label + amount), the
        allowed set, and all invoice fees so the reason can state them plainly.
    """
    allowed = {_normalise(label) for label in (record.allowed_fees or [])}

    offending = []
    for fee in record.extra_fees or []:
        if isinstance(fee, dict):
            label = fee.get("label")
            amount = fee.get("amount")
        else:  # unexpected shape (e.g. a bare string) -- treat the whole value as label
            label, amount = fee, None
        if _normalise(label) not in allowed:
            offending.append({"label": label, "amount": amount})

    facts = {
        "invoice_number": record.invoice_number,
        "offending_fees": offending,
        "allowed_fees": list(record.allowed_fees or []),
        "invoice_fees": list(record.extra_fees or []),
    }

    if offending:
        return Decision(flagged=True, category=CATEGORY, facts=facts)

    return Decision(flagged=False, category=None, facts=facts)
