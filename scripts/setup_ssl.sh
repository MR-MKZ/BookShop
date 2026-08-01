#!/usr/bin/env bash
# Issue a Let's Encrypt certificate for the project domain and install it
# as nginx/ssl/server.{crt,key}. Configures certbot auto-renewal with a
# deploy hook so certificates are refreshed before expiry.
#
# Run on the production server as root (or with sudo), from the project root:
#
#   sudo ./scripts/setup_ssl.sh
#   sudo ./scripts/setup_ssl.sh --force   # re-issue even if cert exists
#   sudo DOMAIN_NAME=shop.example.com SSL_EMAIL=ops@example.com ./scripts/setup_ssl.sh
#
# Prerequisites:
#   - DNS A/AAAA for DOMAIN_NAME → this server
#   - Ports 80 and 443 reachable from the internet
#   - docker compose prod stack (or at least port 80 free for first issue)
#   - apt packages: certbot (script can install)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSL_DIR="${SSL_DIR:-$ROOT/nginx/ssl}"
WEBROOT="${CERTBOT_WEBROOT:-$ROOT/nginx/certbot-www}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/docker-compose.yml}"
FORCE=0

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"
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
  # shellcheck disable=SC1090
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
  mkdir -p "$SSL_DIR" "$WEBROOT"
  chmod 755 "$WEBROOT"
}

nginx_running() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'kabana_nginx'
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

  echo "Nginx not up / webroot failed — using standalone (binds :80 briefly)..."
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

  if [[ -f "$COMPOSE_FILE" ]]; then
    (cd "$ROOT" && docker compose --profile prod up -d nginx) || true
  fi
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
  # Prefer systemd timer shipped with certbot packages
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files 2>/dev/null | grep -q '^certbot.timer'; then
      systemctl enable --now certbot.timer
      echo "Enabled systemd certbot.timer (auto-renew)"
      systemctl list-timers --all 2>/dev/null | grep -i certbot || true
      return
    fi
  fi

  # Fallback twice-daily cron (certbot only renews when near expiry)
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
echo "Webroot: $WEBROOT"
echo "SSL dir: $SSL_DIR (server.crt / server.key)"

# Prefer webroot when nginx is serving ACME path
if nginx_running; then
  if ! issue_with_webroot; then
    echo "Webroot challenge failed; falling back to standalone..."
    issue_with_standalone
  fi
else
  issue_with_standalone
fi

export DOMAIN_NAME
"$ROOT/scripts/deploy_ssl_certs.sh"

install_renewal_hook
ensure_timer_or_cron

# Dry-run renew to validate hook path (does not hit rate limits hard on success path)
echo "Validating renew pipeline (dry-run)..."
certbot renew --dry-run || {
  echo "Warning: dry-run renew reported issues — check DNS / port 80 / nginx ACME location" >&2
}

echo
echo "Done."
echo "  Certs live at: $SSL_DIR/server.crt and $SSL_DIR/server.key"
echo "  Auto-renew: certbot.timer or /etc/cron.d/kabana-certbot-renew"
echo "  After renew, deploy hook runs scripts/deploy_ssl_certs.sh and reloads nginx"
echo
echo "Remember BASE_URL / ZIBAL_CALLBACK_URL use https://${DOMAIN_NAME}/ ..."
