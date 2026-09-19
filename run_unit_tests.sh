#!/usr/bin/env bash
#
# Manually runs the unit tests added for the template engine refactoring:
#   - vibora/templates/engine.py      (transactional template loading)
#   - vibora/templates/loader.py      (incremental hot-reload)
#   - vibora/templates/compilers/cython.py (source-map / error mapping)
#
# Usage:
#   ./run_unit_tests.sh            # run everything
#   ./run_unit_tests.sh -v         # verbose output
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON:-python3}"

echo "==> Using interpreter: $($PYTHON_BIN --version 2>&1) ($(command -v "$PYTHON_BIN"))"
echo "==> Project root: $ROOT_DIR"
echo "==> Discovering unit tests under unit_tests/"
echo

exec "$PYTHON_BIN" -m unittest discover -s unit_tests -p 'test_*.py' -t "$ROOT_DIR" "$@"
