"""Shared local configuration loader."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def load_config() -> dict:
    path = CONFIG_DIR / "settings.json"
    if not path.exists():
        path = CONFIG_DIR / "settings.example.json"
    return json.loads(path.read_text())
