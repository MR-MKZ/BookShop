# Kabana Book Store

Digital bookstore built with **FastAPI**, **Jinja2** (Kabana RTL theme), and **PostgreSQL**.
Book metadata is scraped separately (`app/scraper.py`); this app serves the storefront.

## Features

- Guest checkout + optional account, coupons, multi-gateway payments (Zibal)
- Kabana storefront: home, search, book detail, dual pricing, cart
- Auth with Iranian mobile + password
- Signed, TTL-limited download links; order recovery without relying on IP
- Admin: books, users, orders, coupons, download TTL, scraper status, reports
- Media proxied through the app (clients never talk to FTP/DL host)

## Quick start (Docker — recommended)

```bash
cp .env.example .env
# Set strong SECRET_KEY, POSTGRES_PASSWORD, ADMIN_PASSWORD
# Generate SECRET_KEY:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose --profile dev up --build
```

| Service     | URL |
|-------------|-----|
| Web (dev)   | http://localhost:8000 |
| pgAdmin     | http://localhost:5050 |
| Filebrowser | http://localhost:8080 |
| FTP (dev)   | localhost:21 |

PostgreSQL is **not** published on the host (avoids other apps hammering `:5432`). Inspect with:

```bash
docker exec -it kabana_db psql -U kabana_user -d kabana_db
```

### Scraper

```bash
# Needs db already running (dev or prod profile)
docker compose --profile scraper up -d --build scraper
```

### Production

```bash
# 1. .env: ENVIRONMENT=prod, strong SECRET_KEY, real DOMAIN_NAME / SSL_EMAIL,
#    BASE_URL + ZIBAL_CALLBACK_URL as https://your.domain/...
./scripts/gen_secret_key.sh --write

# 2. Start stack (HTTP needed for ACME)
docker compose --profile prod up --build -d

# 3. Issue Let's Encrypt certs → nginx/ssl/server.{crt,key} + auto-renew
sudo ./scripts/setup_ssl.sh
```

Nginx terminates TLS on `:443` and proxies to `web_prod`. `/docs` is disabled when `ENVIRONMENT=prod`.
Certbot renews automatically (`certbot.timer` or cron); deploy hook reloads nginx.

## Local (no Docker)

```bash
cp .env.example .env
# Point DATABASE_URL at localhost Postgres; FTP_ENABLED=False; MEDIA_ROOT=./storage

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p storage
alembic upgrade head
python scripts/seed_dev.py   # optional; needs books_data_backup.db locally
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Project layout

```
app/                 FastAPI app, templates, static Kabana assets
kabana/              Original static HTML theme (design reference — do not edit as live app)
alembic/             Migrations
scripts/             wait_for_db, ensure_admin, seed_dev, setup_ssl, gen_secret_key
nginx/               Prod reverse proxy + ssl/ + certbot-www/ (certs not in git)
docker-compose.yml   Profiles: dev | prod | scraper
```

## Theme license

Kabana HTML theme from RTL Theme / راست‌چین. Set `KABANA_LICENSE` in `.env`.
