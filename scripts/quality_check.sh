#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

if find . -path './.venv' -prune -o -type d -name __pycache__ -print -quit | grep -q .; then
  echo 'ERROR: __pycache__ found in project tree' >&2
  exit 1
fi
if find . -path './.venv' -prune -o -type f -name '*.pyc' -print -quit | grep -q .; then
  echo 'ERROR: .pyc found in project tree' >&2
  exit 1
fi

cache_dir="$(mktemp -d)"
trap 'rm -rf "$cache_dir"' EXIT
export PYTHONPYCACHEPREFIX="$cache_dir/pycache"

python -m compileall -q app alembic tests
ruff check --no-cache app tests
mypy --cache-dir "$cache_dir/mypy" app
python -m pytest -q -p no:cacheprovider
bash -n scripts/*.sh

if command -v docker >/dev/null 2>&1 && [[ -f .env ]]; then
  docker compose config --quiet
fi

if find . -path './.venv' -prune -o -type d -name __pycache__ -print -quit | grep -q .; then
  echo 'ERROR: quality tools created __pycache__ in project tree' >&2
  exit 1
fi
if find . -path './.venv' -prune -o -type f -name '*.pyc' -print -quit | grep -q .; then
  echo 'ERROR: quality tools created .pyc in project tree' >&2
  exit 1
fi

echo 'Quality checks passed.'
