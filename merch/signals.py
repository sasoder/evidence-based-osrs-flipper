"""Intraday percentile/entropy trade filter inspired by the OSRS price-band workflow.

This complements merch.prices.margins. The margins command looks at the current spread;
this command asks whether recent timeseries data has recurring buy/sell bands worth
placing patient GE offers around.

Execution bands use 1h data (~15 days) for 2-12h flips. Regime and trend checks use 6h data
(~3 months) so a short-lived dip cannot hide a broader falling market.

CLI:
    python -m merch.signals scan --seed-limit 40 --limit 20 [--timestep 6h|1h|24h|5m]
    python -m merch.signals item 13190 [--timestep ...]
    python -m merch.signals backtest 13190 [--timestep ...]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from statistics import median

from . import prices
from .ge_tax import sale_tax

MAX_LATEST_AGE_MINUTES = 90
EXECUTION_TIMESTEP = "1h"
REGIME_TIMESTEP = "6h"
SEED_MIN_VOLUME = 75
FILL_WINDOW_HOURS = 4.0
MAX_HOLD_HOURS = 12

# Active-margin lane: short-lived, high-value opportunities are structurally different from
# patient percentile-band flips. These constants deliberately keep that lane narrow and small.
ACTIVE_MIN_PRICE = 1_000_000
ACTIVE_MIN_NET_MARGIN = 75_000
ACTIVE_MIN_ROI_PCT = 0.40
ACTIVE_MAX_ROI_PCT = 5.0       # larger displayed spreads are usually asynchronous/bad ticks
ACTIVE_MAX_QUOTE_AGE_MINUTES = 20  # high-value items trade a few times/hour, so a 10m gate made
                                   # the eligible pool flicker scan-to-scan (poor utilization); 20m
                                   # keeps a stable pool while still being a genuinely recent print
ACTIVE_MAX_DOWNTREND_PCT = -0.015
ACTIVE_RECENT_POINTS = 12      # one hour of 5m observations
ACTIVE_HORIZON_HOURS = 1.5
ACTIVE_PARTICIPATION_RATE = 0.25
ACTIVE_MAX_EXIT_PROBABILITY = 0.90

# Time-of-day experiment: 6h points provide roughly three months of repeated daily windows.
# The selected hold window is chosen on the older 70% and must remain profitable on the newest
# 30%, so this lane does not promote a pattern merely because it fits the whole sample.
TIME_OF_DAY_TIMESTEP = "6h"
TIME_OF_DAY_STEP_HOURS = 6
TIME_OF_DAY_BUCKETS = 24 // TIME_OF_DAY_STEP_HOURS
TIME_OF_DAY_MAX_HOLD_STEPS = 4
TIME_OF_DAY_MIN_TRAIN_TRADES = 40
TIME_OF_DAY_MIN_TEST_TRADES = 20
TIME_OF_DAY_MIN_WIN_RATE = 0.60


def percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def percentile_rank(values: list[int], value: int | None) -> float | None:
    """Return the fraction of observations at or below value."""
    if value is None or not values:
        return None
    return round(sum(1 for v in values if v <= value) / len(values), 3)


# Uniformity asks: do fill opportunities recur evenly across the natural cycle, or cluster?
# The cycle depends on the timestep. Sub-daily steps have a meaningful time-of-day spread, so
# we bucket by the distinct times-of-day the step can land on (5m/1h → 24, 6h → 4). A 24h step
# always lands at the same UTC hour, so time-of-day is degenerate; there we bucket by weekday
# (7) instead — the recurrence that matters when placing patient offers a day or two apart.
_STEP_MINUTES = {"5m": 5, "1h": 60, "6h": 360, "24h": 1440}


def _cycle(timestep: str) -> tuple[str, int]:
    per_day = 1440 // _STEP_MINUTES[timestep]
    return ("hour", min(24, per_day)) if per_day >= 2 else ("weekday", 7)


def normalized_entropy(timestamps: list[int], timestep: str) -> float:
    if not timestamps:
        return 0.0
    period, n = _cycle(timestep)
    buckets = [0] * n
    for ts in timestamps:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        idx = dt.weekday() if period == "weekday" else (dt.hour * n) // 24
        buckets[idx] += 1
    total = sum(buckets)
    entropy = 0.0
    for count in buckets:
        if not count:
            continue
        p = count / total
        entropy -= p * math.log(p)
    return round(entropy / math.log(n), 3)


def tax(sell_price: int) -> int:
    return sale_tax(sell_price)


def _volume_1h(item_id: int) -> dict:
    row = prices.one_hour().get(str(item_id), {})
    high = row.get("highPriceVolume") or 0
    low = row.get("lowPriceVolume") or 0
    return {"high": high, "low": low, "total": high + low}


def _latest_prices(item_id: int) -> dict:
    # Bulk /latest (one cached universe-wide call) instead of a per-item /latest?id= request:
    # in a scan this is hit once per candidate, so the per-item form was hundreds of redundant calls.
    row = prices.latest().get(str(item_id), {})
    high = row.get("high")
    low = row.get("low")
    now = datetime.now(timezone.utc).timestamp()
    high_time = row.get("highTime")
    low_time = row.get("lowTime")
    high_age = round((now - high_time) / 60, 1) if high_time else None
    low_age = round((now - low_time) / 60, 1) if low_time else None
    fresh = (
        high_age is not None and low_age is not None and
        high_age <= MAX_LATEST_AGE_MINUTES and low_age <= MAX_LATEST_AGE_MINUTES
    )
    return {
        "current_high": high,
        "current_low": low,
        "high_age_minutes": high_age,
        "low_age_minutes": low_age,
        "price_fresh": fresh,
    }


def live_quote(item_id: int) -> dict | None:
    meta = prices.mapping_by_id().get(item_id)
    if not meta:
        return None
    return {"id": item_id, "name": meta["name"], **_latest_prices(item_id)}


def _fillable_qty(ge_limit: int, volume_1h: dict, fill_window_hours: float,
                  participation_rate: float = 0.10) -> int:
    """Conservative round-trip fill estimate using the thinner side of 1h flow,
    projected over the window between logins (not a fixed 12h cycle)."""
    low_side = volume_1h.get("low", 0)
    high_side = volume_1h.get("high", 0)
    projected = max(0, round(min(low_side, high_side) * fill_window_hours * participation_rate))
    if ge_limit:
        return min(ge_limit, projected)
    return projected


def _regime_risk(rows: list[dict], lows: list[int], highs: list[int],
                 current_low: int | None, timestep: str = REGIME_TIMESTEP) -> dict:
    """Small drift/shock check so old percentile bands do not hide a breaking market."""
    if len(rows) < 40 or not lows or not highs:
        return {"level": "unknown", "reason": "insufficient_history"}

    split = max(20, len(rows) // 4)
    recent_rows = rows[-split:]
    prior_rows = rows[:-split]
    recent_lows = [r["avgLowPrice"] for r in recent_rows if r.get("avgLowPrice")]
    prior_lows = [r["avgLowPrice"] for r in prior_rows if r.get("avgLowPrice")]
    recent_vols = [(r.get("highPriceVolume") or 0) + (r.get("lowPriceVolume") or 0)
                   for r in recent_rows]
    prior_vols = [(r.get("highPriceVolume") or 0) + (r.get("lowPriceVolume") or 0)
                  for r in prior_rows]
    if not recent_lows or not prior_lows:
        return {"level": "unknown", "reason": "insufficient_price_history"}

    prior_median = median(prior_lows)
    recent_median = median(recent_lows)
    drift_pct = round((recent_median - prior_median) / prior_median, 4) if prior_median else 0
    latest_rank = percentile_rank(lows, current_low)

    prior_vol_median = median(prior_vols) if prior_vols else 0
    recent_vol_median = median(recent_vols) if recent_vols else 0
    volume_ratio = round(recent_vol_median / prior_vol_median, 2) if prior_vol_median else None

    # The drift comparison above splits at the last quarter of ~3 months (~23 days), so a fast
    # two-day crash barely moves the "recent" median (the Dragon arrow(p+) 45% break registered
    # -1.1%). Compare the last ~2 days of low prints to the week before them so a fast break
    # trips the gate independently of the slow-drift split.
    shock_rows = _recent_window(rows, timestep, days=2)
    shock_lows = [r["avgLowPrice"] for r in shock_rows if r.get("avgLowPrice")]
    week_rows = _recent_window(rows[:len(rows) - len(shock_rows)], timestep, days=7)
    week_lows = [r["avgLowPrice"] for r in week_rows if r.get("avgLowPrice")]
    shock_pct = None
    if len(shock_lows) >= 3 and len(week_lows) >= 5 and median(week_lows):
        shock_pct = round((median(shock_lows) - median(week_lows)) / median(week_lows), 4)

    if shock_pct is not None and shock_pct <= -0.15:
        level = "high"
        reason = "short_window_price_shock"
    elif drift_pct <= -0.08 and (latest_rank is None or latest_rank <= 0.08):
        level = "high"
        reason = "recent_price_breakdown"
    elif volume_ratio is not None and volume_ratio >= 3 and abs(drift_pct) >= 0.05:
        level = "high"
        reason = "volume_shock_with_price_drift"
    elif abs(drift_pct) >= 0.05:
        level = "medium"
        reason = "recent_distribution_drift"
    else:
        level = "low"
        reason = "stable_recent_distribution"

    return {
        "level": level,
        "reason": reason,
        "recent_low_median": round(recent_median),
        "prior_low_median": round(prior_median),
        "drift_pct": drift_pct,
        "shock_pct": shock_pct,
        "volume_ratio": volume_ratio,
    }


# Score multiplier per regime level. Note this is NOT monotonic in the obvious label order:
# "unknown" (sparse history) is penalized harder than "medium", sitting between low and high.
# Downtrend elevation must compare these factors, not the labels, to avoid *lowering* the risk.
_REGIME_FACTOR = {"low": 1.0, "medium": 0.65, "high": 0.25, "unknown": 0.5}


def _recent_window(rows: list[dict], timestep: str, days: float) -> list[dict]:
    """The last `days` of points at the given timestep (whole series if shorter)."""
    pts = max(8, round(days * 1440 / _STEP_MINUTES[timestep]))
    return rows[-pts:] if len(rows) > pts else rows


def _trend(rows: list[dict], timestep: str, days: float = 30) -> dict:
    """Direction of the *recent* ~`days` window, comparing edge medians (first/last fifth) of
    its mid-prices. This deliberately differs from regime drift, which compares the recent
    quarter of the full ~3mo history to everything before it: a peak-then-decline (pump that's
    now bleeding) can read as positive whole-history drift while the last month is sharply down.
    This catches that slow bleed so old percentile bands don't price an unreachable exit."""
    window = _recent_window(rows, timestep, days)
    # Crash buckets are often one-sided (avgHighPrice null — nobody instant-buying), so
    # requiring both sides silently drops exactly the points that show the break: 6 of 8
    # Dragon arrow(p+) crash buckets vanished and a ~45% collapse measured -9.8%. Fall back
    # to whichever side printed rather than discarding the bucket.
    mids = []
    for r in window:
        high = r.get("avgHighPrice")
        low = r.get("avgLowPrice")
        if high and low:
            mids.append((high + low) / 2)
        elif high or low:
            mids.append(high or low)
    if len(mids) < 8:
        return {"direction": "unknown", "pct": None, "window_points": len(mids)}
    # Compare the start and end of the window via edge medians (first/last fifth). Median of a
    # half compresses a steady slide to near-zero; edge medians capture the full move while
    # still smoothing single-point noise.
    edge = max(3, len(mids) // 5)
    older = median(mids[:edge])
    recent = median(mids[-edge:])
    # A fast break lives in the last handful of buckets, and the edge median dilutes it
    # (the Dragon arrow(p+) crash was ~9 of the last 24 points and read -2.7%). Take the
    # lower of the edge median and the last ~2 days' median, so a shock can only steepen
    # the reading — never soften it.
    shock_pts = max(3, round(2 * 1440 / _STEP_MINUTES[timestep]))
    if len(mids) > shock_pts:
        recent = min(recent, median(mids[-shock_pts:]))
    pct = round((recent - older) / older, 4) if older else 0.0
    direction = "down" if pct <= -0.08 else "up" if pct >= 0.08 else "flat"
    return {"direction": direction, "pct": pct, "window_points": len(mids)}


def _with_downtrend_risk(regime: dict, trend: dict) -> dict:
    """A slow bleed the short drift window misses is still a reason to wait. Elevate the regime
    gate (severe past -18%) so a downtrend can't masquerade as a low-risk dip buy. Only elevates
    when it genuinely raises risk (a lower score factor) — never downgrades a regime already
    flagged for another reason, and never softens the harsher 'unknown' (sparse-history) gate."""
    out = dict(regime)
    if trend["direction"] == "down":
        level = "high" if (trend["pct"] or 0) <= -0.18 else "medium"
        if _REGIME_FACTOR[level] < _REGIME_FACTOR[regime["level"]]:
            out["level"] = level
            out["reason"] = "sustained_downtrend"
    out["trend_pct"] = trend["pct"]
    return out


def _capped_sell(rows: list[dict], timestep: str, sell_band_full: int, trend: dict) -> int:
    """In a sustained downtrend the full-window sell band is propped up by stale pre-decline
    highs, pricing an exit the item has already left behind. Cap it to the 90th percentile of
    the last 3 days so an intraday target reflects the market the item is actually in now.
    A fast crash defeats the 3-day window too (pre-crash Dragon arrow(p+) 3,711 prints kept
    the cap at 3,598 while live trade was near 2,100), so the cap must also be validated by a
    high-side print that actually traded volume in the last ~36h; without one the item has no
    evidenced exit and the sell target collapses to 0 (callers drop the non-positive margin).
    Shared by live signals and the backtest so both grade the same strategy."""
    if trend["direction"] != "down":
        return sell_band_full
    recent_highs = [
        r["avgHighPrice"]
        for r in _recent_window(rows, timestep, days=3)
        if r.get("avgHighPrice")
    ]
    recent_cap = percentile(recent_highs, 0.90) if recent_highs else 0
    fresh_cap = max(
        (r["avgHighPrice"] for r in _recent_window(rows, timestep, days=1.5)
         if r.get("avgHighPrice") and (r.get("highPriceVolume") or 0) > 0),
        default=0,
    )
    if not fresh_cap:
        return 0
    cap = min(recent_cap, fresh_cap) if recent_cap else fresh_cap
    return cap if cap < sell_band_full else sell_band_full


# Moderate percentile bands provide recurring intraday opportunities. Selection remains the
# edge: every candidate must survive the 12h forced-exit backtest before recommendation.
BUY_QUANTILE = 0.35
SELL_QUANTILE = 0.75

# Production buys require evidence that the band has just traded: Wiki `low` is the latest
# instant-sell print, so current_low <= target_buy means a seller recently crossed that price.
# Near-band bids are intentionally kept out of production and exposed separately as an
# experimental patient-probe lane whose fills can be measured.
PATIENT_PROBE_MAX_DISTANCE_PCT = 3.0


def item_signal(
    item_id: int,
    timestep: str = EXECUTION_TIMESTEP,
    sell_quantile: float = SELL_QUANTILE,
    buy_quantile: float = BUY_QUANTILE,
    participation_rate: float = 0.10,
    fill_window_hours: float = FILL_WINDOW_HOURS,
) -> dict | None:
    meta = prices.mapping_by_id().get(item_id)
    if not meta:
        return None

    rows = prices.timeseries(item_id, timestep)
    regime_rows = rows if timestep == REGIME_TIMESTEP else prices.timeseries(item_id, REGIME_TIMESTEP)
    highs = [r["avgHighPrice"] for r in rows if r.get("avgHighPrice")]
    lows = [r["avgLowPrice"] for r in rows if r.get("avgLowPrice")]
    regime_highs = [r["avgHighPrice"] for r in regime_rows if r.get("avgHighPrice")]
    regime_lows = [r["avgLowPrice"] for r in regime_rows if r.get("avgLowPrice")]
    if min(len(highs), len(lows), len(regime_highs), len(regime_lows)) < 20:
        return None

    sell_band_full = percentile(highs, sell_quantile)
    target_buy = percentile(lows, buy_quantile)

    trend = _trend(regime_rows, REGIME_TIMESTEP)
    target_sell = _capped_sell(rows, timestep, sell_band_full, trend)

    sell_hits = [
        r["timestamp"] for r in rows
        if r.get("avgHighPrice") and r["avgHighPrice"] >= target_sell
    ]
    buy_hits = [
        r["timestamp"] for r in rows
        if r.get("avgLowPrice") and r["avgLowPrice"] <= target_buy
    ]
    sell_uniformity = normalized_entropy(sell_hits, timestep)
    buy_uniformity = normalized_entropy(buy_hits, timestep)
    uniformity_avg = round((sell_uniformity + buy_uniformity) / 2, 3)
    uniformity_equivalence = round(1 - abs(sell_uniformity - buy_uniformity), 3)
    observation_factor = min(1.0, min(len(sell_hits), len(buy_hits)) / 8)

    ge_limit = meta.get("limit") or 0
    latest = _latest_prices(item_id)
    current_high = latest["current_high"]
    current_low = latest["current_low"]
    price_fresh = latest["price_fresh"]
    distance_to_buy_pct = (
        round((current_low - target_buy) / target_buy * 100, 2)
        if current_low and target_buy else None
    )
    ready_to_buy = bool(price_fresh and current_low and current_low <= target_buy)
    patient_probe_ready = bool(
        price_fresh
        and distance_to_buy_pct is not None
        and 0 < distance_to_buy_pct <= PATIENT_PROBE_MAX_DISTANCE_PCT
    )
    buy_price = current_low if ready_to_buy else target_buy
    margin = target_sell - buy_price - tax(target_sell)
    if margin <= 0:
        return None
    margin_limit = margin * ge_limit if ge_limit else None
    volume_1h = _volume_1h(item_id)
    fillable_qty = _fillable_qty(ge_limit, volume_1h, fill_window_hours, participation_rate)
    liquidity_profit = margin * fillable_qty
    capital_required = buy_price * fillable_qty
    roi_pct = round(margin / buy_price * 100, 2) if buy_price else None
    distance_to_sell_pct = (
        round((target_sell - current_high) / target_sell * 100, 2)
        if current_high and target_sell else None
    )
    ready_to_sell = bool(price_fresh and current_high and current_high >= target_sell)
    regime = _regime_risk(regime_rows, regime_lows, regime_highs, current_low,
                          timestep=REGIME_TIMESTEP)

    regime = _with_downtrend_risk(regime, trend)

    score_base = liquidity_profit or (margin_limit if margin_limit is not None else margin)
    regime_factor = _REGIME_FACTOR[regime["level"]]
    readiness_factor = 1.0 if ready_to_buy else 0.75
    score = round(score_base * uniformity_avg * uniformity_equivalence *
                  observation_factor * regime_factor * readiness_factor)

    return {
        "id": item_id,
        "name": meta["name"],
        "timestep": timestep,
        "regime_timestep": REGIME_TIMESTEP,
        "buy": buy_price,
        "buy_band": target_buy,
        "sell": target_sell,
        "sell_band_full": sell_band_full,
        "trend": trend,
        "margin": margin,
        "ge_limit": ge_limit,
        "margin_limit": margin_limit,
        "current_low": current_low,
        "current_high": current_high,
        "high_age_minutes": latest["high_age_minutes"],
        "low_age_minutes": latest["low_age_minutes"],
        "price_fresh": price_fresh,
        "vol_1h": volume_1h["total"],
        "high_vol_1h": volume_1h["high"],
        "low_vol_1h": volume_1h["low"],
        "fill_window_hours": fill_window_hours,
        "fillable_qty": fillable_qty,
        "liquidity_profit": liquidity_profit,
        "capital_required": capital_required,
        "roi_pct": roi_pct,
        "low_percentile_now": percentile_rank(lows, current_low),
        "high_percentile_now": percentile_rank(highs, current_high),
        "distance_to_buy_pct": distance_to_buy_pct,
        "distance_to_sell_pct": distance_to_sell_pct,
        "ready_to_buy": ready_to_buy,
        "patient_probe_ready": patient_probe_ready,
        "ready_to_sell": ready_to_sell,
        "regime": regime,
        "sell_hits": len(sell_hits),
        "buy_hits": len(buy_hits),
        "sell_uniformity": sell_uniformity,
        "buy_uniformity": buy_uniformity,
        "uniformity_avg": uniformity_avg,
        "uniformity_equivalence": uniformity_equivalence,
        "score": score,
    }


def scan(seed_limit: int | None = 40, limit: int | None = 20, min_volume: int = SEED_MIN_VOLUME,
         timestep: str = EXECUTION_TIMESTEP,
         fill_window_hours: float = FILL_WINDOW_HOURS) -> list[dict]:
    seed_cap = None if seed_limit == 0 else seed_limit
    seeds = prices.margins(min_volume=min_volume, limit=seed_cap)
    prices.prefetch_timeseries([s["id"] for s in seeds], (timestep, REGIME_TIMESTEP))
    rows = []
    for seed in seeds:
        signal = item_signal(seed["id"], timestep=timestep, fill_window_hours=fill_window_hours)
        if signal:
            rows.append(signal)
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:limit] if limit else rows


