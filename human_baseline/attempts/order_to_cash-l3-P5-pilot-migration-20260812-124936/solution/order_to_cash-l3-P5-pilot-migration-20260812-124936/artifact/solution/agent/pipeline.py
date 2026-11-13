"""The embryonic invoice-review agent: gather -> decide -> explain.

Every invoice is processed the same way:
  A. gather   -- fetch the invoice and its PO line, assemble a normalised record
  B. decide   -- run the ordered rules; the first flagged rule wins
  C. explain  -- produce a plain-language reason for the decision

Data-extraction failures in step A are captured (not raised) as an
``ExtractionError`` and flow through as their own flag reason, since an invoice we
cannot verify is itself worth surfacing (requirement 4.2).
"""
from __future__ import annotations

from dataclasses import dataclass

from .record import DB_ERROR, ExtractionError, InvoiceRecord, assemble_record
from ..rules import (
    duplicate,
    off_contract_item,
    off_contract_fee,
    qty_mismatch,
    price_delta,
    total_math,
)
from ..reason import explain
from ..tools.db import get_connection
from ..tools.get_invoice_by_invoice_number import get_invoice_by_invoice_number
from ..tools.join_invoice_po_line import join_invoice_po_line
from ..tools.get_contract_price import get_contract_price

# Ordered list of rules; the first flagged decision wins.
#   duplicate         -- also surfaces extraction errors; a re-submission trumps all.
#   off_contract_item -- if the item isn't on the contract, price/qty/fee checks are
#                        moot; flag the item first.
#   off_contract_fee  -- a disallowed fee is flagged regardless of amount.
#   qty_mismatch      -- invoiced qty != PO qty (no threshold).
#   price_delta       -- material unit-price gap (> $500 impact).
#   total_math        -- material gap between stated amount and qty*price+fees.
RULES = [
    duplicate,
    off_contract_item,
    off_contract_fee,
    qty_mismatch,
    price_delta,
    total_math,
]


@dataclass
class AgentResult:
    """The agent's output for one invoice.

    ``category`` is internal (used for the classification metric); only ``flagged``
    and ``reason`` are packed into the JSON contract the evaluator reads.
    """
    flagged: bool
    category: str | None
    reason: str


def review_invoice(invoice_number: str, conn=None, llm=None) -> AgentResult:
    """Run the full gather -> decide -> explain pipeline for one invoice.

    Args:
        invoice_number: the invoice PK parsed from the review instruction.
        conn: an optional open DB connection. If ``None``, one is opened via
            ``get_connection`` and closed before returning. Injectable for tests.
        llm: an optional Anthropic-style client. If ``None`` and the decision is
            flagged, a real client is built via ``explain._get_llm()`` (which raises
            if no key/SDK is available). Tests inject a stub client here.

    Returns:
        An ``AgentResult``. Data-gathering failures become an ``EXTRACTION_ERROR``
        flag rather than an exception; the pipeline never raises for DB issues.

    Raises:
        ReasonLLMError: only when the decision is flagged and no LLM client can be
            built (no ``ANTHROPIC_API_KEY`` / SDK) and none was injected. Not-flagged
            invoices never touch the LLM, so a clean pass needs no key.
    """
    print(f"reviewing invoice {invoice_number}...")
    record = _gather(invoice_number, conn)

    # B. decide -- first flagged rule wins.
    decision = _decide(record)

    # C. explain -- the LLM is called ONLY for flagged invoices (latency: a clean
    # pass is the common case and needs no network round-trip). Not-flagged
    # invoices get the cheap deterministic reason.
    if decision.flagged:
        if llm is None:
            llm = explain._get_llm()
        reason = explain.build_reason(decision, record, llm=llm)
    else:
        reason = explain.template_reason(decision, record)

    print(f"Finished reviewing invoice {invoice_number}. Flagged: {decision.flagged}")
    return AgentResult(flagged=decision.flagged, category=decision.category, reason=reason)


def _gather(invoice_number: str, conn) -> InvoiceRecord:
    """Step A: fetch data via the tools and assemble a record.

    Any DB error is captured as a ``db_error`` ``ExtractionError`` on the record
    rather than raised, so it can drive a flag downstream.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        invoice_row = get_invoice_by_invoice_number(conn, invoice_number)
        joined_row = join_invoice_po_line(conn, invoice_number)
        # The contract is keyed by (supplier_id, item_id); we can only look it up
        # once the invoice row has given us those ids. A missing contract row is a
        # valid state (off-contract item) -- assemble_record handles ``None``.
        contract_row = None
        if invoice_row is not None:
            supplier_id = invoice_row.get("supplier_id")
            item_id = invoice_row.get("item_id")
            if supplier_id is not None and item_id is not None:
                contract_row = get_contract_price(conn, supplier_id, item_id)
        return assemble_record(invoice_number, invoice_row, joined_row, contract_row)
    except Exception as exc:  # DB/query failure -- surface as an extraction error
        return InvoiceRecord(
            invoice_number=invoice_number,
            extraction_error=ExtractionError(
                kind=DB_ERROR,
                detail=f"database error while reviewing {invoice_number}: {exc}",
            ),
        )
    finally:
        if own_conn:
            conn.close()


def _decide(record: InvoiceRecord):
    """Step B: run the ordered rules and return the first flagged decision.

    Falls back to the last rule's not-flagged decision if nothing flags.
    """
    decision = None
    for rule in RULES:
        decision = rule.check(record)
        if decision.flagged:
            return decision
    return decision
