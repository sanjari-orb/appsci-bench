# Banking-support Task Environment image
#
# Containerizes the banking FastAPI task-environment service
# (tasks/banking_support/environment/service.py) so the banking_support task can run under the
# Docker BenchmarkOrchestrator / TaskRunner, mirroring the airline env image.
#
# The service exposes (all bearer-token gated):
#   GET  /health
#   GET  /documents, GET /documents/{id}   (tool token BANKING_ENV_TOKEN; artifact-facing, raw KB)
#   POST /tools/{name}                     (tool token; agent tools incl. retrieval KB_search_bm25/dense/grep/shell + user-sim)
#   POST /admin/{reset,load,seed}, GET /admin/{state_hash,retrieved_docs,action_trace}   (admin token BANKING_ENV_ADMIN_TOKEN; evaluator-only)
#
# NOTE: build context MUST be the repo root (the image copies the task's env package under tasks/).
# docker-compose.yml sets `context: ../../..` for exactly this reason.
#
#   docker build -f tasks/banking_support/environment/Dockerfile -t appsci-bench/banking:latest .

FROM python:3.11-slim

# curl is used by the TaskRunner readiness gate, which polls token-gated /health in-container.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The banking task-env service is self-contained: it imports only its own package
# (tasks.banking_support.environment.*), the stdlib, and the six third-party libraries below. It
# needs NEITHER appsci_bench NOR shared, so nothing evaluator-side ever lands in this container.
# fastapi/uvicorn/pydantic serve the API; rank_bm25 backs KB_search_bm25; numpy + openai back
# KB_search_dense. `shell` needs nothing here -- it executes in a separate container.
RUN pip install --no-cache-dir "fastapi>=0.136" "uvicorn>=0.49" "pydantic>=2.13" \
    "rank_bm25>=0.2.2" "numpy>=1.26" "openai>=1.40"

# Package chain for `tasks.banking_support.environment` (imported via PYTHONPATH=/app + __init__.py).
COPY tasks/__init__.py                          /app/tasks/__init__.py
COPY tasks/banking_support/__init__.py          /app/tasks/banking_support/__init__.py
COPY tasks/banking_support/environment/*.py     /app/tasks/banking_support/environment/
COPY tasks/banking_support/environment/_runtime /app/tasks/banking_support/environment/_runtime/

# Bake ONLY the raw KB corpus into the image. It is shared whole with both phases -- identical
# documents, identical retrieval index -- so there is nothing to carve and nothing to load per phase,
# and baking it is what lets the service boot with its retrieval pipelines already built rather than
# re-indexing ~700 documents when a phase's data arrives.
#
# THE TRANSACTIONAL STORE IS DELIBERATELY NOT HERE, and BANKING_DB_PATH is deliberately unset. Both
# phases run from this one image, so a store copied in would be present in the BUILD container no
# matter what that phase bundled -- the agent would read every record the eval cases are drawn from
# while every surface reported healthy, which is how a carve becomes decorative. Each phase bundles its
# own store to /fixture/ at run time and its setup script loads it through POST /admin/load; the
# service boots holding no records (see service.py and environment/setup/setup.sh).
COPY tasks/banking_support/environment/documents  /app/banking_docs/

ENV PYTHONPATH=/app
ENV BANKING_DOCS_PATH=/app/banking_docs

# Retrieval runtime: BM25 runs over the baked corpus with NO API key. KB_search_dense needs an
# embeddings key at runtime (BANKING_EMBED_API_KEY, else OPENAI_API_KEY), injected by
# docker-compose / the orchestrator -- deliberately NOT baked into the image. The dense index is
# built lazily on first KB_search_dense and cached on disk at BANKING_EMBEDDINGS_CACHE_DIR (below).
# `shell` executes in a SEPARATE container (environment/shell_sidecar/) reached at BANKING_SHELL_URL --
# nothing for it is installed here, deliberately: a shell running in THIS image could read tools.py and
# with it the policy the agent is supposed to elicit. With that var unset the tool reports itself
# uninitialized and bm25/dense/grep keep working.
# Optional: to bake the dense cache at build time (fast first call, but needs a build-time key), run
#   `python -m tasks.banking_support.environment._runtime.build_embeddings_cache` under a build ARG
# key before EXPOSE. Left out here so the image builds creds-free.
ENV BANKING_EMBEDDINGS_CACHE_DIR=/app/banking_embeddings_cache
RUN mkdir -p /app/banking_embeddings_cache

# The tool-API port other containers reach as http://banking-workspace-1:8080
EXPOSE 8080

CMD ["uvicorn", "tasks.banking_support.environment.service:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8080"]
