"""The knowledge-base corpus (698 documents) served to the artifact.

tau2's retrieval tools (KB_search_bm25 / KB_search_dense / grep / shell) are provided over this
corpus as agent tools (see _runtime/retrieval.py), so the artifact wires the given retrieval rather
than building its own. This class is the plain read-only loader the retrieval backends index and the
raw GET /documents endpoints serve; retrieval itself lives in the runtime, not here.
"""
from __future__ import annotations

import json
import pathlib

# Resolved relative to THIS file, not via the repo root: the task-env container ships only this
# package (no appsci_bench, no shared -- see the Dockerfile), and the corpus is copied in beside the
# code. BANKING_DOCS_PATH can still override for a fixture or a test.
DEFAULT_DOCS_PATH = pathlib.Path(__file__).resolve().parent / "documents"


class DocumentCorpus:
    """Read-only access to the raw KB documents ({id, title, content} json files)."""

    def __init__(self, docs: dict[str, dict]):
        self.docs = docs

    @classmethod
    def load(cls, path: str | pathlib.Path | None = None) -> "DocumentCorpus":
        base = pathlib.Path(path or DEFAULT_DOCS_PATH)
        docs = {}
        for f in sorted(base.glob("*.json")):
            d = json.loads(f.read_text())
            docs[d["id"]] = {"id": d["id"], "title": d.get("title", ""), "content": d.get("content", "")}
        return cls(docs)

    def list(self) -> list[dict]:
        """All documents as {id, title} (no bodies) -- the index behind GET /documents and the retrieval backends."""
        return [{"id": d["id"], "title": d["title"]} for d in self.docs.values()]

    def get(self, doc_id: str) -> dict | None:
        return self.docs.get(doc_id)

    def __len__(self) -> int:
        return len(self.docs)
