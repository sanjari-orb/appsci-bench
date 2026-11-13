"""Composition of the vendored tau2 retrieval runtime into a banking agent toolkit.

This is the appsci-bench-specific glue (NOT a verbatim tau2 port). It:

  * builds bm25 / grep / dense (cosine) RetrievalPipelines and a read-only shell
    sandbox from the DocumentCorpus, once, into a shareable ``RetrievalBundle``
    (the corpus is read-only, so a single bundle is shared across ``fresh()``);
  * exposes ``BankingAgentTools`` = ``KnowledgeTools`` (the existing transactional
    + discoverable tools) + the tau2 retrieval tools ``KB_search_bm25`` and
    ``KB_search_dense`` (via overrides that also LOG returned doc ids) and
    ``shell`` (inherited from ``ShellMixin``). That trio IS upstream's ``alltools``
    variant, the configuration the published leaderboard reports; ``grep`` is
    deliberately NOT exposed, though its pipeline is still built -- see the note in
    ``BankingAgentTools``;
  * keeps everything creds-free except dense: bm25/shell need no API key.
    ``KB_search_dense`` degrades gracefully (returns a guidance message) when no
    embeddings key is configured or the dense index fails to build.

Retrieval is READ-only over the corpus -- it never touches the TransactionalDB --
so ``state_hash()`` and the DB/ACTION reward are unaffected by any KB search.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from ..tools import KnowledgeTools
from ..shell_client import RemoteShell, shell_from_env
from .knowledge.pipeline import RetrievalPipeline
from .retrieval_mixins import ShellMixin, _format_kb_search_result
from .toolkit import ToolType, is_tool

# Fixed dense backend (the confirmed tau2-faithful choice).
DEFAULT_DENSE_EMBEDDING_MODEL = "text-embedding-3-large"
DEFAULT_TOP_K = 10

_DENSE_UNAVAILABLE_MSG = (
    "KB_search_dense is unavailable in this environment: no embeddings API key is "
    "configured (set OPENAI_API_KEY or BANKING_EMBED_API_KEY) or the dense index "
    "failed to build. Use KB_search_bm25 or shell instead -- they require no API key."
)


# =============================================================================
# Corpus -> documents
# =============================================================================


def _corpus_documents(corpus) -> List[Dict[str, Any]]:
    """Flatten a DocumentCorpus into the doc-dict shape every backend expects.

    Each dict carries BOTH ``text`` (bm25/dense/grep read ``text`` first, then
    fall back to ``content``) AND ``content`` (the sandbox's ``export_documents``
    reads ``content``), plus ``title``. The corpus stores ``content``, so ``text``
    mirrors it.
    """
    documents = []
    for doc in corpus.docs.values():
        content = doc.get("content", "") or ""
        documents.append(
            {
                "id": doc["id"],
                "title": doc.get("title", "") or doc["id"],
                "text": content,
                "content": content,
            }
        )
    return documents


# =============================================================================
# Pipeline / sandbox builders
# =============================================================================


def _build_bm25_pipeline(
    documents: List[Dict[str, Any]], top_k: int = DEFAULT_TOP_K
) -> RetrievalPipeline:
    config = {
        "document_preprocessors": [
            {"type": "bm25_indexer", "params": {"state_key": "bm25"}}
        ],
        "input_preprocessors": [],
        "retriever": {"type": "bm25", "params": {"top_k": top_k}},
        "postprocessors": [],
    }
    pipeline = RetrievalPipeline(config)
    pipeline.index_documents(documents)
    return pipeline


def _build_grep_pipeline(
    documents: List[Dict[str, Any]],
    top_k: int = DEFAULT_TOP_K,
    case_sensitive: bool = False,
) -> RetrievalPipeline:
    config = {
        "document_preprocessors": [],
        "input_preprocessors": [],
        "retriever": {
            "type": "grep",
            "params": {"top_k": top_k, "case_sensitive": case_sensitive},
        },
        "postprocessors": [],
    }
    pipeline = RetrievalPipeline(config)
    pipeline.index_documents(documents)
    return pipeline


def _build_dense_pipeline(
    documents: List[Dict[str, Any]],
    model: str,
    top_k: int = DEFAULT_TOP_K,
) -> RetrievalPipeline:
    """Build the cosine/dense pipeline. Indexing embeds all docs (network + key).

    ``embedder_params`` deliberately carries only ``model`` (no api_key): the
    embeddings cache is keyed by embedder config, so keeping the key out makes the
    cache portable and lets a baked cache match a runtime build. The OpenAI
    embedder resolves the key from ``OPENAI_API_KEY`` at call time.
    """
    embedder_params = {"model": model}
    config = {
        "document_preprocessors": [
            {
                "type": "embedding_indexer",
                "params": {
                    "embedder_type": "openai",
                    "embedder_params": embedder_params,
                    "state_key": "doc_embeddings",
                },
            }
        ],
        "input_preprocessors": [
            {
                "type": "embedding_encoder",
                "params": {
                    "embedder_type": "openai",
                    "embedder_params": embedder_params,
                    "input_key": "query",
                    "output_key": "query_embedding",
                },
            }
        ],
        "retriever": {"type": "cosine", "params": {"top_k": top_k}},
        "postprocessors": [],
    }
    pipeline = RetrievalPipeline(config)
    pipeline.index_documents(documents)
    return pipeline


def _build_sandbox(documents: List[Dict[str, Any]]) -> Optional[RemoteShell]:
    """Return a driver for the `shell` tool, or None when this deployment has no shell backend.

    WHAT CHANGED AND WHY. This used to construct upstream's SandboxManager, which shells out to
    Anthropic's sandbox-runtime and therefore needs bubblewrap and user namespaces. In a stock task
    container that fails at construction (`bwrap: No permissions to create a new namespace`), the
    exception was swallowed, and `shell` reported "Sandbox not initialized" in every run we have ever
    done -- so the tool was declared and dead, and our retrieval surface was narrower than the
    `alltools` variant the published leaderboard reports.

    Commands now run in a separate, contentless container (environment/shell_sidecar/) reached over the
    internal network. It holds the exported corpus and nothing else -- no task source, so no way to read
    the policy this task expects the agent to elicit. `documents` is no longer needed here: the sidecar's
    corpus is baked into its image at build time, which is what lets that container run with a read-only
    root filesystem.

    None when BANKING_SHELL_URL is unset -- unit tests, and any deployment that does not bring the
    sidecar up. Behaviour then is exactly what it was before: `shell` reports itself uninitialized while
    bm25 / dense / grep keep working.
    """
    del documents  # the sidecar owns its own copy of the corpus (baked at image build)
    return shell_from_env()


# =============================================================================
# Shared, corpus-derived retrieval bundle
# =============================================================================


class RetrievalBundle:
    """Immutable, corpus-derived retrieval backends shared across ``fresh()``.

    bm25 + grep pipelines and the shell sandbox are built eagerly (creds-free).
    The dense pipeline is built lazily on first ``dense_pipeline()`` call, because
    indexing embeds ~700 docs (needs a key + network, or a baked cache) and many
    runs never call ``KB_search_dense``.
    """

    def __init__(
        self,
        documents: List[Dict[str, Any]],
        *,
        dense_model: str = DEFAULT_DENSE_EMBEDDING_MODEL,
        dense_api_key: Optional[str] = None,
        top_k: int = DEFAULT_TOP_K,
        enable_dense: bool = True,
        enable_shell: bool = True,
    ) -> None:
        self._documents = documents
        self._dense_model = dense_model
        self._dense_api_key = dense_api_key
        self._top_k = top_k
        self._enable_dense = enable_dense

        # Eager, creds-free backends.
        self.bm25_pipeline = _build_bm25_pipeline(documents, top_k=top_k)
        self.grep_pipeline = _build_grep_pipeline(documents, top_k=top_k)
        self.sandbox = _build_sandbox(documents) if enable_shell else None

        # Lazy dense state.
        self._dense_pipeline: Optional[RetrievalPipeline] = None
        self._dense_failed = False

    def _resolve_dense_key(self) -> Optional[str]:
        return (
            self._dense_api_key
            or os.environ.get("BANKING_EMBED_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )

    def dense_pipeline(self) -> Optional[RetrievalPipeline]:
        """Return the dense pipeline, building it once on first use, or None.

        Returns None (without caching failure) when no key is configured yet --
        so a key provided later can still activate dense. A hard build failure
        (bad key, network error) is cached to avoid hammering.
        """
        if not self._enable_dense or self._dense_failed:
            return None
        if self._dense_pipeline is not None:
            return self._dense_pipeline

        key = self._resolve_dense_key()
        if not key:
            return None

        # The OpenAI embedder reads OPENAI_API_KEY from the environment; set it
        # from the resolved key if unset (the faithful tau2 path).
        if not os.environ.get("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = key

        try:
            self._dense_pipeline = _build_dense_pipeline(
                self._documents, self._dense_model, top_k=self._top_k
            )
        except Exception:
            self._dense_failed = True
            self._dense_pipeline = None
            return None
        return self._dense_pipeline


def build_retrieval_bundle(corpus, **kwargs) -> RetrievalBundle:
    """Build a RetrievalBundle from a DocumentCorpus.

    The dense embedding model defaults to ``$BANKING_EMBED_MODEL`` (else
    ``text-embedding-3-large``) unless an explicit ``dense_model`` is passed.
    """
    kwargs.setdefault(
        "dense_model",
        os.environ.get("BANKING_EMBED_MODEL") or DEFAULT_DENSE_EMBEDDING_MODEL,
    )
    return RetrievalBundle(_corpus_documents(corpus), **kwargs)


# =============================================================================
# Composed agent toolkit: transactional/discoverable tools + retrieval tools
# =============================================================================


class BankingAgentTools(ShellMixin, KnowledgeTools):
    """KnowledgeTools + upstream's ``alltools`` retrieval surface: bm25, dense, shell.

    Exactly the three tools upstream's ``alltools`` variant provides, which is what
    the leaderboard rows we compare against were run with. ``grep`` is deliberately
    absent (see the note below it).

    The two KB tools are defined here (rather than inherited from the tau2
    all-tools mixins) so each records the doc ids it returned into
    ``retrieved_document_ids`` -- the per-case set the env exposes for
    env-measured retrieval recall. Recording is read-only bookkeeping and never
    touches the DB. ``shell`` is inherited from ``ShellMixin`` (expects
    ``self._sandbox``); shell output is unstructured, so it is not doc-id logged.
    """

    def __init__(self, db, bundle: RetrievalBundle) -> None:
        super().__init__(db)
        self._bundle = bundle
        # Attribute names the retrieval mixins expect.
        self._kb_bm25_pipeline = bundle.bm25_pipeline
        self._grep_pipeline = bundle.grep_pipeline
        self._sandbox = bundle.sandbox
        # Per-case log of every doc id returned by a KB search / grep call.
        self.retrieved_document_ids: set[str] = set()

    def _record(self, results: List[Tuple[str, float]]) -> None:
        for doc_id, _score in results:
            self.retrieved_document_ids.add(doc_id)

    @is_tool(ToolType.READ)
    def KB_search_bm25(self, query: str, k: int = 10) -> str:
        """Search the knowledge base using BM25 sparse retrieval.

        Args:
            query: The search query to find relevant documents.
            k: Maximum number of documents to return (default 10).

        Returns:
            Relevant document excerpts matching the query.
        """
        result = self._kb_bm25_pipeline.retrieve(query, top_k=k, return_timing=True)
        self._record(result.results)
        return _format_kb_search_result(self._kb_bm25_pipeline, result)

    @is_tool(ToolType.READ)
    def KB_search_dense(self, query: str, k: int = 10) -> str:
        """Search the knowledge base using dense embedding retrieval.

        Args:
            query: The search query to find relevant documents.
            k: Maximum number of documents to return (default 10).

        Returns:
            Relevant document excerpts matching the query.
        """
        pipeline = self._bundle.dense_pipeline()
        if pipeline is None:
            return _DENSE_UNAVAILABLE_MSG
        result = pipeline.retrieve(query, top_k=k, return_timing=True)
        self._record(result.results)
        return _format_kb_search_result(pipeline, result)

    # `grep` IS DELIBERATELY NOT A TOOL HERE. Upstream's `alltools` variant -- the configuration 22 of
    # the leaderboard's 27 rows report, and the only figure our numbers are meant to be read against --
    # is exactly KB_search_bm25 + KB_search_dense + shell, with NO grep. Exposing it as well made our
    # surface a superset by one read-only tool, so every comparison needed a caveat instead of matching.
    # The regex capability is not lost to the agent, just relocated to where upstream has it: `shell`
    # runs real GNU grep over the corpus as files.
    #
    # The grep PIPELINE is still built (see RetrievalBundle) -- other upstream variants are grep-based,
    # so keeping the backend is what lets a variant switch stay a wiring change rather than a rewrite.
    # Vendored upstream tool code for it remains under _runtime/retrieval_mixins.py (GrepMixin), which
    # this class deliberately does not inherit.

