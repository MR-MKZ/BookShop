# TLS for production nginx (`docker compose --profile prod`)
#
# Expected filenames (do not rename — nginx.conf and deploy script use these):
#   server.crt  — full chain (Let's Encrypt fullchain.pem)
#   server.key  — private key (Let's Encrypt privkey.pem)
#
# On the server (DNS must already point here):
#   1. Set DOMAIN_NAME and SSL_EMAIL in .env
#   2. Start stack (or at least free :80):  docker compose --profile prod up -d
#   3. Issue + install auto-renew:
#        sudo ./scripts/setup_ssl.sh
#
# Certbot renew runs via systemd `certbot.timer` (or cron). After each renew,
# `scripts/deploy_ssl_certs.sh` copies into this folder and reloads kabana_nginx.
#
# Local self-signed (dev only, not for public HTTPS):
#   openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
#     -keyout nginx/ssl/server.key -out nginx/ssl/server.crt \
#     -subj "/CN=localhost"
#
# Never commit *.key / *.crt / *.pem
