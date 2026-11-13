FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# tasks/order_to_cash/spec.yaml declares no task_runner.agent_env, so the deployer never passes a DB
# URL to this container -- set it here to the task-env's fixed sandbox credentials (see
# tasks/order_to_cash/environment/docker-compose.yml). The artifact container joins the same
# bench-network as the task's db container, so the `db` hostname resolves.
ENV DATABASE_URL=postgres://sandbox:sandbox@db:5432/oltp

# $PORT is injected by the deployer (appsci_bench/orchestrator/deployer.py) -- server.py must bind
# 0.0.0.0:$PORT, never a hardcoded port.
CMD ["python", "server.py"]
