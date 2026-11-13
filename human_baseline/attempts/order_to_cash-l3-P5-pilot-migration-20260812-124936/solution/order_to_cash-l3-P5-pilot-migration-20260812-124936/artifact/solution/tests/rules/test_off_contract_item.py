"""Tests for the OFF_CONTRACT_ITEM rule (offline: hand-built records)."""
from __future__ import annotations

from solution.rules import off_contract_item
from solution.tests.conftest import make_record


def test_item_without_contract_is_flagged():
    """No contract row (agreed_unit_price None) means off-contract (INV-3672)."""
    record = make_record(
        supplier_id=9,
        item_id=11010,
        item_number="ITEM-X11010",
        agreed_unit_price=None,
    )
    decision = off_contract_item.check(record)
    assert decision.flagged is True
    assert decision.category == "OFF_CONTRACT_ITEM"
    assert decision.facts["item_number"] == "ITEM-X11010"


def test_item_with_contract_is_not_flagged():
    """A contracted item (agreed_unit_price set) is on-contract, not flagged."""
    record = make_record(supplier_id=9, item_id=1010, agreed_unit_price=384.48)
    decision = off_contract_item.check(record)
    assert decision.flagged is False
    assert decision.category is None
