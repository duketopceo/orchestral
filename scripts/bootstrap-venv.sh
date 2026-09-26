#!/usr/bin/env bash
# Create a throwaway venv that matches the CI gate environment.
#
# Why this exists: the repo's `.venv` is shared by every concurrent run, and
# `pip install -e .` in it is a cross-run mutation. A run that strips the
# `[tui]` extra leaves every later run reporting green with the Textual suite
# skipped, and the same blind spot hides mypy errors (without `textual` the
# base class resolves to `Any`). One venv per run removes the shared mutable
# state entirely.
#
# Usage:
#   scripts/bootstrap-venv.sh /tmp/duk-142-venv
#   /tmp/duk-142-venv/bin/python -m unittest discover -s tests
#
# Leave the directory in place for the whole run; deleting it is the cleanup.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <venv-dir>" >&2
  exit 2
fi

VENV_DIR="$1"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -e "$VENV_DIR" ]; then
  echo "refusing to reuse $VENV_DIR: pass a fresh path so runs cannot share state" >&2
  exit 2
fi

PYTHON="${PYTHON:-python3}"
"$PYTHON" -m venv "$VENV_DIR"

# `.[dev,tui]` is exactly what ci.yml installs. Anything less makes the local
# gate measure a different environment than CI does.
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -e "$REPO_ROOT[dev,tui]"

cat <<EOF
gate venv ready: $VENV_DIR

  $VENV_DIR/bin/python -m unittest discover -s tests
  $VENV_DIR/bin/python harness.py audit --strict
  $VENV_DIR/bin/python -m ruff check .
  $VENV_DIR/bin/python -m mypy orchestral harness.py
EOF
