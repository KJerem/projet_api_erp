# Image unique de l'Integration Hub : API, migrations et workers (la commande change par service).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ shared/
COPY migrations/ migrations/
COPY integration-api/ integration-api/
COPY workers/ workers/
COPY infra/catalog.json config/catalog.json

RUN useradd --system --uid 10001 hub
USER hub
