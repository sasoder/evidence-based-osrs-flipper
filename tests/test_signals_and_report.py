from __future__ import annotations

import json
import io
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import urllib.error

from merch import intents, plan, prices, research, runelite, signals

# Fixture exports are always written and read under this name; tests must not
# depend on the machine-local config/settings.json rsn.
_TEST_RSN = "Tester"


class TimeseriesMemoTests(unittest.TestCase):
    def setUp(self) -> None:
        prices._TS_MEMO.clear()
        self.addCleanup(prices._TS_MEMO.clear)

    def test_prefetch_then_timeseries_serves_from_memo_without_refetch(self) -> None:
        calls: list[tuple[int, str]] = []

        def fake_fetch(item_id: int, timestep: str) -> list[dict]:
            calls.append((item_id, timestep))
            return [{"timestamp": 0, "id": item_id, "step": timestep}]

        with patch("merch.prices._fetch_timeseries", side_effect=fake_fetch):
            prices.prefetch_timeseries([1, 2], ("1h", "6h"))
            # Every (item, timestep) pair fetched exactly once, concurrently.
            self.assertEqual(sorted(calls), [(1, "1h"), (1, "6h"), (2, "1h"), (2, "6h")])
            # Reads now come from the memo — no further fetches.
            self.assertEqual(prices.timeseries(1, "1h"), [{"timestamp": 0, "id": 1, "step": "1h"}])
            prices.timeseries(2, "6h")
            prices.prefetch_timeseries([1, 2], ("1h", "6h"))  # idempotent
            self.assertEqual(len(calls), 4)

    def test_timeseries_falls_back_to_single_fetch_when_not_prefetched(self) -> None:
        with patch("merch.prices._fetch_timeseries",
                   return_value=[{"timestamp": 0}]) as fetch:
            self.assertEqual(prices.timeseries(99, "1h"), [{"timestamp": 0}])
            prices.timeseries(99, "1h")  # second read is memoized
        fetch.assert_called_once_with(99, "1h")


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _rows(n: int = 80) -> list[dict]:
    start = int(time.time()) - n * 3600
    return [
        {
            "timestamp": start + i * 3600,
            "avgLowPrice": 100,
            "avgHighPrice": 200,
            "lowPriceVolume": 50,
            "highPriceVolume": 50,
        }
        for i in range(n)
    ]


class SignalTests(unittest.TestCase):
    def test_fillable_qty_uses_thinner_side_volume(self) -> None:
        volume = {"low": 1000, "high": 10, "total": 1010}

        # thinner side (10/h) * 4h window * 0.10 participation = 4
        self.assertEqual(signals._fillable_qty(1000, volume, signals.FILL_WINDOW_HOURS), 4)

    def test_scan_zero_seed_limit_scans_all_and_forwards_fill_window(self) -> None:
        with (
            patch("merch.signals.prices.margins", return_value=[{"id": 1, "score": 1}]) as margins,
            patch("merch.signals.prices.prefetch_timeseries") as prefetch,
            patch("merch.signals.item_signal", return_value={"id": 1, "score": 1}) as item_signal,
        ):
            rows = signals.scan(seed_limit=0, limit=None, fill_window_hours=12)

        self.assertEqual(rows, [{"id": 1, "score": 1}])
        margins.assert_called_once_with(min_volume=signals.SEED_MIN_VOLUME, limit=None)
        prefetch.assert_called_once_with([1], (signals.EXECUTION_TIMESTEP, signals.REGIME_TIMESTEP))
        item_signal.assert_called_once_with(1, timestep=signals.EXECUTION_TIMESTEP, fill_window_hours=12)

    def test_stale_latest_quote_blocks_readiness(self) -> None:
        now = int(time.time())
        latest = {
            "1": {
                "low": 100,
                "high": 200,
                "lowTime": now - 7200,
                "highTime": now,
            }
        }

        with (
            patch("merch.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Test item", "limit": 100}}),
            patch("merch.prices.timeseries", return_value=_rows()),
            patch("merch.prices.latest", return_value=latest),
            patch("merch.prices.one_hour", return_value={"1": {"lowPriceVolume": 100, "highPriceVolume": 100}}),
        ):
            signal = signals.item_signal(1)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertFalse(signal["price_fresh"])
        self.assertFalse(signal["ready_to_buy"])

    def test_ready_entry_uses_the_live_low_not_the_historical_band(self) -> None:
        now = int(time.time())
        with (
            patch("merch.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Test item", "limit": 100}}),
            patch("merch.prices.timeseries", return_value=_rows()),
            patch("merch.prices.latest", return_value={
                "1": {"low": 90, "high": 200, "lowTime": now, "highTime": now}
            }),
            patch("merch.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 100, "highPriceVolume": 100}
            }),
        ):
            signal = signals.item_signal(1)

        assert signal is not None
        self.assertEqual(signal["buy_band"], 100)
        self.assertEqual(signal["buy"], 90)
        self.assertTrue(signal["ready_to_buy"])

    def test_near_band_bid_is_probe_only_not_production_ready(self) -> None:
        # The live low (102) sits 2% above the band (100). That is not evidence that the band
        # recently traded, so production stays blocked while the experimental probe lane may bid.
        now = int(time.time())
        with (
            patch("merch.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Test item", "limit": 100}}),
            patch("merch.prices.timeseries", return_value=_rows()),
            patch("merch.prices.latest", return_value={
                "1": {"low": 102, "high": 200, "lowTime": now, "highTime": now}
            }),
            patch("merch.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 100, "highPriceVolume": 100}
            }),
        ):
            signal = signals.item_signal(1)

        assert signal is not None
        self.assertEqual(signal["buy_band"], 100)
        self.assertEqual(signal["buy"], 100)
        self.assertFalse(signal["ready_to_buy"])
        self.assertTrue(signal["patient_probe_ready"])

    def test_patient_bid_not_ready_when_live_low_far_above_band(self) -> None:
        # Live low 120 is 20% above the band — the bid would never fill in the window. Not ready.
        now = int(time.time())
        with (
            patch("merch.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Test item", "limit": 100}}),
            patch("merch.prices.timeseries", return_value=_rows()),
            patch("merch.prices.latest", return_value={
                "1": {"low": 120, "high": 200, "lowTime": now, "highTime": now}
            }),
            patch("merch.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 100, "highPriceVolume": 100}
            }),
        ):
            signal = signals.item_signal(1)

        assert signal is not None
        self.assertFalse(signal["ready_to_buy"])
        self.assertFalse(signal["patient_probe_ready"])

    def test_active_margin_scan_accepts_fresh_stable_after_tax_spread(self) -> None:
        now = int(time.time())
        stable = _rows(12)
        for row in stable:
            row["avgLowPrice"] = 10_000_000
            row["avgHighPrice"] = 10_300_000
        with (
            patch("merch.prices.margins", return_value=[{
                "id": 1, "name": "Test gear", "buy": 10_000_000, "sell": 10_300_000,
                "margin": 94_000, "ge_limit": 8, "vol_1h": 10, "potential_1h": 752_000,
            }]),
            patch("merch.prices.latest", return_value={
                "1": {"low": 10_000_000, "high": 10_300_000,
                      "lowTime": now, "highTime": now}
            }),
            patch("merch.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 5, "highPriceVolume": 5}
            }),
            patch("merch.prices.timeseries", return_value=stable),
            patch("merch.prices.prefetch_timeseries"),
        ):
            result = signals.active_margin_scan()

        self.assertEqual(len(result["candidates"]), 1)
        row = result["candidates"][0]
        self.assertEqual(row["buy"], 10_000_001)
        self.assertEqual(row["sell"], 10_299_999)
        self.assertEqual(row["max_qty"], 1)
        self.assertEqual(row["fillable_qty"], 1)
        self.assertGreater(row["expected_value_per_unit"], 0)
        self.assertGreater(row["expected_gp_per_hour"], 0)
        self.assertGreaterEqual(row["net_margin"], signals.ACTIVE_MIN_NET_MARGIN)

    def test_active_margin_scan_rejects_stale_quote(self) -> None:
        now = int(time.time())
        with (
            patch("merch.prices.margins", return_value=[{
                "id": 1, "name": "Test gear", "buy": 10_000_000, "sell": 10_300_000,
                "margin": 94_000, "ge_limit": 8, "vol_1h": 10, "potential_1h": 752_000,
            }]),
            patch("merch.prices.latest", return_value={
                "1": {"low": 10_000_000, "high": 10_300_000,
                      "lowTime": now - 1260, "highTime": now}
            }),
            patch("merch.prices.prefetch_timeseries"),
        ):
            result = signals.active_margin_scan()

        self.assertEqual(result["candidates"], [])
        self.assertIn(
            f"older than {signals.ACTIVE_MAX_QUOTE_AGE_MINUTES} minutes",
            result["rejected"][0]["reason"],
        )

    def test_active_margin_scan_requires_ge_limit(self) -> None:
        with (
            patch("merch.prices.margins", return_value=[{
                "id": 1, "name": "Unknown limit gear", "buy": 10_000_000, "sell": 10_300_000,
                "margin": 94_000, "ge_limit": None, "vol_1h": 10, "potential_1h": 0,
            }]),
            patch("merch.prices.prefetch_timeseries"),
        ):
            result = signals.active_margin_scan()

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["rejected"][0]["reason"], "active item has no GE limit")

    def test_time_of_day_signal_requires_profitable_holdout_window(self) -> None:
        step = 6 * 3600
        end = int(time.time()) // step * step
        rows = [
            {
                "timestamp": end - (359 - i) * step,
                "avgLowPrice": 110,
                "avgHighPrice": 111,
                "lowPriceVolume": 100,
                "highPriceVolume": 100,
            }
            for i in range(360)
        ]
        entry_bucket = datetime.now(timezone.utc).hour // 6
        for i, row in enumerate(rows[:-2]):
            if datetime.fromtimestamp(row["timestamp"], tz=timezone.utc).hour // 6 == entry_bucket:
                row["avgLowPrice"] = 100
                rows[i + 2]["avgHighPrice"] = 120

        now = int(time.time())
        with (
            patch("merch.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Timed item", "limit": 5_000}}),
            patch("merch.prices.timeseries", return_value=rows),
            patch("merch.prices.latest", return_value={
                "1": {"low": 100, "high": 111, "lowTime": now, "highTime": now}
            }),
            patch("merch.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 10_000, "highPriceVolume": 10_000}
            }),
        ):
            signal = signals.time_of_day_signal(1)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertGreaterEqual(signal["test"]["trades"], signals.TIME_OF_DAY_MIN_TEST_TRADES)
        self.assertGreater(signal["test"]["median_profit_per_unit"], 0)
        self.assertLessEqual(signal["hold_hours"], 24)


