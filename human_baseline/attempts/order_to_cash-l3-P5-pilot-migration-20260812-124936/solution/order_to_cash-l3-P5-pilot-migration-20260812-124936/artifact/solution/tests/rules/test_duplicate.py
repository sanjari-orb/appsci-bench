"""Tests for the DUPLICATE rule (pure, offline -- the core detection coverage)."""
from __future__ import annotations

from solution.rules import duplicate
from solution.tests.conftest import make_record, make_extraction_record


def test_duplicate_of_set_is_flagged():
    """An invoice with duplicate_of set is flagged as DUPLICATE."""
    record = make_record(invoice_number="INV-3046-DUP", duplicate_of="INV-3046",
                         po_line={"po_line_id": 1350})
    decision = duplicate.check(record)
    assert decision.flagged is True
    assert decision.category == "DUPLICATE"
    assert decision.facts["duplicate_of"] == "INV-3046"
    assert decision.facts["po_line_id"] == 1350


def test_duplicate_of_none_is_not_flagged():
    """An invoice with no duplicate_of is not flagged."""
    record = make_record(duplicate_of=None)
    decision = duplicate.check(record)
    assert decision.flagged is False
    assert decision.category is None


def test_extraction_error_is_flagged():
    """An unverifiable invoice (extraction error) is flagged EXTRACTION_ERROR."""
    record = make_extraction_record()
    decision = duplicate.check(record)
    assert decision.flagged is True
    assert decision.category == "EXTRACTION_ERROR"
    assert "not found" in decision.facts["error_detail"]
