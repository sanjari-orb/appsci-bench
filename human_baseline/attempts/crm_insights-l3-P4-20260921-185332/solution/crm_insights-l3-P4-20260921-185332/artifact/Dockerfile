FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# $PORT is injected by the deployer -- bind it, never a hardcoded port.
CMD ["python", "server.py"]
