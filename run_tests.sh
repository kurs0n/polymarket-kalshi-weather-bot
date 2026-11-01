#!/usr/bin/env bash
# Run the backend and frontend test suites from the repository root.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

echo "==> Backend tests"
"$PYTHON" -m unittest discover -s tests

echo "==> Frontend tests"
(cd frontend && npm run test)
