"""Generate a plain-language reason for a flag decision.

"Code decides, LLM explains": the Decision (flagged + category) is already made by
the rules. This module only phrases the gathered facts into a reason string.

``build_reason`` calls the LLM and RAISES if it cannot -- no silent fallback. When a
reason is needed without a live LLM (tests, the eval container that has no API key),
the caller either injects a stub ``llm`` client or calls ``template_reason``
directly. That keeps LLM failures loud while still allowing fully offline runs.
"""
from __future__ import annotations

import os

# Small, fast model -- this is a single-shot, low-token phrasing call, and the
# latency budget is ~2s per invoice.
_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 256


class ReasonLLMError(RuntimeError):
    """Raised when the LLM reason cannot be produced (missing key/SDK, call failed)."""


def build_reason(decision, record, llm=None) -> str:
    """Return an LLM-written plain-language reason for the decision.

    Args:
        decision: the ``Decision`` from a rule (carries ``category`` and ``facts``).
        record: the ``InvoiceRecord`` under review.
        llm: an optional Anthropic-style client. If ``None``, a real client is
            built from ``ANTHROPIC_API_KEY``; injecting a client is how tests and
            the offline eval run without a key.

    Returns:
        The reason string produced by the LLM.

    Raises:
        ReasonLLMError: if no client can be built (no ``ANTHROPIC_API_KEY`` / SDK)
            or the call fails. Callers who need an offline reason should use
            ``template_reason`` or inject a stub ``llm`` instead.
    """
    client = llm if llm is not None else _get_llm()

    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=(
                "You write one-sentence, plain-language reasons an accounts-payable "
                "reviewer can act on. State the problem and the numbers plainly. "
                "Do not add greetings or preamble."
            ),
            messages=[{"role": "user", "content": _prompt(decision, record)}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
    except Exception as exc:  # network, auth, timeout, SDK shape change
        raise ReasonLLMError(f"LLM reason call failed: {exc}") from exc

    if not text:
        raise ReasonLLMError("LLM returned an empty reason")
    return text


def _get_llm():
    """Return an Anthropic client, or raise ``ReasonLLMError`` if it cannot be built.

    Fails loudly (not ``None``) when ``ANTHROPIC_API_KEY`` is unset or the SDK is
    missing, so a misconfigured environment surfaces immediately rather than
    silently degrading. Callers who intend to run offline inject a stub ``llm`` or
    use ``template_reason`` directly.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ReasonLLMError("ANTHROPIC_API_KEY is not set -- LLM cannot be built")
    try:
        import anthropic
    except ImportError:
        raise ReasonLLMError("`anthropic` SDK is not installed -- LLM cannot be built")
    return anthropic.Anthropic()


def _prompt(decision, record) -> str:
    """Build the user prompt handed to the LLM, grounded in the gathered facts."""
    return (
        f"Invoice {record.invoice_number} was flagged as {decision.category}.\n"
        f"Facts: {decision.facts}\n"
        f"Draft reason: {template_reason(decision, record)}\n"
        "Rewrite the draft as a single clear sentence for the reviewer."
    )


def template_reason(decision, record) -> str:
    """Deterministic reason built from the decision facts (offline, no LLM).

    Used by tests and the eval container, and as the LLM's draft input. Public so
    callers can produce a reason without a key when that is the intended behaviour.
    """
    if not decision.flagged:
        return f"Invoice {record.invoice_number} matches its PO line; no duplicate detected."

    if decision.category == "DUPLICATE":
        original = decision.facts.get("duplicate_of")
        po_line_id = decision.facts.get("po_line_id")
        return (
            f"Duplicate submission: invoice {record.invoice_number} is a duplicate of "
            f"{original} (PO line {po_line_id}); potential double charge. "
            "AP should review before payment."
        )

    if decision.category == "PRICE_DELTA":
        return _price_delta_reason(decision)

    if decision.category == "QTY_MISMATCH":
        return _qty_mismatch_reason(decision)

    if decision.category == "OFF_CONTRACT_ITEM":
        return _off_contract_item_reason(decision)

    if decision.category == "OFF_CONTRACT_FEE":
        return _off_contract_fee_reason(decision)

    if decision.category == "EXTRACTION_ERROR":
        detail = decision.facts.get("error_detail", "data could not be gathered")
        return (
            f"Could not fully verify invoice {record.invoice_number}: {detail}. "
            "Flagged for manual review."
        )

    # Defensive default for future categories not yet wired to a template.
    return f"Invoice {record.invoice_number} flagged ({decision.category}); manual review recommended."


def _num(value) -> str:
    """Format a number for the reason: whole numbers drop the decimal, else 2dp."""
    if value is None:
        return "n/a"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _money(value) -> str:
    """Format a dollar amount to 2 decimals, or ``n/a`` when absent."""
    return "n/a" if value is None else f"${value:.2f}"


def _price_delta_reason(decision) -> str:
    """Spell out every extracted number for a PRICE_DELTA flag (no LLM arithmetic).

    Lays out the invoice, PO, and contract figures and the pre-computed dollar
    impacts so the reviewer (and the LLM, which only rephrases) sees exactly why the
    invoice was flagged. All math is done in the rule; this only formats.
    """
    f = decision.facts
    return (
        f"Invoice {f.get('invoice_number')} unit price differs materially from the "
        f"PO price. The invoice states a quantity of {_num(f.get('invoiced_qty'))}, "
        f"a unit price of {_money(f.get('invoice_unit_price'))} and an amount of "
        f"{_money(f.get('invoice_amount'))}. The PO shows quantity "
        f"{_num(f.get('po_quantity'))}, received quantity {_num(f.get('po_received_qty'))} "
        f"and unit price {_money(f.get('po_unit_price'))} (contract agreed unit price "
        f"{_money(f.get('contract_agreed_unit_price'))}). The price impact vs the PO is "
        f"{_money(f.get('impact_vs_po'))}, which exceeds the "
        f"{_money(f.get('threshold'))} materiality threshold. "
        "AP should review before payment."
    )


def _qty_mismatch_reason(decision) -> str:
    """Spell out the invoiced vs PO quantities for a QTY_MISMATCH flag."""
    f = decision.facts
    return (
        f"Invoice {f.get('invoice_number')} quantity does not match the PO. The "
        f"invoice bills {_num(f.get('invoiced_qty'))} unit(s) but the PO ordered "
        f"{_num(f.get('po_quantity'))} (received {_num(f.get('po_received_qty'))}); "
        f"difference {_num(f.get('qty_difference'))} unit(s). "
        "AP should review before payment."
    )


def _off_contract_item_reason(decision) -> str:
    """State that the invoice's item is not covered by the supplier's contract."""
    f = decision.facts
    return (
        f"Invoice {f.get('invoice_number')} is off-contract: it claims item "
        f"{f.get('item_number')} (item_id {f.get('item_id')}), which supplier "
        f"{f.get('supplier_id')}'s contract does not cover. "
        "AP should review before payment."
    )


def _off_contract_fee_reason(decision) -> str:
    """List the invoice fees not permitted by the contract for an OFF_CONTRACT_FEE flag."""
    f = decision.facts
    offending = f.get("offending_fees") or []
    parts = ", ".join(
        f"'{fee.get('label')}' ({_money(fee.get('amount'))})" for fee in offending
    )
    allowed = f.get("allowed_fees") or []
    allowed_text = ", ".join(str(label) for label in allowed) if allowed else "none"
    return (
        f"Invoice {f.get('invoice_number')} carries fee(s) the contract does not "
        f"allow: {parts}. Contract-allowed fees: {allowed_text}. "
        "AP should review before payment."
    )
