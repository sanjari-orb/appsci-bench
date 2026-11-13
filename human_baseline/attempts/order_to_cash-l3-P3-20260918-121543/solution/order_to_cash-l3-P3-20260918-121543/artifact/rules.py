"""The six invoice checks, the data gates that protect them, and reason strings.

Pure functions over the dataclasses in models.py -- no database, no HTTP.  This is
the scored logic, so it is kept free of I/O and verified independently of whatever
the live schema turns out to look like.

Ordering is CHECK-MAJOR: the outer loop is the check order, the inner loop scans
every line or fee.  The first check that any row fails sets fault_type, which
makes the reported label stable under row reordering.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from models import (
    CLEAN_REASON,
    FAULT_DUPLICATE,
    FAULT_OFF_CONTRACT_FEE,
    FAULT_OFF_CONTRACT_ITEM,
    FAULT_PRICE_DELTA,
    FAULT_QTY_MISMATCH,
    FAULT_TOTAL_MATH,
    MATERIALITY,
    MAX_REASON_CHARS,
    Contract,
    Invoice,
    InvoiceLine,
    POLine,
    Unreviewable,
    Verdict,
    fmt_money,
    fmt_qty,
    norm_key,
)

ZERO = Decimal("0")


@dataclass
class Finding:
    fault_type: str
    reason: str   # standalone sentence, used when this is the primary fault
    brief: str    # short clause, used when this is a secondary mention
    severity: Decimal = ZERO  # bigger == worse; orders rows within one check


# --------------------------------------------------------------------------
# PO line indexing
# --------------------------------------------------------------------------

@dataclass
class POIndex:
    """The PO lines an invoice can be compared against, indexed both ways."""

    by_id: dict[str, POLine]
    by_item: dict[str, list[POLine]]

    def match(self, line: InvoiceLine) -> Optional[POLine]:
        """Find the PO line an invoice line is billing against.

        Where the invoice carries a PO-line foreign key, that key is the documented
        join and is authoritative: a key that does not resolve within this PO means
        the required PO line is missing, which is unreviewable rather than an
        invitation to guess a different line by item id.

        Otherwise fall back to the item id, disambiguated by line number when a PO
        carries several lines for the same item.
        """
        if line.po_line_id:
            return self.by_id.get(norm_key(line.po_line_id))
        candidates = self.by_item.get(norm_key(line.item_id))
        if not candidates:
            return None
        if line.line_no is not None and len(candidates) > 1:
            for candidate in candidates:
                if candidate.line_no == line.line_no:
                    return candidate
        return candidates[0]


def build_po_index(po_lines: Iterable[POLine]) -> POIndex:
    by_id: dict[str, POLine] = {}
    by_item: dict[str, list[POLine]] = {}
    for po_line in po_lines:
        if po_line.po_line_id:
            by_id.setdefault(norm_key(po_line.po_line_id), po_line)
        by_item.setdefault(norm_key(po_line.item_id), []).append(po_line)
    return POIndex(by_id=by_id, by_item=by_item)


def match_po_line(line: InvoiceLine, po_index: POIndex) -> Optional[POLine]:
    return po_index.match(line)


def _describe_line(line: InvoiceLine) -> str:
    if line.item_id:
        return f"item {line.item_id}"
    return f"invoice line {line.line_no}" if line.line_no is not None else "an invoice line"


# --------------------------------------------------------------------------
# Gates (pipeline step 6) -- non-duplicates only
# --------------------------------------------------------------------------

def gate_document_type(invoice: Invoice) -> None:
    """Step 6a. Credit notes and negative quantities/amounts were explicitly left
    undefined by the stakeholder, so they are never routed through checks 2-6."""
    if invoice.doc_type and "credit" in norm_key(invoice.doc_type):
        raise Unreviewable(
            "unsupported_document",
            f"invoice {invoice.invoice_number} is a {invoice.doc_type}; "
            "credit notes are not in scope for automated review",
        )
    if invoice.total is not None and invoice.total < ZERO:
        raise Unreviewable(
            "unsupported_document",
            f"invoice {invoice.invoice_number} has a negative total "
            f"({fmt_money(invoice.total)}); negative documents are not in scope",
        )
    for line in invoice.lines:
        if line.quantity is not None and line.quantity < ZERO:
            raise Unreviewable(
                "unsupported_document",
                f"{_describe_line(line)} on invoice {invoice.invoice_number} has a negative "
                f"quantity ({fmt_qty(line.quantity)}); negative documents are not in scope",
            )
        if line.unit_price is not None and line.unit_price < ZERO:
            raise Unreviewable(
                "unsupported_document",
                f"{_describe_line(line)} on invoice {invoice.invoice_number} has a negative "
                f"unit price ({fmt_money(line.unit_price)}); negative documents are not in scope",
            )
    for fee in invoice.fees:
        if fee.amount is not None and fee.amount < ZERO:
            label = fee.label or "unlabelled fee"
            raise Unreviewable(
                "unsupported_document",
                f"fee '{label}' on invoice {invoice.invoice_number} is negative "
                f"({fmt_money(fee.amount)}); negative documents are not in scope",
            )


def gate_currency(invoice: Invoice, po_lines: Iterable[POLine]) -> None:
    """Step 6b. No conversion is invented; a detectable mismatch is unreviewable.

    Only currencies actually recorded are compared.  Where the schema carries no
    currency column at all there is nothing to detect and the gate passes.
    """
    seen: dict[str, str] = {}
    if invoice.currency:
        seen[norm_key(invoice.currency)] = invoice.currency
    for po_line in po_lines:
        if po_line.currency:
            seen[norm_key(po_line.currency)] = po_line.currency
    if len(seen) > 1:
        shown = ", ".join(sorted(seen.values()))
        raise Unreviewable(
            "currency_mismatch",
            f"invoice {invoice.invoice_number} and its purchase order are in different "
            f"currencies ({shown}); cross-currency comparison is not in scope",
        )


def gate_completeness(invoice: Invoice, po_index: POIndex) -> None:
    """Step 6c. Every input that checks 2-6 need must be present.

    Enforces the governing invariant: a lower-priority verdict must never be
    produced when an earlier required check is unevaluable.  A null invoice total
    blocks TOTAL_MATH, so it also blocks the two off-contract checks below it.
    """
    if not invoice.lines:
        raise Unreviewable(
            "invoice_lines_missing",
            f"invoice {invoice.invoice_number} has no line items to compare against its PO",
        )
    if invoice.total is None:
        raise Unreviewable(
            "invoice_total_missing",
            f"invoice {invoice.invoice_number} has no total, so the total cannot be checked",
        )

    for line in invoice.lines:
        where = _describe_line(line)
        if not line.item_id:
            raise Unreviewable(
                "invoice_line_incomplete",
                f"{where} on invoice {invoice.invoice_number} has no item id",
            )
        if line.quantity is None:
            raise Unreviewable(
                "invoice_line_incomplete",
                f"{where} on invoice {invoice.invoice_number} has no quantity",
            )
        if line.unit_price is None:
            raise Unreviewable(
                "invoice_line_incomplete",
                f"{where} on invoice {invoice.invoice_number} has no unit price",
            )

        po_line = match_po_line(line, po_index)
        if po_line is None:
            detail = f"PO line {line.po_line_id}" if line.po_line_id else where
            raise Unreviewable(
                "po_line_not_found",
                f"no {detail} on {invoice.po_number}"
                + (f" for {where}" if line.po_line_id else ""),
            )
        # Price baseline is the PO line and only the PO line -- no contract fallback.
        if po_line.unit_price is None:
            raise Unreviewable(
                "po_line_price_missing",
                f"PO line on {invoice.po_number} for {where} has no unit price, "
                "so the billed price cannot be compared",
            )
        if po_line.quantity is None:
            raise Unreviewable(
                "po_line_quantity_missing",
                f"PO line on {invoice.po_number} for {where} has no quantity, "
                "so the billed quantity cannot be compared",
            )

    for fee in invoice.fees:
        if not fee.label:
            raise Unreviewable(
                "fee_label_missing",
                f"invoice {invoice.invoice_number} carries a fee with no label, "
                "so it cannot be checked against the contract",
            )
        if fee.amount is None:
            raise Unreviewable(
                "fee_amount_missing",
                f"fee '{fee.label}' on invoice {invoice.invoice_number} has no amount, "
                "so the invoice total cannot be checked",
            )


def run_gates(invoice: Invoice, po_lines: Iterable[POLine], po_index: POIndex) -> None:
    """Steps 6a -> 6b -> 6c in order."""
    po_lines = list(po_lines)
    gate_document_type(invoice)
    gate_currency(invoice, po_lines)
    gate_completeness(invoice, po_index)


# --------------------------------------------------------------------------
# Check 1 -- DUPLICATE (decided before the gates, from the invoice record alone)
# --------------------------------------------------------------------------

def duplicate_verdict(invoice: Invoice, po_reference: Optional[str] = None) -> Verdict:
    """Reason is built from the invoice record and the request only: no PO line or
    contract is loaded on this path."""
    original = invoice.duplicate_of
    reason = f"duplicate of {original}"
    po_ref = po_reference or invoice.po_number
    if po_ref:
        reason += f" for PO {po_ref}"
    reason += "; already submitted, do not pay twice"
    return Verdict(True, FAULT_DUPLICATE, reason)


# --------------------------------------------------------------------------
# Checks 2-6
# --------------------------------------------------------------------------

def _check_price_delta(
    invoice: Invoice, po_index: POIndex
) -> list[Finding]:
    """Signed overcharge, valued at the INVOICED quantity: that is the real exposure."""
    findings: list[Finding] = []
    for line in invoice.lines:
        po_line = match_po_line(line, po_index)
        assert po_line is not None and po_line.unit_price is not None, "gate 6c should have caught this"
        unit_delta = line.unit_price - po_line.unit_price
        impact = unit_delta * line.quantity
        if impact > MATERIALITY:  # strictly greater; exactly $500 does not flag
            findings.append(
                Finding(
                    FAULT_PRICE_DELTA,
                    f"item {line.item_id} billed at {fmt_money(line.unit_price)} vs "
                    f"{fmt_money(po_line.unit_price)} agreed on {invoice.po_number}; "
                    f"overcharge {fmt_money(impact)} across {fmt_qty(line.quantity)} units",
                    f"price overcharge on item {line.item_id} ({fmt_money(impact)})",
                    impact,
                )
            )
    return findings


def _check_qty_mismatch(
    invoice: Invoice, po_index: POIndex
) -> list[Finding]:
    """Exact comparison: no tolerance, no partial-invoice exception, no remaining
    balance carried across invoices on the same PO."""
    findings: list[Finding] = []
    for line in invoice.lines:
        po_line = match_po_line(line, po_index)
        assert po_line is not None and po_line.quantity is not None, "gate 6c should have caught this"
        if line.quantity != po_line.quantity:
            gap = abs(line.quantity - po_line.quantity)
            findings.append(
                Finding(
                    FAULT_QTY_MISMATCH,
                    f"item {line.item_id} invoiced {fmt_qty(line.quantity)} units vs "
                    f"{fmt_qty(po_line.quantity)} ordered on {invoice.po_number}",
                    f"quantity mismatch on item {line.item_id} "
                    f"({fmt_qty(line.quantity)} vs {fmt_qty(po_line.quantity)})",
                    gap * (line.unit_price or ZERO),
                )
            )
    return findings


def compute_total_components(invoice: Invoice) -> tuple[Decimal, Decimal, Decimal]:
    """Literally sum(qty x unit price) + sum(fees).

    Anything the source system stores outside lines and fees -- tax, shipping, a
    discount -- is deliberately NOT added.  Returned separately so every
    evaluation can log the components (see plan section 7).
    """
    lines_total = sum((line.quantity * line.unit_price for line in invoice.lines), ZERO)
    fees_total = sum((fee.amount for fee in invoice.fees), ZERO)
    return lines_total, fees_total, lines_total + fees_total


def _check_total_math(invoice: Invoice) -> list[Finding]:
    _lines_total, _fees_total, computed = compute_total_components(invoice)
    delta = invoice.total - computed
    if abs(delta) <= MATERIALITY:  # strictly greater than 500 flags
        return []
    return [
        Finding(
            FAULT_TOTAL_MATH,
            f"invoice total {fmt_money(invoice.total)} vs {fmt_money(computed)} computed "
            f"from line items plus fees; off by {fmt_money(delta)}",
            f"invoice total off by {fmt_money(delta)} against line items plus fees",
            abs(delta),
        )
    ]


def _check_off_contract_item(invoice: Invoice, contract: Contract) -> list[Finding]:
    """Pure membership test against the distinct item ids on this supplier's own
    contract rows.  Another supplier's contract never rescues an item."""
    findings: list[Finding] = []
    for line in invoice.lines:
        if norm_key(line.item_id) not in contract.allowed_items:
            findings.append(
                Finding(
                    FAULT_OFF_CONTRACT_ITEM,
                    f"item {line.item_id} is not covered by {contract.supplier_id}'s contract",
                    f"off-contract item {line.item_id}",
                    line.quantity * line.unit_price,
                )
            )
    return findings


