#!/usr/bin/env python3
"""Block until Postgres accepts connections (used by entrypoint / scraper)."""

from __future__ import annotations

import os
import sys
import time
from urllib.parse import unquote, urlparse

import psycopg2


def main() -> int:
    raw = os.environ.get("SYNC_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if not raw:
        print("SYNC_DATABASE_URL / DATABASE_URL is not set", file=sys.stderr)
        return 1

    url = urlparse(raw.replace("postgresql+asyncpg://", "postgresql://", 1))
    dbname = (url.path or "/").lstrip("/") or "postgres"
    user = unquote(url.username or "postgres")
    password = unquote(url.password or "")
    host = url.hostname or "db"
    port = url.port or 5432
    timeout = int(os.environ.get("DB_WAIT_TIMEOUT", "90"))

    print(f"Waiting for database at {host}:{port}/{dbname} (up to {timeout}s)...")
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            conn = psycopg2.connect(
                dbname=dbname,
                user=user,
                password=password,
                host=host,
                port=port,
                connect_timeout=3,
            )
            conn.close()
            print("Database is ready.")
            return 0
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1)

    print(f"Database not ready after {timeout}s: {last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
