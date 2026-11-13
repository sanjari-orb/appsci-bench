"""Embedders for dense retrieval. Vendored from tau2.knowledge.embedders.

Only the OpenAI embedder is ported: the banking_support dense backend is fixed to OpenAI
`text-embedding-3-large` (the confirmed "faithful to tau2" choice). tau2's OpenRouter/Qwen
embedder is out of scope and is not vendored, so the EMBEDDER_REGISTRY here is openai-only.
"""

from .base import BaseEmbedder
from .openai_embedder import OpenAIEmbedder

__all__ = ["BaseEmbedder", "OpenAIEmbedder"]
