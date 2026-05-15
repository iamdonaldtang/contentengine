# TaskOn Marketing Engine · ingestion HTTP service container.
#
# Slim Flask + gunicorn server. Designed to sit behind a Cloudflare Tunnel —
# no TLS termination, no certificates, just a plain HTTP socket on :5051.
#
# Build:
#   docker compose build ingestion
#
# Run:
#   docker compose up -d ingestion
#
# This image shares ./runtime + ./config with the engine container via the
# host bind mount declared in docker-compose.yml, so both processes see the
# same state.db.

FROM python:3.12-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Shanghai \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tini \
        tzdata \
 && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
 && rm -rf /var/lib/apt/lists/*

# Minimal Python deps — only what ingestion needs.
RUN pip install --upgrade pip \
 && pip install \
        "flask>=3.0,<4.0" \
        "gunicorn>=21.0,<22.0" \
        "requests>=2.31,<3.0" \
        "python-dotenv>=1.0,<2.0" \
        "pyyaml>=6.0,<7.0"

# Non-root runtime user.
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} taskon \
 && useradd -m -u ${UID} -g ${GID} -s /bin/bash taskon

WORKDIR /app

# Copy the engine's shared `lib/` package + the ingestion code itself.
# Build context is the engine root (see docker-compose.yml `context: .`).
COPY lib/        /app/lib/
COPY ingestion/  /app/ingestion/

RUN mkdir -p /app/runtime/logs \
 && chown -R taskon:taskon /app

USER taskon

ENV PYTHONPATH=/app \
    INGESTION_PORT=5051

EXPOSE 5051

# Healthcheck — hit the /health endpoint directly.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5051/health || exit 1

# tini → gunicorn → flask app.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5051", \
     "--workers", "2", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "ingestion.wsgi:app"]
