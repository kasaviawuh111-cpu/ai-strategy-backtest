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
    RESEARCH_PROVIDER_MODE=tencent_web_search \
    RESEARCH_PROVIDER_TIMEOUT_SECONDS=30 \
    CANDIDATE_PROVIDER_MODE=openai_compatible \
    CANDIDATE_PROVIDER_NAME=deepseek \
    CANDIDATE_PROVIDER_ENDPOINT=https://api.deepseek.com/chat/completions \
    CANDIDATE_PROVIDER_MODEL=deepseek-v4-flash \
    CANDIDATE_PROVIDER_RESPONSE_MODE=json_object \
    CANDIDATE_PROVIDER_THINKING=disabled \
    CANDIDATE_PROVIDER_TIMEOUT_SECONDS=30 \
    PLAN_DEEP_PROVIDER_MODE=openai_compatible \
    PLAN_DEEP_PROVIDER_NAME=deepseek \
    PLAN_DEEP_PROVIDER_ENDPOINT=https://api.deepseek.com/chat/completions \
    PLAN_DEEP_PROVIDER_MODEL=deepseek-v4-pro \
    PLAN_DEEP_PROVIDER_RESPONSE_MODE=json_object \
    PLAN_DEEP_PROVIDER_THINKING=enabled \
    PLAN_DEEP_PROVIDER_REASONING_EFFORT=high \
    PLAN_DEEP_PROVIDER_TIMEOUT_SECONDS=180 \
    SKILL_HISTORY_CACHE_ROOT=/app/var/cache/mx-daily-history \
    PROVIDER_INDICATOR_CACHE_ROOT=/app/var/cache/provider-indicators \
    MINUTE_GRID_ENABLED=true \
    EXTERNAL_MINUTE_ROOT=/data/stock_1min \
    MINUTE_MARKET_CALENDAR_PATH=/data/market-calendar.json \
    MINUTE_SNAPSHOT_ROOT=/app/var/cache/eastmoney-minute \
    PRICE_PLAN_CORPORATE_ACTION_ROOT=/app/var/cache/price-plan-corporate-actions \
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
RUN python -c "from pathlib import Path; roots=['ashare_lab','astock_backtest','catalogs','contracts','alembic','deploy/private_preview','web/dist']; files=[p for r in roots for p in Path('/app',r).rglob('*') if p.is_file()]; [p.open('rb').close() for p in files]"
EXPOSE 8000
# Liveness only; the ASGI boundary applies the configured public/private policy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1',8000),timeout=3).close()"]
CMD ["python", "-m", "deploy.private_preview.entrypoint"]