def _active_short_drift(rows: list[dict]) -> float | None:
    """Recent midpoint drift for the active lane.

    High-value items are sparse, so only use rows where both sides traded. Fewer than four
    complete points is reported as unknown rather than fabricated from one-sided prints.
    """
    mids = [
        (r["avgHighPrice"] + r["avgLowPrice"]) / 2
        for r in rows[-ACTIVE_RECENT_POINTS:]
        if r.get("avgHighPrice") and r.get("avgLowPrice")
    ]
    if len(mids) < 4:
        return None
    edge = max(2, len(mids) // 3)
    older = median(mids[:edge])
    recent = median(mids[-edge:])
    return round((recent - older) / older, 4) if older else None


def _active_fillable_qty(ge_limit: int, volume: dict) -> int:
    """Quantity plausibly able to complete both legs inside the 90-minute window."""
    thinner_flow = min(volume["high"], volume["low"])
    projected = math.floor(
        thinner_flow * ACTIVE_HORIZON_HOURS * ACTIVE_PARTICIPATION_RATE
    )
    return min(ge_limit, max(1, projected)) if thinner_flow > 0 else 0


def _active_exit_loss_per_unit(buy: int, live_bid: int) -> int:
    """Loss if the spread does not close and the position must cross the live bid."""
    return max(0, buy - live_bid + tax(live_bid))


def active_margin_scan(seed_limit: int | None = None, limit: int = 20) -> dict:
    """Find small, short-horizon high-value margin probes.

    This is not the percentile strategy and intentionally has no 12h band backtest. It screens
    current after-tax spreads for fresh two-sided prints, real flow on both sides, plausible
    spread size, and no sharp one-hour decline. Quantity is capped by the GE limit.
    """
    candidates = []
    rejected = []
    seeds = prices.margins(min_volume=1, limit=None)
    high_value_seeds = [seed for seed in seeds if seed["buy"] >= ACTIVE_MIN_PRICE]
    # 0 / None both mean "scan all" — consistent with merch.plan's `--seed-limit 0 == full pool`.
    if seed_limit:
        high_value_seeds = high_value_seeds[:seed_limit]
    prices.prefetch_timeseries([s["id"] for s in high_value_seeds], ("5m",))

    for seed in high_value_seeds:
        iid = seed["id"]
        buy = seed["buy"] + 1
        sell = seed["sell"] - 1
        net_margin = sell - buy - tax(sell)
        roi_pct = round(net_margin / buy * 100, 2) if buy else 0

        if buy < ACTIVE_MIN_PRICE:
            continue
        if not seed.get("ge_limit"):
            rejected.append({"id": iid, "name": seed["name"],
                             "reason": "active item has no GE limit"})
            continue
        if net_margin < ACTIVE_MIN_NET_MARGIN:
            rejected.append({"id": iid, "name": seed["name"],
                             "reason": f"active net margin {net_margin:,}gp < {ACTIVE_MIN_NET_MARGIN:,}gp"})
            continue
        if roi_pct < ACTIVE_MIN_ROI_PCT:
            rejected.append({"id": iid, "name": seed["name"],
                             "reason": f"active net ROI {roi_pct:.2f}% < {ACTIVE_MIN_ROI_PCT:.2f}%"})
            continue
        if roi_pct > ACTIVE_MAX_ROI_PCT:
            rejected.append({"id": iid, "name": seed["name"],
                             "reason": f"active spread {roi_pct:.2f}% looks like asynchronous/bad ticks"})
            continue

        latest = _latest_prices(iid)
        if (
            not latest["price_fresh"]
            or latest["high_age_minutes"] is None
            or latest["low_age_minutes"] is None
            or latest["high_age_minutes"] > ACTIVE_MAX_QUOTE_AGE_MINUTES
            or latest["low_age_minutes"] > ACTIVE_MAX_QUOTE_AGE_MINUTES
        ):
            rejected.append({
                "id": iid,
                "name": seed["name"],
                "reason": (
                    f"active quote older than {ACTIVE_MAX_QUOTE_AGE_MINUTES} minutes"
                ),
            })
            continue

        volume = _volume_1h(iid)
        if min(volume["high"], volume["low"]) < 1:
            rejected.append({"id": iid, "name": seed["name"],
                             "reason": "active market lacks two-sided 1h flow"})
            continue

        drift = _active_short_drift(prices.timeseries(iid, "5m"))
        if drift is not None and drift <= ACTIVE_MAX_DOWNTREND_PCT:
            rejected.append({"id": iid, "name": seed["name"],
                             "reason": f"active 1h midpoint drift {drift:.2%} is falling"})
            continue

        max_qty = _active_fillable_qty(seed["ge_limit"], volume)
        if max_qty <= 0:
            rejected.append({"id": iid, "name": seed["name"],
                             "reason": "active market has no fillable 90m quantity"})
            continue
        exit_probability = min(
            ACTIVE_MAX_EXIT_PROBABILITY,
            volume["high"] * ACTIVE_HORIZON_HOURS / max_qty,
        )
        forced_exit_loss = _active_exit_loss_per_unit(buy, latest["current_low"])
        expected_value_per_unit = round(
            exit_probability * net_margin
            - (1 - exit_probability) * forced_exit_loss
        )
        if expected_value_per_unit <= 0:
            rejected.append({
                "id": iid,
                "name": seed["name"],
                "reason": (
                    f"active forced-exit EV {expected_value_per_unit:,}gp/u <= 0 "
                    f"({exit_probability:.0%} exit estimate)"
                ),
            })
            continue
        expected_profit = expected_value_per_unit * max_qty
        capital_required = buy * max_qty
        candidates.append({
            "id": iid,
            "name": seed["name"],
            "buy": buy,
            "sell": sell,
            "net_margin": net_margin,
            "roi_pct": roi_pct,
            "ge_limit": seed.get("ge_limit"),
            "vol_1h": volume["total"],
            "high_vol_1h": volume["high"],
            "low_vol_1h": volume["low"],
            "high_age_minutes": latest["high_age_minutes"],
            "low_age_minutes": latest["low_age_minutes"],
            "short_drift_pct": drift,
            "max_qty": max_qty,
            "fillable_qty": max_qty,
            "exit_probability": round(exit_probability, 3),
            "forced_exit_loss_per_unit": forced_exit_loss,
            "expected_value_per_unit": expected_value_per_unit,
            "expected_profit": expected_profit,
            "capital_required": capital_required,
            "expected_gp_per_hour": round(expected_profit / ACTIVE_HORIZON_HOURS),
            "expected_return_per_capital_hour": round(
                expected_profit / capital_required / ACTIVE_HORIZON_HOURS, 6
            ) if capital_required else 0,
        })

    candidates.sort(
        key=lambda r: (
            r["expected_gp_per_hour"],
            r["expected_return_per_capital_hour"],
            r["expected_profit"],
        ),
        reverse=True,
    )
    return {"candidates": candidates[:limit], "rejected": rejected}


def _time_samples(rows: list[dict], entry_bucket: int, hold_steps: int,
                  start: int, stop: int) -> list[dict]:
    samples = []
    expected_seconds = hold_steps * TIME_OF_DAY_STEP_HOURS * 3600
    for i in range(start, min(stop, len(rows) - hold_steps)):
        entry = rows[i]
        exit_row = rows[i + hold_steps]
        if datetime.fromtimestamp(entry["timestamp"], tz=timezone.utc).hour // TIME_OF_DAY_STEP_HOURS != entry_bucket:
            continue
        if abs((exit_row["timestamp"] - entry["timestamp"]) - expected_seconds) > 3600:
            continue
        buy = entry.get("avgLowPrice")
        sell = exit_row.get("avgHighPrice")
        if not buy or not sell:
            continue
        samples.append({
            "profit": sell - buy - tax(sell),
            "ratio": sell / buy,
        })
    return samples


def _time_metrics(samples: list[dict]) -> dict:
    profits = [row["profit"] for row in samples]
    return {
        "trades": len(samples),
        "total_profit_per_unit": sum(profits),
        "median_profit_per_unit": round(median(profits)) if profits else 0,
        "win_rate": round(sum(1 for profit in profits if profit > 0) / len(profits), 3)
        if profits else 0,
        "median_exit_ratio": median(row["ratio"] for row in samples) if samples else None,
    }


def _utc_window(bucket: int) -> str:
    start = bucket * TIME_OF_DAY_STEP_HOURS
    return f"{start:02d}:00-{start + TIME_OF_DAY_STEP_HOURS:02d}:00"


def time_of_day_signal(item_id: int) -> dict | None:
    """Find a recurring UTC entry/exit window with out-of-sample evidence."""
    meta = prices.mapping_by_id().get(item_id)
    if not meta or not meta.get("limit"):
        return None
    rows = sorted(
        prices.timeseries(item_id, TIME_OF_DAY_TIMESTEP),
        key=lambda row: row["timestamp"],
    )
    if len(rows) < 120:
        return None

    latest = _latest_prices(item_id)
    buy = latest["current_low"]
    if not latest["price_fresh"] or not buy:
        return None

    entry_bucket = datetime.now(timezone.utc).hour // TIME_OF_DAY_STEP_HOURS
    split = int(len(rows) * 0.70)
    training = []
    for hold_steps in range(1, TIME_OF_DAY_MAX_HOLD_STEPS + 1):
        metrics = _time_metrics(_time_samples(
            rows, entry_bucket, hold_steps, 0, split - hold_steps
        ))
        if (
            metrics["trades"] >= TIME_OF_DAY_MIN_TRAIN_TRADES
            and metrics["total_profit_per_unit"] > 0
            and metrics["median_profit_per_unit"] > 0
            and metrics["win_rate"] >= TIME_OF_DAY_MIN_WIN_RATE
        ):
            training.append((hold_steps, metrics))
    if not training:
        return None
    hold_steps, train = max(
        training,
        key=lambda row: row[1]["median_profit_per_unit"] / row[0],
    )

    test = _time_metrics(_time_samples(rows, entry_bucket, hold_steps, split, len(rows)))
    if (
        test["trades"] < TIME_OF_DAY_MIN_TEST_TRADES
        or test["total_profit_per_unit"] <= 0
        or test["median_profit_per_unit"] <= 0
        or test["win_rate"] < TIME_OF_DAY_MIN_WIN_RATE
    ):
        return None

    highs = [row["avgHighPrice"] for row in rows if row.get("avgHighPrice")]
    lows = [row["avgLowPrice"] for row in rows if row.get("avgLowPrice")]
    trend = _trend(rows, TIME_OF_DAY_TIMESTEP)
    regime = _with_downtrend_risk(
        _regime_risk(rows, lows, highs, buy, timestep=TIME_OF_DAY_TIMESTEP), trend)
    if regime["level"] == "high":
        return None

    sell = round(buy * test["median_exit_ratio"])
    expected_profit = sell - buy - tax(sell)
    if expected_profit <= 0:
        return None
    volume = _volume_1h(item_id)
    fillable_qty = _fillable_qty(
        meta["limit"], volume, TIME_OF_DAY_STEP_HOURS, participation_rate=0.05
    )
    if fillable_qty <= 0:
        return None

    exit_bucket = (entry_bucket + hold_steps) % TIME_OF_DAY_BUCKETS
    hold_hours = hold_steps * TIME_OF_DAY_STEP_HOURS
    return {
        "id": item_id,
        "name": meta["name"],
        "buy": buy,
        "sell": sell,
        "ge_limit": meta["limit"],
        "fillable_qty": fillable_qty,
        "expected_profit_per_unit": expected_profit,
        "expected_profit": expected_profit * fillable_qty,
        "hold_hours": hold_hours,
        "entry_window_utc": _utc_window(entry_bucket),
        "exit_window_utc": _utc_window(exit_bucket),
        "train": train,
        "test": test,
        "regime": regime,
        "score": round(expected_profit * fillable_qty / hold_hours),
    }


def time_of_day_scan(seed_limit: int | None = 80, limit: int = 10) -> dict:
    candidates = []
    seed_cap = None if seed_limit == 0 else seed_limit
    seeds = prices.margins(min_volume=SEED_MIN_VOLUME, limit=seed_cap)
    prices.prefetch_timeseries([s["id"] for s in seeds], (TIME_OF_DAY_TIMESTEP,))
    for seed in seeds:
        signal = time_of_day_signal(seed["id"])
        if signal:
            candidates.append(signal)
    candidates.sort(key=lambda row: row["score"], reverse=True)
    return {
        "candidates": candidates[:limit],
        "evaluated": len(seeds),
        "rejected_count": len(seeds) - len(candidates),
    }


def backtest_signal(
    item_id: int,
    timestep: str = EXECUTION_TIMESTEP,
    sell_quantile: float = SELL_QUANTILE,
    buy_quantile: float = BUY_QUANTILE,
    lookback: int = 60,
    max_hold_points: int | None = None,
) -> dict | None:
    """Walk-forward replay using only prior points to set bands.

    This is deliberately compact and per-unit. It checks whether percentile bands would have
    produced repeatable exits, not whether the agent could fill a full GE limit.

    ``max_hold_points`` models the repricing a real flipper does: if a position has not hit its
    sell band after this many points, force an exit at the current market high (reprice-to-clear),
    booking whatever profit/loss results instead of holding a falling knife indefinitely. ``None``
    keeps the original hold-forever behavior.
    """
    meta = prices.mapping_by_id().get(item_id)
    if not meta:
        return None
    rows = prices.timeseries(item_id, timestep)
    if len(rows) <= lookback + 2:
        return {
            "id": item_id,
            "name": meta["name"],
            "timestep": timestep,
            "points": len(rows),
            "error": "insufficient_history_for_lookback",
        }

    position = None
    trades = []
    adverse_pct = []
    for i in range(lookback, len(rows)):
        prior = rows[i - lookback:i]
        prior_highs = [r["avgHighPrice"] for r in prior if r.get("avgHighPrice")]
        prior_lows = [r["avgLowPrice"] for r in prior if r.get("avgLowPrice")]
        row = rows[i]
        if len(prior_highs) < 20 or len(prior_lows) < 20:
            continue
        buy = percentile(prior_lows, buy_quantile)
        # Mirror live: cap the sell band to the recent ceiling in a downtrend so the backtest
        # grades the strategy actually deployed, not an uncapped one it overstates exits for.
        sell = _capped_sell(prior, timestep, percentile(prior_highs, sell_quantile),
                            _trend(prior, timestep))
        net = sell - buy - tax(sell)
        if net <= 0:
            continue

        low = row.get("avgLowPrice")
        high = row.get("avgHighPrice")
        if position is None:
            if low and low <= buy:
                position = {
                    "buy": buy,
                    "sell": sell,
                    "entry_index": i,
                    "entry_ts": row["timestamp"],
                    "min_low": low,
                }
            continue

        if low:
            position["min_low"] = min(position["min_low"], low)
        held = i - position["entry_index"]
        timed_out = max_hold_points is not None and held >= max_hold_points and high
        if (high and high >= position["sell"]) or timed_out:
            hold_points = held
            exit_price = position["sell"] if (high and high >= position["sell"]) else high
            if exit_price is None:
                continue
            profit = exit_price - position["buy"] - tax(exit_price)
            trades.append({
                "entry_ts": position["entry_ts"],
                "exit_ts": row["timestamp"],
                "profit": profit,
                "hold_points": hold_points,
                "forced": exit_price != position["sell"],
            })
            adverse_pct.append(
                round((position["min_low"] - position["buy"]) / position["buy"] * 100, 2)
            )
            position = None

    profits = [t["profit"] for t in trades]
    hold_points = [t["hold_points"] for t in trades]
    step_hours = _STEP_MINUTES[timestep] / 60
    median_hold_hours = round(median(hold_points) * step_hours, 1) if hold_points else None
    # If a position is still open at the series end, how long has it been held? A small value
    # means it's a boundary artifact (just entered, no future data to exit); a large value means
    # the sell band was genuinely unreachable and capital would have been stranded.
    open_hold_points = (len(rows) - 1 - position["entry_index"]) if position else None
    return {
        "id": item_id,
        "name": meta["name"],
        "timestep": timestep,
        "points": len(rows),
        "lookback": lookback,
        "trades": len(trades),
        "forced_exits": sum(1 for t in trades if t.get("forced")),
        "open_position": bool(position),
        "open_hold_points": open_hold_points,
        "avg_profit_per_unit": round(sum(profits) / len(profits)) if profits else 0,
        "median_profit_per_unit": round(median(profits)) if profits else 0,
        "median_hold_points": round(median(hold_points), 1) if hold_points else None,
        "median_hold_hours": median_hold_hours,
        "max_adverse_pct": min(adverse_pct) if adverse_pct else None,
        "total_profit_per_unit": sum(profits),
    }


_TIMESTEPS = ["5m", "1h", "6h", "24h"]


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="merch.signals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--seed-limit", type=int, default=40)
    p_scan.add_argument("--limit", type=int, default=20)
    p_scan.add_argument("--min-volume", type=int, default=SEED_MIN_VOLUME)
    p_scan.add_argument("--timestep", choices=_TIMESTEPS, default=EXECUTION_TIMESTEP)
    p_active = sub.add_parser("active-scan")
    p_active.add_argument(
        "--seed-limit",
        type=int,
        default=None,
        help="optional cap after filtering to high-value items; default scans all",
    )
    p_active.add_argument("--limit", type=int, default=20)
    p_time = sub.add_parser("time-scan")
    p_time.add_argument("--seed-limit", type=int, default=80)
    p_time.add_argument("--limit", type=int, default=10)
    p_item = sub.add_parser("item")
    p_item.add_argument("item_id", type=int)
    p_item.add_argument("--timestep", choices=_TIMESTEPS, default=EXECUTION_TIMESTEP)
    p_item.add_argument("--buy-quantile", type=float, default=BUY_QUANTILE)
    p_item.add_argument("--sell-quantile", type=float, default=SELL_QUANTILE)
    p_backtest = sub.add_parser("backtest")
    p_backtest.add_argument("item_id", type=int)
    p_backtest.add_argument("--timestep", choices=_TIMESTEPS, default=EXECUTION_TIMESTEP)
    p_backtest.add_argument("--lookback", type=int, default=60)
    p_backtest.add_argument("--buy-quantile", type=float, default=BUY_QUANTILE)
    p_backtest.add_argument("--sell-quantile", type=float, default=SELL_QUANTILE)
    # Twelve 1h points = the strategy's hard intraday exit.
    p_backtest.add_argument("--max-hold", type=int, default=MAX_HOLD_HOURS,
                            help="force a reprice-to-clear exit after N points (0 = hold forever)")
    args = ap.parse_args(argv)

    if args.cmd == "scan":
        out = scan(seed_limit=args.seed_limit, limit=args.limit,
                   min_volume=args.min_volume, timestep=args.timestep)
    elif args.cmd == "active-scan":
        out = active_margin_scan(seed_limit=args.seed_limit, limit=args.limit)
    elif args.cmd == "time-scan":
        out = time_of_day_scan(seed_limit=args.seed_limit, limit=args.limit)
    elif args.cmd == "item":
        out = item_signal(args.item_id, timestep=args.timestep,
                          buy_quantile=args.buy_quantile, sell_quantile=args.sell_quantile)
    elif args.cmd == "backtest":
        out = backtest_signal(args.item_id, timestep=args.timestep, lookback=args.lookback,
                              buy_quantile=args.buy_quantile, sell_quantile=args.sell_quantile,
                              max_hold_points=(args.max_hold or None))
    else:
        return 2
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
