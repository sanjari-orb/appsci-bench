#!/bin/bash
# Compliance Review Task Setup Script
# Run by TaskRunner after the bundle is extracted. Initializes the database with fixture
# data, then cleans up.

set -e

echo "=== Compliance Review Setup ==="

# Export once rather than prefixing each psql invocation -- PGPASSWORD=... psql ... leaks the
# value via /proc/<pid>/environ to other processes on the same container. Low risk here (disposable
# sandbox placeholder), but this script is a template other tasks are likely to copy.
export PGPASSWORD="$POSTGRES_PASSWORD"

echo "Waiting for database connection..."
for i in {1..30}; do
    if psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT 1" > /dev/null 2>&1; then
        echo "Database connection established"
        break
    fi
    echo "Attempt $i/30: Database not ready, waiting..."
    sleep 1
done

echo "Creating database schema..."
psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DB -f /scripts/01_schema.sql

echo "Loading fixture data into database..."
python /scripts/02_load_data.py

echo "Cleaning up fixture files..."
rm -rf /fixture

echo "=== Setup Complete ==="
