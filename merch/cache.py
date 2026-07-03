"""Tiny TTL'd disk cache for network responses.

Every tool runs as its own short-lived ``uv run python -m ...`` process, so the
in-process ``lru_cache`` on the prices client dies at exit. Within one on-demand session the
agent invokes several tools, each of which would otherwise re-download the same 1.1MB item mapping
and the universe-wide latest/1h blobs. This persists responses to ``data/cache/`` with a
per-entry TTL so repeated invocations reuse them, which also honours the Wiki's "be polite"
request — the upstream data only refreshes every ~60s anyway.

The cache is disposable: deleting ``data/cache/`` just forces a refetch. It is gitignored.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .config import ROOT

CACHE_DIR = ROOT / "data/cache"


def _path(key: str) -> Path:
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{digest}.json"


def get_or_set(key: str, ttl_seconds: float, producer):
    """Return cached value for ``key`` if it is younger than ``ttl_seconds``; otherwise call
    ``producer()``, store the result, and return it."""
    path = _path(key)
    if path.exists():
        try:
            envelope = json.loads(path.read_text())
            if time.time() - envelope["stored_at"] < ttl_seconds:
                return envelope["value"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass  # corrupt/partial entry — fall through and refetch

    value = producer()
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"stored_at": time.time(), "key": key, "value": value}))
        tmp.replace(path)  # atomic: never leave a half-written entry
    except OSError:
        pass  # caching is best-effort; a write failure must not break the caller
    return value
