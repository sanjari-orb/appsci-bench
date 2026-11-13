"""Document preprocessors: build retrieval indexes during index_documents().

Vendored from tau2.knowledge.document_preprocessors. Only bm25_indexer (sparse) and
embedding_indexer (dense) are ported -- the retrieval surface here is bm25 + dense + grep + shell,
and grep needs no index (it reads doc_content_map, populated by the pipeline itself).
"""
