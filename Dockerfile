# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GEMINI_PROXY_CONFIG=/config/config.json \
    GEMINI_PROXY_DATA_DIR=/data \
    GEMINI_PROXY_HOST=0.0.0.0 \
    GEMINI_PROXY_COOKIE_FILE=/config/cookies.json

WORKDIR /app

# Dependencies first for layer caching
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

# /config : your config.json + cookies.json (read)
# /data   : cookie cache, activity.db, admin_credential (read/write) — must persist
RUN mkdir -p /data /config
VOLUME ["/data", "/config"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
