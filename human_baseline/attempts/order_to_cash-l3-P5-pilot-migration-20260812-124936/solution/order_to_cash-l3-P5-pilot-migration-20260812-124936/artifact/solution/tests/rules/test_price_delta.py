"""Tests for the PRICE_DELTA rule (offline: hand-built records)."""
from __future__ import annotations

from solution.rules import price_delta
from solution.tests.conftest import make_record


def _record(inv_price, qty, po_price, agreed=None, amount=None):
    """Build a record with the fields the price rule reads."""
    return make_record(
        unit_price=inv_price,
        invoiced_qty=qty,
        amount=amount,
        agreed_unit_price=agreed,
        po_line={"po_line_id": 1, "unit_price": po_price, "quantity": qty, "received_qty": 0},
    )


def test_material_po_delta_is_flagged():
    """A unit-price gap whose qty-scaled impact is > $500 flags as PRICE_DELTA."""
    # |148.14 - 137.08| * 287 = 3174.22 -- the labelled INV-7984 case.
    record = _record(inv_price=148.14, qty=287, po_price=137.08)
    decision = price_delta.check(record)
    assert decision.flagged is True
    assert decision.category == "PRICE_DELTA"
    assert round(decision.facts["impact_vs_po"], 2) == 3174.22


def test_subthreshold_delta_is_not_flagged():
    """A price gap under the $500 impact bar is not flagged (INV-4071 case)."""
    # |260.51 - 260.46| * 443 = 22.15 -- below $500.
    record = _record(inv_price=260.51, qty=443, po_price=260.46)
    decision = price_delta.check(record)
    assert decision.flagged is False
    assert decision.category is None


def test_matching_price_is_not_flagged():
    """Invoice price equal to PO price yields zero impact, not flagged."""
    record = _record(inv_price=176.2, qty=54, po_price=176.2)
    decision = price_delta.check(record)
    assert decision.flagged is False


def test_contract_price_does_not_drive_flag():
    """A contract price far from the (matching) PO/invoice price must NOT flag.

    Guards the 'flags everything' regression: agreed_unit_price is context only.
    """
    record = _record(inv_price=176.2, qty=54, po_price=176.2, agreed=384.48)
    decision = price_delta.check(record)
    assert decision.flagged is False


def test_missing_po_price_does_not_flag_here():
    """With no PO price the rule cannot compute an impact and defers (no flag).

    The missing-field flag is raised upstream in assemble_record, not here.
    """
    record = _record(inv_price=100.0, qty=10, po_price=None)
    decision = price_delta.check(record)
    assert decision.flagged is False
