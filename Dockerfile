# TaskOn Marketing Engine · single-container deployment.
#
# Container layout:
#   * Python 3.12-slim base + system locale UTF-8
#   * supercronic 0.2.30 (Go binary) drives all 8 cron jobs and writes logs
#     to stdout/stderr so `docker logs` works as the unified log surface
#   * Source code copied to /app; runtime state mounted from host volume
#
# Build:
#   docker compose build
#
# Run:
#   docker compose up -d
#
# Manual one-off invocation:
#   docker compose exec engine python -m jobs.metrics_collector --date 2026-05-13

FROM python:3.12-slim AS base

# ---- System deps + locale ----
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
        tzdata \
        sqlite3 \
        tini \
 && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
 && echo "${TZ}" > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# ---- supercronic (Go cron drop-in, logs to stdout) ----
ENV SUPERCRONIC_VERSION=0.2.30 \
    SUPERCRONIC_SHA1SUM=9f27ad28c5c57cd133325b2a66bba69ba2235799 \
    SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.30/supercronic-linux-amd64
RUN curl -fsSLo /usr/local/bin/supercronic "${SUPERCRONIC_URL}" \
 && echo "${SUPERCRONIC_SHA1SUM}  /usr/local/bin/supercronic" | sha1sum -c - \
 && chmod +x /usr/local/bin/supercronic

# ---- Non-root runtime user (UID 1000 by default; override via build-arg) ----
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} taskon \
 && useradd -m -u ${UID} -g ${GID} -s /bin/bash taskon

WORKDIR /app

# ---- Python deps (cache layer) ----
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /app/requirements.txt

# ---- App code ----
# Only the engine's own code; runtime/, .env, .venv, newsletter/, skills/, plugin-src/, dify/
# are excluded via .dockerignore.
COPY lib/        /app/lib/
COPY sources/    /app/sources/
COPY jobs/       /app/jobs/
COPY scripts/    /app/scripts/
COPY tests/      /app/tests/
COPY config/     /app/config/
COPY docker/     /app/docker/
# ingestion/ package is copied so engine container can run tests/test_ingestion.py
# even though the gunicorn HTTP server itself runs in the separate ingestion service.
COPY ingestion/  /app/ingestion/
COPY pyproject.toml requirements.txt config.yaml README.md /app/

# ---- Permissions ----
RUN mkdir -p /app/runtime/logs /app/runtime/drafts /app/runtime/backups \
 && chown -R taskon:taskon /app

USER taskon

# Healthcheck: ensure the SQLite file is reachable + schema present.
# Runs every 60s; if the engine container can't open state.db, we're broken.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -m scripts.init_db --verify-only || exit 1

# tini reaps zombies from the supercronic-spawned python processes.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["supercronic", "/app/docker/crontab"]