def _trend_rows(start_mid: int, end_mid: int, n: int = 120, spread: int = 20) -> list[dict]:
    """Linear price path of n points from start_mid to end_mid, half-spread each side."""
    start = int(time.time()) - n * 21600  # 6h steps
    rows = []
    for i in range(n):
        mid = round(start_mid + (end_mid - start_mid) * i / (n - 1))
        rows.append({
            "timestamp": start + i * 21600,
            "avgHighPrice": mid + spread,
            "avgLowPrice": mid - spread,
            "lowPriceVolume": 50,
            "highPriceVolume": 50,
        })
    return rows


class TrendTests(unittest.TestCase):
    def test_trend_flat_for_stable_prices(self) -> None:
        t = signals._trend(_trend_rows(2000, 2000), "6h")
        self.assertEqual(t["direction"], "flat")

    def test_median_of_halves_pitfall_is_avoided(self) -> None:
        # A steady slide must register; edge medians (not half medians) make this work.
        t = signals._trend(_trend_rows(450_000, 360_000), "6h")
        self.assertEqual(t["direction"], "down")
        self.assertLess(t["pct"], -0.08)

    def test_downtrend_overrides_a_low_regime(self) -> None:
        elevated = signals._with_downtrend_risk(
            {"level": "low", "reason": "stable_recent_distribution"},
            {"direction": "down", "pct": -0.25},
        )
        self.assertEqual(elevated["level"], "high")
        self.assertEqual(elevated["reason"], "sustained_downtrend")

    def test_downtrend_does_not_downgrade_a_higher_regime(self) -> None:
        kept = signals._with_downtrend_risk(
            {"level": "high", "reason": "volume_shock_with_price_drift"},
            {"direction": "down", "pct": -0.10},  # would only be "medium"
        )
        self.assertEqual(kept["level"], "high")
        self.assertEqual(kept["reason"], "volume_shock_with_price_drift")

    def test_downtrend_elevates_regime_and_caps_sell(self) -> None:
        now = int(time.time())
        with (
            patch("merch.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Bleeder", "limit": 100}}),
            patch("merch.prices.timeseries", return_value=_trend_rows(2400, 1800)),
            patch("merch.prices.latest", return_value={"1": {"low": 1780, "high": 1820, "lowTime": now, "highTime": now}}),
            patch("merch.prices.one_hour", return_value={"1": {"lowPriceVolume": 100, "highPriceVolume": 100}}),
        ):
            signal = signals.item_signal(1)

        assert signal is not None
        self.assertEqual(signal["trend"]["direction"], "down")
        self.assertIn(signal["regime"]["level"], {"medium", "high"})
        self.assertLess(signal["sell"], signal["sell_band_full"])

    def test_flat_market_leaves_sell_band_uncapped(self) -> None:
        now = int(time.time())
        with (
            patch("merch.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Stable", "limit": 100}}),
            patch("merch.prices.timeseries", return_value=_trend_rows(2000, 2000, spread=80)),
            patch("merch.prices.latest", return_value={"1": {"low": 1925, "high": 2075, "lowTime": now, "highTime": now}}),
            patch("merch.prices.one_hour", return_value={"1": {"lowPriceVolume": 100, "highPriceVolume": 100}}),
        ):
            signal = signals.item_signal(1)

        assert signal is not None
        self.assertEqual(signal["trend"]["direction"], "flat")
        self.assertEqual(signal["sell"], signal["sell_band_full"])
        self.assertNotEqual(signal["regime"]["reason"], "sustained_downtrend")


def _crash_rows(crash_highs: list[tuple[int | None, int]] | None = None) -> list[dict]:
    """A stable two-sided 6h regime followed by a fast one-sided collapse: lows break down
    while avgHighPrice goes null with zero high-side volume (nobody instant-buying) — the
    Dragon arrow(p+) 11228 crash shape of 2026-06-30→07-02. ``crash_highs`` optionally sets
    (avgHighPrice, highPriceVolume) per crash bucket to model sparse high-side prints."""
    crash_lows = [3250, 3016, 2714, 2200, 1788, 2000, 1788, 2100]
    crash_highs = crash_highs or [(None, 0)] * len(crash_lows)
    step = 21600
    n_stable = 160
    start = int(time.time()) - (n_stable + len(crash_lows)) * step
    rows = [
        {
            "timestamp": start + i * step,
            "avgLowPrice": 3300,
            "avgHighPrice": 3500,
            "lowPriceVolume": 500,
            "highPriceVolume": 500,
        }
        for i in range(n_stable)
    ]
    for j, low in enumerate(crash_lows):
        high, high_vol = crash_highs[j]
        rows.append({
            "timestamp": start + (n_stable + j) * step,
            "avgLowPrice": low,
            "avgHighPrice": high,
            "lowPriceVolume": 800,
            "highPriceVolume": high_vol,
        })
    return rows


class CrashGuardTests(unittest.TestCase):
    """The 11228 incident (2026-06-30→07-02): a 45% two-day one-sided crash slipped past all
    three deterministic guards, and the planner recommended a 3,598 sell from a dead regime."""

    def test_regime_flags_short_window_shock_the_slow_drift_split_misses(self) -> None:
        rows = _crash_rows()
        lows = [r["avgLowPrice"] for r in rows if r.get("avgLowPrice")]
        highs = [r["avgHighPrice"] for r in rows if r.get("avgHighPrice")]
        regime = signals._regime_risk(rows, lows, highs, 1900, timestep="6h")
        # The quarter-split drift stays tiny (the crash is 8 of ~42 "recent" buckets)...
        self.assertGreater(regime["drift_pct"], -0.08)
        # ...but the 2-days-vs-prior-week shock check must trip on its own.
        self.assertEqual(regime["level"], "high")
        self.assertEqual(regime["reason"], "short_window_price_shock")
        self.assertLessEqual(regime["shock_pct"], -0.15)

    def test_regime_shock_stays_quiet_in_a_stable_market(self) -> None:
        rows = _trend_rows(3300, 3300, n=168)
        lows = [r["avgLowPrice"] for r in rows]
        highs = [r["avgHighPrice"] for r in rows]
        regime = signals._regime_risk(rows, lows, highs, 3300, timestep="6h")
        self.assertEqual(regime["level"], "low")
        self.assertAlmostEqual(regime["shock_pct"], 0.0, places=2)

    def test_trend_sees_one_sided_crash_buckets(self) -> None:
        # Crash buckets have null avgHighPrice; a both-sides-only mid series drops exactly
        # those points and reads the collapse as flat. The low-side fallback plus the
        # short-window override must read it as a severe downtrend.
        trend = signals._trend(_crash_rows(), "6h")
        self.assertEqual(trend["direction"], "down")
        self.assertLessEqual(trend["pct"], -0.18)

    def test_capped_sell_refuses_a_target_with_no_fresh_high_side_prints(self) -> None:
        rows = _crash_rows()
        trend = signals._trend(rows, "6h")
        # Stale pre-crash 3,500 highs are still inside the 3-day cap window, but nothing has
        # traded on the high side in ~36h: there is no evidenced exit, so the target is 0.
        self.assertEqual(signals._capped_sell(rows, "6h", 3500, trend), 0)

    def test_capped_sell_caps_to_the_freshest_traded_high(self) -> None:
        highs = [(None, 0)] * 6 + [(2044, 2), (2575, 1992)]
        rows = _crash_rows(crash_highs=highs)
        trend = signals._trend(rows, "6h")
        self.assertEqual(signals._capped_sell(rows, "6h", 3500, trend), 2575)

    def test_item_signal_drops_the_crash_item_without_an_evidenced_exit(self) -> None:
        now = int(time.time())
        with (
            patch("merch.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Crasher", "limit": 11000}}),
            patch("merch.prices.timeseries", return_value=_crash_rows()),
            patch("merch.prices.latest", return_value={
                "1": {"low": 1900, "high": 2000, "lowTime": now, "highTime": now}
            }),
            patch("merch.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 800, "highPriceVolume": 0}
            }),
        ):
            self.assertIsNone(signals.item_signal(1, timestep="6h"))

    def test_item_signal_grades_the_crash_high_risk_with_a_realistic_exit(self) -> None:
        now = int(time.time())
        highs = [(None, 0)] * 6 + [(2044, 2), (2575, 1992)]
        with (
            patch("merch.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Crasher", "limit": 11000}}),
            patch("merch.prices.timeseries", return_value=_crash_rows(crash_highs=highs)),
            patch("merch.prices.latest", return_value={
                "1": {"low": 1900, "high": 2000, "lowTime": now, "highTime": now}
            }),
            patch("merch.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 800, "highPriceVolume": 400}
            }),
        ):
            signal = signals.item_signal(1, timestep="6h")

        assert signal is not None
        self.assertEqual(signal["regime"]["level"], "high")
        self.assertEqual(signal["regime"]["reason"], "short_window_price_shock")
        self.assertEqual(signal["sell"], 2575)
        self.assertLess(signal["sell"], signal["sell_band_full"])


class BacktestTimeStopTests(unittest.TestCase):
    def test_time_stop_books_forced_exits_that_hold_forever_hides(self) -> None:
        # A steady downtrend: a buy near the end never recovers to its sell band.
        rows = _trend_rows(2400, 1600, n=120, spread=60)
        with (
            patch("merch.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Bleeder", "limit": 100}}),
            patch("merch.prices.timeseries", return_value=rows),
        ):
            hold_forever = signals.backtest_signal(1, max_hold_points=None)
            time_stopped = signals.backtest_signal(1, max_hold_points=4)

        # Hold-forever never forces an exit and strands the unrecovered position open.
        self.assertEqual(hold_forever["forced_exits"], 0)
        self.assertTrue(hold_forever["open_position"])
        # The time-stop books that stranding as forced reprice-to-clear exits.
        self.assertGreaterEqual(time_stopped["forced_exits"], 1)
        self.assertGreaterEqual(time_stopped["trades"], hold_forever["trades"])


class TriageOutlierBidTests(unittest.TestCase):
    """A stale sell must not clear against a single anomalous live tick (the antifire
    15000-vs-20086 incident, 2026-06-24)."""

    def _sig(self, current_high: int) -> dict:
        return {
            "name": "Extended super antifire(4)",
            "regime": {"level": "low", "reason": "stable_recent_distribution"},
            "buy": 19000, "buy_band": 20086, "sell": 21391,
            "current_low": 19000, "current_high": current_high,
            "price_fresh": True, "ready_to_buy": False, "ready_to_sell": False,
        }

    def _stale_sell(self) -> dict:
        return {"id": 22209, "side": "sell", "qty": 204, "price": 20006,
                "age_hours": 13.8, "filled_qty": 216, "state": "ACTIVE"}

    def test_outlier_bid_is_dropped(self) -> None:
        self.assertIsNone(plan._sane_bid(self._sig(15000)))   # 25% below the recent low band
        self.assertEqual(plan._sane_bid(self._sig(18500)), 18500)  # plausible dip, trusted

    def test_stale_sell_holds_instead_of_dumping_on_a_bad_tick(self) -> None:
        sig = self._sig(15000)
        res = plan._decide_triage(self._stale_sell(), sig, sig)
        self.assertEqual(res["verdict"], "hold")
        self.assertNotIn("new_price", res)

    def test_stale_sell_still_clears_against_a_real_bid(self) -> None:
        sig = self._sig(18500)
        res = plan._decide_triage(self._stale_sell(), sig, sig)
        self.assertEqual(res["verdict"], "reprice")
        self.assertEqual(res["new_price"], 18500)


