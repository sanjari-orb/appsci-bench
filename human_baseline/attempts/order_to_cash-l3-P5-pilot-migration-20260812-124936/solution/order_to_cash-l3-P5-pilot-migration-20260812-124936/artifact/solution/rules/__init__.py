"""Detection rules for the invoice-review agent.

Each rule is a module exposing ``check(record) -> Decision``. The pipeline runs
them in order and takes the first flagged decision. This iteration ships only the
DUPLICATE rule; future fault types (price, quantity, off-contract, ...) are added
here as sibling modules with the same signature.

The shared ``Decision`` type lives here so every rule returns the same shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Category used when data could not be gathered for an invoice. Not one of the
# product fault types, but a valid reason to flag for manual review.
EXTRACTION_ERROR = "EXTRACTION_ERROR"


@dataclass
class Decision:
    """The outcome of a single rule check.

    Attributes:
        flagged: whether this rule flags the invoice.
        category: the fault category when flagged (e.g. ``"DUPLICATE"``), else ``None``.
        facts: raw values the reason layer will phrase (e.g. ``duplicate_of``).
    """
    flagged: bool
    category: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)
