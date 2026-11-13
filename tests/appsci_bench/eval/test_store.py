"""Unit tests: the transcript store (one JSON per conversation; artifacts by ref)."""
from __future__ import annotations

import pathlib

from appsci_bench.eval.store import TranscriptStore
from appsci_bench.eval.datatypes import (
    TERMINATION_MAX_TURNS,
    ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, File, GenerationRecord,
)


def _record_with_file():
    q = ArtifactAgentQuery(text="make a deck")
    r = ArtifactAgentOutput(text="done", json='{"slides": 3}',
                            files=[File("pptx", "deck.pptx", b"PPTXBYTES")])
    return GenerationRecord("dp1", 0, conversation=[ConversationTurn(q, r)])


def test_save_writes_conversation_json(tmp_path):
    store = TranscriptStore(tmp_path)
    path = store.save(_record_with_file())
    assert path.exists()
    doc = store.load(path)
    assert doc["data_point_id"] == "dp1" and doc["error"] is None
    assert doc["conversation"][0]["query"]["text"] == "make a deck"
    assert doc["conversation"][0]["response"]["json"] == '{"slides": 3}'


def test_saved_transcript_records_how_the_conversation_ended(tmp_path):
    # Persisted so a truncated trace is identifiable from the transcript alone, not only from the
    # score -- "why is this case 0?" is answerable without re-running it.
    store = TranscriptStore(tmp_path)
    rec = _record_with_file()
    rec.termination_reason = TERMINATION_MAX_TURNS
    doc = store.load(store.save(rec))
    assert doc["termination_reason"] == TERMINATION_MAX_TURNS
    assert GenerationRecord.from_dict(doc).termination_reason == TERMINATION_MAX_TURNS


def test_artifact_bytes_dumped_and_referenced(tmp_path):
    store = TranscriptStore(tmp_path)
    store.save(_record_with_file())
    doc = store.load(store.list_conversations()[0])
    f = doc["conversation"][0]["response"]["files"][0]
    assert f["file_type"] == "pptx" and f["file_name"] == "deck.pptx"
    assert "file_data" not in f                          # bytes are NOT inlined
    ref = store.root / f["file_ref"]                     # referenced by relative path
    assert ref.exists() and ref.read_bytes() == b"PPTXBYTES"


def test_default_root_is_tempdir():
    store = TranscriptStore()
    assert store.conversations_dir.exists() and store.artifacts_dir.exists()
