"""Regression test for a real bug: `_source_files_text` read a File's content via `.data` /
`f.get("data")`, but `File` has no `.data` attribute (it's `.file_data`) -- so the hasattr check
always fell through to `.get()`, which a `File` dataclass doesn't have either, raising
`AttributeError("'File' object has no attribute 'get'")` on every real (non-dict) conversation
that attached a text-like source file.

Exercises the live-`File`-object path (what a real eval run passes) and the dict-round-tripped
path (base64 `file_data`, what a conversation looks like after a JSON save/load).
"""
from __future__ import annotations

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, File
from tasks.slides_generator import eval_safety as safety


def test_source_files_text_reads_a_live_file_object_without_raising():
    query = ArtifactAgentQuery(
        text="build slides from this brief",
        files=[File(file_type="md", file_name="brief.md", file_data=b"# Q3 results\nRevenue grew 12%.")],
    )
    turn = ConversationTurn(query=query, response=ArtifactAgentOutput(text="deck built"))

    assert "Revenue grew 12%." in safety._source_files_text([turn])


def test_source_files_text_reads_a_dict_round_tripped_query_without_raising():
    query = ArtifactAgentQuery(
        text="build slides from this brief",
        files=[File(file_type="md", file_name="brief.md", file_data=b"# Q3 results\nRevenue grew 12%.")],
    ).to_dict()
    turn = {"query": query, "response": {"text": "deck built", "json": None, "files": []}}

    assert "Revenue grew 12%." in safety._source_files_text([turn])


def test_source_files_text_skips_binary_formats_without_raising():
    query = ArtifactAgentQuery(
        text="build slides from this deck",
        files=[File(file_type="pptx", file_name="template.pptx", file_data=b"\x00binary\x00")],
    )
    turn = ConversationTurn(query=query, response=ArtifactAgentOutput(text="deck built"))

    assert safety._source_files_text([turn]) == ""


def test_source_files_text_returns_empty_string_when_no_file_was_attached():
    turn = ConversationTurn(query=ArtifactAgentQuery(text="build slides from this brief"),
                            response=ArtifactAgentOutput(text="deck built"))

    assert safety._source_files_text([turn]) == ""
