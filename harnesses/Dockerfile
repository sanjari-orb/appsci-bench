# No benchmark/evaluator source or task ground truth is copied into this image.
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv curl ca-certificates git ripgrep build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY harnesses/versions.env /opt/appsci/harnesses/versions.env
COPY scripts/install_harness_clis.sh /opt/appsci/scripts/install_harness_clis.sh
RUN APPSCI_CLI_PREFIX=/opt/harness-clis bash /opt/appsci/scripts/install_harness_clis.sh
ENV PATH="/opt/harness-clis/bin:/opt/venv/bin:${PATH}"
RUN useradd --create-home --uid 1001 --shell /bin/bash builder \
    && python3 -m venv /opt/venv \
    && mkdir -p /work/out \
    && chown -R builder:builder /work /opt/venv
COPY appsci_bench/core/native_harness.py /opt/appsci/adapter.py
USER builder
WORKDIR /work
ENTRYPOINT ["python3", "/opt/appsci/adapter.py"]
