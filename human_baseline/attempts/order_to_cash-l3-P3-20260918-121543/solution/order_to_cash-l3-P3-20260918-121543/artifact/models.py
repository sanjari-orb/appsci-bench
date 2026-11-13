"""Domain types, fault labels and money helpers for the invoice review service.

The fault vocabulary is a closed set of six labels (see sop/kpis.md and the
elicited output contract).  Nothing outside CHECK_ORDER may ever reach the wire.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

# Materiality bar for both money checks.  Strictly greater than this flags;
# exactly this does not.
MATERIALITY = Decimal("500")

FAULT_DUPLICATE = "DUPLICATE"
FAULT_PRICE_DELTA = "PRICE_DELTA"
FAULT_QTY_MISMATCH = "QTY_MISMATCH"
FAULT_TOTAL_MATH = "TOTAL_MATH"
FAULT_OFF_CONTRACT_ITEM = "OFF_CONTRACT_ITEM"
FAULT_OFF_CONTRACT_FEE = "OFF_CONTRACT_FEE"

# Strict check order, first failure wins.  DUPLICATE is decided by the pipeline
# before the data gates; the remaining five run check-major in rules.py.
CHECK_ORDER = (
    FAULT_DUPLICATE,
    FAULT_PRICE_DELTA,
    FAULT_QTY_MISMATCH,
    FAULT_TOTAL_MATH,
    FAULT_OFF_CONTRACT_ITEM,
    FAULT_OFF_CONTRACT_FEE,
)
FAULT_TYPES = frozenset(CHECK_ORDER)

CLEAN_REASON = (
    "invoice agrees with the purchase order on quantity, price and total; "
    "no unauthorised charges"
)

MAX_REASON_CHARS = 400


class Unreviewable(Exception):
    """The request cannot be reviewed, so no business verdict may be produced."""

    status_code = 422

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DatabaseUnavailable(Exception):
    """The database itself could not be reached or read."""

    status_code = 503

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def to_decimal(value: Any) -> Optional[Decimal]:
    """Coerce a database value to Decimal without ever going through float.

    Returns None when the value is absent or not numeric; callers treat None as
    'unevaluable' rather than as zero.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # str() first: Decimal(float) would import the binary rounding error.
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    for junk in ("$", ",", " "):
        text = text.replace(junk, "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def fmt_money(value: Decimal) -> str:
    """Render money for a reason string: -$1,250.00 (sign outside the symbol)."""
    cents = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents):,.2f}"


def fmt_qty(value: Decimal) -> str:
    """Render a quantity without trailing noise: 120, not 120.00."""
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def norm_key(value: Any) -> str:
    """Case- and whitespace-insensitive comparison key for labels and ids."""
    return str(value).strip().casefold() if value is not None else ""


@dataclass
class InvoiceLine:
    item_id: Optional[str]
    quantity: Optional[Decimal]
    unit_price: Optional[Decimal]
    line_no: Optional[int] = None
    # Foreign key to the agreed PO line.  Where the source system carries it, it is
    # the authoritative join -- more reliable than matching on item id.
    po_line_id: Optional[str] = None


@dataclass
class Fee:
    label: Optional[str]
    amount: Optional[Decimal]


@dataclass
class Invoice:
    invoice_number: str
    supplier_id: Optional[str] = None
    # A second supplier-identifying column where the source carries one.  The request
    # sentence names one supplier token; which column holds it is not guaranteed.
    supplier_name: Optional[str] = None
    po_number: Optional[str] = None
    total: Optional[Decimal] = None
    duplicate_of: Optional[str] = None
    currency: Optional[str] = None
    doc_type: Optional[str] = None
    lines: list[InvoiceLine] = field(default_factory=list)
    fees: list[Fee] = field(default_factory=list)


@dataclass
class POLine:
    po_number: Optional[str]
    item_id: Optional[str]
    quantity: Optional[Decimal]
    unit_price: Optional[Decimal]
    line_no: Optional[int] = None
    currency: Optional[str] = None
    po_line_id: Optional[str] = None


@dataclass
class Contract:
    supplier_id: str
    allowed_items: frozenset[str] = frozenset()
    allowed_fees: frozenset[str] = frozenset()
    # Kept for reporting only; the price baseline is the PO line, never this.
    agreed_prices: dict[str, Decimal] = field(default_factory=dict)
    row_count: int = 0


@dataclass
class Verdict:
    flagged: bool
    fault_type: Optional[str]
    reason: str

    def __post_init__(self) -> None:
        if self.flagged:
            if self.fault_type not in FAULT_TYPES:
                raise ValueError(f"fault_type outside the closed set: {self.fault_type!r}")
        elif self.fault_type is not None:
            raise ValueError("fault_type must be null when flagged is false")

    def to_dict(self) -> dict:
        """Exactly three keys, singular, each a single value."""
        return {
            "flagged": self.flagged,
            "fault_type": self.fault_type,
            "reason": self.reason,
        }
