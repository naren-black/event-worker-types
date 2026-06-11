#!/usr/bin/env bash
# Lint, security-scan and test the worker. Used by `make test` and CI.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR/worker"

if [ ! -d .venv ]; then
  echo "Creating worker/.venv ..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements-dev.txt
fi

echo "==> ruff"
.venv/bin/ruff check src tests

echo "==> bandit"
.venv/bin/bandit -c pyproject.toml -r src

echo "==> pip-audit"
.venv/bin/pip-audit -r requirements.txt

echo "==> pytest"
.venv/bin/pytest --cov=src --cov-report=term-missing
