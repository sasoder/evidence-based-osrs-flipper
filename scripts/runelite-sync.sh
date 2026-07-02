#!/usr/bin/env bash
# Copy RuneLite's auto-saved data into this local repo before a planning run.
#
# Sources are RuneLite's own persisted data, narrowed to the files/keys the agent needs:
#   ~/.runelite/flipping/          <- Flipping Utilities, if autosave is enabled
#   ~/.runelite/flipping/current-slots/ <- FU fork current GE slot export, if enabled
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RL="${RUNELITE_HOME:-$HOME/.runelite}"
cd "$REPO"

echo "runelite-sync $(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p data/incoming/flipping
# Sync only the live per-profile flip files. FU's backup/checkpoint/account-wide files are
# ignored by read_flips (it reads <rsn>.json), so copying them is pure local churn.
[ -d "$RL/flipping" ] && rsync -a --delete \
  --exclude='*.backup.json' --exclude='backupCheckpoints*.json' --exclude='accountwide.json' \
  --exclude='current-slots/' \
  "$RL/flipping/" data/incoming/flipping/ 2>/dev/null || true

mkdir -p data/incoming/ge-slots
if [ -d "$RL/flipping/current-slots" ]; then
  rsync -a --delete "$RL/flipping/current-slots/" data/incoming/ge-slots/ 2>/dev/null || true
else
  rm -f data/incoming/ge-slots/*.json
fi

echo "synced RuneLite exports into $REPO/data/incoming"
