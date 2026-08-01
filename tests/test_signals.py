from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flipper import signals

# One UTC day as four 6h buckets. Bucket 0 is the entry window: it is the only bucket whose
# low is reachable at buy=101, so a block that opens on any other bucket cannot fill — which is
# what makes wrong-phase starts detectable rather than merely different.
_SELLS_OUT = [
    {"avgLowPrice": 100, "avgHighPrice": 105, "lowPriceVolume": 100, "highPriceVolume": 0},
    {"avgLowPrice": 105, "avgHighPrice": 130, "lowPriceVolume": 0, "highPriceVolume": 100},
    {"avgLowPrice": 105, "avgHighPrice": 110, "lowPriceVolume": 0, "highPriceVolume": 0},
    {"avgLowPrice": 105, "avgHighPrice": 110, "lowPriceVolume": 0, "highPriceVolume": 0},
]
_NEVER_SELLS = [
    {"avgLowPrice": 100, "avgHighPrice": 105, "lowPriceVolume": 100, "highPriceVolume": 0},
    *[
        {"avgLowPrice": 90, "avgHighPrice": 95, "lowPriceVolume": 0, "highPriceVolume": 100}
        for _ in range(3)
    ],
]


def _on_grid(rows: list[dict], step_seconds: int) -> list[dict]:
    """Stamp rows onto a gapless timestamp grid, as a complete API response would be."""
    start = int(time.time()) - len(rows) * step_seconds
    return [{**row, "timestamp": start + index * step_seconds}
            for index, row in enumerate(rows)]


