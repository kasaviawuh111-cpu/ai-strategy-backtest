# Build only the allowlisted context produced by prepare_private_preview_bundle.py.
# Inject its printed CODE_REVISION into the runtime environment.
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm
FROM ${PYTHON_IMAGE} AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
RUN python -m pip install --no-cache-dir uv==0.12.1
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --extra demo --no-install-project --no-cache
COPY ashare_lab ./ashare_lab
COPY astock_backtest ./astock_backtest
COPY catalogs ./catalogs
COPY contracts ./contracts
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
RUN uv sync --locked --extra demo --no-cache && \
    .venv/bin/python -c "import baostock, pypdfium2"

FROM ${PYTHON_IMAGE} AS runtime
ENV APP_ENV=production \
    DEPLOYMENT_PROFILE=private_skill_ephemeral \
    PERSISTENCE_MODE=ephemeral \
    RESTART_RECOVERY_VERIFIED=false \
    INITIALIZE_SCHEMA=true \
    PREVIEW_STATE_ROOT=/app/var/ephemeral \
    DATABASE_URL=sqlite+pysqlite:////app/var/ephemeral/ashare.db \
    QUEUE_BACKEND=thread \
    LOCAL_WORKER_THREADS=1 \
    MARKET_DATA_PROFILE=eastmoney_skill \
    SKILL_HISTORY_CACHE_ROOT=/app/var/cache/mx-daily-history \
    PROVIDER_INDICATOR_CACHE_ROOT=/app/var/cache/provider-indicators \
    CATALOG_ROOT=/app/catalogs \
    WEB_DIST_ROOT=/app/web/dist \
    CORS_ALLOWED_ORIGINS="" \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app && \
    mkdir -p /app/var/ephemeral /app/var/cache && chown -R app:app /app/var
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/ashare_lab /app/ashare_lab
COPY --from=builder /app/astock_backtest /app/astock_backtest
COPY --from=builder /app/catalogs /app/catalogs
COPY --from=builder /app/contracts /app/contracts
COPY --from=builder /app/alembic.ini /app/alembic.ini
COPY --from=builder /app/alembic /app/alembic
COPY deploy/private_preview /app/deploy/private_preview
COPY web/dist /app/web/dist
COPY source-manifest.json /app/source-manifest.json
RUN python -c "import hashlib,json; m=json.load(open('/app/source-manifest.json')); v='sha256:'+hashlib.sha256(json.dumps(m['files'],ensure_ascii=False,separators=(',',':')).encode()).hexdigest(); assert m['codeRevision']==v"
USER app:app
EXPOSE 8000
# Liveness only; the ASGI boundary applies the configured public/private policy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1',8000),timeout=3).close()"]
CMD ["python", "-m", "deploy.private_preview.entrypoint"]
