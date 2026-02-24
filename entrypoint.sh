#!/bin/bash

# Initialize alembic if not already initialized (folder check)
# But here we assume alembic folder structure is part of repo, so init might not be needed if copied.
# However, if alembic folder is mounted or missing, `alembic init` might fail if it exists.
# The user log showed "FAILED: Directory alembic already exists".
# So we should probably skip init if it exists or force it?
# Better: Just run upgrade. The repo comes with alembic/ folder.
# alembic init alembic  <-- This was causing the "Directory already exists" error!

# Apply migrations
alembic upgrade head

# Start application
exec "$@"
