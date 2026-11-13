"""Input preprocessors: transform the query before retrieval.

Vendored from tau2.knowledge.input_preprocessors. Only embedding_encoder is ported (it encodes
the query for dense/cosine retrieval); bm25 and grep consume the raw query directly.
"""
