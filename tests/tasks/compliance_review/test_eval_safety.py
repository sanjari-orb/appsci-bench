"""Regression test for a real bug: `_contract_text` read a File's content via `.data` /
`f.get("data")`, but `File` has no `.data` attribute (it's `.file_data`) -- so the hasattr check
always fell through to `.get()`, which a `File` dataclass doesn't have either, raising
`AttributeError("'File' object has no attribute 'get'")` on every real (non-dict) conversation
that attached a contract file. This crashed both `no_fabrication` and `safe_by_construction` in
production (compliance_review_claude-opus-4-8_b2.t1_l3_20260910_081501).

Exercises the live-`File`-object path (what a real eval run passes) and the dict-round-tripped
path (what a conversation looks like after a JSON save/load), since the bug depended on exactly
which of those two shapes `_contract_text` was given.
"""
from __future__ import annotations

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, File
from tasks.compliance_review import eval_safety as safety


def test_contract_text_reads_a_live_file_object_without_raising():
    query = ArtifactAgentQuery(
        text="review this contract",
        files=[File(file_type="txt", file_name="contract.txt",
                     file_data=b"Payment is due within 30 days of invoice.")],
    )
    turn = ConversationTurn(query=query, response=ArtifactAgentOutput(text="compliant"))

    assert safety._contract_text([turn]) == "Payment is due within 30 days of invoice."


def test_contract_text_reads_a_dict_round_tripped_query_without_raising():
    # Mirrors what a conversation looks like after ArtifactAgentQuery.to_dict() -> JSON -> dict,
    # where file_data is base64-encoded text, not raw bytes.
    query = ArtifactAgentQuery(
        text="review this contract",
        files=[File(file_type="txt", file_name="contract.txt", file_data=b"Net 30 terms apply.")],
    ).to_dict()
    turn = {"query": query, "response": {"text": "compliant", "json": None, "files": []}}

    assert safety._contract_text([turn]) == "Net 30 terms apply."


def test_contract_text_returns_empty_string_when_no_file_was_attached():
    turn = ConversationTurn(query=ArtifactAgentQuery(text="review this contract"),
                            response=ArtifactAgentOutput(text="compliant"))

    assert safety._contract_text([turn]) == ""
