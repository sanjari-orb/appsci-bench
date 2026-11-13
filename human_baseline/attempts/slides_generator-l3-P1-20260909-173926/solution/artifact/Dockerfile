FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# $PORT is injected by the deployer (appsci_bench/orchestrator/deployer.py) -- server.py must bind
# 0.0.0.0:$PORT, never a hardcoded port.
CMD ["python", "server.py"]
