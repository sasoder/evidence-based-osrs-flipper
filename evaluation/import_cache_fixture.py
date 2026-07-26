"""Build reproducible multi-date market-universe fixtures from the local Wiki cache.

The importer is not used during evaluation. It converts ignored cache envelopes into a checked-in
gzip corpus with pre-cut history and withheld future buckets. Selection is performed by the planner,
never by this script. By default every eligible item is retained; an explicit cohort limit can bound
repository size while deliberately mixing top-ranked and stratified random items.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from flipper.ge_tax import sale_tax


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "data/cache"
DEFAULT_OUTPUT = ROOT / "evaluation/fixtures/real_market.json.gz"
TARGET_DATES = ("2026-07-11", "2026-07-22")
REGRESSION_IDS = {823, 4151, 11228, 20997, 22486, 27277, 26374}


def _cache_rows(cache_dir: Path):
    for path in cache_dir.glob("*.json"):
        try:
            envelope = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        stored = envelope.get("stored_at")
        if not stored:
            continue
        day = datetime.fromtimestamp(stored, timezone.utc).date().isoformat()
        yield day, envelope.get("key", ""), envelope.get("value")


def _query(key: str) -> tuple[int, str] | None:
    if "/timeseries" not in key:
        return None
    parsed = urllib.parse.urlsplit(key.split(" ", 1)[-1])
    query = urllib.parse.parse_qs(parsed.query)
    try:
        return int(query["id"][0]), query["timestep"][0]
    except (KeyError, TypeError, ValueError):
        return None


def _load(cache_dir: Path) -> tuple[dict[int, dict], dict[str, dict[tuple[int, str], list[dict]]]]:
    mapping = {}
    by_day: dict[str, dict[tuple[int, str], list[dict]]] = defaultdict(dict)
    for day, key, value in _cache_rows(cache_dir):
        if "/mapping" in key and isinstance(value, list):
            mapping = {int(row["id"]): row for row in value}
            continue
        parsed = _query(key)
        if not parsed or not isinstance(value, dict):
            continue
        rows = value.get("data")
        if isinstance(rows, list):
            by_day[day][parsed] = sorted(rows, key=lambda row: row["timestamp"])
    if not mapping:
        raise ValueError(f"no mapping cache found under {cache_dir}")
    return mapping, by_day


def _common_end(series: dict[tuple[int, str], list[dict]], ids: set[int], timestep: str) -> int:
    ends = Counter(
        series[(iid, timestep)][-1]["timestamp"]
        for iid in ids if series.get((iid, timestep))
    )
    if not ends:
        raise ValueError(f"no {timestep} series")
    return ends.most_common(1)[0][0]


def _visible_and_future(rows: list[dict], cut: int, history_points: int,
                        future_points: int) -> tuple[list[dict], list[dict]]:
    before = [row for row in rows if row["timestamp"] <= cut]
    after = [row for row in rows if row["timestamp"] > cut]
    return before[-history_points:], after[:future_points]


def _features(iid: int, row: dict, meta: dict) -> dict:
    low = row.get("avgLowPrice") or 0
    high = row.get("avgHighPrice") or 0
    high_volume = row.get("highPriceVolume") or 0
    low_volume = row.get("lowPriceVolume") or 0
    volume = high_volume + low_volume
    margin = max(0, high - low - sale_tax(high)) if high and low else 0
    limit = meta.get("limit") or volume
    return {
        "id": iid, "price": low, "volume": volume, "margin": margin,
        "profit_proxy": margin * min(volume, limit),
        "capital_proxy": low * min(high_volume, limit),
    }


def _bucket(feature: dict) -> tuple[int, int]:
    price = feature["price"]
    volume = feature["volume"]
    price_bin = next((i for i, bound in enumerate((1_000, 100_000, 1_000_000,
                                                   10_000_000, 100_000_000))
                      if price < bound), 5)
    volume_bin = next((i for i, bound in enumerate((10, 100, 1_000, 10_000))
                       if volume < bound), 4)
    return price_bin, volume_bin


def _cohort(features: list[dict], has_5m: set[int], size: int, seed: int) -> list[int]:
    if size <= 0 or len(features) <= size:
        return sorted(row["id"] for row in features)
    chosen = set(REGRESSION_IDS)
    chosen.update(row["id"] for row in sorted(
        features, key=lambda row: row["profit_proxy"], reverse=True)[:100])
    chosen.update(row["id"] for row in sorted(
        features, key=lambda row: row["capital_proxy"], reverse=True)[:60])
    chosen.update(has_5m)
    allowed = {row["id"] for row in features}
    chosen &= allowed

    groups: dict[tuple[int, int], list[int]] = defaultdict(list)
    for row in features:
        if row["id"] not in chosen:
            groups[_bucket(row)].append(row["id"])
    rng = random.Random(seed)
    for values in groups.values():
        rng.shuffle(values)
    keys = sorted(groups)
    while len(chosen) < size and keys:
        remaining = []
        for key in keys:
            values = groups[key]
            if values and len(chosen) < size:
                chosen.add(values.pop())
            if values:
                remaining.append(key)
        keys = remaining
    return sorted(chosen)


def _fixture(day: str, mapping: dict[int, dict], series: dict[tuple[int, str], list[dict]],
             cohort_size: int) -> dict:
    ids = {
        iid for iid, meta in mapping.items()
        if meta.get("members") and series.get((iid, "1h")) and series.get((iid, "6h"))
    }
    end = min(_common_end(series, ids, "1h"), _common_end(series, ids, "6h"))
    cut = end - 24 * 3_600
    cut -= cut % (6 * 3_600)
    eligible = []
    staged = {}
    for iid in ids:
        h1, f1 = _visible_and_future(series[(iid, "1h")], cut, 180, 30)
        h6, f6 = _visible_and_future(series[(iid, "6h")], cut, 180, 6)
        if len(h1) < 70 or len(h6) < 120 or len(f1) < 12 or len(f6) < 4:
            continue
        h5, f5 = _visible_and_future(series.get((iid, "5m"), []), cut, 120, 24)
        latest = h1[-1]
        feature = _features(iid, latest, mapping[iid])
        eligible.append(feature)
        staged[iid] = (h1, f1, h6, f6, h5, f5, latest)
    has_5m = {iid for iid, rows in staged.items() if len(rows[4]) >= 4 and len(rows[5]) >= 6}
    selected = _cohort(eligible, has_5m, cohort_size, seed=int(day.replace("-", "")))
    items = []
    for iid in selected:
        h1, f1, h6, f6, h5, f5, latest = staged[iid]
        meta = mapping[iid]
        items.append({
            "id": iid, "name": meta["name"], "members": True, "limit": meta.get("limit"),
            "latest": {
                "high": latest.get("avgHighPrice"), "low": latest.get("avgLowPrice"),
                "highTime": latest["timestamp"], "lowTime": latest["timestamp"],
            },
            "one_hour": {
                "highPriceVolume": latest.get("highPriceVolume") or 0,
                "lowPriceVolume": latest.get("lowPriceVolume") or 0,
            },
            "history": {"1h": h1, "6h": h6, "5m": h5},
            "future": {"1h": f1, "6h": f6, "5m": f5},
        })
    return {
        "name": f"wiki_cache_{day}", "seed": int(day.replace("-", "")),
        "regime": "observed", "as_of": datetime.fromtimestamp(cut, timezone.utc).isoformat(),
        "universe": {
            "eligible_items": len(eligible), "cohort_items": len(items),
            "items_with_5m": len(has_5m),
            "selection": (
                "all eligible items" if len(items) == len(eligible)
                else "top profit/capital proxies + all 5m + stratified random remainder"
            ),
        },
        "items": items,
    }


def _write_gzip(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    with path.open("wb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as compressed:
            compressed.write(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dates", nargs="*", default=list(TARGET_DATES))
    parser.add_argument(
        "--cohort-size", type=int, default=0,
        help="maximum items per date; 0 retains the full eligible universe",
    )
    args = parser.parse_args()
    mapping, by_day = _load(args.cache_dir)
    fixtures = [
        _fixture(day, mapping, by_day[day], args.cohort_size)
        for day in args.dates if day in by_day
    ]
    if not fixtures:
        raise ValueError(f"no requested cache dates found: {args.dates}")
    payload = {"version": 1, "fixtures": fixtures}
    _write_gzip(args.output, payload)
    print(json.dumps({
        "path": str(args.output),
        "fixtures": [
            {"name": row["name"], **row["universe"]} for row in fixtures
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