def _time_rows(day: list[dict], days: int) -> list[dict]:
    """6h rows on a real UTC grid, bucket 0 of each day aligned to 00:00."""
    start = (int(time.time()) // 86400 - days) * 86400
    return [
        {**dict(day[index % 4]), "timestamp": start + index * 21600}
        for index in range(days * 4)
    ]


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
    def test_item_signal_explains_missing_mapping(self) -> None:
        with patch("flipper.prices.mapping_by_id", return_value={}):
            evaluation = signals.item_signal(1)

        self.assertIsNone(evaluation["signal"])
        self.assertEqual(evaluation["blocked_by"]["code"], "mapping_missing")

    def test_item_evaluation_explains_insufficient_history(self) -> None:
        with (
            patch("flipper.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Thin item", "limit": 100}}),
            patch("flipper.prices.timeseries", return_value=_rows(19)),
        ):
            evaluation = signals.item_signal(1)

        blocked = evaluation["blocked_by"]
        self.assertEqual(blocked["code"], "insufficient_history")
        self.assertEqual(blocked["required_observations"], 20)
        self.assertEqual(blocked["usable_observations"]["execution_lows"], 19)

    def test_item_evaluation_distinguishes_band_margin_rejection(self) -> None:
        now = int(time.time())
        flat = _rows()
        for row in flat:
            row["avgLowPrice"] = 200
            row["avgHighPrice"] = 200
        with (
            patch("flipper.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Flat item", "limit": 100}}),
            patch("flipper.prices.timeseries", return_value=flat),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 200, "high": 200, "lowTime": now, "highTime": now}
            }),
        ):
            evaluation = signals.item_signal(1)

        blocked = evaluation["blocked_by"]
        self.assertEqual(blocked["code"], "band_margin_non_positive")
        self.assertIn("historical band spread", blocked["reason"])
        self.assertGreater(blocked["shortfall_gp_per_unit"], 0)

    def test_item_evaluation_distinguishes_live_executable_margin_rejection(self) -> None:
        now = int(time.time())
        with (
            patch("flipper.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Crossed item", "limit": 100}}),
            patch("flipper.prices.timeseries", return_value=_rows()),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 102, "high": 103, "lowTime": now, "highTime": now}
            }),
            patch("flipper.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 100, "highPriceVolume": 100}
            }),
        ):
            evaluation = signals.item_signal(1)

        blocked = evaluation["blocked_by"]
        self.assertEqual(blocked["code"], "executable_margin_non_positive")
        self.assertIn("live executable spread", blocked["reason"])
        self.assertGreaterEqual(blocked["shortfall_gp_per_unit"], 0)

    def test_fillable_qty_uses_thinner_side_volume(self) -> None:
        volume = {"low": 1000, "high": 10, "total": 1010}

        # thinner side (10/h) * 4h window * 0.10 participation = 4
        self.assertEqual(signals._fillable_qty(1000, volume, signals.FILL_WINDOW_HOURS), 4)

    def test_scan_zero_seed_limit_scans_all_and_forwards_fill_window(self) -> None:
        with (
            patch("flipper.signals.prices.margins", return_value=[{"id": 1, "score": 1}]) as margins,
            patch("flipper.signals.prices.prefetch_timeseries") as prefetch,
            patch("flipper.signals.item_signal", return_value={
                "signal": {"id": 1, "score": 1}, "blocked_by": None,
            }) as item_signal,
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
            signal = signals.item_signal(1)["signal"]

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
            signal = signals.item_signal(1)["signal"]

        assert signal is not None
        self.assertEqual(signal["buy_band"], 100)
        self.assertEqual(signal["entry_price"], 90)
        self.assertTrue(signal["ready_to_buy"])

    def test_patient_order_replay_applies_touch_capacity_and_capital_cost(self) -> None:
        rows = []
        for _ in range(3):
            rows.extend([
                {
                    "avgLowPrice": 100,
                    "avgHighPrice": 105,
                    "lowPriceVolume": 100,
                    "highPriceVolume": 0,
                },
                {
                    "avgLowPrice": 105,
                    "avgHighPrice": 130,
                    "lowPriceVolume": 0,
                    "highPriceVolume": 100,
                },
                *[
                    {
                        "avgLowPrice": 105,
                        "avgHighPrice": 110,
                        "lowPriceVolume": 0,
                        "highPriceVolume": 0,
                    }
                    for _ in range(14)
                ],
            ])
        rows = _on_grid(rows, 3600)

        evidence = signals._patient_order_replay(
            rows,
            buy=101,
            sell=129,
            quantity=10,
            entry_hours=4,
            participation=0.10,
        )
        thin = signals._patient_order_replay(
            rows, buy=101, sell=129, quantity=10, entry_hours=4, participation=0.01,
        )

        self.assertEqual(evidence["blocks"], 3)
        self.assertEqual(evidence["opportunity_episodes"], 3)
        self.assertTrue(evidence["qualifies"])
        # Touch capacity: the same order against the same prints, but able to take only 1% of
        # each bucket instead of 10%, must book strictly less. Without the capacity cap the two
        # runs would be identical.
        self.assertLess(thin["mean_profit_gp"], evidence["mean_profit_gp"])
        # Capital cost: utility is profit minus the reservation charge on posted gp.
        self.assertLess(evidence["mean_utility_gp"], evidence["mean_profit_gp"])

    def test_patient_replay_uses_the_requested_timestep_duration(self) -> None:
        block = [
            {
                "avgLowPrice": 100 if index == 0 else 105,
                "avgHighPrice": 130 if index == 48 else 110,
                "lowPriceVolume": 100 if index == 0 else 0,
                "highPriceVolume": 100 if index == 48 else 0,
            }
            for index in range(192)  # 4h entry + 12h hold at five-minute resolution
        ]

        evidence = signals._patient_order_replay(
            _on_grid(block * 3, 300),
            buy=101,
            sell=129,
            quantity=5,
            entry_hours=4,
            participation=0.10,
            timestep="5m",
        )

        self.assertEqual(evidence["blocks"], 3)
        self.assertEqual(evidence["opportunity_episodes"], 3)
        self.assertTrue(evidence["qualifies"])

    def test_patient_replay_counts_only_blocks_it_actually_entered(self) -> None:
        # A graded block whose entry never touched is still evidence — it just is not an
        # opportunity. Counting it would pad the quorum `qualifies` requires.
        rows = []
        for block in range(3):
            entry_low = 105 if block == 1 else 100  # the middle block never fills
            rows.extend([
                {"avgLowPrice": entry_low, "avgHighPrice": 105,
                 "lowPriceVolume": 100, "highPriceVolume": 0},
                *[
                    {"avgLowPrice": 105, "avgHighPrice": 110,
                     "lowPriceVolume": 0, "highPriceVolume": 0}
                    for _ in range(3)
                ],
                {"avgLowPrice": 105, "avgHighPrice": 130,
                 "lowPriceVolume": 0, "highPriceVolume": 100},
                *[
                    {"avgLowPrice": 105, "avgHighPrice": 110,
                     "lowPriceVolume": 0, "highPriceVolume": 0}
                    for _ in range(11)
                ],
            ])

        evidence = signals._patient_order_replay(
            _on_grid(rows, 3600), buy=101, sell=129, quantity=10,
            entry_hours=4, participation=0.10,
        )

        self.assertEqual(evidence["blocks"], 3)
        self.assertEqual(evidence["opportunity_episodes"], 2)

    def test_patient_replay_skips_a_block_that_spans_more_than_its_row_count(self) -> None:
        # Half of all cached 1h blocks span more wall-clock time than their row count implies.
        # A stretched block charges the wrong capital hours and force-exits at a price from the
        # wrong hour, so it is not evidence about this order.
        rows = []
        for _ in range(3):
            rows.extend([
                {"avgLowPrice": 100, "avgHighPrice": 105,
                 "lowPriceVolume": 100, "highPriceVolume": 0},
                {"avgLowPrice": 105, "avgHighPrice": 130,
                 "lowPriceVolume": 0, "highPriceVolume": 100},
                *[
                    {"avgLowPrice": 105, "avgHighPrice": 110,
                     "lowPriceVolume": 0, "highPriceVolume": 0}
                    for _ in range(14)
                ],
            ])
        rows = _on_grid(rows, 3600)
        for row in rows[20:]:  # a two-hour hole inside the second block
            row["timestamp"] += 7200

        evidence = signals._patient_order_replay(
            rows, buy=101, sell=129, quantity=10, entry_hours=4, participation=0.10,
        )

        self.assertEqual(evidence["blocks"], 2)

    def test_patient_replay_drops_a_block_whose_forced_exit_never_printed(self) -> None:
        # Inventory remains and no low printed anywhere in the hold segment. The old code
        # wrote off the full purchase price (10 * -101), which then sized the position.
        rows = []
        for _ in range(3):
            rows.extend([
                {"avgLowPrice": 100, "avgHighPrice": 105,
                 "lowPriceVolume": 100, "highPriceVolume": 0},
                *[
                    {"avgLowPrice": None, "avgHighPrice": 110,
                     "lowPriceVolume": 0, "highPriceVolume": 0}
                    for _ in range(15)
                ],
            ])

        evidence = signals._patient_order_replay(
            _on_grid(rows, 3600), buy=101, sell=129, quantity=10,
            entry_hours=4, participation=0.10,
        )

        self.assertEqual(evidence["blocks"], 0)
        self.assertEqual(evidence["worst_profit_gp"], 0)
        self.assertFalse(evidence["qualifies"])

    def test_patient_replay_keeps_an_early_sellout_when_the_terminal_low_is_null(self) -> None:
        # The position cleared before the deadline, so a missing terminal print says nothing
        # about it. Discarding the block would delete a successful observation.
        rows = []
        for _ in range(3):
            rows.extend([
                {"avgLowPrice": 100, "avgHighPrice": 105,
                 "lowPriceVolume": 100, "highPriceVolume": 0},
                *[
                    {"avgLowPrice": 105, "avgHighPrice": 110,
                     "lowPriceVolume": 0, "highPriceVolume": 0}
                    for _ in range(3)
                ],
                # First row of the sell segment clears the whole position.
                {"avgLowPrice": 105, "avgHighPrice": 130,
                 "lowPriceVolume": 0, "highPriceVolume": 100},
                *[
                    {"avgLowPrice": None, "avgHighPrice": 110,
                     "lowPriceVolume": 0, "highPriceVolume": 0}
                    for _ in range(11)
                ],
            ])

        evidence = signals._patient_order_replay(
            _on_grid(rows, 3600), buy=101, sell=129, quantity=10,
            entry_hours=4, participation=0.10,
        )

        self.assertEqual(evidence["blocks"], 3)
        self.assertGreater(evidence["worst_profit_gp"], 0)

    def test_probe_posts_and_replays_the_lower_band_order(self) -> None:
        now = int(time.time())
        with (
            patch("flipper.prices.mapping_by_id",
                  return_value={1: {"id": 1, "name": "Test item", "limit": 100}}),
            patch("flipper.prices.timeseries", return_value=_rows()),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 103, "high": 200, "lowTime": now, "highTime": now}
            }),
            patch("flipper.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 100, "highPriceVolume": 100}
            }),
        ):
            signal = signals.item_signal(1)["signal"]

        assert signal is not None
        self.assertFalse(signal["ready_to_buy"])
        self.assertTrue(signal["patient_probe_ready"])
        self.assertEqual(signal["entry_price"], signal["buy_band"])
        self.assertEqual(signal["exit_price"], signal["sell_band"])

    def test_bid_inside_the_ready_window_posts_at_the_live_low(self) -> None:
        # The live low (102) sits 2% above the band (100), inside PATIENT_READY_MAX_DISTANCE_PCT.
        # The order that gets posted — and that the replay evidence validates — is the live one, so
        # entry_price is the live low, not the band. The band stays visible as `buy_band`.
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
            signal = signals.item_signal(1)["signal"]

        assert signal is not None
        self.assertEqual(signal["buy_band"], 100)
        self.assertEqual(signal["entry_price"], 102)
        self.assertTrue(signal["ready_to_buy"])
        self.assertFalse(signal["patient_probe_ready"])
        self.assertEqual(signal["capital_required"], 102 * signal["fillable_qty"])
        self.assertEqual(signal["liquidity_profit"], signal["margin"] * signal["fillable_qty"])
        self.assertEqual(signal["roi_pct"], round(signal["margin"] / 102 * 100, 2))

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
            signal = signals.item_signal(1)["signal"]

        assert signal is not None
        self.assertFalse(signal["ready_to_buy"])
        self.assertFalse(signal["patient_probe_ready"])

    def test_active_margin_scan_accepts_fresh_stable_after_tax_spread(self) -> None:
        now = int(time.time())
        stable = _rows(12)
        for index, row in enumerate(stable):
            row["avgLowPrice"] = 10_000_000 if index % 4 == 0 else 10_100_000
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
        # Quote one coin inside the touch on both sides. Same-price GE offers fill first-in-
        # first-out, so an order posted exactly at the last executed bid queues behind everything
        # already resting there; one coin buys priority against a six-figure margin.
        self.assertEqual(row["entry_price"], 10_000_000 + 1)
        self.assertEqual(row["exit_price"], 10_300_000 - 1)
        self.assertEqual(row["current_low"], 10_000_000)
        self.assertEqual(row["current_high"], 10_300_000)
        self.assertEqual(row["max_qty"], 1)
        self.assertEqual(row["fillable_qty"], 1)
        self.assertGreater(row["expected_value_per_unit"], 0)
        self.assertGreater(row["expected_gp_per_hour"], 0)
        # Admission is on the scale-free ROI floor, not an absolute per-unit margin: the same
        # spread must read the same way to a 5m and a 1b bankroll.
        self.assertGreaterEqual(row["roi_pct"], signals.ACTIVE_MIN_ROI_PCT)

    def test_active_margin_scan_rejects_edge_smaller_than_its_replayed_loss(self) -> None:
        # Same spread as the accepted case, but this order has actually been force-exited into a
        # drawdown far bigger than its edge during the replay window. Thin edge, fat tail.
        now = int(time.time())
        rows = _rows(60)
        for index, row in enumerate(rows):
            row["avgLowPrice"] = 10_000_000 if index % 4 == 0 else 10_100_000
            row["avgHighPrice"] = 10_300_000
        for row in rows[40:]:              # entry touch, then no target inside the hold window
            row["avgLowPrice"] = 9_900_000
            row["avgHighPrice"] = 10_000_000
        with (
            patch("flipper.prices.margins", return_value=[{
                "id": 1, "name": "Thin gear", "buy": 10_000_000, "sell": 10_300_000,
                "margin": 94_000, "ge_limit": 8, "vol_1h": 10, "potential_1h": 752_000,
            }]),
            patch("flipper.prices.latest", return_value={
                "1": {"low": 10_000_000, "high": 10_300_000,
                      "lowTime": now, "highTime": now}
            }),
            patch("flipper.prices.one_hour", return_value={
                "1": {"lowPriceVolume": 5, "highPriceVolume": 5}
            }),
            patch("flipper.prices.timeseries", return_value=rows),
            patch("flipper.prices.prefetch_timeseries"),
        ):
            result = signals.active_margin_scan()

        self.assertEqual(result["candidates"], [])
        self.assertIn("worst replayed loss", result["rejected"][0]["reason"])

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

    def test_active_replay_requires_repeatable_profitable_round_trips(self) -> None:
        rows = []
        for cycle in range(3):
            rows.extend([
                {
                    "avgLowPrice": 100,
                    "avgHighPrice": 105,
                    "timestamp": cycle * 1_000,
                },
                {
                    "avgLowPrice": 104,
                    "avgHighPrice": 120,
                    "timestamp": cycle * 1_000 + 300,
                },
                {
                    "avgLowPrice": 110,
                    "avgHighPrice": 115,
                    "timestamp": cycle * 1_000 + 600,
                },
            ])

        evidence = signals._active_replay_evidence(rows, buy=101, sell=119)

        self.assertEqual(evidence["trades"], 3)
        self.assertEqual(evidence["opportunity_episodes"], 3)
        self.assertEqual(evidence["win_rate"], 1)
        self.assertGreater(evidence["mean_profit_per_unit"], 0)

    def test_active_replay_does_not_force_exit_an_incomplete_tail_trade(self) -> None:
        rows = [
            {"avgLowPrice": 100, "avgHighPrice": 105},
            {"avgLowPrice": 104, "avgHighPrice": 120},
            {"avgLowPrice": 110, "avgHighPrice": 115},
            {"avgLowPrice": 100, "avgHighPrice": 105},
            {"avgLowPrice": 104, "avgHighPrice": 120},
            {"avgLowPrice": 110, "avgHighPrice": 115},
            {"avgLowPrice": 100, "avgHighPrice": 105},
        ]

        evidence = signals._active_replay_evidence(rows, buy=101, sell=119)

        self.assertEqual(evidence["trades"], 2)
        self.assertEqual(evidence["opportunity_episodes"], 2)

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
                "1": {"lowPriceVolume": 100, "highPriceVolume": 100}
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
        self.assertEqual(
            signal["expected_profit_per_unit"],
            round(signal["replay_evidence"]["mean_profit_gp"] / max(1, signal["fillable_qty"])),
        )
        self.assertEqual(signal["expected_profit"], signal["replay_evidence"]["mean_profit_gp"])

    def test_time_replay_charges_unfilled_cash_and_forces_inventory_out(self) -> None:
        rows = _time_rows(_SELLS_OUT, days=4)

        evidence = signals._time_replay_evidence(
            rows, buy=101, sell=129, quantity=5, entry_bucket=0,
        )

        self.assertEqual(evidence["blocks"], 3)
        # Four entry buckets touch, but the fourth has no complete horizon after it and so
        # has no observable outcome. Only graded entries count toward the quorum.
        self.assertEqual(evidence["opportunity_episodes"], 3)
        self.assertTrue(evidence["qualifies"])
        self.assertLess(evidence["mean_utility_gp"], evidence["mean_profit_gp"])

    def test_time_replay_books_a_loss_when_the_target_never_trades(self) -> None:
        # The sell target is never reached, so every block ends by crossing back to the low side.
        # Without a forced exit the replay would report a costless zero instead of the real loss.
        rows = _time_rows(_NEVER_SELLS, days=4)

        evidence = signals._time_replay_evidence(
            rows, buy=101, sell=129, quantity=5, entry_bucket=0,
        )

        self.assertEqual(evidence["blocks"], 3)
        self.assertLess(evidence["worst_profit_gp"], 0)
        self.assertFalse(evidence["qualifies"])

    def test_time_replay_skips_a_block_whose_horizon_lost_a_bucket(self) -> None:
        # A missing 6h bucket makes the next five rows span 30h, not 24h. Grading it would
        # charge 24h of capital against a horizon that ran a quarter longer.
        rows = _time_rows(_SELLS_OUT, days=4)
        del rows[6]

        evidence = signals._time_replay_evidence(
            rows, buy=101, sell=129, quantity=5, entry_bucket=0,
        )

        # Day 1's block is the one that spans the gap; days 0 and 2 survive.
        self.assertEqual(evidence["blocks"], 2)

    def test_time_replay_start_follows_the_utc_bucket_not_the_row_stride(self) -> None:
        # After a dropped bucket, "four rows later" is no longer "one day later". A strided
        # walk keeps its old phase and opens a block on bucket 1, where the low is 105 and
        # the order cannot fill — a zero-profit block that never happened. Every graded block
        # must still begin in the entry window, so every one of them fills and sells out.
        rows = _time_rows(_SELLS_OUT, days=6)
        del rows[6]

        evidence = signals._time_replay_evidence(
            rows, buy=101, sell=129, quantity=5, entry_bucket=0,
        )

        self.assertEqual(evidence["blocks"], 4)
        self.assertGreater(evidence["worst_profit_gp"], 0)

    def test_time_replay_keeps_an_early_sellout_when_the_terminal_low_is_null(self) -> None:
        # Inventory already cleared, so the missing terminal print is irrelevant to the
        # result. Discarding the block would delete a successful observation.
        rows = _time_rows(_SELLS_OUT, days=4)
        for index in (4, 8, 12):  # each block's terminal bucket prints nothing
            rows[index]["avgLowPrice"] = None

        evidence = signals._time_replay_evidence(
            rows, buy=101, sell=129, quantity=5, entry_bucket=0,
        )

        # Day 0 fills and clears before the deadline, so its missing terminal print is
        # irrelevant to the result; days 1 and 2 cannot fill and score a bare zero.
        self.assertEqual(evidence["blocks"], 3)
        self.assertGreater(evidence["mean_profit_gp"], 0)
        # Three graded blocks, but only one of them was ever entered.
        self.assertEqual(evidence["opportunity_episodes"], 1)

    def test_time_replay_drops_a_block_whose_forced_exit_never_printed(self) -> None:
        # Day 0 fills, never reaches the target, and then nothing prints on the low side for
        # the whole 24h horizon — the forced exit is unobservable. The old code wrote off the
        # entire purchase price (5 * -101 = -505) and that number went straight into position
        # sizing via worst_profit_gp.
        rows = _time_rows(_NEVER_SELLS, days=4)
        for index, row in enumerate(rows):
            if index % 4 or index:
                row["avgLowPrice"] = None

        evidence = signals._time_replay_evidence(
            rows, buy=101, sell=129, quantity=5, entry_bucket=0,
        )

        self.assertEqual(evidence["blocks"], 2)  # the two silent zero-fill days remain
        self.assertEqual(evidence["worst_profit_gp"], 0)
        self.assertFalse(evidence["qualifies"])

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
            signal = signals.item_signal(1)["signal"]

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
            signal = signals.item_signal(1)["signal"]

        assert signal is not None
        self.assertEqual(signal["trend"]["direction"], "flat")
        self.assertEqual(signal["sell_band"], signal["sell_band_full"])
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
            evaluation = signals.item_signal(1, timestep="6h")

        self.assertIsNone(evaluation["signal"])
        self.assertEqual(evaluation["blocked_by"]["code"], "band_margin_non_positive")

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
            signal = signals.item_signal(1, timestep="6h")["signal"]

        assert signal is not None
        self.assertEqual(signal["regime"]["level"], "high")
        self.assertEqual(signal["regime"]["reason"], "short_window_price_shock")
        self.assertEqual(signal["sell_band"], 2575)
        self.assertLess(signal["sell_band"], signal["sell_band_full"])



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
