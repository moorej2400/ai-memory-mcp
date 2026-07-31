#!/usr/bin/env sh
# POSIX entry point. The implementation lives in invoke_graphify_codebase.py so
# Windows, macOS, and Linux share one code path.
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$DIR/../.." && pwd)

if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON="$ROOT/.venv/bin/python"
elif [ -x "$ROOT/.venv/bin/python3" ]; then
  # Some distribution builds create only the versioned interpreter name.
  PYTHON="$ROOT/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "error: Python 3.11 or newer was not found on PATH." >&2
  exit 1
fi

exec "$PYTHON" "$DIR/invoke_graphify_codebase.py" "$@"
