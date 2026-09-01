# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app

RUN python -m pip install --no-cache-dir uv==0.12.1
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --extra demo --no-install-project --no-cache
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY ashare_lab ./ashare_lab
COPY astock_backtest ./astock_backtest
COPY catalogs ./catalogs
COPY contracts ./contracts
RUN uv sync --locked --extra demo --no-cache \
    && .venv/bin/python -c "import baostock, pypdfium2"

FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=10001
ARG APP_GID=10001
ARG CODE_REVISION
ARG SNAPSHOT_DIGEST

ENV DEPLOYMENT_PROFILE=ephemeral_candidate \
    APP_ENV=staging \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    DATABASE_URL=sqlite+pysqlite:////app/var/ephemeral/ashare.db \
    INITIALIZE_SCHEMA=true \
    QUEUE_BACKEND=thread \
    LOCAL_WORKER_THREADS=1 \
    PERSISTENCE_MODE=ephemeral \
    RESTART_RECOVERY_VERIFIED=false \
    DATA_ROOT=/app/var/snapshots/composite/${SNAPSHOT_DIGEST} \
    MARKET_DATA_PROFILE=on_demand_snapshot \
    ON_DEMAND_REFRESH_EACH_SUBMISSION=false \
    SESSION_REFERENCE_MODE=parquet \
    SESSION_REFERENCE_PATH=/app/var/snapshots/composite/${SNAPSHOT_DIGEST}/instrument_sessions.parquet \
    EVENT_DATA_REQUIRED=false \
    SNAPSHOT_STORAGE_MODE=ephemeral_local \
    CHOICE_SNAPSHOT_ROOT=/app/var/ephemeral/snapshots/choice \
    TECHNICAL_SNAPSHOT_ROOT=/app/var/ephemeral/snapshots/technical \
    EVENT_SNAPSHOT_ROOT=/app/var/ephemeral/snapshots/events \
    COMPOSITE_SNAPSHOT_ROOT=/app/var/snapshots/composite \
    SNAPSHOT_PREPARATION_ROOT=/app/var/ephemeral/snapshots/preparations \
    ARTIFACT_ROOT=/app/var/ephemeral/artifacts \
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
    && mkdir -p \
        /app/var/snapshots/composite \
        /app/var/ephemeral/snapshots/choice \
        /app/var/ephemeral/snapshots/technical \
        /app/var/ephemeral/snapshots/events \
        /app/var/ephemeral/snapshots/composite \
        /app/var/ephemeral/snapshots/preparations \
        /app/var/ephemeral/artifacts \
    && chown -R app:app /app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app ashare_lab ./ashare_lab
COPY --chown=app:app astock_backtest ./astock_backtest
COPY --chown=app:app catalogs ./catalogs
COPY --chown=app:app contracts ./contracts
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app deploy/cloudbase/deployment_entrypoint.py ./deploy/cloudbase/deployment_entrypoint.py
COPY --chown=app:app deploy/cloudbase/runtime_entrypoint.py ./deploy/cloudbase/runtime_entrypoint.py
COPY --chown=app:app deploy-snapshot/ /app/var/snapshots/composite/

USER app:app
EXPOSE 8000

HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "scripts/container_healthcheck.py", "api"]

CMD ["python", "deploy/cloudbase/deployment_entrypoint.py"]
