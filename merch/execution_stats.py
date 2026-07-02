"""Personal execution statistics derived from Flipping Utilities history.

Market data remains responsible for candidate selection. These statistics are a one-way sizing
cap: repeated slow or partial personal fills may reduce a proposed quantity, but historical
success never increases the market-derived fill estimate.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from statistics import median

from . import runelite

MIN_RELEVANT_ORDERS = 4
DEFAULT_WINDOW_HOURS = 4.0
STAPLE_MIN_PROFITABLE_TRIPS = 5
STAPLE_MAX_MEDIAN_HOURS = 12.0


def by_item(window_hours: float = DEFAULT_WINDOW_HOURS) -> dict[int, dict]:
    orders = runelite.read_offer_history()
    flips = runelite.read_flips()
    grouped: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for order in orders:
        if not _relevant(order):
            continue
        grouped[(order["id"], order["side"])].append(order)

    result: dict[int, dict] = {}
    ids = {iid for iid, _ in grouped}
    for iid in ids:
        buy = _order_stats(grouped.get((iid, "buy"), []), window_hours)
        sell = _order_stats(grouped.get((iid, "sell"), []), window_hours)
        item_flips = [f for f in flips if f.get("id") == iid]
        name = next(
            (o["name"] for side in ("buy", "sell") for o in grouped.get((iid, side), [])),
            next((f.get("name") for f in item_flips), f"item_{iid}"),
        )
        result[iid] = {
            "id": iid,
            "name": name,
            "buy": buy,
            "sell": sell,
            "round_trip": _round_trip_stats(item_flips),
        }
    return result


def adjusted_fillable_qty(item_id: int, market_qty: int, stats: dict[int, dict]) -> tuple[int, dict | None]:
    item = stats.get(item_id)
    buy = (item or {}).get("buy") or {}
    if market_qty <= 0 or not buy.get("eligible"):
        return market_qty, None
    factor = min(1.0, max(0.0, buy["window_fill_factor"]))
    adjusted = min(market_qty, round(market_qty * factor))
    evidence = {
        "orders": buy["orders"],
        "window_fill_factor": factor,
        "market_fillable_qty": market_qty,
        "adjusted_fillable_qty": adjusted,
    }
    return adjusted, evidence


def _relevant(order: dict) -> bool:
    return (
        order.get("total_qty", 0) > 1
        and order.get("filled_qty", 0) > 0
        and order.get("price", 0) > 0
        and order.get("state") in {"BOUGHT", "SOLD", "CANCELLED_BUY", "CANCELLED_SELL"}
    )


def _order_stats(orders: list[dict], window_hours: float) -> dict:
    window_seconds = window_hours * 3600
    factors = []
    durations = []
    partials = 0
    for order in orders:
        total = order["total_qty"]
        fill_ratio = min(1.0, order["filled_qty"] / total) if total else 0.0
        duration = order.get("duration_seconds")
        if duration is not None and not order.get("before_login"):
            durations.append(duration)
            time_factor = min(1.0, window_seconds / max(duration, 1))
        else:
            # Unknown timing cannot justify a reduction or an increase.
            time_factor = 1.0
        factors.append(fill_ratio * time_factor)
        if fill_ratio < 1:
            partials += 1
    count = len(orders)
    return {
        "orders": count,
        "eligible": count >= MIN_RELEVANT_ORDERS,
        "window_hours": window_hours,
        "window_fill_factor": round(median(factors), 3) if factors else None,
        "median_fill_seconds": round(median(durations)) if durations else None,
        "partial_rate": round(partials / count, 3) if count else None,
    }


def _round_trip_stats(flips: list[dict]) -> dict:
    closed = [
        f for f in flips
        if (f.get("bought_qty") or 0) > 0
        and (f.get("sold_qty") or 0) >= (f.get("bought_qty") or 0)
        and f.get("buy_ts") and f.get("sell_ts")
    ]
    rois = []
    hours = []
    gp_per_capital_hour = []
    profits = []
    for flip in closed:
        qty = flip["bought_qty"]
        capital = flip["bought"] * qty
        duration_hours = max(1 / 3600, (flip["sell_ts"] - flip["buy_ts"]) / 3_600_000)
        profit = flip.get("profit") or 0
        profits.append(profit)
        hours.append(duration_hours)
        if capital > 0:
            rois.append(profit / capital)
            gp_per_capital_hour.append(profit / duration_hours)
    profitable_trips = sum(1 for profit in profits if profit > 0)
    net_profit = sum(profits)
    median_hours = round(median(hours), 2) if hours else None
    staple = bool(
        profitable_trips >= STAPLE_MIN_PROFITABLE_TRIPS
        and net_profit > 0
        and median_hours is not None
        and median_hours <= STAPLE_MAX_MEDIAN_HOURS
    )
    return {
        "trips": len(closed),
        "profitable_trips": profitable_trips,
        "losing_trips": sum(1 for profit in profits if profit < 0),
        "net_profit": net_profit,
        "win_rate": round(profitable_trips / len(closed), 3) if closed else None,
        "median_hours": median_hours,
        "median_net_roi_pct": round(median(rois) * 100, 3) if rois else None,
        "median_gp_per_capital_hour": round(median(gp_per_capital_hour)) if gp_per_capital_hour else None,
        "staple": staple,
        "staple_progress": (
            f"{profitable_trips}/{STAPLE_MIN_PROFITABLE_TRIPS} profitable trips; "
            f"median {median_hours}h/{STAPLE_MAX_MEDIAN_HOURS:g}h"
            if median_hours is not None else
            f"{profitable_trips}/{STAPLE_MIN_PROFITABLE_TRIPS} profitable trips"
        ),
    }


def _main() -> int:
    json.dump(list(by_item().values()), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
