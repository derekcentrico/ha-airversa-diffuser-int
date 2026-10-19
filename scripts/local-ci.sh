#!/usr/bin/env bash
# Lint, tests and hassfest. Run from the repo root.
set -euo pipefail

PY="${PYTHON:-.venv/bin/python}"

"$PY" -m ruff check .
"$PY" -m ruff format --check .
"$PY" -m pytest -q -p no:cacheprovider

if command -v docker >/dev/null 2>&1; then
  docker run --rm \
    -v "$PWD/custom_components:/github/workspace/custom_components:ro" \
    ghcr.io/home-assistant/hassfest
else
  echo "docker not found; skipping hassfest" >&2
fi
