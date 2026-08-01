#!/usr/bin/env bash
# Issue a Let's Encrypt certificate for the project domain and install it
# as nginx/ssl/server.{crt,key}. Configures certbot auto-renewal with a
# deploy hook so certificates are refreshed before expiry.
#
# Designed for Docker nginx holding :80/:443 — uses webroot only (never
# fights nginx for port 80). Certbot writes into nginx/certbot-www; nginx
# serves /.well-known/acme-challenge/ from that folder.
#
# Run on the production server as root, from the project root:
#
#   sudo ./scripts/setup_ssl.sh
#   sudo ./scripts/setup_ssl.sh --force
#
# Prerequisites:
#   - DOMAIN_NAME + SSL_EMAIL in .env
#   - DNS A/AAAA for DOMAIN_NAME → this server
#   - docker compose prod stack (nginx must be up for HTTP-01)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSL_DIR="${SSL_DIR:-$ROOT/nginx/ssl}"
WEBROOT="${CERTBOT_WEBROOT:-$ROOT/nginx/certbot-www}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/docker-compose.yml}"
FORCE=0
USE_STANDALONE=0

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --standalone) USE_STANDALONE=1 ;;
    -h|--help)
      sed -n '2,22p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

load_env() {
  local env_file="$ROOT/.env"
  if [[ ! -f "$env_file" ]]; then
    echo "Missing $env_file — copy .env.example and set DOMAIN_NAME / SSL_EMAIL" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1091
  source <(grep -E '^(DOMAIN_NAME|SSL_EMAIL|BASE_URL)=' "$env_file" | sed 's/\r$//')
  set +a
}

need_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run as root: sudo $0 $*" >&2
    exit 1
  fi
}

ensure_certbot() {
  if command -v certbot >/dev/null 2>&1; then
    return
  fi
  echo "Installing certbot..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y certbot
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y certbot
  else
    echo "Install certbot manually, then re-run." >&2
    exit 1
  fi
}

ensure_dirs() {
  mkdir -p "$SSL_DIR" "$WEBROOT/.well-known/acme-challenge"
  chmod 755 "$WEBROOT"
}

nginx_running() {
  command -v docker >/dev/null 2>&1 \
    && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'kabana_nginx'
}

ensure_placeholder_certs() {
  # nginx refuses to start without these files — keep a self-signed placeholder
  # until Let's Encrypt replaces them.
  if [[ -f "$SSL_DIR/server.crt" && -f "$SSL_DIR/server.key" ]]; then
    return
  fi
  echo "Creating temporary self-signed certs so nginx can listen on :80/:443..."
  mkdir -p "$SSL_DIR"
  openssl req -x509 -nodes -days 3 -newkey rsa:2048 \
    -keyout "$SSL_DIR/server.key" \
    -out "$SSL_DIR/server.crt" \
    -subj "/CN=${DOMAIN_NAME}" >/dev/null 2>&1
  chmod 600 "$SSL_DIR/server.key"
  chmod 644 "$SSL_DIR/server.crt"
}

ensure_nginx_up() {
  ensure_placeholder_certs
  if nginx_running; then
    return
  fi
  echo "Starting nginx (prod profile) for ACME webroot..."
  (cd "$ROOT" && docker compose --profile prod up -d nginx)
  sleep 2
  if ! nginx_running; then
    echo "kabana_nginx is not running. Start the prod stack first:" >&2
    echo "  docker compose --profile prod up -d" >&2
    exit 1
  fi
}

probe_acme_webroot() {
  local token="kabana-acme-probe-$$"
  local file="$WEBROOT/.well-known/acme-challenge/${token}"
  echo "ok" >"$file"
  chmod 644 "$file"

  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' \
    --connect-timeout 5 --max-time 15 \
    "http://${DOMAIN_NAME}/.well-known/acme-challenge/${token}" || true)"
  rm -f "$file"

  if [[ "$code" != "200" ]]; then
    echo "ACME webroot probe failed (HTTP ${code:-none}) for" >&2
    echo "  http://${DOMAIN_NAME}/.well-known/acme-challenge/..." >&2
    echo "Check: DNS → this host, nginx certbot-www volume, firewall :80" >&2
    return 1
  fi
  echo "ACME webroot OK (nginx serves challenges on :80)"
}

