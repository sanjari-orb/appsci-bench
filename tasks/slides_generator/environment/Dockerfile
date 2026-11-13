# Slide Generator Task Environment
# Provides the runtime workspace where the applied-scientist agent explores resources
# and builds the slides-generator artifact.
# Note: the agent's build-time inputs (/data/samples/*, /sop/kpis.md) are delivered by TaskRunner at
# runtime, as a tar bundle extracted at / per the build phase's declaration in spec.yaml. They are
# NOT baked into this image and NOT bind-mounted. Only /scripts/ below is baked in.

FROM python:3.11-slim

WORKDIR /workspace

# System libraries required by python-pptx (lxml XML parser, Pillow image codec)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    libjpeg62-turbo \
    libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Directory skeleton. TaskRunner's tar extraction creates these anyway; declared so the image's shape
# matches what the bundle delivers, and so an operator who execs in before setup sees the layout.
RUN mkdir -p /data /sop /docs /work/out

# Utility scripts
COPY scripts/ /scripts/
RUN chmod +x /scripts/*.sh

# Set working directory
WORKDIR /work

# Default command - keep container running for TaskRunner to exec into
CMD ["tail", "-f", "/dev/null"]
