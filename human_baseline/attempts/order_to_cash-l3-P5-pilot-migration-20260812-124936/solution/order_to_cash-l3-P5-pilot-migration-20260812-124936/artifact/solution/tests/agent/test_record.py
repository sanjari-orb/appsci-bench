"""Tests for assemble_record's normalisation and missing-field flagging."""
from __future__ import annotations

from solution.agent.record import (
    INVOICE_NOT_FOUND,
    MISSING_FIELD,
    PO_LINE_MISSING,
    assemble_record,
)


def _invoice(**overrides):
    row = {
        "invoice_number": "INV-0001",
        "supplier_id": 1,
        "item_id": 1001,
        "invoiced_qty": 10,
        "unit_price": 5.0,
        "amount": 50.0,
        "duplicate_of": None,
        "extra_fees": [],
        "po_line_id": 1,
    }
    row.update(overrides)
    return row


def _joined(**overrides):
    row = {
        "invoice_number": "INV-0001",
        "po_po_line_id": 1,
        "po_quantity": 10,
        "po_unit_price": 5.0,
        "po_received_qty": 0,
    }
    row.update(overrides)
    return row


def test_missing_invoice_row_flags_not_found():
    record = assemble_record("INV-0001", None, None)
    assert record.extraction_error is not None
    assert record.extraction_error.kind == INVOICE_NOT_FOUND


def test_missing_po_line_flags_po_missing():
    record = assemble_record("INV-0001", _invoice(), None)
    assert record.extraction_error is not None
    assert record.extraction_error.kind == PO_LINE_MISSING


def test_null_invoice_amount_flags_missing_field():
    """A null in a necessary field (amount) is flagged for review, per requirement."""
    record = assemble_record("INV-0001", _invoice(amount=None), _joined())
    assert record.extraction_error is not None
    assert record.extraction_error.kind == MISSING_FIELD
    assert "amount" in record.extraction_error.detail


def test_null_po_price_flags_missing_field():
    """A null PO unit_price (PO line present) is a missing necessary field."""
    record = assemble_record("INV-0001", _invoice(), _joined(po_unit_price=None))
    assert record.extraction_error is not None
    assert record.extraction_error.kind == MISSING_FIELD
    assert "po_unit_price" in record.extraction_error.detail


def test_complete_record_has_no_extraction_error():
    record = assemble_record("INV-0001", _invoice(), _joined())
    assert record.extraction_error is None
    assert record.unit_price == 5.0
    assert record.po_line["unit_price"] == 5.0


def test_contract_row_populates_agreed_price():
    record = assemble_record(
        "INV-0001", _invoice(), _joined(), {"agreed_unit_price": 42.5}
    )
    assert record.agreed_unit_price == 42.5
