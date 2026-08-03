FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# fastapi-sitemap pins fastapi<0.116; install --no-deps (API is tiny / compatible)
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps fastapi-sitemap==1.0.4

COPY alembic.docker.ini /app/alembic.ini
COPY app /app/app
COPY scripts /app/scripts
COPY alembic /app/alembic

ENV PYTHONPATH=/app

CMD ["python", "-m", "app.scraper", "--update", "--loop"]
