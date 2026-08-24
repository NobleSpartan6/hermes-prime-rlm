#!/usr/bin/env bash
# Local macOS/Linux gate. Prefers uv when present; falls back to python -m.
set -euo pipefail

run_gate() {
  local label="$1"; shift
  echo "==> $label"
  if command -v uv >/dev/null 2>&1; then
    uv run "$@"
  else
    python3 -m "$@"
  fi
}

run_gate "ruff check ." ruff check .
run_gate "pytest -q" pytest -q

echo "==> hermes plugins doctor . --ci"
if command -v hermes >/dev/null 2>&1; then
  hermes plugins doctor . --ci
else
  echo "hermes CLI not found; doctor gate runs locally on an installed Hermes"
fi

echo "All local gates passed."
