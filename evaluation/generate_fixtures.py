"""Generate the checked-in synthetic evaluator-v2 regression corpus.

The output is deterministic. Each strategy family receives its own decision cutoff so its visible
history ends exactly one bucket before withheld outcomes begin.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "fixtures/core_market.json"
AS_OF = int(datetime(2026, 7, 1, 12, tzinfo=timezone.utc).timestamp())
TIERS = (
    (500, "cheap-liquid", 500, 80, 10_000, 5_000),
    (50_000, "mid-liquid", 50_000, 4_000, 1_000, 500),
    (5_000_000, "high-active", 5_000_000, 250_000, 8, 20),
    (50_000_000, "thin-gear", 50_000_000, 1_500_000, 8, 5),
)
SCENARIOS = (
    ("stable_seed_11", 11, "stable"),
    ("stable_seed_29", 29, "stable"),
    ("quiet_seed_47", 47, "quiet"),
    ("reversal_seed_71", 71, "reversal"),
    ("crash_guard_seed_89", 89, "crash"),
)


def _price(base: int, spread: int, i: int, step_hours: float, phase: float,
           rng: random.Random, mode: str, future: bool) -> tuple[int, int]:
    cycle = math.sin((i * step_hours / 24) * math.tau + phase)
    slower = math.sin((i * step_hours / (24 * 7)) * math.tau + phase / 2)
    jitter = rng.uniform(-0.0015, 0.0015)
    center = base * (1 + 0.025 * cycle + 0.012 * slower + jitter)
    if future and mode == "reversal":
        center *= max(0.78, 1 - 0.012 * (i + 1) * step_hours)
    elif mode == "crash":
        if future:
            center *= max(0.58, 1 - 0.026 * (i + 1) * step_hours)
        elif i >= 0:
            center *= max(0.72, 1 - 0.006 * (i + 1) * step_hours)
    half = spread / 2
    return max(1, round(center - half)), max(2, round(center + half))


def _rows(base: int, spread: int, volume: int, phase: float, seed: int, mode: str,
          timestep: str, count: int, future: bool) -> list[dict]:
    step_seconds = {"5m": 300, "1h": 3_600, "6h": 21_600}[timestep]
    step_hours = step_seconds / 3_600
    rng = random.Random((seed * 10_000) + base + step_seconds + (1 if future else 0))
    start = AS_OF if future else AS_OF - count * step_seconds
    effective_volume = max(1, round(volume * (0.08 if mode == "quiet" else 1)))
    rows = []
    for offset in range(count):
        # Historical crash evidence is concentrated at the boundary; future indices begin at zero.
        price_i = offset if future else offset - count
        low, high = _price(base, spread, price_i, step_hours, phase, rng, mode, future)
        rows.append({
            "timestamp": start + offset * step_seconds,
            "avgLowPrice": low,
            "avgHighPrice": high,
            "lowPriceVolume": effective_volume,
            "highPriceVolume": effective_volume,
        })
    return rows


def _item(scenario_index: int, seed: int, mode: str, tier: tuple,
          tier_index: int) -> dict:
    _, label, base, spread, limit, volume = tier
    iid = 90_000 + scenario_index * 100 + tier_index
    phase = (seed % 17) / 17 * math.tau + tier_index * 0.41
    history = {
        "5m": _rows(base, spread, volume, phase, seed, mode, "5m", 120, False),
        "1h": _rows(base, spread, volume, phase, seed, mode, "1h", 180, False),
        "6h": _rows(base, spread, volume, phase, seed, mode, "6h", 240, False),
    }
    future = {
        "5m": _rows(base, spread, volume, phase, seed, mode, "5m", 36, True),
        "1h": _rows(base, spread, volume, phase, seed, mode, "1h", 30, True),
        "6h": _rows(base, spread, volume, phase, seed, mode, "6h", 8, True),
    }
    last = history["1h"][-1]
    current_low = last["avgLowPrice"]
    current_high = last["avgHighPrice"]
    if mode == "reversal":
        current_high = round(current_high * 1.012)
    return {
        "id": iid,
        "name": f"{label}-{seed}",
        "members": True,
        "limit": limit,
        "latest": {
            "high": current_high,
            "low": current_low,
            "highTime": AS_OF - 60,
            "lowTime": AS_OF - 60,
        },
        "one_hour": {
            "highPriceVolume": history["1h"][-1]["highPriceVolume"],
            "lowPriceVolume": history["1h"][-1]["lowPriceVolume"],
        },
        "history": history,
        "future": future,
    }


def build() -> dict:
    fixtures = []
    for scenario_index, (name, seed, mode) in enumerate(SCENARIOS):
        items = [
            _item(scenario_index, seed, mode, tier, tier_index)
            for tier_index, tier in enumerate(TIERS)
        ]
        decision_cutoffs = {
            "patient": datetime.fromtimestamp(
                items[0]["history"]["1h"][-1]["timestamp"], timezone.utc
            ).isoformat(),
            "active": datetime.fromtimestamp(
                items[0]["history"]["5m"][-1]["timestamp"], timezone.utc
            ).isoformat(),
            "time": datetime.fromtimestamp(
                items[0]["history"]["6h"][-1]["timestamp"], timezone.utc
            ).isoformat(),
        }
        fixtures.append({
            "name": name,
            "seed": seed,
            "regime": mode,
            "fixture_class": "synthetic",
            "as_of": decision_cutoffs["patient"],
            "decision_cutoffs": decision_cutoffs,
            "source_provenance": {
                "kind": "deterministic_generator",
                "generator": "evaluation.generate_fixtures",
                "seed": seed,
                "review_status": "checked_in",
            },
            "items": items,
        })
    return {"version": 2, "fixtures": fixtures}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build(), separators=(",", ":")) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
