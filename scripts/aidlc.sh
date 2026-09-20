#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$project_root/.venv/bin/python" ]]; then
  echo "Missing project virtual environment. Run 'uv sync --locked' first." >&2
  exit 1
fi

exec "$project_root/.venv/bin/python" "$project_root/scripts/services.py" "$@"
