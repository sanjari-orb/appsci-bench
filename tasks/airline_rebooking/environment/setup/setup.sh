#!/usr/bin/env bash
# Airline task-env setup, run by TaskRunner inside the container AFTER the phase's bundle is
# extracted. Shared by both phases -- it loads whatever that phase put in /fixture/, which is what
# makes the build/eval split a data decision rather than a code one (the same shape order_to_cash
# uses).
#
# WHY A LOAD STEP EXISTS AT ALL. The service boots holding NO records. Both phases run from one image,
# and the harness copies a phase's data in only after the container is running, so a service that read
# its fixture at start-up could serve only what was baked into the image -- and a baked store is
# present in the build container no matter what was bundled, which is how a carve becomes decorative.
#
# THE ASSERTIONS AT THE END ARE THE POINT, not decoration. `/health` answers while the store is empty,
# by design, so container readiness proves nothing about data. Without a check here, an absent or
# partial load reads as success and the run comes back all zeros with nothing to explain it. A failing
# setup script aborts the run, which is the right outcome when the records are missing.
set -euo pipefail

BASE="http://localhost:8080"
FIXTURE="${AIRLINE_FIXTURE_PATH:-/fixture/db.json}"
TOOL_TOKEN="${AIRLINE_ENV_TOKEN:-}"
# The load route is ADMIN-gated: which records exist is the build/eval difference, so the artifact must
# be able neither to load them nor to count them. Only this script and the evaluator hold this token.
ADMIN_TOKEN="${AIRLINE_ENV_ADMIN_TOKEN:-}"

if [ -z "$ADMIN_TOKEN" ]; then
    echo "AIRLINE_ENV_ADMIN_TOKEN is not set; cannot load this phase's store" >&2
    exit 1
fi

# 1. Wait for the process, not for data.
for _ in $(seq 1 30); do
    if curl -fsS -H "Authorization: Bearer ${TOOL_TOKEN}" "${BASE}/health" >/dev/null 2>&1; then
        echo "airline task-env process healthy"
        break
    fi
    sleep 1
done
if ! curl -fsS -H "Authorization: Bearer ${TOOL_TOKEN}" "${BASE}/health" >/dev/null 2>&1; then
    echo "airline task-env did not become healthy within 30s" >&2
    exit 1
fi

if [ ! -f "$FIXTURE" ]; then
    echo "no fixture at ${FIXTURE} -- this phase bundled no data, so nothing would be loaded" >&2
    exit 1
fi

# 2. Load this phase's store. The route takes a PATH: the file is already here, and the eval store is
#    ~2 MB of JSON that has no business going through a request body.
echo "loading ${FIXTURE} ..."
RESPONSE=$(curl -fsS -X POST \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"path\": \"${FIXTURE}\"}" \
    "${BASE}/admin/load")
echo "load reported: ${RESPONSE}"

# 3. Assert the records actually landed, per collection, reading the counts the service reports back.
#    Deliberately "> 0" per collection rather than an expected total: this one script serves both
#    phases, whose totals differ by two orders of magnitude, and hardcoding either here would make the
#    script wrong for the other phase. The exact per-phase figures are checked by
#    scripts/carve_guard.py and tests/tasks/airline_rebooking/, against the files themselves.
python3 - "$RESPONSE" <<'PY'
import json, sys

counts = (json.loads(sys.argv[1]) or {}).get("counts") or {}
missing = [name for name in ("flights", "users", "reservations") if not counts.get(name)]
if missing:
    print(f"FAILED: these collections loaded no records: {missing} (counts={counts})", file=sys.stderr)
    raise SystemExit(1)
print("loaded:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
PY

# 4. Remove the raw fixture now the store is live, as order_to_cash does. The agent reaches this data
#    through the tool API; leaving a readable copy of the records on the container filesystem is a
#    second, unaudited path to them.
rm -rf /fixture

echo "airline task-env setup complete"
