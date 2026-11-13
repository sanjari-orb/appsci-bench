"""Tests for the QTY_MISMATCH rule (offline: hand-built records)."""
from __future__ import annotations

from solution.rules import qty_mismatch
from solution.tests.conftest import make_record


def _record(invoiced_qty, po_qty, received_qty=0):
    return make_record(
        invoiced_qty=invoiced_qty,
        po_line={"po_line_id": 1, "quantity": po_qty, "received_qty": received_qty},
    )


def test_quantity_mismatch_is_flagged():
    """Invoiced qty != PO qty flags (INV-2474: 612 vs 496), no threshold."""
    decision = qty_mismatch.check(_record(invoiced_qty=612, po_qty=496))
    assert decision.flagged is True
    assert decision.category == "QTY_MISMATCH"
    assert decision.facts["qty_difference"] == 116


def test_matching_quantity_is_not_flagged():
    """Invoiced qty equal to PO qty is not flagged (INV-1696: 54 == 54)."""
    decision = qty_mismatch.check(_record(invoiced_qty=54, po_qty=54))
    assert decision.flagged is False
    assert decision.category is None


def test_small_difference_is_still_flagged():
    """There is no materiality bar: even a 1-unit gap flags."""
    decision = qty_mismatch.check(_record(invoiced_qty=101, po_qty=100))
    assert decision.flagged is True


def test_missing_quantity_does_not_flag_here():
    """With a missing PO quantity the rule defers (missing-field flag is upstream)."""
    decision = qty_mismatch.check(_record(invoiced_qty=100, po_qty=None))
    assert decision.flagged is False
