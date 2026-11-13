from typing import Any, Dict, List

import numpy as np

from ..embedders import OpenAIEmbedder
from ..embeddings_cache import get_embeddings_cache
from ..registry import register_document_preprocessor
from .base import BaseDocumentPreprocessor

# OpenAI-only: the banking_support dense backend is fixed to OpenAI text-embedding-3-large.
# tau2's OpenRouter/Qwen embedder is out of scope and not vendored.
EMBEDDER_REGISTRY = {
    "openai": OpenAIEmbedder,
}


@register_document_preprocessor("embedding_indexer")
class EmbeddingIndexer(BaseDocumentPreprocessor):
    def __init__(
        self,
        embedder_type: str = "openai",
        embedder_params: Dict[str, Any] = None,
        state_key: str = "doc_embeddings",
        content_field: str = "text",
        batch_size: int = None,
        use_cache: bool = True,
        **kwargs,
    ):
        super().__init__(
            embedder_type=embedder_type,
            embedder_params=embedder_params,
            state_key=state_key,
            content_field=content_field,
            batch_size=batch_size,
            use_cache=use_cache,
            **kwargs,
        )
        self.embedder_type = embedder_type
        self.embedder_params = embedder_params or {}
        self.state_key = state_key
        self.content_field = content_field
        self.batch_size = batch_size
        self.use_cache = use_cache
        self._embedder = None

    def _get_embedder(self):
        if self._embedder is None:
            if self.embedder_type not in EMBEDDER_REGISTRY:
                available = list(EMBEDDER_REGISTRY.keys())
                raise ValueError(
                    f"Unknown embedder_type: {self.embedder_type}. Available: {available}"
                )

            # For document indexing, documents are embedded as-is (no instruction prefix).
            params = dict(self.embedder_params)
            self._embedder = EMBEDDER_REGISTRY[self.embedder_type](**params)

        return self._embedder

    def process(
        self, documents: List[Dict[str, Any]], state: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        docs_for_cache = [
            {
                "id": doc["id"],
                "text": doc.get(self.content_field)
                or doc.get("content")
                or doc.get("text"),
            }
            for doc in documents
        ]

        for doc, cache_doc in zip(documents, docs_for_cache):
            if cache_doc["text"] is None:
                raise ValueError(
                    f"Document {doc.get('id', 'unknown')} missing content field"
                )

        if self.use_cache:
            cache = get_embeddings_cache()
            cached = cache.get(docs_for_cache, self.embedder_type, self.embedder_params)

            if cached is not None:
                embeddings, doc_ids = cached
                state[self.state_key] = embeddings
                state[f"{self.state_key}_doc_ids"] = doc_ids
                return documents

        texts = [doc["text"] for doc in docs_for_cache]
        embedder = self._get_embedder()

        if self.batch_size is None or self.batch_size >= len(texts):
            embeddings = embedder.embed(texts)
        else:
            all_embeddings = []
            for i in range(0, len(texts), self.batch_size):
                batch_texts = texts[i : i + self.batch_size]
                batch_embeddings = embedder.embed(batch_texts)
                all_embeddings.append(batch_embeddings)
            embeddings = np.vstack(all_embeddings)

        doc_ids = [doc["id"] for doc in documents]

        if self.use_cache:
            cache.put(
                docs_for_cache,
                self.embedder_type,
                embeddings,
                doc_ids,
                self.embedder_params,
            )

        state[self.state_key] = embeddings
        state[f"{self.state_key}_doc_ids"] = doc_ids

        return documents
