# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.1

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Resolve third-party wheels in a cacheable layer before copying application code.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

COPY ashare_lab ./ashare_lab
COPY astock_backtest ./astock_backtest
COPY catalogs ./catalogs
COPY contracts ./contracts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked


FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=10001
ARG APP_GID=10001
ARG CODE_REVISION

ENV APP_ENV=production \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    CATALOG_ROOT=/app/catalogs \
    DATA_ROOT=/app/var/data \
    ARTIFACT_ROOT=/app/var/artifacts \
    CODE_REVISION=${CODE_REVISION} \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/var/data /app/var/artifacts \
    && chown -R app:app /app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app ashare_lab ./ashare_lab
COPY --chown=app:app astock_backtest ./astock_backtest
COPY --chown=app:app catalogs ./catalogs
COPY --chown=app:app contracts ./contracts
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app scripts ./scripts

USER app:app

EXPOSE 8000

HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "scripts/container_healthcheck.py", "api"]

CMD ["uvicorn", "ashare_lab.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
