#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${SCRIPT_DIR}/.venv/bin/python}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python executable not found: ${PYTHON_BIN}. Run ./run.sh --setup-only or set PYTHON_BIN to an executable on PATH." >&2
  exit 2
fi

"${PYTHON_BIN}" -m ruff format --check .
"${PYTHON_BIN}" -m ruff check .
"${PYTHON_BIN}" -m coverage erase
"${PYTHON_BIN}" -m coverage run --branch -m unittest discover -v
"${PYTHON_BIN}" -m coverage report --fail-under=80
