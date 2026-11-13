"""Warm the dense-embeddings cache for the banking corpus (optional image-build step).

Embeds every corpus document once via the configured OpenAI embedder and writes the vectors to
``$BANKING_EMBEDDINGS_CACHE_DIR`` (tau2's on-disk ``EmbeddingsCache``, populated by the dense
pipeline's embedding_indexer). With the cache present, the first ``KB_search_dense`` at runtime
skips the ~700-doc embed pass and is fast/cheap.

Needs an embeddings key (``BANKING_EMBED_API_KEY`` or ``OPENAI_API_KEY``) and network access, so it
is NOT part of the default (creds-free) image build. Run it manually, or in a Docker build stage
that passes a build-time key:

    BANKING_EMBED_API_KEY=sk-... \
        python -m tasks.banking_support.environment._runtime.build_embeddings_cache

Exit codes: 0 = cache warmed; 2 = no key configured (nothing done); 1 = dense build failed.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    from ..corpus import DocumentCorpus
    from .retrieval import build_retrieval_bundle

    if not (os.environ.get("BANKING_EMBED_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        print(
            "No embeddings key set (BANKING_EMBED_API_KEY / OPENAI_API_KEY); nothing to bake.",
            file=sys.stderr,
        )
        return 2

    corpus = DocumentCorpus.load()
    bundle = build_retrieval_bundle(corpus)
    # Building the dense pipeline runs the embedding_indexer, which embeds every doc and writes the
    # vectors to the on-disk EmbeddingsCache under BANKING_EMBEDDINGS_CACHE_DIR.
    pipeline = bundle.dense_pipeline()
    if pipeline is None:
        print("Dense pipeline failed to build (bad key / network / import).", file=sys.stderr)
        return 1

    cache_dir = os.environ.get("BANKING_EMBEDDINGS_CACHE_DIR") or os.path.join(
        "data", ".embeddings_cache"
    )
    print(f"Dense embeddings cache warmed for {len(corpus)} docs at {cache_dir}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
