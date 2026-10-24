#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ "${1:-}" == "--cli" ]]; then
  shift
  exec python3 -m app.cli "$@"
fi

exec python3 -m app.web "$@"
