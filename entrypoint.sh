#!/bin/bash

alembic init alembic
alembic upgrade head
exec "$@"
