"""Tests for the detector adapter (invoice parsing + JSON contract shape)."""
from __future__ import annotations

import json

from solution import detector
from solution.datatypes import ArtifactAgentOutput, ArtifactAgentQuery


def test_parse_keeps_dup_suffix():
    text = "Review invoice INV-3046-DUP (supplier Supplier_9, PO PO-000681)."
    assert detector._parse_invoice_number(text) == "INV-3046-DUP"


def test_parse_plain_invoice():
    assert detector._parse_invoice_number("check INV-2474 please") == "INV-2474"


def test_parse_returns_none_without_token():
    assert detector._parse_invoice_number("no invoice mentioned here") is None
    assert detector._parse_invoice_number(None) is None


def test_respond_unparseable_flags_for_review():
    """Text with no invoice number returns a flagged, review JSON payload."""
    out = detector.respond(ArtifactAgentQuery(text="please look at this"))
    assert isinstance(out, ArtifactAgentOutput)
    payload = json.loads(out.json)
    assert payload["flagged"] is True
    assert payload["fault_type"] is None
    assert isinstance(payload["reason"], str)
    # JSON contract carries exactly flagged + fault_type + reason (migration fix: fault_type is
    # now surfaced from the rule's category instead of being dropped -- see detector.py::respond).
    assert set(payload) == {"flagged", "fault_type", "reason"}
