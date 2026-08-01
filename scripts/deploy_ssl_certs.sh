#!/usr/bin/env bash
# Copy Let's Encrypt live certs into nginx/ssl with the names nginx expects,
# then reload the kabana_nginx container (if running).
#
# Expected names (mounted at /etc/nginx/ssl inside the container):
#   nginx/ssl/server.crt  ← fullchain.pem
#   nginx/ssl/server.key  ← privkey.pem
#
# Called by certbot --deploy-hook after each successful renew/issue.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSL_DIR="${SSL_DIR:-$ROOT/nginx/ssl}"
DOMAIN="${DOMAIN_NAME:-}"

# Load DOMAIN_NAME from .env when not already set
if [[ -z "$DOMAIN" && -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  # Only export DOMAIN-related lines to avoid executing secrets as shell oddly
  DOMAIN="$(grep -E '^DOMAIN_NAME=' "$ROOT/.env" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
  set +a
fi

if [[ -z "$DOMAIN" ]]; then
  echo "DOMAIN_NAME is required (env or .env)" >&2
  exit 1
fi

LIVE_DIR="${CERTBOT_LIVE_DIR:-/etc/letsencrypt/live/${DOMAIN}}"
SRC_FULLCHAIN="${LIVE_DIR}/fullchain.pem"
SRC_PRIVKEY="${LIVE_DIR}/privkey.pem"

if [[ ! -f "$SRC_FULLCHAIN" || ! -f "$SRC_PRIVKEY" ]]; then
  echo "Missing Let's Encrypt files under $LIVE_DIR" >&2
  exit 1
fi

mkdir -p "$SSL_DIR"
umask 077
cp -f "$SRC_FULLCHAIN" "$SSL_DIR/server.crt"
cp -f "$SRC_PRIVKEY" "$SSL_DIR/server.key"
chmod 644 "$SSL_DIR/server.crt"
chmod 600 "$SSL_DIR/server.key"
# If nginx runs as non-root in container, keep group-readable key as needed
chown root:root "$SSL_DIR/server.crt" "$SSL_DIR/server.key" 2>/dev/null || true

echo "Installed:"
echo "  $SSL_DIR/server.crt"
echo "  $SSL_DIR/server.key"

reload_nginx() {
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' | grep -qx 'kabana_nginx'; then
    docker exec kabana_nginx nginx -t && docker exec kabana_nginx nginx -s reload
    echo "Reloaded kabana_nginx"
    return
  fi
  if command -v docker >/dev/null 2>&1; then
    (cd "$ROOT" && docker compose --profile prod exec -T nginx nginx -t \
      && docker compose --profile prod exec -T nginx nginx -s reload) 2>/dev/null \
      && echo "Reloaded nginx via compose" && return
  fi
  echo "Nginx container not running — certs updated; restart nginx when ready."
}

reload_nginx
