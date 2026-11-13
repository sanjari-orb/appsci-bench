"""Tests for the agent pipeline (offline: injected conn + stub LLM client)."""
from __future__ import annotations

from solution.agent import pipeline
from solution.agent.record import ExtractionError
from solution.reason import explain


class _EchoLLM:
    """Stub Anthropic-style client: echoes the deterministic template as its text.

    The pipeline now requires an LLM, so tests inject this instead of relying on a
    real key. Returning the template keeps the reason assertions (which check for
    invoice numbers / "not found") valid without a network call.
    """

    class messages:
        @staticmethod
        def create(*, messages, **_kwargs):
            # The user prompt embeds the template as "Draft reason: <text>"; echoing
            # the whole prompt is enough for the substring assertions below.
            class _Block:
                type = "text"
                text = messages[0]["content"]

            class _Resp:
                content = [_Block()]

            return _Resp()


class _FakeCursor:
    """Minimal cursor returning a preset row for any query."""

    def __init__(self, row, description):
        self._row = row
        self.description = description

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_args):
        pass

    def fetchone(self):
        return self._row


class _FakeConn:
    """Fake connection whose cursor returns preset rows keyed by call order."""

    def __init__(self, invoice_row, joined_row, contract_row=None):
        # Call order: get_invoice_by_invoice_number, join_invoice_po_line, then
        # get_contract_price (only when the invoice has supplier_id + item_id).
        self._responses = [invoice_row, joined_row]
        if contract_row is not None:
            self._responses.append(contract_row)
        self._i = 0

    def cursor(self):
        # A lookup past the preset responses (e.g. an un-stubbed contract query)
        # returns no row rather than raising.
        if self._i >= len(self._responses):
            return _FakeCursor(None, [])
        row, description = self._responses[self._i]
        self._i += 1
        return _FakeCursor(row, description)

    def close(self):
        pass


def _col(name):
    class _C:
        pass

    c = _C()
    c.name = name
    return c


def test_duplicate_invoice_is_flagged():
    """A duplicate invoice flows through as flagged/DUPLICATE with a reason."""
    invoice_cols = [_col(n) for n in ("invoice_number", "duplicate_of", "po_line_id",
                                      "supplier_id", "item_id", "invoiced_qty",
                                      "unit_price", "amount", "extra_fees")]
    invoice_row = ("INV-3046-DUP", "INV-3046", 1350, 9, 1010, 350, 384.48, 134568.0, "[]")
    joined_cols = [_col(n) for n in ("invoice_number", "po_po_line_id", "po_po_number",
                                     "po_quantity", "po_unit_price", "po_received_qty")]
    joined_row = ("INV-3046-DUP", 1350, "PO-000681", 350, 384.48, 0)

    conn = _FakeConn((invoice_row, invoice_cols), (joined_row, joined_cols))
    result = pipeline.review_invoice("INV-3046-DUP", conn=conn, llm=_EchoLLM())

    assert result.flagged is True
    assert result.category == "DUPLICATE"
    assert "INV-3046" in result.reason


def test_clean_invoice_is_not_flagged():
    """A clean invoice (duplicate_of NULL, price matches PO) is not flagged."""
    invoice_cols = [_col(n) for n in ("invoice_number", "duplicate_of", "po_line_id",
                                      "supplier_id", "item_id", "invoiced_qty",
                                      "unit_price", "amount", "extra_fees")]
    invoice_row = ("INV-1696", None, 517, 9, 1010, 54, 176.2, 9514.8, "[]")
    joined_cols = [_col(n) for n in ("invoice_number", "po_po_line_id",
                                     "po_quantity", "po_unit_price", "po_received_qty")]
    joined_row = ("INV-1696", 517, 54, 176.2, 0)
    # INV-1696's item (supplier 9, item 1010) is on-contract; without a contract row
    # the off-contract-item rule would (correctly) flag it, so supply the row.
    contract_cols = [_col(n) for n in ("supplier_id", "item_id", "agreed_unit_price",
                                       "allowed_fees")]
    contract_row = (9, 1010, 384.48, "[]")

    conn = _FakeConn(
        (invoice_row, invoice_cols),
        (joined_row, joined_cols),
        contract_row=(contract_row, contract_cols),
    )
    result = pipeline.review_invoice("INV-1696", conn=conn, llm=_EchoLLM())

    assert result.flagged is False
    assert result.category is None


def test_db_error_becomes_extraction_flag():
    """A DB error during gather is surfaced as an EXTRACTION_ERROR flag (req 4.2)."""
    class _BoomConn:
        def cursor(self):
            raise RuntimeError("connection reset")

        def close(self):
            pass

    result = pipeline.review_invoice("INV-9999", conn=_BoomConn(), llm=_EchoLLM())
    assert result.flagged is True
    assert result.category == "EXTRACTION_ERROR"
    assert "INV-9999" in result.reason


def test_missing_invoice_becomes_extraction_flag():
    """An invoice absent from the DB (None row) is flagged, not silently passed."""
    conn = _FakeConn((None, [_col("invoice_number")]), (None, [_col("invoice_number")]))
    result = pipeline.review_invoice("INV-0000", conn=conn, llm=_EchoLLM())
    assert result.flagged is True
    assert result.category == "EXTRACTION_ERROR"
    assert "not found" in result.reason
