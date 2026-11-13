# Customer-Call Insights task environment image.
#
# Self-contained (FROM python:3.11-slim), matching order_to_cash and airline_rebooking so a fresh
# clone needs no prerequisite base-image build. This task does not use appsci-base's extras
# (loguru, jq, git, smoke.sh) -- setup.sh needs only psql, python, and psycopg2.
#
# Do NOT bake task data here -- the TaskRunner injects it into /fixture and /scripts at runtime, and
# db/setup.sh deletes /fixture afterwards because it carries the author's label record.

FROM python:3.11-slim

# Keeps the agent's bind-mounted scratch space free of root-owned __pycache__ dirs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# gcc is only needed while pip builds; drop it from the agent's sandbox.
RUN apt-get purge -y gcc \
    && apt-get autoremove -y

WORKDIR /work

# Keep-alive so the TaskRunner can `docker exec` into a long-lived container.
CMD ["tail", "-f", "/dev/null"]
