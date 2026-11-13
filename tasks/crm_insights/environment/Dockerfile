# CRM Insights Task Environment
# This image provides the runtime environment for the crm_insights benchmark task.
# Note: Data files are transferred by TaskRunner via bundle, not baked into the image.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /data /fixture /scripts /work/out

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

RUN apt-get purge -y gcc \
    && apt-get autoremove -y

WORKDIR /work

CMD ["tail", "-f", "/dev/null"]
