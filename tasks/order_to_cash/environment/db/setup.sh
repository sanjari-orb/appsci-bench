#!/bin/bash
# Order-to-Cash Task Setup Script
# This script is run by TaskRunner after the bundle is extracted.
# It initializes the database with fixture data, then cleans up.

set -e

echo "=== Order-to-Cash Setup ==="

# Wait for database to be fully ready (in case healthcheck passed but DB not fully initialized)
echo "Waiting for database connection..."
for i in {1..30}; do
    if PGPASSWORD=$POSTGRES_PASSWORD psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT 1" > /dev/null 2>&1; then
        echo "Database connection established"
        break
    fi
    echo "Attempt $i/30: Database not ready, waiting..."
    sleep 1
done

# Initialize schema
echo "Creating database schema..."
PGPASSWORD=$POSTGRES_PASSWORD psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DB -f /scripts/01_schema.sql

# Load fixture data
echo "Loading fixture data into database..."
python /scripts/02_load_data.py

# Cleanup: remove fixture files now that they're in the DB
echo "Cleaning up fixture files..."
rm -rf /fixture

echo "=== Setup Complete ==="
