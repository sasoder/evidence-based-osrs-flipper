from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flipper import signals

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

    def test_exit_capacity_uses_sell_side_flow_over_the_exit_window(self) -> None:
        volume = {"low": 1000, "high": 10, "total": 1010}

        # sell side (10/h) * 12h exit window * 0.10 participation = 12, GE limit caps at 1000
        self.assertEqual(signals._exit_capacity_qty(1000, volume, 12, 0.10), 12)
        # GE limit binds when the sell side is deep
        self.assertEqual(signals._exit_capacity_qty(5, volume, 12, 0.10), 5)

    def test_scan_zero_seed_limit_scans_all_and_forwards_fill_window(self) -> None:
        with (
            patch("flipper.signals.prices.margins", return_value=[{"id": 1, "score": 1}]) as margins,
            patch("flipper.signals.prices.prefetch_timeseries") as prefetch,
            patch("flipper.signals.item_signal", return_value={"id": 1, "score": 1}) as item_signal,
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
            patch("flipper.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Test item", "limit": 100}}),
            patch("flipper.prices.timeseries", return_value=_rows()),
            patch("flipper.prices.latest", return_value=latest),
            patch("flipper.prices.one_hour", return_value={"1": {"lowPriceVolume": 100, "highPriceVolume": 100}}),
        ):
            signal = signals.item_signal(1)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertFalse(signal["price_fresh"])
        self.assertFalse(signal["ready_to_buy"])

    def test_ready_entry_uses_the_live_low_not_the_historical_band(self) -> None:
        now = int(time.time())
        with (
            patch("flipper.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Test item", "limit": 100}}),
            patch("flipper.prices.timeseries", return_value=_rows()),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 90, "high": 200, "lowTime": now, "highTime": now}
            }),
            patch("flipper.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 100, "highPriceVolume": 100}
            }),
        ):
            signal = signals.item_signal(1)

        assert signal is not None
        self.assertEqual(signal["buy_band"], 100)
        self.assertEqual(signal["entry_price"], 90)
        self.assertTrue(signal["ready_to_buy"])

    def test_near_band_bid_is_probe_only_not_production_ready(self) -> None:
        # The live low (102) sits 2% above the band (100). That is not evidence that the band
        # recently traded, so production stays blocked while the experimental probe strategy may bid.
        now = int(time.time())
        with (
            patch("flipper.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Test item", "limit": 100}}),
            patch("flipper.prices.timeseries", return_value=_rows()),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 102, "high": 200, "lowTime": now, "highTime": now}
            }),
            patch("flipper.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 100, "highPriceVolume": 100}
            }),
        ):
            signal = signals.item_signal(1)

        assert signal is not None
        self.assertEqual(signal["buy_band"], 100)
        self.assertEqual(signal["entry_price"], 100)
        self.assertFalse(signal["ready_to_buy"])
        self.assertTrue(signal["patient_probe_ready"])

    def test_patient_bid_not_ready_when_live_low_far_above_band(self) -> None:
        # Live low 120 is 20% above the band — the bid would never fill in the window. Not ready.
        now = int(time.time())
        with (
            patch("flipper.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Test item", "limit": 100}}),
            patch("flipper.prices.timeseries", return_value=_rows()),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 120, "high": 200, "lowTime": now, "highTime": now}
            }),
            patch("flipper.prices.one_hour", return_value={
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
            patch("flipper.prices.margins", return_value=[{
                "id": 1, "name": "Test gear", "buy": 10_000_000, "sell": 10_300_000,
                "margin": 94_000, "ge_limit": 8, "vol_1h": 10, "potential_1h": 752_000,
            }]),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 10_000_000, "high": 10_300_000,
                      "lowTime": now, "highTime": now}
            }),
            patch("flipper.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 5, "highPriceVolume": 5}
            }),
            patch("flipper.prices.timeseries", return_value=stable),
            patch("flipper.prices.prefetch_timeseries"),
        ):
            result = signals.active_margin_scan()

        self.assertEqual(len(result["candidates"]), 1)
        row = result["candidates"][0]
        self.assertEqual(row["entry_price"], 10_000_001)
        self.assertEqual(row["exit_price"], 10_299_999)
        self.assertEqual(row["current_low"], 10_000_000)
        self.assertEqual(row["current_high"], 10_300_000)
        self.assertEqual(row["max_qty"], 1)
        self.assertEqual(row["fillable_qty"], 1)
        self.assertGreater(row["expected_value_per_unit"], 0)
        self.assertGreater(row["expected_gp_per_hour"], 0)
        self.assertGreaterEqual(row["net_margin"], signals.ACTIVE_MIN_NET_MARGIN)

    def test_active_margin_scan_rejects_stale_quote(self) -> None:
        now = int(time.time())
        with (
            patch("flipper.prices.margins", return_value=[{
                "id": 1, "name": "Test gear", "buy": 10_000_000, "sell": 10_300_000,
                "margin": 94_000, "ge_limit": 8, "vol_1h": 10, "potential_1h": 752_000,
            }]),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 10_000_000, "high": 10_300_000,
                      "lowTime": now - 1260, "highTime": now}
            }),
            patch("flipper.prices.prefetch_timeseries"),
        ):
            result = signals.active_margin_scan()

        self.assertEqual(result["candidates"], [])
        self.assertIn(
            f"older than {signals.ACTIVE_MAX_QUOTE_AGE_MINUTES} minutes",
            result["rejected"][0]["reason"],
        )

    def test_active_margin_scan_requires_ge_limit(self) -> None:
        with (
            patch("flipper.prices.margins", return_value=[{
                "id": 1, "name": "Unknown limit gear", "buy": 10_000_000, "sell": 10_300_000,
                "margin": 94_000, "ge_limit": None, "vol_1h": 10, "potential_1h": 0,
            }]),
            patch("flipper.prices.prefetch_timeseries"),
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
            patch("flipper.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Timed item", "limit": 5_000}}),
            patch("flipper.prices.timeseries", return_value=rows),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 100, "high": 111, "lowTime": now, "highTime": now}
            }),
            patch("flipper.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 10_000, "highPriceVolume": 10_000}
            }),
        ):
            signal = signals.time_of_day_signal(1)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertGreaterEqual(signal["test"]["trades"], signals.TIME_OF_DAY_MIN_TEST_TRADES)
        self.assertGreater(signal["test"]["median_profit_per_unit"], 0)
        self.assertEqual(signal["current_low"], 100)
        self.assertEqual(signal["current_high"], 111)
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
            patch("flipper.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Bleeder", "limit": 100}}),
            patch("flipper.prices.timeseries", return_value=_trend_rows(2400, 1800)),
            patch("flipper.prices.latest", return_value={"1": {"low": 1780, "high": 1820, "lowTime": now, "highTime": now}}),
            patch("flipper.prices.one_hour", return_value={"1": {"lowPriceVolume": 100, "highPriceVolume": 100}}),
        ):
            signal = signals.item_signal(1)

        assert signal is not None
        self.assertEqual(signal["trend"]["direction"], "down")
        self.assertIn(signal["regime"]["level"], {"medium", "high"})
        self.assertLess(signal["exit_price"], signal["sell_band_full"])

    def test_flat_market_leaves_sell_band_uncapped(self) -> None:
        now = int(time.time())
        with (
            patch("flipper.prices.mapping_by_id", return_value={1: {"id": 1, "name": "Stable", "limit": 100}}),
            patch("flipper.prices.timeseries", return_value=_trend_rows(2000, 2000, spread=80)),
            patch("flipper.prices.latest", return_value={"1": {"low": 1925, "high": 2075, "lowTime": now, "highTime": now}}),
            patch("flipper.prices.one_hour", return_value={"1": {"lowPriceVolume": 100, "highPriceVolume": 100}}),
        ):
            signal = signals.item_signal(1)

        assert signal is not None
        self.assertEqual(signal["trend"]["direction"], "flat")
        self.assertEqual(signal["exit_price"], signal["sell_band_full"])
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
            patch("flipper.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Crasher", "limit": 11000}}),
            patch("flipper.prices.timeseries", return_value=_crash_rows()),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 1900, "high": 2000, "lowTime": now, "highTime": now}
            }),
            patch("flipper.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 800, "highPriceVolume": 0}
            }),
        ):
            self.assertIsNone(signals.item_signal(1, timestep="6h"))

    def test_item_signal_grades_the_crash_high_risk_with_a_realistic_exit(self) -> None:
        now = int(time.time())
        highs = [(None, 0)] * 6 + [(2044, 2), (2575, 1992)]
        with (
            patch("flipper.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Crasher", "limit": 11000}}),
            patch("flipper.prices.timeseries", return_value=_crash_rows(crash_highs=highs)),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 1900, "high": 2000, "lowTime": now, "highTime": now}
            }),
            patch("flipper.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 800, "highPriceVolume": 400}
            }),
        ):
            signal = signals.item_signal(1, timestep="6h")

        assert signal is not None
        self.assertEqual(signal["regime"]["level"], "high")
        self.assertEqual(signal["regime"]["reason"], "short_window_price_shock")
        self.assertEqual(signal["exit_price"], 2575)
        self.assertLess(signal["exit_price"], signal["sell_band_full"])



class BacktestTimeStopTests(unittest.TestCase):
    def test_time_stop_books_forced_exits_that_hold_forever_hides(self) -> None:
        # A steady downtrend: a buy near the end never recovers to its sell band.
        rows = _trend_rows(2400, 1600, n=120, spread=60)
        with (
            patch("flipper.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Bleeder", "limit": 100}}),
            patch("flipper.prices.timeseries", return_value=rows),
        ):
            hold_forever = signals.backtest_signal(1, max_hold_points=None)
            time_stopped = signals.backtest_signal(1, max_hold_points=4)

        # Hold-forever never forces an exit and strands the unrecovered position open.
        self.assertEqual(hold_forever["forced_exits"], 0)
        self.assertTrue(hold_forever["open_position"])
        # The time-stop books that stranding as forced reprice-to-clear exits.
        self.assertGreaterEqual(time_stopped["forced_exits"], 1)
        self.assertGreaterEqual(time_stopped["trades"], hold_forever["trades"])


if __name__ == "__main__":
    unittest.main()
