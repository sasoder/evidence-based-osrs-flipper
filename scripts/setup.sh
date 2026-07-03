#!/usr/bin/env bash
# One-command local setup for the agent-facing merchanting harness.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 127
fi

uv sync

uv run python - "${1:-}" <<'PY'
import json
import sys
from pathlib import Path

rsn = sys.argv[1]
root = Path.cwd()
example = root / "config" / "settings.example.json"
target = root / "config" / "settings.json"

data = json.loads(example.read_text())
if rsn:
    data["rsn"] = rsn
target.write_text(json.dumps(data, indent=2) + "\n")
print(f"wrote {target}")
PY

mkdir -p data/incoming/flipping data/incoming/ge-slots state

echo "setup complete"
