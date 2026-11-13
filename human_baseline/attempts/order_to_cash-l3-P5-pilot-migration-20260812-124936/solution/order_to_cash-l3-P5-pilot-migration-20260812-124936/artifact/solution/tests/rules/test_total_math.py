"""Tests for the TOTAL_MATH rule (offline: hand-built records)."""
from __future__ import annotations

from solution.rules import total_math
from solution.tests.conftest import make_record


def _record(qty, unit_price, amount, extra_fees=None):
    """Build a record with the fields the total-math rule reads."""
    return make_record(
        invoiced_qty=qty,
        unit_price=unit_price,
        amount=amount,
        extra_fees=extra_fees or [],
    )


def test_material_total_mismatch_is_flagged():
    """Stated amount exceeding qty*price by > $500 flags as TOTAL_MATH (INV-3704)."""
    # 325 * 158.55 = 51528.75; invoice amount 52760.19 -> impact $1231.44 > $500.
    record = _record(qty=325, unit_price=158.55, amount=52760.19)
    decision = total_math.check(record)
    assert decision.flagged is True
    assert decision.category == "TOTAL_MATH"
    assert round(decision.facts["impact_vs_computed"], 2) == 1231.44


def test_matching_total_is_not_flagged():
    """Amount equal to qty*price is not flagged."""
    record = _record(qty=54, unit_price=176.2, amount=54 * 176.2)
    decision = total_math.check(record)
    assert decision.flagged is False
    assert decision.category is None


def test_fees_are_included_in_computed_total():
    """A fee amount is added to qty*price before comparing against the total."""
    record = _record(qty=8, unit_price=483.45, amount=8 * 483.45 + 363.46,
                      extra_fees=[{"label": "fuel surcharge", "amount": 363.46}])
    decision = total_math.check(record)
    assert decision.flagged is False


def test_missing_amount_does_not_flag_here():
    """With no invoice amount the rule cannot compute an impact and defers."""
    record = _record(qty=10, unit_price=100.0, amount=None)
    decision = total_math.check(record)
    assert decision.flagged is False