def _check_off_contract_fee(invoice: Invoice, contract: Contract) -> list[Finding]:
    """A fee label absent from this supplier's allowed_fees is unauthorised
    whatever the amount.  Fees are per-supplier; nothing is generalised."""
    findings: list[Finding] = []
    for fee in invoice.fees:
        if norm_key(fee.label) not in contract.allowed_fees:
            findings.append(
                Finding(
                    FAULT_OFF_CONTRACT_FEE,
                    f"fee '{fee.label}' ({fmt_money(fee.amount)}) is not authorised by "
                    f"{contract.supplier_id}'s contract",
                    f"unauthorised fee '{fee.label}' ({fmt_money(fee.amount)})",
                    fee.amount,
                )
            )
    return findings


# --------------------------------------------------------------------------
# Check-major evaluation
# --------------------------------------------------------------------------

def _rank(findings: list[Finding]) -> list[Finding]:
    """Worst first, so the representative row is stable under reordering."""
    return sorted(findings, key=lambda f: (-f.severity, f.brief))


def _compose_reason(primary: Finding, secondary: list[Finding]) -> str:
    reason = primary.reason
    if not secondary:
        return reason
    clauses: list[str] = []
    seen: set[str] = set()
    for finding in secondary:
        if finding.brief in seen:
            continue
        seen.add(finding.brief)
        clauses.append(finding.brief)
    kept: list[str] = []
    for clause in clauses:
        candidate = f"{reason}; also {', '.join(kept + [clause])}"
        if len(candidate) > MAX_REASON_CHARS:
            break
        kept.append(clause)
    if not kept:
        return reason
    tail = ", ".join(kept)
    dropped = len(clauses) - len(kept)
    if dropped:
        tail += f", and {dropped} further issue{'s' if dropped > 1 else ''}"
    return f"{reason}; also {tail}"


def evaluate_non_duplicate(
    invoice: Invoice, po_index: POIndex, contract: Contract
) -> Verdict:
    """Checks 2-6 check-major.  Assumes run_gates() has already passed."""
    by_check: list[list[Finding]] = [
        _rank(_check_price_delta(invoice, po_index)),
        _rank(_check_qty_mismatch(invoice, po_index)),
        _rank(_check_total_math(invoice)),
        _rank(_check_off_contract_item(invoice, contract)),
        _rank(_check_off_contract_fee(invoice, contract)),
    ]

    primary: Optional[Finding] = None
    secondary: list[Finding] = []
    for findings in by_check:
        if not findings:
            continue
        if primary is None:
            primary = findings[0]
            secondary.extend(findings[1:])
        else:
            secondary.extend(findings)

    if primary is None:
        return Verdict(False, None, CLEAN_REASON)
    return Verdict(True, primary.fault_type, _compose_reason(primary, secondary))
