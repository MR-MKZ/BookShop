#!/bin/bash
set -euo pipefail

export PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}"

echo "Waiting for database..."
python /app/scripts/wait_for_db.py

echo "Running migrations..."
alembic upgrade head

echo "Ensuring first admin..."
python /app/scripts/ensure_admin.py

echo "Starting application..."
exec "$@"
