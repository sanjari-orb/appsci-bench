"""Regression test for a real bug: the call-transcript reader used `.data` / `f.get("data")` on a
query's attached file, but `File` has no `.data` attribute (it's `.file_data`) -- so the hasattr
check always fell through to `.get()`, which a `File` dataclass doesn't have either, raising
`AttributeError("'File' object has no attribute 'get'")` on every real (non-dict) conversation
that attached a call transcript file.

Exercises the live-`File`-object path (what a real eval run passes) and the dict-round-tripped
path (base64 `file_data`, what a conversation looks like after a JSON save/load).
"""
from __future__ import annotations

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, File
from tasks.customer_call_insights import eval_safety as safety


def test_transcript_text_reads_a_live_file_object_without_raising():
    query = ArtifactAgentQuery(
        text="summarize this call",
        files=[File(file_type="txt", file_name="call.txt",
                     file_data=b"Agent: how can I help? Customer: I'd like a refund.")],
    )
    turn = ConversationTurn(query=query, response=ArtifactAgentOutput(text="summary"))

    assert safety._transcript_text([turn]) == "Agent: how can I help? Customer: I'd like a refund."


def test_transcript_text_reads_a_dict_round_tripped_query_without_raising():
    query = ArtifactAgentQuery(
        text="summarize this call",
        files=[File(file_type="txt", file_name="call.txt", file_data=b"Hello, this is the transcript.")],
    ).to_dict()
    turn = {"query": query, "response": {"text": "summary", "json": None, "files": []}}

    assert safety._transcript_text([turn]) == "Hello, this is the transcript."


def test_transcript_text_returns_empty_string_when_no_file_was_attached():
    turn = ConversationTurn(query=ArtifactAgentQuery(text="summarize this call"),
                            response=ArtifactAgentOutput(text="summary"))

    assert safety._transcript_text([turn]) == ""
