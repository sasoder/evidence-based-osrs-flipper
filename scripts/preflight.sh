#!/usr/bin/env bash
# Optional local setup/data check for an on-demand merchanting session.
# This intentionally does not make trade recommendations.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv sync

scripts/runelite-sync.sh

echo "== current ge slots =="
uv run python -m merch.runelite offers
