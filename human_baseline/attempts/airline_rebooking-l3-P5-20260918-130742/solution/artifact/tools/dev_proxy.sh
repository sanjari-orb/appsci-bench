#!/usr/bin/env bash
# Open (or close) a tunnel from the host to the task environment, and reset its data.
#
# The task environment listens on 8080 INSIDE its container and that port is not published, so nothing
# on the host can reach it directly. This puts a socat container on the same docker network and
# publishes 18080 on the host, which is what tools/chat.py expects by default.
#
#   tools/dev_proxy.sh up      open the tunnel
#   tools/dev_proxy.sh down    close it
#   tools/dev_proxy.sh status  is it up, and is the environment answering?
#   tools/dev_proxy.sh reset   restore the pristine store (undo writes made while testing)
set -euo pipefail

PROXY_NAME="airline-env-proxy"
ENV_CONTAINER="${AIRLINE_ENV_CONTAINER:-airline-workspace-1}"
NETWORK="${AIRLINE_ENV_NETWORK:-bench-network}"
HOST_PORT="${AIRLINE_PROXY_PORT:-18080}"
TOOL_TOKEN="${AIRLINE_ENV_TOKEN:-airline-tool-token-local}"
ADMIN_TOKEN="${AIRLINE_ENV_ADMIN_TOKEN:-airline-admin-token-local}"

case "${1:-status}" in
  up)
    docker rm -f "$PROXY_NAME" >/dev/null 2>&1 || true
    docker run -d --name "$PROXY_NAME" --network "$NETWORK" -p "${HOST_PORT}:${HOST_PORT}" \
      alpine/socat "TCP-LISTEN:${HOST_PORT},fork,reuseaddr" "TCP:${ENV_CONTAINER}:8080" >/dev/null
    sleep 2
    if curl -fsS -H "Authorization: Bearer ${TOOL_TOKEN}" "http://localhost:${HOST_PORT}/health" >/dev/null; then
      echo "tunnel up: http://localhost:${HOST_PORT} -> ${ENV_CONTAINER}:8080"
    else
      echo "tunnel started but the environment did not answer; is ${ENV_CONTAINER} running?" >&2
      exit 1
    fi
    ;;
  down)
    docker rm -f "$PROXY_NAME" >/dev/null 2>&1 || true
    echo "tunnel down"
    ;;
  status)
    if docker ps --format '{{.Names}}' | grep -qx "$PROXY_NAME"; then
      echo "tunnel container: up"
    else
      echo "tunnel container: down  (run: tools/dev_proxy.sh up)"
    fi
    if curl -fsS -H "Authorization: Bearer ${TOOL_TOKEN}" "http://localhost:${HOST_PORT}/health" 2>/dev/null; then
      echo "  environment answering on localhost:${HOST_PORT}"
    else
      echo "  environment NOT answering on localhost:${HOST_PORT}"
    fi
    # The admin routes are only reachable from inside the container.
    docker exec "$ENV_CONTAINER" curl -s -H "Authorization: Bearer ${ADMIN_TOKEN}" \
      http://localhost:8080/admin/state_hash 2>/dev/null | sed 's/^/  db: /' || true
    echo
    ;;
  reset)
    # Restores the pristine fixture. Use after a test conversation performed a write.
    docker exec "$ENV_CONTAINER" curl -s -X POST \
      -H "Authorization: Bearer ${ADMIN_TOKEN}" http://localhost:8080/admin/reset
    echo
    docker exec "$ENV_CONTAINER" curl -s -H "Authorization: Bearer ${ADMIN_TOKEN}" \
      http://localhost:8080/admin/state_hash | sed 's/^/db now: /'
    echo
    ;;
  *)
    echo "usage: tools/dev_proxy.sh {up|down|status|reset}" >&2
    exit 2
    ;;
esac
