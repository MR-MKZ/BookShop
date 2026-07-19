# Kabana Book Store

Digital bookstore built with **FastAPI**, **Jinja2** (Kabana RTL theme), and **PostgreSQL**.
Book metadata is scraped separately (`app/scraper.py`); this app serves the storefront.

## Quick start (local, no Docker)

Useful when Docker is unavailable (e.g. some cloud agent environments).

### Prerequisites

- Python 3.12+
- PostgreSQL 15+ (16 works)
- `libmagic1` (optional, for media sniffing)

### Setup

```bash
# 1. Environment
cp .env.example .env
# Edit .env: point DATABASE_URL / SYNC_DATABASE_URL at localhost,
# set FTP_ENABLED=False and MEDIA_ROOT to a local folder (e.g. ./storage)

# 2. Database
sudo -u postgres createuser -P kabana_user   # password: kabana_pass
sudo -u postgres createdb -O kabana_user kabana_db

# 3. Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4. Migrate + seed (~600 sample books + admin user)
mkdir -p storage
alembic upgrade head
python scripts/seed_dev.py

# 5. Run
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open http://127.0.0.1:8000 — health check: http://127.0.0.1:8000/health

**Seeded admin:** phone `09153276607` / password `admin123` (پنل: `/admin/`)

### Local `.env` highlights

```env
DB_HOST=127.0.0.1
DATABASE_URL=postgresql+asyncpg://kabana_user:kabana_pass@127.0.0.1:5432/kabana_db
SYNC_DATABASE_URL=postgresql://kabana_user:kabana_pass@127.0.0.1:5432/kabana_db
MEDIA_ROOT=/absolute/path/to/storage
FTP_ENABLED=False
SECRET_KEY=change_me
ZIBAL_MERCHANT=zibal
ZIBAL_CALLBACK_URL=http://127.0.0.1:8000/payment/callback
BASE_URL=http://127.0.0.1:8000
```

### Features (vertical slice)

- Kabana Jinja storefront: home, search (paginated + trigram indexes), book detail, dual pricing
- Auth with Iranian mobile + password (register/login)
- Zibal sandbox checkout (`merchant=zibal`) → owned library + gated PDF download
- Admin panel: books (filter missing PDF + upload to FTP), users, orders, date-range reports
- Scraper stores sale price 2–3k below source and strikethrough original 30–40k above

## Docker Compose (when Docker is available)

```bash
cp .env.example .env
# For Compose, use DB_HOST=db and FTP_ENABLED=True (defaults in .env.example)

docker compose --profile dev up --build
```

| Service      | URL / port        |
|--------------|-------------------|
| Web (dev)    | http://localhost:8000 |
| PostgreSQL   | localhost:5432    |
| pgAdmin      | http://localhost:5050 |
| Filebrowser  | http://localhost:8080 |
| FTP          | localhost:21      |

```bash
# Production
docker compose --profile prod up --build

# Scraper (needs DB up)
docker compose --profile scraper up --build scraper
```

## Project layout

```
app/                 FastAPI app, templates, static Kabana assets
kabana/              Original static HTML theme (reference)
alembic/             Migrations
scripts/seed_dev.py  Dev seed from books_data_backup.db
docker-compose.yml   Profiles: dev | prod | scraper
```

## Theme license

Kabana HTML theme purchased from RTL Theme / راست‌چین.
Purchase/license code (for theme records): `56417913883`

## Current status

Implemented: home, search, book detail, auth (username/email + password), media proxy.
Not yet: Zibal payment, phone-based auth, full admin panel, purchase-gated downloads, dual pricing.
