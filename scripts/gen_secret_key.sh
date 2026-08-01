#!/usr/bin/env bash
# Generate a cryptographically strong SECRET_KEY for Kabana BookShop.
#
# Usage:
#   ./scripts/gen_secret_key.sh           # print key
#   ./scripts/gen_secret_key.sh --env     # print as SECRET_KEY=...
#   ./scripts/gen_secret_key.sh --write   # write/update SECRET_KEY in .env

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
MODE="${1:-}"

gen_key() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -d '\n' | tr '+/' '-_'
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")'
    return
  fi
  echo "Need openssl or python3 to generate SECRET_KEY" >&2
  exit 1
}

KEY="$(gen_key)"

case "$MODE" in
  --env)
    echo "SECRET_KEY=${KEY}"
    ;;
  --write)
    if [[ ! -f "$ENV_FILE" ]]; then
      echo "No .env at $ENV_FILE — creating from key only." >&2
      echo "SECRET_KEY=${KEY}" >"$ENV_FILE"
    elif grep -qE '^SECRET_KEY=' "$ENV_FILE"; then
      # portable in-place replace
      tmp="$(mktemp)"
      sed -E "s|^SECRET_KEY=.*|SECRET_KEY=${KEY}|" "$ENV_FILE" >"$tmp"
      mv "$tmp" "$ENV_FILE"
    else
      printf '\nSECRET_KEY=%s\n' "$KEY" >>"$ENV_FILE"
    fi
    echo "Updated SECRET_KEY in $ENV_FILE"
    ;;
  ""|-h|--help)
    if [[ "$MODE" == "-h" || "$MODE" == "--help" ]]; then
      sed -n '2,10p' "$0"
      exit 0
    fi
    echo "$KEY"
    ;;
  *)
    echo "Unknown option: $MODE (use --env, --write, or no args)" >&2
    exit 1
    ;;
esac
