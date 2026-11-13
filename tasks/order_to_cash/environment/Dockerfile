# Order-to-Cash Task Environment
# This image provides the runtime environment for the order_to_cash benchmark task.
# Note: Data files are transferred by TaskRunner via bundle, not baked into the image.

FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create directory structure
RUN mkdir -p /data /fixture /scripts /work/out

# Copy and install Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Cleanup build dependencies
RUN apt-get purge -y gcc \
    && apt-get autoremove -y

# Set working directory
WORKDIR /work

# Default command - keep container running for TaskRunner to exec into
CMD ["tail", "-f", "/dev/null"]
