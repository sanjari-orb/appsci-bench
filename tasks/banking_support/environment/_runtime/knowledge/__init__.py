"""Vendored tau2 retrieval runtime (knowledge pipeline) for banking_support.

Ported from tau2.knowledge @ 3b6a28ed8206dc31fc8b2dfdb497b8600821b463. Faithful to tau2 except:
  - postprocessors/ (rerankers) are NOT ported -- the banking retrieval configs use empty
    postprocessor lists -- which drops the sentence_transformers/sklearn dependency;
  - embedders are trimmed to OpenAI-only (the confirmed dense backend);
  - config.py is not ported (pipeline configs are built inline by _runtime.retrieval).

Import repoints: `tau2.knowledge.*` -> relative; `tau2.environment.toolkit` -> `_runtime.toolkit`.
"""

_REGISTERED = False


def _ensure_registered() -> None:
    """Import component modules so their @register_* decorators populate the registry.

    Idempotent and lazy: RetrievalPipeline.__init__ calls this before looking up components.
    Only the bm25 + dense + grep surface is registered (no rerankers/postprocessors).
    """
    global _REGISTERED
    if _REGISTERED:
        return

    from .document_preprocessors import bm25_indexer, embedding_indexer  # noqa: F401
    from .input_preprocessors import embedding_encoder  # noqa: F401
    from .retrievers import bm25_retriever, cosine_retriever, grep_retriever  # noqa: F401

    _REGISTERED = True
