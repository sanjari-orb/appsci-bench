"""The `shell` tool's file tree: pins the exact bytes the agent sees.

WHY THIS TEST IS BYTE-LEVEL rather than structural. For every other retrieval tool the corpus layout is
invisible -- a search returns excerpts through a pipeline. `shell` exposes a FILESYSTEM, so filenames,
extensions and body framing are all agent-visible, and a model's habitual `ls *.md` or `head -1` returns
different output if any of them drift. The published leaderboard scores we want to be comparable to were
produced against upstream's tree, so "close enough" is a comparability bug, not a cosmetic one.

The strongest assertion here is test_export_is_byte_identical_to_upstreams_own_exporter: rather than
trusting a transcription of the format, it runs UPSTREAM's vendored exporter and diffs every file.
"""
from __future__ import annotations

import pathlib
import types

import pytest

from tasks.banking_support.environment._runtime import kb_export as kx
from tasks.banking_support.environment._runtime.knowledge import sandbox_manager as sm

_DOCS = pathlib.Path(__file__).resolve().parents[3] / "tasks" / "banking_support" / "environment" / "documents"

_SAMPLE = [
    {"id": "doc_a_(general)_001", "title": "Opening Accounts", "content": "## Rules\n\nBe careful.\n"},
    {"id": "doc_b_002", "title": "Fees", "content": "No fees.\n"},
]


def test_the_document_body_is_a_markdown_heading_then_a_blank_line_then_content(tmp_path):
    kx.export_corpus(_SAMPLE, tmp_path)
    body = (tmp_path / "doc_b_002.md").read_text()
    assert body == "# Fees\n\nNo fees.\n"


def test_ids_are_sanitized_into_filenames_so_they_differ_from_our_json_names(tmp_path):
    # Banking ids contain parentheses; those become underscores. The agent never sees the raw id as a
    # filename, which is why the sanitizer is part of the contract and not an implementation detail.
    kx.export_corpus(_SAMPLE, tmp_path)
    assert (tmp_path / "doc_a__general__001.md").is_file()
    assert not (tmp_path / "doc_a_(general)_001.md").exists()


def test_the_index_file_exists_because_the_tool_docstring_tells_the_model_to_read_it(tmp_path):
    kx.export_corpus(_SAMPLE, tmp_path)
    index = (tmp_path / "INDEX.md").read_text()
    assert index.startswith("# Knowledge Base Index\n\n**Total documents:** 2\n\n## Documents\n\n")
    # Sorted by id, so the tree is reproducible from the corpus rather than from directory order.
    assert index.index("doc_a__general__001.md") < index.index("doc_b_002.md")
    assert "- **doc_b_002.md** - Fees\n" in index


def test_the_tree_holds_exactly_the_documents_and_the_index_and_nothing_else(tmp_path):
    # The confinement guarantee is that the shell's whole filesystem view is this tree, so an extra file
    # here would be an extra file the agent can read.
    kx.export_corpus(_SAMPLE, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "INDEX.md", "doc_a__general__001.md", "doc_b_002.md"]


def test_exporting_twice_gives_identical_bytes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    kx.export_corpus(_SAMPLE, a)
    kx.export_corpus(_SAMPLE, b)
    for name in (p.name for p in a.iterdir()):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_json_format_is_refused_rather_than_silently_exported(tmp_path):
    # No variant we run uses it, and a JSON tree would change every filename the agent globs for.
    with pytest.raises(ValueError):
        kx.export_corpus(_SAMPLE, tmp_path, file_format="json")


@pytest.mark.skipif(not _DOCS.is_dir(), reason="committed corpus not present")
def test_export_is_byte_identical_to_upstreams_own_exporter(tmp_path):
    """Diff our export against UPSTREAM's vendored exporter, file by file.

    Upstream's method hangs off SandboxManager, whose constructor raises without Anthropic's
    sandbox-runtime -- so it is called on a stub supplying only the attributes it touches. That is the
    point of the test: the format is reproduced without the runtime, which is exactly what our sidecar
    needs and what the old dead code path could never deliver.
    """
    docs = kx.load_documents(_DOCS)
    assert len(docs) == 698

    mine = tmp_path / "mine"
    kx.export_corpus(docs, mine)

    theirs = tmp_path / "theirs"
    theirs.mkdir()
    stub = types.SimpleNamespace(kb_dir=theirs)
    stub._sanitize_filename = types.MethodType(sm.SandboxManager._sanitize_filename, stub)
    stub._create_index_file = types.MethodType(sm.SandboxManager._create_index_file, stub)
    sm.SandboxManager.export_documents(stub, docs, file_format="md")

    assert sorted(p.name for p in mine.iterdir()) == sorted(p.name for p in theirs.iterdir())
    differing = [p.name for p in sorted(mine.iterdir())
                 if p.read_bytes() != (theirs / p.name).read_bytes()]
    assert differing == [], f"{len(differing)} file(s) drifted from upstream: {differing[:5]}"


@pytest.mark.skipif(not _DOCS.is_dir(), reason="committed corpus not present")
def test_the_real_corpus_exports_698_documents_plus_an_index(tmp_path):
    kx.export_corpus(kx.load_documents(_DOCS), tmp_path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert len(names) == 699 and "INDEX.md" in names
    assert all(n.endswith(".md") for n in names)
