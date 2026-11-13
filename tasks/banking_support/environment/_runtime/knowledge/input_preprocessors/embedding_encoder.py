from typing import Any, Dict, List

from ..embedders import OpenAIEmbedder
from ..embeddings_cache import (
    cache_query_embedding,
    get_cached_query_embedding,
)
from ..registry import register_input_preprocessor
from .base import BaseInputPreprocessor

# OpenAI-only: the banking_support dense backend is fixed to OpenAI text-embedding-3-large.
EMBEDDER_REGISTRY = {
    "openai": OpenAIEmbedder,
}


@register_input_preprocessor("embedding_encoder")
class EmbeddingEncoder(BaseInputPreprocessor):
    """Encodes queries into embeddings for dense/cosine retrieval."""

    def __init__(
        self,
        embedder_type: str = "openai",
        embedder_params: Dict[str, Any] = None,
        input_key: str = "query",
        output_key: str = "query_embedding",
        **kwargs,
    ):
        super().__init__(
            embedder_type=embedder_type,
            embedder_params=embedder_params,
            input_key=input_key,
            output_key=output_key,
            **kwargs,
        )
        self.embedder_type = embedder_type
        self.embedder_params = embedder_params or {}
        self.input_key = input_key
        self.output_key = output_key
        self._embedder = None
        self._cache_config: Dict[str, Any] | None = None

    def _get_embedder(self):
        if self._embedder is None:
            if self.embedder_type not in EMBEDDER_REGISTRY:
                available = list(EMBEDDER_REGISTRY.keys())
                raise ValueError(
                    f"Unknown embedder_type: {self.embedder_type}. Available: {available}"
                )

            self._embedder = EMBEDDER_REGISTRY[self.embedder_type](
                **self.embedder_params
            )

        return self._embedder

    def _get_cache_config(self) -> Dict[str, Any]:
        """Return embedder params augmented with the effective instruction prefix.

        The instruction prefix affects the embedding output but is derived
        internally by the embedder, not passed in embedder_params.  Including it
        in the cache config ensures that changing the instruction invalidates
        stale cached embeddings instead of serving them silently. (The OpenAI
        embedder has no query_instruction, so this is a no-op for it.)
        """
        if self._cache_config is None:
            embedder = self._get_embedder()
            config = dict(self.embedder_params)
            instruction = getattr(embedder, "query_instruction", None)
            if instruction:
                config["_query_instruction"] = instruction
            self._cache_config = config
        return self._cache_config

    def process(
        self, input_data: Dict[str, Any], state: Dict[str, Any]
    ) -> Dict[str, Any]:
        text = input_data.get(self.input_key, "")
        if not text or not text.strip():
            raise ValueError(
                f"Empty or missing input for key '{self.input_key}': "
                f"cannot generate embedding from blank text."
            )

        cache_config = self._get_cache_config()

        cached = get_cached_query_embedding(text, self.embedder_type, cache_config)
        if cached is not None:
            input_data[self.output_key] = cached
            return input_data

        embedder = self._get_embedder()
        embedding = embedder.embed([text])[0]

        cache_query_embedding(text, embedding, self.embedder_type, cache_config)

        input_data[self.output_key] = embedding
        return input_data

    def process_batch(
        self, input_data_list: List[Dict[str, Any]], state: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        texts = [input_data[self.input_key] for input_data in input_data_list]

        cache_config = self._get_cache_config()

        cached_embeddings: Dict[int, Any] = {}
        texts_to_embed: List[tuple[int, str]] = []

        for i, text in enumerate(texts):
            cached = get_cached_query_embedding(text, self.embedder_type, cache_config)
            if cached is not None:
                cached_embeddings[i] = cached
            else:
                texts_to_embed.append((i, text))

        if texts_to_embed:
            embedder = self._get_embedder()
            uncached_texts = [t for _, t in texts_to_embed]
            new_embeddings = embedder.embed(uncached_texts)

            for (i, text), embedding in zip(texts_to_embed, new_embeddings):
                cache_query_embedding(text, embedding, self.embedder_type, cache_config)
                cached_embeddings[i] = embedding

        for i, input_data in enumerate(input_data_list):
            input_data[self.output_key] = cached_embeddings[i]

        return input_data_list
