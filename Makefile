.PHONY: install demo-data format lint typecheck test test-unit test-legacy check api worker

PYTHON ?= .venv/bin/python
UV ?= uv
UV_CACHE_DIR ?= .uv-cache
RUFF ?= .venv/bin/ruff
PYRIGHT ?= .venv/bin/pyright
PYTEST ?= .venv/bin/pytest
ALEMBIC ?= .venv/bin/alembic
UVICORN ?= .venv/bin/uvicorn
RQ ?= .venv/bin/rq
PYTHON_CHECK_PATHS = ashare_lab tests scripts alembic

install:
	UV_CACHE_DIR=$(UV_CACHE_DIR) $(UV) sync --locked --extra dev --extra demo

demo-data:
	$(PYTHON) scripts/prepare_demo_data.py

format:
	$(RUFF) format $(PYTHON_CHECK_PATHS)
	$(RUFF) check --fix $(PYTHON_CHECK_PATHS)

lint:
	$(RUFF) check $(PYTHON_CHECK_PATHS)
	$(RUFF) format --check $(PYTHON_CHECK_PATHS)

typecheck:
	$(PYRIGHT)

test:
	$(PYTEST)

test-unit:
	$(PYTEST) tests/unit tests/contract tests/architecture

test-legacy:
	$(PYTEST) -m legacy tests/legacy_v1

check: lint typecheck test

api:
	$(UVICORN) ashare_lab.main:create_app --factory --reload

worker:
	$(RQ) worker $${QUEUE_NAME:-backtests} --url $${REDIS_URL:-redis://localhost:6379/0}
