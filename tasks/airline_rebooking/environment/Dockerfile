# Airline Task Environment image
#
# Containerizes the airline FastAPI task-environment service
# (tasks/airline_rebooking/environment/service.py) so the airline task can run
# under the Docker BenchmarkOrchestrator / TaskRunner, alongside compose-based
# tasks like order_to_cash.
#
# The service exposes (all bearer-token gated):
#   GET  /health
#   POST /tools/{name}      (tool token AIRLINE_ENV_TOKEN; artifact-facing)
#   POST /admin/reset       (admin token AIRLINE_ENV_ADMIN_TOKEN; evaluator-only)
#   GET  /admin/state_hash  (admin token; evaluator-only)
#
# NOTE: build context MUST be the repo root (the image copies the task's env
# package under tasks/). docker-compose.yml sets `context: ../../..` for this.
#
#   docker build -f tasks/airline_rebooking/environment/Dockerfile -t appsci-bench/airline:latest .

FROM python:3.11-slim

# curl is used by the TaskRunner readiness gate (environment/setup/setup.sh),
# which polls the token-gated /health from inside the container.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The airline task-env service is self-contained: it imports only its own package
# (tasks.airline_rebooking.environment.*, stdlib + FastAPI/pydantic) plus the DB fixture beside it.
# It needs NEITHER appsci_bench NOR shared, so install just the web deps and copy the task env
# package -- nothing evaluator-side ever lands in this container.
RUN pip install --no-cache-dir "fastapi>=0.136" "uvicorn>=0.49" "pydantic>=2.13"

# Package chain for `tasks.airline_rebooking.environment` (imported via PYTHONPATH=/app + __init__.py).
COPY tasks/__init__.py                        /app/tasks/__init__.py
COPY tasks/airline_rebooking/__init__.py      /app/tasks/airline_rebooking/__init__.py
COPY tasks/airline_rebooking/environment/*.py /app/tasks/airline_rebooking/environment/
# NO DATA IS BAKED IN. Both phases run from this one image, so anything copied here is present
# in the BUILD container too -- which would make the carved build share decorative while the
# agent still reads the full store. Each phase bundles its own fixture at run time and its
# setup script loads it (docs/task_env_carve_spec.md steps 5-7); the service boots empty.

ENV PYTHONPATH=/app

# The tool-API port other containers reach as http://airline-workspace-1:8080
EXPOSE 8080

CMD ["uvicorn", "tasks.airline_rebooking.environment.service:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8080"]