issue_with_webroot() {
  local extra=()
  [[ "$FORCE" -eq 1 ]] && extra+=(--force-renewal)

  certbot certonly \
    --webroot \
    -w "$WEBROOT" \
    -d "$DOMAIN_NAME" \
    --email "$SSL_EMAIL" \
    --agree-tos \
    --non-interactive \
    --keep-until-expiring \
    "${extra[@]}"
}

issue_with_standalone() {
  local extra=()
  [[ "$FORCE" -eq 1 ]] && extra+=(--force-renewal)

  echo "Stopping kabana_nginx briefly so certbot can bind :80 (--standalone)..."
  if nginx_running; then
    (cd "$ROOT" && docker compose --profile prod stop nginx) || true
  fi

  certbot certonly \
    --standalone \
    -d "$DOMAIN_NAME" \
    --email "$SSL_EMAIL" \
    --agree-tos \
    --non-interactive \
    --preferred-challenges http \
    --keep-until-expiring \
    "${extra[@]}"

  (cd "$ROOT" && docker compose --profile prod up -d nginx) || true
}

install_renewal_hook() {
  local hook_dir="/etc/letsencrypt/renewal-hooks/deploy"
  local hook="$hook_dir/kabana-bookshop-deploy.sh"
  mkdir -p "$hook_dir"

  cat >"$hook" <<EOF
#!/usr/bin/env bash
# Auto-installed by Kabana BookShop scripts/setup_ssl.sh
set -euo pipefail
export DOMAIN_NAME="${DOMAIN_NAME}"
export SSL_DIR="${SSL_DIR}"
"${ROOT}/scripts/deploy_ssl_certs.sh"
EOF
  chmod 755 "$hook"
  echo "Installed deploy hook: $hook"
}

ensure_timer_or_cron() {
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files 2>/dev/null | grep -q '^certbot.timer'; then
      systemctl enable --now certbot.timer
      echo "Enabled systemd certbot.timer (auto-renew)"
      systemctl list-timers --all 2>/dev/null | grep -i certbot || true
      return
    fi
  fi

  local cron_file="/etc/cron.d/kabana-certbot-renew"
  cat >"$cron_file" <<EOF
# Kabana BookShop — certbot renew (deploy hook copies into nginx/ssl)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 3,15 * * * root certbot renew --quiet
EOF
  chmod 644 "$cron_file"
  echo "Installed cron: $cron_file"
}

# --- main ---
need_root "$@"
load_env

DOMAIN_NAME="${DOMAIN_NAME:-}"
SSL_EMAIL="${SSL_EMAIL:-}"

if [[ -z "$DOMAIN_NAME" || "$DOMAIN_NAME" == "example.com" || "$DOMAIN_NAME" == "kabana.local" ]]; then
  echo "Set a real DOMAIN_NAME in .env (got: '${DOMAIN_NAME:-empty}')" >&2
  exit 1
fi
if [[ -z "$SSL_EMAIL" || "$SSL_EMAIL" == *"example.com"* ]]; then
  echo "Set SSL_EMAIL in .env to a real admin address for Let's Encrypt notices" >&2
  exit 1
fi

ensure_certbot
ensure_dirs

echo "Domain : $DOMAIN_NAME"
echo "Email  : $SSL_EMAIL"
echo "Webroot: $WEBROOT  (nginx serves this — certbot does NOT bind :80)"
echo "SSL dir: $SSL_DIR (server.crt / server.key)"

if [[ "$USE_STANDALONE" -eq 1 ]]; then
  issue_with_standalone
else
  ensure_nginx_up
  probe_acme_webroot
  issue_with_webroot
fi

export DOMAIN_NAME
"$ROOT/scripts/deploy_ssl_certs.sh"

install_renewal_hook
ensure_timer_or_cron

echo "Validating renew pipeline (dry-run)..."
certbot renew --dry-run || {
  echo "Warning: dry-run renew reported issues — check DNS / ACME webroot" >&2
}

echo
echo "Done."
echo "  Certs: $SSL_DIR/server.crt + server.key"
echo "  Renew: certbot.timer / cron → deploy hook → reload kabana_nginx"
echo "  Set BASE_URL=https://${DOMAIN_NAME} and ZIBAL_CALLBACK_URL accordingly"
echo
echo "Redeploy web so proxy headers fix https asset URLs:"
echo "  docker compose --profile prod up -d --build web_prod"
