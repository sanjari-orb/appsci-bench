"""Step-A output types: the assembled record and the captured extraction error.

``assemble_record`` turns the raw dicts returned by the DB tools into a single,
type-normalised ``InvoiceRecord`` the rules can reason over. Any failure to gather
data is captured as an ``ExtractionError`` (not raised) so it can be surfaced to
the user as a flag reason -- an invoice we cannot verify is itself worth flagging.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# Recognised extraction-failure kinds. Kept as plain strings for simplicity.
INVOICE_NOT_FOUND = "invoice_not_found"
PO_LINE_MISSING = "po_line_missing"
MISSING_FIELD = "missing_field"
DB_ERROR = "db_error"


@dataclass
class ExtractionError:
    """A captured (not raised) failure while gathering data for one review.

    Attributes:
        kind: one of ``INVOICE_NOT_FOUND``, ``PO_LINE_MISSING``, ``DB_ERROR``.
        detail: a human-readable description, passed through to the reason layer.
    """
    kind: str
    detail: str


@dataclass
class InvoiceRecord:
    """Normalised facts for one invoice review (the result of Step A: gather).

    Numeric DECIMAL columns from psycopg2 arrive as ``decimal.Decimal``; they are
    converted to ``float`` here so nothing downstream (comparisons, ``json.dumps``)
    has to handle ``Decimal``.
    """
    invoice_number: str
    supplier_id: int | None = None
    supplier_name: str | None = None
    item_id: int | None = None
    item_number: str | None = None
    invoiced_qty: float | None = None
    unit_price: float | None = None
    amount: float | None = None
    extra_fees: list = field(default_factory=list)
    duplicate_of: str | None = None
    po_line: dict[str, Any] | None = None
    agreed_unit_price: float | None = None
    allowed_fees: list = field(default_factory=list)
    extraction_error: ExtractionError | None = None


def _to_float(value: Any) -> float | None:
    """Coerce a psycopg2 Decimal (or numeric) to float; pass through ``None``."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _extract_po_line(joined_row: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the ``po_*`` columns out of a join row into a plain PO-line dict.

    Returns ``None`` when the LEFT JOIN found no PO line (all ``po_*`` are None).
    """
    po = {
        key[len("po_"):]: joined_row[key]
        for key in joined_row
        if key.startswith("po_")
    }
    if all(value is None for value in po.values()):
        return None
    # Normalise the numeric PO-line fields we may compare against later.
    for numeric_key in ("quantity", "unit_price", "received_qty"):
        if numeric_key in po:
            po[numeric_key] = _to_float(po[numeric_key])
    return po


def assemble_record(
    invoice_number: str,
    invoice_row: dict[str, Any] | None,
    joined_row: dict[str, Any] | None,
    contract_row: dict[str, Any] | None = None,
) -> InvoiceRecord:
    """Combine the DB-tool outputs into one normalised ``InvoiceRecord``.

    Args:
        invoice_number: the invoice PK being reviewed (used for the record even when
            the lookup failed).
        invoice_row: result of ``get_invoice_by_invoice_number`` (dict or ``None``).
        joined_row: result of ``join_invoice_po_line`` (dict or ``None``).
        contract_row: result of ``get_contract_price`` (dict or ``None``). ``None``
            means no contract covers this supplier/item -- the ``agreed_unit_price``
            stays ``None`` and price checks fall back to the PO price alone.

    Returns:
        An ``InvoiceRecord``. If ``invoice_row`` is ``None`` the record carries an
        ``INVOICE_NOT_FOUND`` extraction error; if the invoice exists but has no PO
        line, a ``PO_LINE_MISSING`` error is recorded; if a field needed to verify
        the invoice (qty, price, amount, or a present PO line's qty/price) is
        ``None``, a ``MISSING_FIELD`` error is recorded so it is flagged for review.
    """
    if invoice_row is None:
        return InvoiceRecord(
            invoice_number=invoice_number,
            extraction_error=ExtractionError(
                kind=INVOICE_NOT_FOUND,
                detail=f"invoice {invoice_number} not found in the database",
            ),
        )

    po_line = _extract_po_line(joined_row) if joined_row else None
    extraction_error = None
    if po_line is None:
        extraction_error = ExtractionError(
            kind=PO_LINE_MISSING,
            detail=f"no PO line found for invoice {invoice_number} "
            f"(po_line_id={invoice_row.get('po_line_id')})",
        )

    extra_fees = invoice_row.get("extra_fees") or []
    if not isinstance(extra_fees, list):
        extra_fees = []

    agreed_unit_price = (
        _to_float(contract_row.get("agreed_unit_price")) if contract_row else None
    )

    # Fee labels the contract permits; an empty list (or no contract) means no fees
    # are allowed, so any invoice fee is off-contract.
    allowed_fees = contract_row.get("allowed_fees") if contract_row else None
    if not isinstance(allowed_fees, list):
        allowed_fees = []

    invoiced_qty = _to_float(invoice_row.get("invoiced_qty"))
    unit_price = _to_float(invoice_row.get("unit_price"))
    amount = _to_float(invoice_row.get("amount"))

    # A null in any field needed to verify the invoice means we could not check it;
    # flag it for manual review rather than passing it as clean. The PO-line fields
    # are only required when a PO line exists (a wholly missing PO line is already
    # reported as PO_LINE_MISSING above and takes precedence).
    if extraction_error is None:
        required = {
            "invoiced_qty": invoiced_qty,
            "unit_price": unit_price,
            "amount": amount,
            "po_quantity": po_line.get("quantity") if po_line else 0,
            "po_unit_price": po_line.get("unit_price") if po_line else 0,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            extraction_error = ExtractionError(
                kind=MISSING_FIELD,
                detail=f"invoice {invoice_number} is missing required field(s): "
                f"{', '.join(missing)}",
            )

    return InvoiceRecord(
        invoice_number=invoice_row.get("invoice_number", invoice_number),
        supplier_id=invoice_row.get("supplier_id"),
        supplier_name=invoice_row.get("supplier_name"),
        item_id=invoice_row.get("item_id"),
        item_number=invoice_row.get("item_number"),
        invoiced_qty=invoiced_qty,
        unit_price=unit_price,
        amount=amount,
        extra_fees=extra_fees,
        duplicate_of=invoice_row.get("duplicate_of"),
        po_line=po_line,
        agreed_unit_price=agreed_unit_price,
        allowed_fees=allowed_fees,
        extraction_error=extraction_error,
    )
