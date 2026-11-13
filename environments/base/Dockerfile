# Shared base image for appsci-bench task environments.
#
# Task images do `FROM appsci-base:v0` and add ONLY their own system packages
# and Python dependencies (see tasks/_TEMPLATE/environment/Dockerfile).
#
# Task DATA is injected at runtime by the TaskRunner (bundle + docker cp) into
# the container. Bundle extraction (tar -xzf -C /) creates destination dirs
# such as /data, /fixture, and /scripts per the task's spec.yaml bundle config.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LOG_LEVEL=INFO

# Tooling common to every task environment.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    jq \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Shared Python deps (loguru for opt-in in-container logging in task scripts).
COPY environments/base/requirements.txt /tmp/base-requirements.txt
RUN pip install --no-cache-dir -r /tmp/base-requirements.txt \
    && rm -f /tmp/base-requirements.txt

# Generic environment smoke check (tooling + writable WORKDIR).
COPY --chmod=755 environments/base/smoke.sh /opt/appsci/smoke.sh

WORKDIR /work

# tini reaps zombies and forwards signals; the keep-alive CMD lets the
# TaskRunner `docker exec` into a long-lived container. Task images may
# override CMD but should keep the container running.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["tail", "-f", "/dev/null"]
