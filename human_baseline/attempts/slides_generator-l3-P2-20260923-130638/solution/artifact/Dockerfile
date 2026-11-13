FROM python:3.11-slim

WORKDIR /app

# System libraries:
#  - libxml2 / libxslt1.1     : lxml, used by python-pptx and python-docx
#  - libjpeg62-turbo / libpng16-16 : Pillow image codecs
#  - libreoffice + fonts      : headless render of the .pptx to images for the visual self-check.
#    LibreOffice is large; the self-check degrades to geometry-only checks if soffice is absent, so
#    this layer can be dropped to shrink the image at the cost of the VLM render pass.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
        libjpeg62-turbo \
        libpng16-16 \
        libreoffice-impress \
        fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# $PORT is injected by the deployer (appsci_bench/orchestrator/deployer.py) -- server.py must bind
# 0.0.0.0:$PORT, never a hardcoded port.
CMD ["python", "server.py"]