class CostBasisTests(unittest.TestCase):
    def test_weights_open_lots_and_ignores_sold_inventory(self) -> None:
        flips = [
            # 100 held @ 200 and 300 held @ 240 -> weighted avg over 400 held = 230
            {"id": 1, "bought": 200, "bought_qty": 100, "sold_qty": 0},
            {"id": 1, "bought": 240, "bought_qty": 400, "sold_qty": 100},
            # fully sold lot contributes nothing
            {"id": 2, "bought": 999, "bought_qty": 50, "sold_qty": 50},
            # a pure sell record (no buy price) is skipped
            {"id": 3, "bought": 0, "bought_qty": 0, "sold_qty": 10},
        ]
        with patch("merch.plan.runelite.read_flips", return_value=flips):
            self.assertEqual(plan._cost_basis(), {1: 230})


class IntentTests(unittest.TestCase):
    def test_intents_from_plan_are_exact_offer_signatures(self) -> None:
        plan_json = {
            "generated_at": "2026-06-27T12:00:00+00:00",
            "buys": [{
                "id": 7,
                "action": "buy",
                "qty": 3,
                "price": 100,
                "strategy": "patient-band",
                "reason": "exact reason",
                "predicted": {"direction": "up", "target": 120},
            }],
            "active_buys": [{
                "id": 8,
                "action": "buy",
                "qty": 1,
                "price": 1_000_000,
                "strategy": "active-margin",
                "reason": "active reason",
            }],
            "offer_triage": [{"id": 9, "verdict": "cancel"}],
        }

        rows = intents.intents_from_plan(plan_json)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["itemId"], 7)
        self.assertEqual(rows[0]["side"], "buy")
        self.assertEqual(rows[0]["qty"], 3)
        self.assertEqual(rows[0]["price"], 100)
        self.assertEqual(rows[0]["strategy"], "patient-band")
        self.assertEqual(rows[0]["note"], "exact reason")
        self.assertNotIn("status", rows[0])

    def test_write_intents_targets_plugin_queue(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = intents.write_intents(
                [{"intentId": "i", "itemId": 7, "side": "buy", "qty": 1, "price": 100}],
                rsn="Evidence",
                runelite_home=Path(d),
            )

            self.assertEqual(path, Path(d) / "flipping" / "merch-intents" / "Evidence.jsonl")
            self.assertEqual(json.loads(path.read_text()), {
                "intentId": "i",
                "itemId": 7,
                "side": "buy",
                "qty": 1,
                "price": 100,
            })


class FlipParsingTests(unittest.TestCase):
    """Flipping Utilities stores every offer (both sides) in h.sO; the side lives on each
    offer's b/st, not the list it came from. Regression guard for buys being miscounted as
    sells (bought=0), which silently breaks fill reconciliation."""

    def _read(self, tmp_path, records: list[dict]) -> list[dict]:
        flip_dir = tmp_path / "flipping"
        flip_dir.mkdir()
        (flip_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
            "trades": records,
            "lastOffers": {},
        }))
        with (
            patch.object(runelite, "INCOMING", tmp_path),
            patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}),
        ):
            return runelite.read_flips()

    def test_single_profile_export_can_supply_rsn(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            flip_dir = tmp / "flipping"
            flip_dir.mkdir()
            (flip_dir / "DetectedName.json").write_text(json.dumps({
                "trades": [{
                    "id": 5974,
                    "name": "Coconut",
                    "h": {"sO": [{
                        "b": True,
                        "st": "BOUGHT",
                        "id": 5974,
                        "p": 1734,
                        "cQIT": 6000,
                        "t": 1781769616000,
                    }]},
                }],
                "lastOffers": {},
            }))

            with (
                patch.object(runelite, "CONFIG", {**runelite.CONFIG, "rsn": ""}),
                patch.object(runelite, "INCOMING", tmp),
            ):
                self.assertEqual(runelite.profile_rsn(), "DetectedName")
                self.assertEqual(runelite.read_flips()[0]["name"], "Coconut")

    def test_buy_offer_in_sO_is_counted_as_a_buy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            flips = self._read(Path(d), [{
                "id": 5974, "name": "Coconut",
                "h": {"sO": [{"b": True, "st": "BOUGHT", "id": 5974, "p": 1734, "cQIT": 6000, "t": 1781769616000}]},
            }])
        self.assertEqual(len(flips), 1)
        self.assertEqual(flips[0]["bought"], 1734)
        self.assertEqual(flips[0]["sold"], 0)
        self.assertEqual(flips[0]["qty"], 6000)

    def test_sell_offer_in_sO_is_counted_as_a_sell(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            flips = self._read(Path(d), [{
                "id": 561, "name": "Nature rune",
                "h": {"sO": [{"b": False, "st": "SOLD", "id": 561, "p": 200, "cQIT": 1000, "t": 1781769616000}]},
            }])
        self.assertEqual(flips[0]["bought"], 0)
        self.assertEqual(flips[0]["sold"], 200)

    def test_round_trip_in_one_record_reports_both_sides_and_profit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            flips = self._read(Path(d), [{
                "id": 11212, "name": "Dragon arrow",
                "h": {"sO": [
                    {"b": True, "st": "BOUGHT", "p": 2850, "cQIT": 100, "t": 1},
                    {"b": False, "st": "SOLD", "p": 3400, "cQIT": 100, "t": 2},
                ]},
            }])
        self.assertEqual(flips[0]["bought"], 2850)
        self.assertEqual(flips[0]["sold"], 3400)
        self.assertEqual(flips[0]["net_sold"], 3332)
        self.assertEqual(flips[0]["profit"], (3332 - 2850) * 100)

    def test_multiple_buy_cycles_split_into_lots(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            flips = self._read(Path(d), [{
                "id": 12695, "name": "Super combat potion(4)",
                "h": {"sO": [
                    {"uuid": "old-buy", "b": True, "st": "BOUGHT", "p": 12795, "cQIT": 270, "t": 1},
                    {"uuid": "old-sell", "b": False, "st": "SOLD", "p": 12805, "cQIT": 270, "t": 2},
                    {"uuid": "new-buy", "b": True, "st": "BOUGHT", "p": 12499, "cQIT": 2000, "t": 3},
                    {"uuid": "new-sell", "b": False, "st": "SOLD", "p": 12804, "cQIT": 2000, "t": 4},
                ]},
            }])

        self.assertEqual(len(flips), 2)
        self.assertEqual(flips[0]["bought_qty"], 270)
        self.assertEqual(flips[0]["profit"], -66420)
        self.assertEqual(flips[1]["bought"], 12499)
        self.assertEqual(flips[1]["sold_qty"], 2000)
        self.assertEqual(flips[1]["profit"], 98000)

    def test_current_ge_slots_parse_non_empty_open_offers(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            (slot_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                "rsn": _TEST_RSN,
                "exportedAt": "2026-06-23T16:30:00Z",
                "slots": [
                    {
                        "slot": 0,
                        "state": "ACTIVE",
                        "side": "sell",
                        "itemId": 32032,
                        "offerQty": 261,
                        "filledQty": 0,
                        "offerPrice": 41324,
                        "filledPrice": 0,
                        "offerCreationTime": "2026-06-23T10:00:00Z",
                        "ageSeconds": 23400,
                        "beforeLogin": False,
                        "merchIntentId": "intent-1",
                        "merchStrategy": "active-margin",
                        "merchNote": "test note",
                        "merchHardExitAt": "2026-06-23T18:00:00Z",
                    },
                    {
                        "slot": 1,
                        "state": "EMPTY",
                        "side": None,
                        "itemId": None,
                        "offerQty": None,
                        "filledQty": None,
                        "offerPrice": None,
                        "filledPrice": None,
                        "offerCreationTime": None,
                        "ageSeconds": None,
                        "beforeLogin": False,
                    },
                    {
                        "slot": 2,
                        "state": "FILLED",
                        "side": "buy",
                        "itemId": 26219,
                        "offerQty": 1,
                        "filledQty": 1,
                        "offerPrice": 20050010,
                        "filledPrice": 20050010,
                        # Distinct from slot 0's placement time: identical
                        # cross-slot times are the batch-restamp signature and
                        # would be distrusted.
                        "offerCreationTime": "2026-06-23T09:00:00Z",
                        "ageSeconds": 27000,
                        "beforeLogin": False,
                    },
                ],
            }))

            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", tmp / "offer_ages.json"),
                patch.object(runelite, "_FILL_ANCHOR_PATH", tmp / "offer_fills.json"),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                offers = runelite.read_open_offers()

        self.assertEqual(offers, [
            {
                "slot": 0,
                "id": 32032,
                "side": "sell",
                "qty": 261,
                "filled_qty": 0,
                "price": 41324,
                "intent_id": "intent-1",
                "strategy": "active-margin",
                "note": "test note",
                "hard_exit_at": "2026-06-23T18:00:00Z",
                "age_hours": 6.5,
                "last_fill_at": None,
                "last_fill_age_hours": None,
                "state": "ACTIVE",
            },
            {
                "slot": 2,
                "id": 26219,
                "side": "buy",
                "qty": 1,
                "filled_qty": 1,
                "price": 20050010,
                "intent_id": None,
                "strategy": None,
                "note": None,
                "hard_exit_at": None,
                "age_hours": 7.5,
                "last_fill_at": None,
                "last_fill_age_hours": None,
                "state": "FILLED",
            },
        ])

    def test_current_ge_slots_derives_age_when_seconds_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            (slot_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                "rsn": _TEST_RSN,
                "exportedAt": "2026-06-23T16:30:00Z",
                "slots": [{
                    "slot": 4,
                    "state": "ACTIVE",
                    "side": "buy",
                    "itemId": 11090,
                    "offerQty": 4000,
                    "filledQty": 0,
                    "offerPrice": 1377,
                    "filledPrice": 0,
                    "offerCreationTime": "2026-06-23T14:00:00Z",
                    "ageSeconds": None,
                    "beforeLogin": False,
                }],
            }))

            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", tmp / "offer_ages.json"),
                patch.object(runelite, "_FILL_ANCHOR_PATH", tmp / "offer_fills.json"),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                offers = runelite.read_open_offers()

        self.assertEqual(offers[0]["age_hours"], 2.5)
        self.assertEqual(offers[0]["price"], 1377)
        self.assertEqual(offers[0]["state"], "ACTIVE")
        self.assertIsNone(offers[0]["hard_exit_at"])

    def test_current_ge_slots_include_time_since_last_fill(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            flip_dir = tmp / "flipping"
            flip_dir.mkdir()
            (slot_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                "rsn": _TEST_RSN,
                "exportedAt": "2026-06-23T16:30:00Z",
                "slots": [{
                    "slot": 2,
                    "state": "ACTIVE",
                    "side": "buy",
                    "itemId": 21902,
                    "offerQty": 25,
                    "filledQty": 4,
                    "offerPrice": 656421,
                    "offerCreationTime": "2026-06-23T14:00:00Z",
                    "ageSeconds": 9000,
                }],
            }))
            # last_fill_at comes from fill-anchored trade history, not the live snapshot's
            # rewritten `t`. The bogus live `t` (=export time) must be ignored.
            history_ms = int(_epoch("2026-06-23T16:00:00Z") * 1000)
            export_ms = int(_epoch("2026-06-23T16:30:00Z") * 1000)
            (flip_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                "trades": [{
                    "id": 21902,
                    "name": "item",
                    "h": {"sO": [{"uuid": "fill-offer", "st": "BUYING", "cQIT": 4,
                                   "p": 656421, "t": history_ms}]},
                }],
                "lastOffers": {
                    "2": {
                        "id": 21902,
                        "st": "BUYING",
                        "cQIT": 4,
                        "uuid": "fill-offer",
                        "t": export_ms,
                    },
                },
            }))

            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", tmp / "offer_ages.json"),
                patch.object(runelite, "_FILL_ANCHOR_PATH", tmp / "offer_fills.json"),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                offers = runelite.read_open_offers()

        self.assertEqual(offers[0]["last_fill_at"], "2026-06-23T16:00:00+00:00")
        self.assertEqual(offers[0]["last_fill_age_hours"], 0.5)

    def test_open_offer_partial_fill_dated_by_qty_growth_anchor(self) -> None:
        # A partial fill on a still-open offer is invisible to trade history; the qty-growth
        # anchor must date it once filled_qty grows between two exports.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            flip_dir = tmp / "flipping"
            flip_dir.mkdir()
            age_path = tmp / "offer_ages.json"
            anchor_path = tmp / "offer_fills.json"

            def write_export(exported_at: str, filled: int) -> None:
                (slot_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                    "rsn": _TEST_RSN,
                    "exportedAt": exported_at,
                    "slots": [{
                        "slot": 0,
                        "state": "ACTIVE",
                        "side": "sell",
                        "itemId": 30816,
                        "offerQty": 39,
                        "filledQty": filled,
                        "offerPrice": 49164,
                        "offerCreationTime": "2026-06-23T10:00:00Z",
                        "ageSeconds": 23400,
                    }],
                }))
                (flip_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                    "trades": [],  # no terminal history for this still-open offer
                    "lastOffers": {"0": {"id": 30816, "st": "SELLING", "cQIT": filled,
                                          "uuid": "abc", "t": 0}},
                }))

            # First sight at 16:00 with a partial: we can't know when it happened -> unknown.
            write_export("2026-06-23T16:00:00Z", 2)
            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", age_path),
                patch.object(runelite, "_FILL_ANCHOR_PATH", anchor_path),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                first = runelite.read_open_offers()
            self.assertIsNone(first[0]["last_fill_age_hours"])

            # Next export at 16:30 shows growth -> anchor stamps the fill at export time.
            write_export("2026-06-23T16:30:00Z", 5)
            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", age_path),
                patch.object(runelite, "_FILL_ANCHOR_PATH", anchor_path),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                grew = runelite.read_open_offers()
            self.assertEqual(grew[0]["last_fill_at"], "2026-06-23T16:30:00+00:00")
            self.assertEqual(grew[0]["last_fill_age_hours"], 0.0)

    def test_open_offer_last_fill_ignores_history_for_different_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            flip_dir = tmp / "flipping"
            flip_dir.mkdir()
            rsn = _TEST_RSN
            (slot_dir / f"{rsn}.json").write_text(json.dumps({
                "rsn": rsn,
                "exportedAt": "2026-06-23T16:30:00Z",
                "slots": [{
                    "slot": 0,
                    "state": "ACTIVE",
                    "side": "sell",
                    "itemId": 30816,
                    "offerQty": 39,
                    "filledQty": 5,
                    "offerPrice": 49164,
                    "offerCreationTime": "2026-06-23T10:00:00Z",
                    "ageSeconds": 23400,
                }],
            }))
            other_fill_ms = int(_epoch("2026-06-23T16:25:00Z") * 1000)
            (flip_dir / f"{rsn}.json").write_text(json.dumps({
                "trades": [{
                    "id": 30816,
                    "name": "item",
                    "h": {"sO": [{"uuid": "old-offer", "st": "SOLD", "cQIT": 5,
                                   "p": 49164, "t": other_fill_ms}]},
                }],
                "lastOffers": {"0": {"id": 30816, "st": "SELLING", "cQIT": 5,
                                      "uuid": "current-offer", "t": 0}},
            }))

            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", tmp / "offer_ages.json"),
                patch.object(runelite, "_FILL_ANCHOR_PATH", tmp / "offer_fills.json"),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                offers = runelite.read_open_offers()

        self.assertIsNone(offers[0]["last_fill_at"])
        self.assertIsNone(offers[0]["last_fill_age_hours"])

    def test_current_ge_slots_reject_stale_export(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            (slot_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                "exportedAt": "2020-01-01T00:00:00Z",
                "slots": [],
            }))

            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}),
            ):
                with self.assertRaisesRegex(RuntimeError, "GE slot export is stale"):
                    runelite.read_open_offers()

    def test_runelite_cli_reports_stale_export_without_traceback(self) -> None:
        err = io.StringIO()
        with (
            patch.object(runelite, "read_open_offers",
                         side_effect=RuntimeError("current GE slot export is stale")),
            redirect_stderr(err),
        ):
            code = runelite._main(["offers"])

        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "error: current GE slot export is stale\n")


