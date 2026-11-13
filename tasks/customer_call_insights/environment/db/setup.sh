#!/usr/bin/env bash
# Customer-Call Insights task setup. Run by the TaskRunner after bundle extraction.
#
# Creates the schema, loads the seeded calls, deletes /fixture, and asserts the result.
#
# The /fixture deletion is load-bearing: it contains labels.csv, the author's record of the correct
# reading for every seeded call. If it survived, the agent could read the answer key out of the
# container. The rm is verified rather than trusted.
#
# The row-count assertion exists because this script is the only place that can fail a run on a broken
# environment -- the framework validates spec.yaml's shape but never checks the resources are real.
set -euo pipefail

echo "=== Customer-Call Insights setup ==="

if [[ ! -f /fixture/transcripts.jsonl ]]; then
  echo "expected /fixture/transcripts.jsonl after bundle extraction, not found" >&2
  exit 1
fi
expected_calls=$(grep -c . /fixture/transcripts.jsonl)

# Export once rather than prefixing each psql call -- PGPASSWORD=... psql ... leaks the value via
# /proc/<pid>/environ to other processes in this container.
export PGPASSWORD="$POSTGRES_PASSWORD"

echo "Waiting for database connection..."
for i in {1..30}; do
    if psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1" > /dev/null 2>&1; then
        echo "Database connection established"
        break
    fi
    echo "Attempt $i/30: Database not ready, waiting..."
    sleep 1
done

echo "Creating database schema..."
psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /scripts/01_schema.sql

echo "Loading the seeded calls..."
python /scripts/02_load_data.py

rm -rf /fixture
if [[ -e /fixture ]]; then
  echo "FATAL: /fixture still present after rm -- refusing to continue with the label record readable" >&2
  exit 1
fi
echo "removed /fixture (label record no longer readable)"

loaded_calls=$(psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM calls")
if [[ "$loaded_calls" != "$expected_calls" ]]; then
  echo "FATAL: fixture carried $expected_calls call(s) but the calls table holds $loaded_calls" >&2
  exit 1
fi
echo "verified $loaded_calls call(s) in the calls table"

echo "=== Setup complete ==="
