"""Tests for the OFF_CONTRACT_FEE rule (offline: hand-built records)."""
from __future__ import annotations

from solution.rules import off_contract_fee
from solution.tests.conftest import make_record


def test_fee_not_in_allowed_is_flagged():
    """A fee label absent from allowed_fees flags (INV-4706: 'fuel surcharge')."""
    record = make_record(
        extra_fees=[{"label": "fuel surcharge", "amount": 363.46}],
        allowed_fees=[],
    )
    decision = off_contract_fee.check(record)
    assert decision.flagged is True
    assert decision.category == "OFF_CONTRACT_FEE"
    assert decision.facts["offending_fees"][0]["label"] == "fuel surcharge"


def test_allowed_fee_is_not_flagged():
    """A fee present in allowed_fees (case-insensitive) is not flagged."""
    record = make_record(
        extra_fees=[{"label": "Fuel Surcharge", "amount": 10.0}],
        allowed_fees=["fuel surcharge"],
    )
    decision = off_contract_fee.check(record)
    assert decision.flagged is False
    assert decision.category is None


def test_no_fees_is_not_flagged():
    """An invoice with no extra fees is never flagged by this rule."""
    record = make_record(extra_fees=[], allowed_fees=[])
    decision = off_contract_fee.check(record)
    assert decision.flagged is False


def test_mixed_fees_flags_only_disallowed():
    """Only the disallowed label is reported; the allowed one is ignored."""
    record = make_record(
        extra_fees=[
            {"label": "handling", "amount": 5.0},
            {"label": "fuel surcharge", "amount": 20.0},
        ],
        allowed_fees=["handling"],
    )
    decision = off_contract_fee.check(record)
    assert decision.flagged is True
    labels = [fee["label"] for fee in decision.facts["offending_fees"]]
    assert labels == ["fuel surcharge"]