class OpenOfferContractTests(unittest.TestCase):
    """Open offers must come from the enriched current-slot export."""

    def test_plan_rejects_open_offer_without_limit_price(self) -> None:
        # Patch every data source: this must fail on the offer contract, not on
        # whatever FU exports / price cache happen to exist on this machine.
        with (
            patch("merch.plan.signals.item_signal", return_value=None),
            patch("merch.plan.signals.live_quote", return_value=None),
            patch("merch.plan._cost_basis", return_value={}),
            patch("merch.plan._personal_execution_stats", return_value={}),
        ):
            with self.assertRaisesRegex(ValueError, "no limit price"):
                plan.plan(cash=1_000_000, offers=[{
                    "slot": 0,
                    "id": 13237,
                    "side": "buy",
                    "qty": 1,
                    "filled_qty": 0,
                    "price": 0,
                    "age_hours": 0.1,
                    "state": "ACTIVE",
                }])

    def test_plan_cli_reports_stale_export_without_traceback(self) -> None:
        err = io.StringIO()
        with (
            patch("merch.runelite.read_open_offers",
                  side_effect=RuntimeError("current GE slot export is stale")),
            redirect_stderr(err),
        ):
            code = plan._main(["--cash", "1000000"])

        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "error: current GE slot export is stale\n")


