#!/bin/sh
set -eu

if [ -x .venv/bin/python ]; then
  exec .venv/bin/python scripts/smoke.py "$@"
fi

exec python3 scripts/smoke.py "$@"
