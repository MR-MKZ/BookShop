# Place TLS certificates here for production nginx (profile: prod).
#
# Expected filenames:
#   server.crt  — full chain (Let's Encrypt fullchain.pem)
#   server.key  — private key (Let's Encrypt privkey.pem)
#
# On the server (DNS must already point here):
#   1. Set DOMAIN_NAME, SSL_EMAIL, BASE_URL=https://your.domain in .env
#   2. docker compose --profile prod up -d
#   3. sudo ./scripts/setup_ssl.sh
#      (uses nginx webroot — does NOT take over port 80; nginx stays up)
#
# Optional emergency mode (stops nginx briefly):
#   sudo ./scripts/setup_ssl.sh --standalone
#
# Certbot renews via systemd timer/cron; deploy hook reloads kabana_nginx.
