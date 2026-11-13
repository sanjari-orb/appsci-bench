"""DUPLICATE rule: flag invoices that are marked as duplicate submissions.

An invoice's ``duplicate_of`` column names the original invoice it duplicates.
When set, the invoice is a re-submission and risks a double charge, so it is
flagged regardless of amount.
"""
from __future__ import annotations

from . import Decision, EXTRACTION_ERROR

CATEGORY = "DUPLICATE"


def check(record) -> Decision:
    """Decide whether an ``InvoiceRecord`` is a duplicate submission.

    Rules, in order:
        1. If data could not be gathered (``record.extraction_error`` set), flag the
           invoice with category ``EXTRACTION_ERROR`` -- an invoice we cannot verify
           is surfaced for manual review rather than passed as clean.
        2. Otherwise flag when ``record.duplicate_of`` is not ``None`` (category
           ``DUPLICATE``).
        3. Otherwise do not flag.

    Args:
        record: the assembled ``InvoiceRecord`` for one invoice.

    Returns:
        A ``Decision``. ``facts`` carries the raw values the reason layer phrases.
    """
    if record.extraction_error is not None:
        return Decision(
            flagged=True,
            category=EXTRACTION_ERROR,
            facts={
                "error_kind": record.extraction_error.kind,
                "error_detail": record.extraction_error.detail,
                "invoice_number": record.invoice_number,
            },
        )

    if record.duplicate_of is not None:
        po_line_id = record.po_line.get("po_line_id") if record.po_line else None
        return Decision(
            flagged=True,
            category=CATEGORY,
            facts={
                "invoice_number": record.invoice_number,
                "duplicate_of": record.duplicate_of,
                "po_line_id": po_line_id,
            },
        )

    return Decision(flagged=False, category=None, facts={"invoice_number": record.invoice_number})
