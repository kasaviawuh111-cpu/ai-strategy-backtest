# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app

RUN python -m pip install --no-cache-dir uv==0.12.1
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-install-project --no-cache
COPY ashare_lab ./ashare_lab
COPY astock_backtest ./astock_backtest
COPY catalogs ./catalogs
COPY contracts ./contracts
RUN uv sync --locked --no-cache

FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=10001
ARG APP_GID=10001
ARG CODE_REVISION
ARG SNAPSHOT_DIGEST

ENV APP_ENV=production \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    DATABASE_URL=sqlite+pysqlite:////tmp/ashare-demo.db \
    INITIALIZE_SCHEMA=true \
    QUEUE_BACKEND=thread \
    LOCAL_WORKER_THREADS=1 \
    DATA_ROOT=/app/var/snapshots/composite/${SNAPSHOT_DIGEST} \
    MARKET_DATA_PROFILE=composite_snapshot \
    SESSION_REFERENCE_MODE=parquet \
    SESSION_REFERENCE_PATH=/app/var/snapshots/composite/${SNAPSHOT_DIGEST}/instrument_sessions.parquet \
    EVENT_DATA_REQUIRED=true \
    ARTIFACT_ROOT=/tmp/ashare-artifacts \
    CATALOG_ROOT=/app/catalogs \
    ENGINE_VERSION=2.0.0a0 \
    CODE_REVISION=${CODE_REVISION} \
    CORS_ALLOWED_ORIGINS=https://59ac3319a9594be59fa3034fcae82a8f.app.workbuddy.link \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/var/snapshots/composite /tmp/ashare-artifacts \
    && chown -R app:app /app /tmp/ashare-artifacts

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app ashare_lab ./ashare_lab
COPY --chown=app:app astock_backtest ./astock_backtest
COPY --chown=app:app catalogs ./catalogs
COPY --chown=app:app contracts ./contracts
COPY --chown=app:app scripts/container_healthcheck.py ./scripts/container_healthcheck.py
COPY --chown=app:app deploy-snapshot/ /app/var/snapshots/composite/

USER app:app
EXPOSE 8000

HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "scripts/container_healthcheck.py", "api"]

CMD ["sh", "-c", "exec uvicorn ashare_lab.main:create_app --factory --host 0.0.0.0 --port \"${PORT:-${APP_PORT:-8000}}\" --workers 1 --proxy-headers"]
