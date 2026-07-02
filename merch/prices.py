"""OSRS Wiki real-time prices client.

Data source: https://prices.runescape.wiki/api/v1/osrs (RuneLite + Wiki partnership).
The Wiki blocks generic User-Agents (python-requests, curl, ...), so we always send a
descriptive one from config. Be polite: no sustained multi-large-queries/sec.

The CLI is context-aware: the universe-wide endpoints (mapping/latest/5m/1h) are enormous,
so by default they print a one-line summary and require a filter (id / --ids / name query).
Pass --raw to dump the full payload (for piping to a file, not for an agent's context).

CLI:
    python -m merch.prices latest [item_id] [--ids 4151,560] [--raw]
    python -m merch.prices mapping [query] [--limit N] [--raw]
    python -m merch.prices 5m | 1h [--ids 4151,560] [--raw]
    python -m merch.prices timeseries <item_id> <5m|1h|6h|24h> [--tail N] [--raw]
    python -m merch.prices margins [--min-volume N] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from . import cache
from .config import ROOT, load_config

CONFIG = load_config()
BASE = CONFIG["wiki_api_base"]
UA = CONFIG["user_agent"]
TIMEOUT = CONFIG["request_timeout_seconds"]

# TTLs reflect how often upstream actually changes: the mapping is near-static, while the
# real-time aggregates refresh on their own period (latest ~continuously, 5m/1h on the bucket).
TTL_MAPPING = 86_400  # 24h
TTL_REALTIME = 60     # latest / 5m / 1h
TTL_TIMESERIES = 300  # historical points settle quickly

# Polite concurrency for the parallel timeseries prefetch. The Wiki asks callers not to hammer it;
# ~24 in-flight requests is well-behaved for a once-per-session pull and is where throughput
# plateaus — the host caps aggregate timeseries fetches around here, so more workers don't help.
HISTORY_WORKERS = 24

# In-process memo of per-item timeseries, keyed by (item_id, timestep). A /flip run evaluates the
# same items across the patient/time/active lanes and the survival backtest, so without this the
# identical series would be refetched several times per item. prefetch_timeseries() fills this
# concurrently; timeseries() reads it and otherwise falls back to a single per-item fetch.
_TS_MEMO: dict[tuple[int, str], list[dict]] = {}


def _get(path: str, params: dict | None = None, ttl: float = 0) -> dict:
    url = f"{BASE}{path}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        if query:
            url = f"{url}?{query}"

    def fetch() -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())

    return cache.get_or_set(f"GET {url}", ttl, fetch)


def latest(item_id: int | None = None) -> dict:
    """Current high/low buy/sell + transaction timestamps, keyed by item id."""
    return _get("/latest", {"id": item_id} if item_id else None, ttl=TTL_REALTIME)["data"]


@lru_cache(maxsize=1)
def mapping() -> list[dict]:
    """Item metadata: id, name, members, limit (GE buy limit), value, highalch, ..."""
    return _get("/mapping", ttl=TTL_MAPPING)


@lru_cache(maxsize=1)
def mapping_by_id() -> dict[int, dict]:
    return {m["id"]: m for m in mapping()}


def five_min(timestamp: int | None = None) -> dict:
    return _get("/5m", {"timestamp": timestamp} if timestamp else None, ttl=TTL_REALTIME)["data"]


def one_hour(timestamp: int | None = None) -> dict:
    return _get("/1h", {"timestamp": timestamp} if timestamp else None, ttl=TTL_REALTIME)["data"]


def _fetch_timeseries(item_id: int, timestep: str) -> list[dict]:
    return _get("/timeseries", {"id": item_id, "timestep": timestep}, ttl=TTL_TIMESERIES)["data"]


def prefetch_timeseries(item_ids, timesteps, max_workers: int = HISTORY_WORKERS) -> None:
    """Warm the per-item timeseries memo for every (item, timestep) pair, concurrently.

    This is the speedup for plan research: instead of fetching each item's history serially inside
    the scan loop (hundreds of sequential round-trips), every series is pulled in parallel up front
    and reused from memory by all lanes and the backtest. The data is identical to a serial fetch —
    same endpoint — so this changes only timing, never the strategy. Idempotent: pairs already in
    the memo (or freshly fetched here) are not refetched."""
    jobs = [(i, t) for i in item_ids for t in timesteps if (i, t) not in _TS_MEMO]
    if not jobs:
        return
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for (i, t), rows in zip(jobs, ex.map(lambda j: _fetch_timeseries(*j), jobs)):
            _TS_MEMO[(i, t)] = rows


def timeseries(item_id: int, timestep: str) -> list[dict]:
    """Up to 365 points of high/low + volume at the given timestep.

    Served from the in-process memo when prefetch_timeseries() has loaded it (no network call);
    otherwise fetched once and memoized."""
    if timestep not in {"5m", "1h", "6h", "24h"}:
        raise ValueError(f"bad timestep: {timestep}")
    key = (item_id, timestep)
    rows = _TS_MEMO.get(key)
    if rows is None:
        rows = _fetch_timeseries(item_id, timestep)
        _TS_MEMO[key] = rows
    return rows


def margins(min_volume: int = 0, limit: int | None = 50,
            members_only: bool = True) -> list[dict]:
    """Naive margin scan: spread between instant-buy (high) and instant-sell (low),
    enriched with 1h volume and GE buy limit. A starting point for the agent to reason
    over — NOT a buy signal on its own. Tax (2% on sells, capped) is applied.
    """
    data = latest()
    vol = one_hour()
    meta = mapping_by_id()
    rows = []
    for sid, p in data.items():
        iid = int(sid)
        m = meta.get(iid)
        if not m:
            continue
        if members_only and not m.get("members"):
            continue
        high, low = p.get("high"), p.get("low")
        if not high or not low or high <= low:
            continue
        v = vol.get(sid, {})
        traded = (v.get("highPriceVolume") or 0) + (v.get("lowPriceVolume") or 0)
        if traded < min_volume:
            continue
        tax = min(int(high * 0.02), 5_000_000)  # GE tax: 2% on sale, 5M cap
        margin = high - low - tax
        if margin <= 0:
            continue
        rows.append({
            "id": iid,
            "name": m["name"],
            "buy": low,
            "sell": high,
            "margin": margin,
            "ge_limit": m.get("limit"),
            "vol_1h": traded,
            "potential_1h": margin * min(traded, m.get("limit") or traded),
        })
    rows.sort(key=lambda r: r["potential_1h"], reverse=True)
    # 0 and None both mean "no cap" — matches the `--limit 0 == full pool` convention used by
    # merch.plan, so the same mental model can't silently truncate to an empty list here.
    return rows[:limit] if limit else rows


# --- CLI presentation helpers -------------------------------------------------------------
# The raw endpoints are huge (mapping ~280k tokens, latest/1h ~100k). Dumping them into an
# agent's context is wasteful and usually overflows it. The Python functions above always
# return full data for internal callers (margins, signals); the CLI instead reduces to what
# an agent actually needs, with an explicit `--raw` escape hatch for piping to a file.

def _parse_ids(spec: str | None) -> list[int]:
    if not spec:
        return []
    return [int(x) for x in spec.replace(",", " ").split() if x.strip()]


def _filter_universe(data: dict, ids: list[int]) -> dict:
    return {str(i): data[str(i)] for i in ids if str(i) in data}


def resolve_items(query: str, limit: int = 20) -> list[dict]:
    """Look up mapping rows by item id or case-insensitive name substring — the agent's way
    to turn 'twisted bow' into an id without ingesting the full 4000-item mapping."""
    q = query.strip().lower()
    rows = []
    for m in mapping():
        if q == str(m["id"]) or q in m["name"].lower():
            rows.append({k: m.get(k) for k in ("id", "name", "members", "limit", "value", "highalch")})
    return rows[:limit]


def timeseries_digest(rows: list[dict]) -> dict:
    """Compact summary of a timeseries: enough for grading/banding decisions without the
    hundreds of raw points. Use `--raw`/`--tail` when the points themselves are needed."""
    highs = [r["avgHighPrice"] for r in rows if r.get("avgHighPrice")]
    lows = [r["avgLowPrice"] for r in rows if r.get("avgLowPrice")]
    vols = [(r.get("highPriceVolume") or 0) + (r.get("lowPriceVolume") or 0) for r in rows]
    last = rows[-1] if rows else {}
    return {
        "points": len(rows),
        "first_ts": rows[0]["timestamp"] if rows else None,
        "last_ts": last.get("timestamp"),
        "latest_high": last.get("avgHighPrice"),
        "latest_low": last.get("avgLowPrice"),
        "high_min": min(highs) if highs else None,
        "high_max": max(highs) if highs else None,
        "low_min": min(lows) if lows else None,
        "low_max": max(lows) if lows else None,
        "avg_vol_per_step": round(sum(vols) / len(vols)) if vols else 0,
    }


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="merch.prices")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_map = sub.add_parser("mapping")
    p_map.add_argument("query", nargs="?", help="item id or name substring")
    p_map.add_argument("--limit", type=int, default=20)
    p_map.add_argument("--raw", action="store_true", help="dump the full mapping (~280k tokens)")

    for name in ("5m", "1h"):
        p = sub.add_parser(name)
        p.add_argument("--ids", help="comma/space-separated item ids")
        p.add_argument("--raw", action="store_true", help="dump the whole universe (~100k tokens)")

    p_latest = sub.add_parser("latest")
    p_latest.add_argument("item_id", nargs="?", type=int)
    p_latest.add_argument("--ids", help="comma/space-separated item ids")
    p_latest.add_argument("--raw", action="store_true", help="dump the whole universe (~120k tokens)")

    p_ts = sub.add_parser("timeseries")
    p_ts.add_argument("item_id", type=int)
    p_ts.add_argument("timestep", choices=["5m", "1h", "6h", "24h"])
    p_ts.add_argument("--raw", action="store_true", help="all points instead of a digest")
    p_ts.add_argument("--tail", type=int, help="last N raw points instead of a digest")

    p_m = sub.add_parser("margins")
    p_m.add_argument("--min-volume", type=int, default=100)
    p_m.add_argument("--limit", type=int, default=50)
    args = ap.parse_args(argv)

    if args.cmd == "mapping":
        if args.raw:
            out = mapping()
        elif args.query:
            out = resolve_items(args.query, limit=args.limit)
        else:
            out = {"items": len(mapping()),
                   "note": "full mapping suppressed; pass a name/id query, or --raw to dump all"}
    elif args.cmd in ("5m", "1h"):
        data = five_min() if args.cmd == "5m" else one_hour()
        ids = _parse_ids(args.ids)
        if args.raw:
            out = data
        elif ids:
            out = _filter_universe(data, ids)
        else:
            out = {"items": len(data),
                   "note": f"universe suppressed; pass --ids, use margins, or --raw"}
    elif args.cmd == "latest":
        if args.raw:
            out = latest()
        elif args.item_id:
            out = latest(args.item_id)
        elif args.ids:
            out = _filter_universe(latest(), _parse_ids(args.ids))
        else:
            out = {"items": len(latest()),
                   "note": "universe suppressed; pass an id, --ids, use margins, or --raw"}
    elif args.cmd == "timeseries":
        rows = timeseries(args.item_id, args.timestep)
        if args.raw:
            out = rows
        elif args.tail:
            out = rows[-args.tail:]
        else:
            out = timeseries_digest(rows)
    elif args.cmd == "margins":
        out = margins(min_volume=args.min_volume, limit=args.limit)
    else:
        return 2
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
