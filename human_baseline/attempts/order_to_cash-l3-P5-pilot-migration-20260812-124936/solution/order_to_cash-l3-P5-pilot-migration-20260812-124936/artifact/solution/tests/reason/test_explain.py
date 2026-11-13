"""Tests for the reason layer (offline: template + no-key raise + injected client)."""
from __future__ import annotations

import pytest

from solution.reason import explain
from solution.rules import duplicate
from solution.tests.conftest import make_record, make_extraction_record


def test_template_reason_duplicate():
    record = make_record(invoice_number="INV-3046-DUP", duplicate_of="INV-3046",
                         po_line={"po_line_id": 1350})
    decision = duplicate.check(record)
    reason = explain.template_reason(decision, record)
    assert "duplicate of INV-3046" in reason
    assert "double charge" in reason


def test_template_reason_not_flagged():
    record = make_record(duplicate_of=None)
    decision = duplicate.check(record)
    reason = explain.template_reason(decision, record)
    assert "no duplicate detected" in reason


def test_template_reason_extraction_error():
    record = make_extraction_record()
    decision = duplicate.check(record)
    reason = explain.template_reason(decision, record)
    assert "Could not fully verify" in reason
    assert "manual review" in reason


def test_get_llm_raises_without_key(monkeypatch):
    """With no ANTHROPIC_API_KEY, _get_llm() raises ReasonLLMError (fails loud)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(explain.ReasonLLMError):
        explain._get_llm()


def test_build_reason_raises_without_llm(monkeypatch):
    """build_reason raises when no key is configured and no client is injected."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    record = make_record(duplicate_of="INV-3046")
    decision = duplicate.check(record)
    with pytest.raises(explain.ReasonLLMError):
        explain.build_reason(decision, record)


def test_build_reason_uses_injected_client():
    """An injected client's text is returned verbatim."""
    class _Block:
        type = "text"
        text = "Injected reason."

    class _Resp:
        content = [_Block()]

    class _Client:
        class messages:
            @staticmethod
            def create(**_kwargs):
                return _Resp()

    record = make_record(duplicate_of="INV-3046")
    decision = duplicate.check(record)
    assert explain.build_reason(decision, record, llm=_Client()) == "Injected reason."


def test_build_reason_raises_on_client_failure():
    """A client that raises surfaces as ReasonLLMError, not the raw exception."""
    class _Client:
        class messages:
            @staticmethod
            def create(**_kwargs):
                raise RuntimeError("boom")

    record = make_record(duplicate_of="INV-3046")
    decision = duplicate.check(record)
    with pytest.raises(explain.ReasonLLMError):
        explain.build_reason(decision, record, llm=_Client())