class OfferAgeAnchorTests(unittest.TestCase):
    """Age must survive a relog: the plugin forgets placement time, but the
    uuid-keyed anchor reconstructs it."""

    EXPORTED = "2026-06-25T12:00:00Z"

    def _write_export(self, tmp: Path, *, unknown_time: bool) -> None:
        slot_dir = tmp / "ge-slots"
        slot_dir.mkdir()
        flip_dir = tmp / "flipping"
        flip_dir.mkdir()
        rsn = _TEST_RSN
        # Plugin has lost the placement time — both fields null, like a relog.
        (slot_dir / f"{rsn}.json").write_text(json.dumps({
            "rsn": rsn,
            "exportedAt": self.EXPORTED,
            "slots": [{
                "slot": 0, "state": "ACTIVE", "side": "sell", "itemId": 565,
                "offerQty": 100, "filledQty": 0, "offerPrice": 5000,
                "filledPrice": 0, "offerCreationTime": None,
                "ageSeconds": None, "beforeLogin": False,
            }],
        }))
        (flip_dir / f"{rsn}.json").write_text(json.dumps({
            "trades": [],
            "lastOffers": {"0": {"id": 565, "st": "SELLING", "cQIT": 0,
                                 "t": int(_epoch(self.EXPORTED) * 1000),
                                 "uuid": "U-RELOG"}},
            "slotTimers": [{"slotIndex": 0,
                            "offerOccurredAtUnknownTime": unknown_time}],
        }))

    def _run(self, tmp: Path):
        with (
            patch.object(runelite, "INCOMING", tmp),
            patch.object(runelite, "_OFFER_AGE_PATH", tmp / "offer_ages.json"),
            patch.object(runelite, "_FILL_ANCHOR_PATH", tmp / "offer_fills.json"),
            patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999_999),
        ):
            return runelite.read_open_offers()

    def test_persisted_anchor_survives_null_plugin_age(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_export(tmp, unknown_time=True)
            placed = int((_epoch(self.EXPORTED) - 24 * 3600) * 1000)
            (tmp / "offer_ages.json").write_text(json.dumps({
                "U-RELOG": {"anchor_ms": placed, "first_seen_ms": placed,
                            "item_id": 565, "side": "sell", "price": 5000,
                            "source": "anchor"},
            }))
            offers = self._run(tmp)
        # Plugin said "no age"; the anchor recovers the real ~24h.
        self.assertEqual(offers[0]["age_hours"], 24.0)

    def test_first_observation_is_used_when_no_plugin_age_or_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_export(tmp, unknown_time=True)
            offers = self._run(tmp)
            persisted = json.loads((tmp / "offer_ages.json").read_text())
        self.assertEqual(offers[0]["age_hours"], 0.0)
        self.assertEqual(persisted["U-RELOG"]["anchor_ms"],
                         int(_epoch(self.EXPORTED) * 1000))

    def test_first_observation_anchors_then_grows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_export(tmp, unknown_time=True)
            first = self._run(tmp)
            self.assertEqual(first[0]["age_hours"], 0.0)  # no evidence yet
            # A later snapshot of the same uuid must show real elapsed time.
            later = "2026-06-25T15:00:00Z"
            rsn = _TEST_RSN
            export = json.loads((tmp / "ge-slots" / f"{rsn}.json").read_text())
            export["exportedAt"] = later
            (tmp / "ge-slots" / f"{rsn}.json").write_text(json.dumps(export))
            second = self._run(tmp)
        self.assertEqual(second[0]["age_hours"], 3.0)

    def test_batch_restamped_times_are_distrusted(self) -> None:
        """FU restamps every open offer with the same placement time at a
        re-observation event while still claiming the times are known. Shared
        stamps must be discarded; a genuinely distinct one must survive."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rsn = _TEST_RSN
            (tmp / "ge-slots").mkdir()
            (tmp / "flipping").mkdir()
            stamp = int((_epoch(self.EXPORTED) - 10 * 3600) * 1000)
            genuine = int((_epoch(self.EXPORTED) - 2 * 3600) * 1000)

            def _slot(i: int, item: int) -> dict:
                return {"slot": i, "state": "ACTIVE", "side": "sell",
                        "itemId": item, "offerQty": 1, "filledQty": 0,
                        "offerPrice": 100, "filledPrice": 0,
                        "offerCreationTime": None, "ageSeconds": None,
                        "beforeLogin": False}

            (tmp / "ge-slots" / f"{rsn}.json").write_text(json.dumps({
                "rsn": rsn, "exportedAt": self.EXPORTED,
                "slots": [_slot(0, 565), _slot(1, 566), _slot(2, 567)],
            }))
            (tmp / "flipping" / f"{rsn}.json").write_text(json.dumps({
                "trades": [],
                "lastOffers": {
                    "0": {"id": 565, "st": "SELLING", "cQIT": 0, "uuid": "U-A"},
                    "1": {"id": 566, "st": "SELLING", "cQIT": 0, "uuid": "U-B"},
                    "2": {"id": 567, "st": "SELLING", "cQIT": 0, "uuid": "U-C"},
                },
                "slotTimers": [
                    {"slotIndex": 0, "offerOccurredAtUnknownTime": False,
                     "tradeStartTime": stamp},
                    {"slotIndex": 1, "offerOccurredAtUnknownTime": False,
                     "tradeStartTime": stamp + 2},
                    {"slotIndex": 2, "offerOccurredAtUnknownTime": False,
                     "tradeStartTime": genuine},
                ],
            }))
            offers = self._run(tmp)
        by_slot = {o["slot"]: o for o in offers}
        # Restamped pair falls back to first observation; the genuine time survives.
        self.assertEqual(by_slot[0]["age_hours"], 0.0)
        self.assertEqual(by_slot[1]["age_hours"], 0.0)
        self.assertEqual(by_slot[2]["age_hours"], 2.0)

    def test_trusted_plugin_time_used_when_not_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_export(tmp, unknown_time=False)
            rsn = _TEST_RSN
            flip = json.loads((tmp / "flipping" / f"{rsn}.json").read_text())
            flip["slotTimers"][0]["tradeStartTime"] = int(
                (_epoch(self.EXPORTED) - 5 * 3600) * 1000)
            (tmp / "flipping" / f"{rsn}.json").write_text(json.dumps(flip))
            offers = self._run(tmp)
        self.assertEqual(offers[0]["age_hours"], 5.0)


class UnknownAgeTriageTests(unittest.TestCase):
    """When age cannot be anchored at all, triage must fail stale, not hold."""

    def _offer(self, side: str) -> dict:
        return {"id": 565, "side": side, "qty": 100, "filled_qty": 0,
                "price": 5000, "age_hours": None, "last_fill_age_hours": None,
                "state": "ACTIVE"}

    def test_unknown_age_sell_clears_instead_of_holding(self) -> None:
        quote = {"name": "Fire rune", "current_high": 4800}
        res = plan._decide_triage(self._offer("sell"), None, quote)
        self.assertEqual(res["verdict"], "reprice")
        self.assertEqual(res["new_price"], 4800)
        self.assertIn("age unknown", res["note"])

    def test_unknown_age_high_value_buy_not_held_as_fresh(self) -> None:
        # Unattributed high-value buy with unknown age must not get the "fresh,
        # preserve through the 30m window" hold; it falls through to band logic.
        offer = {"id": 565, "side": "buy", "qty": 1, "price": 2_000_000,
                 "filled_qty": 0, "age_hours": None, "last_fill_age_hours": None,
                 "state": "ACTIVE"}
        res = plan._decide_triage(offer, None, {"current_high": 1_900_000})
        # no-signal buy -> free the slot, rather than a phantom-fresh hold
        self.assertEqual(res["verdict"], "cancel")
        self.assertNotIn("fresh", res["note"])


class ResearchTests(unittest.TestCase):
    _NEWS_RSS = (
        '<rss version="2.0"><channel><title>OSRS</title>'
        "<item><title>Frost Dragons &amp; More</title>"
        "<pubDate>Wed, 17 Jun 2026 00:00:00 GMT</pubDate>"
        "<link>https://x/news</link>"
        "<description>&lt;p&gt;New Slayer unlocks&lt;/p&gt;</description></item>"
        "</channel></rss>"
    )

    def test_news_parses_rss_and_strips_html(self) -> None:
        with patch.object(research, "_fetch", return_value=self._NEWS_RSS):
            out = research.news()
        self.assertTrue(out["ok"])
        self.assertEqual(out["items"][0]["title"], "Frost Dragons & More")
        self.assertEqual(out["items"][0]["summary"], "New Slayer unlocks")

    def test_failed_fetch_surfaces_citable_error(self) -> None:
        err = urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)
        with patch.object(research, "_fetch", side_effect=err):
            out = research.news()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "HTTP 403")

    _REDDIT_RSS = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><title>Frost dragon drops are nuts</title>"
        '<link href="https://www.reddit.com/r/2007scape/comments/1"/>'
        "<published>2026-06-18T00:00:00+00:00</published></entry>"
        "</feed>"
    )

    def test_reddit_reads_rss_feed(self) -> None:
        with (patch.dict(research.RESEARCH, {"subreddit": "2007scape"}),
              patch.object(research, "_fetch", return_value=self._REDDIT_RSS)):
            out = research.reddit(limit=5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["items"][0]["title"], "Frost dragon drops are nuts")

    def test_reddit_uses_plain_rss_path_not_hot(self) -> None:
        seen = {}
        def fake_fetch(url, ua=research.UA):
            seen["url"], seen["ua"] = url, ua
            return self._REDDIT_RSS
        with (patch.dict(research.RESEARCH, {"subreddit": "2007scape"}),
              patch.object(research, "_fetch", side_effect=fake_fetch)):
            research.reddit(limit=3)
        self.assertTrue(seen["url"].endswith("/r/2007scape/.rss"))  # not /hot.rss
        self.assertIn("Mozilla", seen["ua"])  # browser UA, not the wiki UA

    def test_reddit_merges_and_tags_multiple_subreddits(self) -> None:
        with (
            patch.dict(research.RESEARCH, {"subreddit": ["2007scape", "OSRS"]}),
            patch.object(research, "REDDIT_LIMIT", 5),
            patch.object(research, "_reddit_via_rss",
                         side_effect=lambda sub, limit: [{"title": f"{sub} post", "url": "u", "published": "p"}]),
            patch.object(research.time, "sleep") as sleeper,
        ):
            out = research.reddit()
        self.assertTrue(out["ok"])
        self.assertEqual(out["subreddits"], ["2007scape", "OSRS"])
        self.assertEqual([(i["subreddit"], i["title"]) for i in out["items"]],
                         [("2007scape", "2007scape post"), ("OSRS", "OSRS post")])
        sleeper.assert_called_once()  # one polite delay between the two fetches


def _sig(iid, score, *, regime="low", buy=100, sell=200, fillable=50, ge_limit=1000):
    return {"id": iid, "name": f"item{iid}", "buy": buy, "sell": sell,
            "buy_band": buy,
            "regime": {"level": regime, "reason": "x"}, "fillable_qty": fillable,
            "fill_window_hours": 4.0, "ge_limit": ge_limit, "score": score,
            "current_low": buy, "current_high": sell, "price_fresh": True,
            "ready_to_buy": True, "patient_probe_ready": False,
            "distance_to_buy_pct": 0.0}


def _active_scan():
    return {"candidates": [{
        "id": 7, "name": "Test gear", "buy": 150_000, "sell": 165_000,
        "net_margin": 11_700, "roi_pct": 7.8, "max_qty": 8,
        "fillable_qty": 8, "expected_value_per_unit": 10_000,
        "high_age_minutes": 1.0, "low_age_minutes": 2.0,
        "high_vol_1h": 5, "low_vol_1h": 5, "short_drift_pct": 0.0,
    }], "rejected": []}


def _bt(ok=True):
    return {
        "total_profit_per_unit": 500,
        "avg_profit_per_unit": 100,
        "trades": 5,
        "median_hold_points": 4,
        "median_hold_hours": 4,
    } if ok else None


class PlanTests(unittest.TestCase):
    def _plan(self, scan_sigs, *, active=None, time_scan=None, bt=lambda i: _bt(True), item=lambda i: None,
              quote=lambda i: None, cost_map=None, strategies=None, personal=None, **kw):
        with (
            patch("merch.plan.signals.scan", return_value=scan_sigs),
            patch("merch.plan.signals.active_margin_scan",
                  return_value=active or {"candidates": [], "rejected": []}),
            patch("merch.plan.signals.time_of_day_scan",
                  return_value=time_scan or {"candidates": [], "rejected": []}),
            patch("merch.plan.signals.backtest_signal", side_effect=lambda iid, **k: bt(iid)),
            patch("merch.plan.signals.item_signal", side_effect=lambda iid, **k: item(iid)),
            patch("merch.plan.signals.live_quote", side_effect=lambda iid: quote(iid)),
            patch("merch.plan._cost_basis", return_value=cost_map or {}),
            patch("merch.plan._personal_execution_stats", return_value=personal or {}),
            patch("merch.plan._open_strategy_by_item", return_value=strategies or {}),
        ):
            return plan.plan(cash=1_000_000, **kw)

    def test_time_of_day_lane_uses_available_capital_not_a_percentage_cap(self) -> None:
        time_scan = {"candidates": [{
            "id": 7,
            "name": "Timed item",
            "buy": 100,
            "sell": 120,
            "ge_limit": 5_000,
            "fillable_qty": 5_000,
            "expected_profit_per_unit": 18,
            "score": 7_500,
            "hold_hours": 12,
            "entry_window_utc": "00:00-06:00",
            "exit_window_utc": "12:00-18:00",
            "train": {"trades": 20, "win_rate": 0.7, "median_profit_per_unit": 15},
            "test": {"trades": 10, "win_rate": 0.6, "median_profit_per_unit": 12},
        }], "rejected": []}

        p = self._plan([], time_scan=time_scan)

        self.assertEqual(p["time_buys"][0]["qty"], 5_000)
        self.assertEqual(p["time_buys"][0]["strategy"], "time-of-day")
        self.assertEqual(p["budget_left_gp"], 500_000)

    def test_time_of_day_only_can_use_requested_slots(self) -> None:
        time_scan = {"candidates": [
            {
                "id": i,
                "name": f"Timed item {i}",
                "buy": 100,
                "sell": 120,
                "ge_limit": 5_000,
                "fillable_qty": 1_000,
                "expected_profit_per_unit": 18,
                "score": 7_500 - i,
                "hold_hours": 12,
                "entry_window_utc": "00:00-06:00",
                "exit_window_utc": "12:00-18:00",
                "train": {"trades": 20, "win_rate": 0.7, "median_profit_per_unit": 15},
                "test": {"trades": 10, "win_rate": 0.6, "median_profit_per_unit": 12},
            }
            for i in range(1, 4)
        ], "rejected": []}

        p = self._plan([], time_scan=time_scan, lanes="time", max_new_slots=3)

        self.assertEqual(len(p["time_buys"]), 3)
        self.assertEqual(p["slots"]["time_buys"], 3)

    def test_time_of_day_competes_with_patient_on_gp_per_hour(self) -> None:
        time_scan = {"candidates": [{
            "id": 7,
            "name": "Slower timed item",
            "buy": 100,
            "sell": 120,
            "ge_limit": 5_000,
            "fillable_qty": 5_000,
            "expected_profit_per_unit": 18,
            "score": 100,
            "hold_hours": 12,
            "entry_window_utc": "00:00-06:00",
            "exit_window_utc": "12:00-18:00",
            "train": {"trades": 20, "win_rate": 0.7, "median_profit_per_unit": 15},
            "test": {"trades": 10, "win_rate": 0.6, "median_profit_per_unit": 12},
        }], "rejected": []}

        p = self._plan(
            [_sig(1, 100, buy=100_000, fillable=9, ge_limit=9)],
            time_scan=time_scan,
        )

        self.assertEqual(p["buys"][0]["qty"], 9)
        self.assertEqual(p["time_buys"][0]["qty"], 1_000)
        self.assertEqual(p["budget_left_gp"], 0)

    def test_survival_gate_and_avoid_drop(self) -> None:
        sigs = [_sig(1, 100), _sig(2, 50)]
        # item 2 fails the survival gate; item 1 is research-avoided -> no buys.
        p = self._plan(sigs, bt=lambda i: _bt(i == 1), overlay={"avoid": [{"id": 1}]})
        self.assertEqual(p["buys"], [])
        reasons = {s["id"]: s["reason"] for s in p["skipped"]}
        self.assertEqual(reasons[1], "research avoid")
        self.assertEqual(reasons[2], "fails survival gate")

    def test_boost_reranks_survivors(self) -> None:
        sigs = [_sig(1, 100), _sig(2, 50)]  # 1 outranks 2 by score
        p = self._plan(sigs, overlay={"boost": [{"id": 2}]})
        self.assertEqual([b["id"] for b in p["buys"]], [2, 1])  # boost puts 2 first

    def test_survivors_rank_by_expected_realized_gp_per_hour(self) -> None:
        sigs = [_sig(1, 100, fillable=10), _sig(2, 50, fillable=100)]
        p = self._plan(sigs)
        self.assertEqual([b["id"] for b in p["buys"]], [2, 1])

    def test_personal_best_flip_is_checked_outside_margin_seed_scan(self) -> None:
        personal = {
            99: {
                "name": "Known winner",
                "round_trip": {
                    "staple": True,
                    "profitable_trips": 7,
                    "net_profit": 100_000,
                    "median_hours": 3,
                    "median_gp_per_capital_hour": 20_000,
                },
            }
        }
        p = self._plan([], item=lambda i: _sig(i, 100, fillable=20), personal=personal)

        self.assertEqual(p["buys"][0]["id"], 99)
        self.assertEqual(p["buys"][0]["confidence"], 0.65)
        self.assertIn("personal staple", p["buys"][0]["reason"])

    def test_regime_high_skipped_without_boost(self) -> None:
        p = self._plan([_sig(1, 100, regime="high")])
        self.assertEqual(p["buys"], [])
        self.assertEqual(p["skipped"][0]["reason"], "regime high — needs research thesis")

    def test_candidate_with_unreachable_bid_is_skipped(self) -> None:
        sig = {**_sig(1, 100), "ready_to_buy": False,
               "patient_probe_ready": False, "distance_to_buy_pct": 8.5}
        p = self._plan([sig])
        self.assertEqual(p["buys"], [])
        self.assertIn("outside the 3% probe window", p["skipped"][0]["reason"])

    def test_near_band_candidate_uses_five_percent_probe_cap(self) -> None:
        sig = {
            **_sig(1, 100, buy=100, fillable=1000),
            "ready_to_buy": False,
            "patient_probe_ready": True,
            "distance_to_buy_pct": 2.0,
            "current_low": 102,
        }
        p = self._plan([sig])

        self.assertEqual(p["buys"], [])
        self.assertEqual(len(p["patient_probes"]), 1)
        self.assertEqual(p["patient_probes"][0]["qty"], 500)  # 5% of 1M / 100gp
        self.assertEqual(p["patient_probes"][0]["bucket"], "flip-patient-probe")
        self.assertEqual(p["slots"]["patient_probes"], 1)

    def test_probe_only_can_use_requested_slots_within_total_cap(self) -> None:
        sigs = [
            {
                **_sig(i, 100 - i, buy=100, fillable=250),
                "ready_to_buy": False,
                "patient_probe_ready": True,
                "distance_to_buy_pct": 2.0,
                "current_low": 102,
            }
            for i in range(1, 3)
        ]

        p = self._plan(sigs, lanes="probe", max_new_slots=2)

        self.assertEqual(len(p["patient_probes"]), 2)
        self.assertEqual([b["qty"] for b in p["patient_probes"]], [250, 250])
        self.assertEqual(p["slots"]["patient_probes"], 2)

    def test_sizing_respects_budget_and_limit(self) -> None:
        # buy 100, fillable 50, ge_limit 1000 -> min(50, 1_000_000//100, 1000) = 50
        p = self._plan([_sig(1, 100, fillable=50)])
        self.assertEqual(p["buys"][0]["qty"], 50)

    def test_active_lane_sizes_to_ge_limit_and_available_gp(self) -> None:
        active = _active_scan()
        p = self._plan([], active=active)

        self.assertEqual(p["buys"], [])
        self.assertEqual(len(p["active_buys"]), 1)
        self.assertEqual(p["active_buys"][0]["qty"], 6)
        self.assertEqual(p["active_buys"][0]["bucket"], "flip-active")
        self.assertEqual(p["slots"]["active_buys"], 1)

    def test_lanes_can_select_patient_only(self) -> None:
        active = _active_scan()
        time_scan = {"candidates": [{
            "id": 8, "name": "Timed item", "buy": 100, "sell": 120,
            "ge_limit": 5_000, "fillable_qty": 5_000,
            "expected_profit_per_unit": 18, "score": 7_500, "hold_hours": 12,
            "entry_window_utc": "00:00-06:00", "exit_window_utc": "12:00-18:00",
            "train": {"trades": 20}, "test": {"trades": 10},
        }], "rejected": []}

        p = self._plan(
            [_sig(1, 100, buy=100, fillable=50)],
            active=active,
            time_scan=time_scan,
            lanes="patient",
        )

        self.assertEqual([b["id"] for b in p["buys"]], [1])
        self.assertEqual(p["active_buys"], [])
        self.assertEqual(p["time_buys"], [])
        self.assertEqual(p["inputs"]["lanes"], ["patient"])

    def test_lanes_can_select_active_only(self) -> None:
        active = _active_scan()

        personal = {
            99: {
                "name": "Known patient winner",
                "round_trip": {
                    "staple": True,
                    "profitable_trips": 7,
                    "net_profit": 100_000,
                    "median_hours": 3,
                    "median_gp_per_capital_hour": 20_000,
                },
            }
        }

        p = self._plan(
            [_sig(1, 100, buy=100, fillable=50)],
            active=active,
            personal=personal,
            lanes="active",
        )

        self.assertEqual(p["buys"], [])
        self.assertEqual(len(p["active_buys"]), 1)
        self.assertEqual(p["inputs"]["lanes"], ["active"])

    def test_conservative_lanes_exclude_patient_probes(self) -> None:
        sig = {
            **_sig(1, 100, buy=100, fillable=1000),
            "ready_to_buy": False,
            "patient_probe_ready": True,
            "distance_to_buy_pct": 2.0,
            "current_low": 102,
        }

        p = self._plan([sig], lanes="conservative")

        self.assertEqual(p["patient_probes"], [])
        self.assertEqual(p["inputs"]["lanes"], ["active", "patient", "time"])
        self.assertEqual(p["skipped"][0]["reason"], "patient-probe lane disabled")

    def test_overnight_horizon_excludes_active_lane_before_intents(self) -> None:
        active = _active_scan()
        p = self._plan([], active=active, horizon="overnight")

        self.assertEqual(p["inputs"]["horizon"], "overnight")
        self.assertEqual(p["active_buys"], [])
        self.assertEqual(intents.intents_from_plan(p), [])
        self.assertTrue(any("active lane disabled" in reason
                            for reason in p["active_filter_summary"]))

    def test_away_hours_excludes_active_lane_before_intents(self) -> None:
        active = _active_scan()
        p = self._plan([], active=active, away_hours=3)

        self.assertEqual(p["inputs"]["horizon"], "intraday")
        self.assertEqual(p["inputs"]["away_hours"], 3)
        self.assertEqual(p["active_buys"], [])
        self.assertEqual(intents.intents_from_plan(p), [])
        self.assertTrue(any("active lane disabled" in reason
                            for reason in p["active_filter_summary"]))

    def test_brief_absence_keeps_active_lane(self) -> None:
        active = _active_scan()
        p = self._plan([], active=active, away_hours=0.25)

        self.assertEqual(len(p["active_buys"]), 1)

    def test_long_absence_implies_overnight_horizon(self) -> None:
        p = self._plan([], away_hours=plan.OVERNIGHT_AWAY_HOURS)

        self.assertEqual(p["inputs"]["horizon"], "overnight")

    def test_overnight_horizon_implies_absence(self) -> None:
        p = self._plan([], horizon="overnight")

        self.assertEqual(p["inputs"]["away_hours"], plan.OVERNIGHT_FILL_WINDOW_HOURS)

    def test_overnight_horizon_keeps_active_excluded_despite_short_away_hours(self) -> None:
        p = self._plan([], active=_active_scan(), horizon="overnight", away_hours=0.25)

        self.assertEqual(p["active_buys"], [])
        self.assertEqual(p["inputs"]["away_hours"], plan.OVERNIGHT_FILL_WINDOW_HOURS)

    def test_active_only_while_away_is_a_contradiction(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._plan([], lanes="active", away_hours=3)
        self.assertIn("contradiction", str(ctx.exception))

    def test_overnight_horizon_sizes_patient_scan_to_twelve_hour_window(self) -> None:
        captured = {}

        def scan(**kwargs):
            captured.update(kwargs)
            return [_sig(1, 100, fillable=50)]

        with (
            patch("merch.plan.signals.scan", side_effect=scan),
            patch("merch.plan.signals.active_margin_scan",
                  return_value={"candidates": [], "rejected": []}),
            patch("merch.plan.signals.time_of_day_scan",
                  return_value={"candidates": [], "rejected": []}),
            patch("merch.plan.signals.backtest_signal", return_value=_bt(True)),
            patch("merch.plan.signals.item_signal", return_value=None),
            patch("merch.plan._cost_basis", return_value={}),
            patch("merch.plan._personal_execution_stats", return_value={}) as personal_stats,
            patch("merch.plan._open_strategy_by_item", return_value={}),
        ):
            plan.plan(cash=1_000_000, horizon="overnight")

        self.assertEqual(captured["fill_window_hours"], signals.MAX_HOLD_HOURS)
        personal_stats.assert_called_once_with(signals.MAX_HOLD_HOURS)

    def test_active_buy_cancels_after_thirty_minutes(self) -> None:
        offer = {"id": 7, "side": "buy", "qty": 1, "filled_qty": 0,
                 "price": 100, "age_hours": 0.5, "state": "ACTIVE"}
        row = plan._decide_triage(
            offer, None, {"id": 7, "name": "gear"}, strategy="active-margin"
        )
        self.assertEqual(row["verdict"], "cancel")
        self.assertIn("30m", row["note"])

    def test_unattributed_fresh_high_value_buy_is_not_cancelled_by_patient_band(self) -> None:
        offer = {
            "id": 7,
            "side": "buy",
            "qty": 1,
            "filled_qty": 0,
            "price": 10_000_000,
            "age_hours": 0.1,
            "state": "ACTIVE",
        }
        sig = {
            **_sig(7, 100, buy=9_000_000, sell=9_500_000, fillable=1),
            "ready_to_buy": False,
        }

        row = plan._decide_triage(
            offer,
            sig,
            {"id": 7, "name": "gear"},
            strategy=None,
        )

        self.assertEqual(row["verdict"], "hold")
        self.assertIn("no FU strategy tag", row["note"])

    def test_partially_filled_active_buy_cancels_only_the_remainder(self) -> None:
        offer = {"id": 7, "side": "buy", "qty": 2, "filled_qty": 1,
                 "price": 100, "age_hours": 0.5, "state": "ACTIVE"}
        row = plan._decide_triage(
            offer, None, {"id": 7, "name": "gear"}, strategy="active-margin"
        )
        self.assertEqual(row["verdict"], "cancel")
        self.assertIn("cancel unfilled remainder", row["note"])
        self.assertIn("collect 1 filled", row["note"])

    def test_active_sell_hard_exits_after_ninety_minutes(self) -> None:
        offer = {"id": 7, "side": "sell", "qty": 1, "filled_qty": 0,
                 "price": 200, "age_hours": 1.5, "state": "ACTIVE"}
        quote = {"id": 7, "name": "gear", "current_high": 180}
        row = plan._decide_triage(offer, None, quote, strategy="active-margin")
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)
        self.assertIn("90m", row["note"])

    def test_active_sell_uses_original_call_deadline_not_sell_offer_age(self) -> None:
        offer = {"id": 7, "side": "sell", "qty": 1, "filled_qty": 0,
                 "price": 200, "age_hours": 0.1, "state": "ACTIVE"}
        quote = {"id": 7, "name": "gear", "current_high": 180}
        row = plan._decide_triage(
            offer,
            None,
            quote,
            strategy="active-margin",
            hard_exit_at="2020-01-01T00:00:00+00:00",
        )
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)

    def test_open_strategy_comes_from_fu_slot_tag(self) -> None:
        offer = {
            "slot": 3,
            "id": 7,
            "side": "buy",
            "qty": 10,
            "price": 80,
            "strategy": "patient-band",
            "hard_exit_at": "2026-06-25T22:00:00Z",
        }

        strategies = plan._open_strategy_by_item([offer])

        self.assertEqual(strategies[3]["strategy"], "patient-band")
        self.assertEqual(strategies[3]["hard_exit_at"], "2026-06-25T22:00:00Z")

    def test_time_of_day_buy_cancels_after_its_entry_window(self) -> None:
        offer = {"id": 7, "side": "buy", "qty": 100, "filled_qty": 0,
                 "price": 100, "age_hours": 6, "state": "ACTIVE"}

        row = plan._decide_triage(
            offer, None, {"id": 7, "name": "timed item"}, strategy="time-of-day"
        )

        self.assertEqual(row["verdict"], "cancel")
        self.assertIn("UTC entry window expired", row["note"])

    def test_active_deadline_allows_the_documented_stop_loss(self) -> None:
        offer = {"id": 7, "side": "sell", "qty": 1, "filled_qty": 0,
                 "price": 210, "age_hours": 0.1, "state": "ACTIVE"}
        quote = {"id": 7, "name": "gear", "current_high": 180}
        row = plan._apply_cost_guard(
            plan._decide_triage(
                offer,
                None,
                quote,
                strategy="active-margin",
                hard_exit_at="2020-01-01T00:00:00+00:00",
            ),
            offer,
            quote,
            cost=200,
            strategy="active-margin",
            hard_exit_due=True,
        )
        self.assertEqual(row["verdict"], "reprice")
        self.assertIn("90m active stop-loss", row["note"])

    def test_tiny_flip_is_skipped_below_manual_liquid_profit_floor(self) -> None:
        # liquid 50M -> floor 0.02% = 10,000. A flip realizing avg 100/u * 50 = 5,000 is noise.
        with (
            patch("merch.plan.signals.scan", return_value=[_sig(1, 100, fillable=50)]),
            patch("merch.plan.signals.active_margin_scan",
                  return_value={"candidates": [], "rejected": []}),
            patch("merch.plan.signals.backtest_signal", return_value=_bt(True)),  # avg 100/u realized
            patch("merch.plan.signals.item_signal", return_value=None),
            patch("merch.plan._cost_basis", return_value={}),
            patch("merch.plan._personal_execution_stats", return_value={}),
            patch("merch.plan._open_strategy_by_item", return_value={}),
        ):
            p = plan.plan(cash=50_000_000, time_candidate_limit=0)
        self.assertEqual(p["buys"], [])
        self.assertEqual(p["inputs"]["profit_floor_gp"], 10_000)
        self.assertIn("< floor", p["skipped"][0]["reason"])

    def test_same_flip_clears_floor_at_a_smaller_bankroll(self) -> None:
        # liquid 1M -> floor 200; the 5,000-gp flip is worth a slot.
        p = self._plan([_sig(1, 100, fillable=50)])
        self.assertEqual(p["buys"][0]["qty"], 50)
        self.assertEqual(p["inputs"]["profit_floor_gp"], 200)

    def test_personal_execution_history_can_only_reduce_sizing(self) -> None:
        personal = {
            1: {"buy": {"eligible": True, "orders": 4, "window_fill_factor": 0.4}}
        }
        with (
            patch("merch.plan.signals.scan", return_value=[_sig(1, 100, fillable=50)]),
            patch("merch.plan.signals.active_margin_scan",
                  return_value={"candidates": [], "rejected": []}),
            patch("merch.plan.signals.backtest_signal", return_value=_bt(True)),
            patch("merch.plan.signals.item_signal", return_value=None),
            patch("merch.plan._cost_basis", return_value={}),
            patch("merch.plan._personal_execution_stats", return_value=personal),
            patch("merch.plan._open_strategy_by_item", return_value={}),
        ):
            p = plan.plan(cash=1_000_000, time_candidate_limit=0)

        self.assertEqual(p["buys"][0]["qty"], 20)
        self.assertEqual(p["buys"][0]["execution_stats"]["market_fillable_qty"], 50)
        self.assertIn("personal FU cap 20", p["buys"][0]["reason"])

    def test_offer_triage_reprice_and_cancel(self) -> None:
        item = lambda i: _sig(i, 100, regime=("high" if i == 9 else "low"), buy=100, sell=200)
        offers = [{"id": 1, "side": "buy", "qty": 10, "price": 150},   # far from band 100 -> reprice
                  {"id": 2, "side": "buy", "qty": 10, "price": 100},   # at band -> hold
                  {"id": 9, "side": "buy", "qty": 10, "price": 100}]   # regime high -> cancel
        p = self._plan([], item=item, offers=offers)
        v = {o["id"]: o["verdict"] for o in p["offer_triage"]}
        self.assertEqual(v, {1: "reprice", 2: "hold", 9: "cancel"})
        self.assertEqual(next(o["new_price"] for o in p["offer_triage"] if o["id"] == 1), 100)

    def test_stale_zero_fill_buy_is_cancelled(self) -> None:
        offers = [{"id": 1, "side": "buy", "qty": 10, "filled_qty": 0,
                   "price": 100, "age_hours": 4}]
        row = self._plan([], item=lambda i: _sig(i, 100), offers=offers)["offer_triage"][0]
        self.assertEqual(row["verdict"], "cancel")
        self.assertIn("entry window expired", row["note"])

    def test_cancelled_buy_frees_slot_and_budget_for_new_buy(self) -> None:
        offers = [
            {"id": 99, "side": "buy", "qty": 10, "filled_qty": 0,
             "price": 100, "age_hours": 4},
        ] + [
            {"id": i, "side": "sell", "qty": 1, "filled_qty": 0,
             "price": 200, "age_hours": 1}
            for i in range(100, 107)
        ]

        def item(i):
            if i == 99:
                return _sig(i, 100, buy=100, sell=200)
            return {**_sig(i, 100, buy=100, sell=200), "current_high": 200}

        p = self._plan([_sig(1, 100, buy=100, fillable=20_000, ge_limit=20_000)],
                       item=item, offers=offers)

        self.assertEqual(p["offer_triage"][0]["verdict"], "cancel")
        self.assertEqual(p["projection"]["released_buy_gp"], 1_000)
        self.assertEqual(p["buys"][0]["qty"], 10_010)
        self.assertEqual(p["slots"]["new_buys"], 1)

    def test_partially_filled_patient_buy_emits_sell_fill_instruction(self) -> None:
        sig = {**_sig(1, 100, buy=100, sell=200), "ready_to_buy": False}
        offers = [{"id": 1, "side": "buy", "qty": 10, "filled_qty": 4,
                   "price": 100, "age_hours": 1}]
        p = self._plan([], item=lambda i: sig, offers=offers)

        self.assertEqual(p["offer_triage"][0]["verdict"], "cancel")
        self.assertEqual(p["sell_fills"][0]["qty"], 4)
        self.assertEqual(p["sell_fills"][0]["price"], 200)
        self.assertEqual(p["sell_fills"][0]["action"], "sell")

    def test_patient_probe_is_not_cancelled_for_waiting_below_live_low(self) -> None:
        offers = [{"id": 1, "side": "buy", "qty": 10, "filled_qty": 0,
                   "price": 100, "age_hours": 1}]
        sig = {
            **_sig(1, 100),
            "ready_to_buy": False,
            "patient_probe_ready": True,
            "distance_to_buy_pct": 2.0,
            "current_low": 102,
        }
        row = self._plan(
            [], item=lambda i: sig, offers=offers,
            strategies={1: {"strategy": "patient-probe"}},
        )["offer_triage"][0]
        self.assertEqual(row["verdict"], "hold")
        self.assertIn("never reprice upward", row["note"])

    def test_stale_sell_without_a_band_uses_the_live_bid(self) -> None:
        offers = [{"id": 1, "side": "sell", "qty": 10, "filled_qty": 0,
                   "price": 220, "age_hours": 6}]
        quote = lambda i: {"id": i, "name": "item1", "current_high": 180}
        row = self._plan([], quote=quote, offers=offers)["offer_triage"][0]
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)

    def test_buy_is_never_repriced_upward(self) -> None:
        offers = [{"id": 1, "side": "buy", "qty": 10, "filled_qty": 0,
                   "price": 90, "age_hours": 1}]
        row = self._plan([], item=lambda i: _sig(i, 100), offers=offers)["offer_triage"][0]
        self.assertEqual(row["verdict"], "hold")
        self.assertNotIn("new_price", row)
        self.assertIn("never reprice upward", row["note"])

    def test_filled_and_cancelled_slots_are_collect_actions(self) -> None:
        item = lambda i: _sig(i, 100, buy=100, sell=200)
        offers = [
            {"id": 1, "side": "buy", "qty": 1984, "filled_qty": 1984,
             "price": 8508, "state": "FILLED"},
            {"id": 2, "side": "sell", "qty": 100, "filled_qty": 20,
             "price": 200, "state": "CANCELLED"},
        ]
        p = self._plan([], item=item, offers=offers)

        self.assertEqual([o["verdict"] for o in p["offer_triage"]], ["collect", "collect"])
        self.assertIn("filled but uncollected", p["offer_triage"][0]["note"])
        self.assertIn("cancelled but uncollected", p["offer_triage"][1]["note"])
        self.assertEqual(p["sell_fills"][0]["qty"], 1984)
        self.assertEqual(p["sell_fills"][0]["price"], 200)

    def test_markdown_uses_one_stable_action_table(self) -> None:
        p = self._plan([_sig(1, 100, buy=100, sell=200, fillable=10)])
        md = plan._render_md(p)

        self.assertIn("## Actions", md)
        self.assertIn(
            "| action | item | qty | price | sell target | deadline | reason |",
            md,
        )
        self.assertNotIn("## Buy", md)

    def test_stale_sell_reprices_down_to_market(self) -> None:
        item = lambda i: {**_sig(i, 100, buy=100, sell=250), "current_high": 180}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 220,
                   "age_hours": 6, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers)
        row = p["offer_triage"][0]
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)
        self.assertIn("no fills for 6h", row["note"])

    def test_recent_partial_fill_keeps_sell_open(self) -> None:
        item = lambda i: {**_sig(i, 100, buy=100, sell=250), "current_high": 180}
        offers = [{
            "id": 1,
            "side": "sell",
            "qty": 10,
            "price": 220,
            "age_hours": 8,
            "filled_qty": 5,
            "last_fill_age_hours": 0.25,
        }]
        row = self._plan([], item=item, offers=offers)["offer_triage"][0]

        self.assertEqual(row["verdict"], "hold")

    def test_stale_sell_never_reprices_upward(self) -> None:
        item = lambda i: {**_sig(i, 100, buy=100, sell=250), "current_high": 240}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 220,
                   "age_hours": 18, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers)
        row = p["offer_triage"][0]
        self.assertEqual(row["verdict"], "hold")
        self.assertNotIn("new_price", row)
        self.assertIn("do not raise", row["note"])

    def test_fresh_sell_never_reprices_above_live_bid(self) -> None:
        # Regression: the band-top sell (250) is unreachable when the live bid
        # (current_high) sits below it. A fresh or partially-filled sell must clear
        # at the bid, not chase the band — the keel/vial/antifire failure.
        item = lambda i: {**_sig(i, 100, buy=100, sell=250), "current_high": 180}
        offers = [
            {"id": 1, "side": "sell", "qty": 10, "price": 230,        # above bid -> clear down
             "age_hours": 0.7, "filled_qty": 0},
            {"id": 2, "side": "sell", "qty": 10, "price": 181,        # already at bid -> hold
             "age_hours": 0.7, "filled_qty": 0},
            {"id": 3, "side": "sell", "qty": 10, "price": 230,        # partial fill, still stuck high
             "age_hours": 0.7, "filled_qty": 5},
        ]
        p = self._plan([], item=item, offers=offers)
        rows = {o["id"]: o for o in p["offer_triage"]}
        self.assertEqual(rows[1]["verdict"], "reprice")
        self.assertEqual(rows[1]["new_price"], 180)
        self.assertEqual(rows[2]["verdict"], "hold")
        self.assertEqual(rows[3]["new_price"], 180)
        # No reprice anywhere posts an ask above the live bid.
        for o in p["offer_triage"]:
            if o.get("verdict") == "reprice" and o.get("side") == "sell":
                self.assertLessEqual(o["new_price"], 180)

    def test_enforce_fillable_rejects_ask_above_bid(self) -> None:
        # The invariant is a hard guard: a sell reprice above the live bid is a
        # logic error and must raise, not ship into the GE.
        sig = {"id": 1, "current_high": 180}
        bad = {"id": 1, "side": "sell", "verdict": "reprice", "new_price": 250}
        with self.assertRaises(AssertionError):
            plan._enforce_fillable(bad, sig)
        ok = {"id": 1, "side": "sell", "verdict": "reprice", "new_price": 180}
        self.assertEqual(plan._enforce_fillable(ok, sig), ok)

    def test_below_cost_sell_holds_instead_of_clearing_at_a_loss(self) -> None:
        # Live bid (180) is below our cost (200): clearing down to it books a loss.
        # A fresh thin-item sell must hold, not clear — the keel-parts case.
        item = lambda i: {**_sig(i, 100, buy=100, sell=250),
                          "current_high": 180, "trend": {"direction": "flat"}}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 210,
                   "age_hours": 1, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers, cost_map={1: 200})
        row = p["offer_triage"][0]
        self.assertEqual(row["verdict"], "hold")
        self.assertNotIn("new_price", row)
        self.assertIn("break-even", row["note"])

    def test_below_cost_sell_clears_at_the_hard_12h_stop(self) -> None:
        item = lambda i: {**_sig(i, 100, buy=100, sell=250),
                          "current_high": 180, "trend": {"direction": "flat"}}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 210,
                   "age_hours": 12, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers, cost_map={1: 200})
        row = p["offer_triage"][0]
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)
        self.assertIn("12h stop-loss", row["note"])

    def test_above_cost_sell_still_clears_to_market(self) -> None:
        # Cost (150) below the bid (180): clearing is profitable, so the guard stays out
        # of the way and the clamp-to-bid reprice proceeds.
        item = lambda i: {**_sig(i, 100, buy=100, sell=250),
                          "current_high": 180, "trend": {"direction": "flat"}}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 230,
                   "age_hours": 1, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers, cost_map={1: 150})
        row = p["offer_triage"][0]
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)

    def test_members_slots_allow_eight_offers(self) -> None:
        sigs = [_sig(i, 100 - i, buy=100, fillable=10) for i in range(1, 9)]
        p = self._plan(sigs)
        self.assertEqual(len(p["buys"]), 8)
        self.assertEqual(p["slots"]["max"], 8)

    def test_max_new_slots_caps_new_recommendations(self) -> None:
        sigs = [_sig(i, 100 - i, buy=100, fillable=10) for i in range(1, 9)]
        p = self._plan(sigs, max_new_slots=2)
        self.assertEqual(len(p["buys"]), 2)
        self.assertEqual(p["slots"]["new_slot_cap"], 2)

    def test_active_only_can_use_requested_slots(self) -> None:
        active = {"candidates": [
            {
                "id": i,
                "name": f"gear{i}",
                "buy": 100_000,
                "sell": 110_000,
                "net_margin": 7_800,
                "roi_pct": 7.8,
                "max_qty": 1,
                "fillable_qty": 1,
                "expected_value_per_unit": 5_000,
                "expected_gp_per_hour": 5_000 - i,
                "high_age_minutes": 1.0,
                "low_age_minutes": 2.0,
                "high_vol_1h": 5,
                "low_vol_1h": 5,
                "short_drift_pct": 0.0,
            }
            for i in range(1, 6)
        ], "rejected": []}

        p = self._plan([], active=active, lanes="active", max_new_slots=5)

        self.assertEqual(len(p["active_buys"]), 5)
        self.assertEqual(p["slots"]["active_buys"], 5)

    def test_deployment_uses_full_liquid(self) -> None:
        p = self._plan([
            _sig(1, 100, buy=100, fillable=20_000, ge_limit=20_000),
        ])

        self.assertEqual(p["inputs"]["budget_gp"], 1_000_000)
        self.assertEqual(p["buys"][0]["qty"], 10_000)
        self.assertEqual(p["deployment"]["planned_gp"], 1_000_000)
        self.assertEqual(p["deployment"]["utilization_pct"], 100.0)
        self.assertEqual(p["deployment"]["unspent_gp"], 0)
        self.assertIsNone(p["deployment"]["constraint"])

    def test_deployment_shortfall_reports_constraint_without_weak_trade(self) -> None:
        p = self._plan([_sig(1, 100, buy=100, fillable=50)])

        self.assertEqual(p["deployment"]["planned_gp"], 5_000)
        self.assertGreater(p["deployment"]["unspent_gp"], 0)
        self.assertIsNotNone(p["deployment"]["constraint"])

    def test_cash_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "cash is required"):
            plan.plan(cash=None, time_candidate_limit=0)


if __name__ == "__main__":
    unittest.main()
