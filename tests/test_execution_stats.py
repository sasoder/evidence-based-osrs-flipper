from __future__ import annotations

import unittest
from unittest.mock import patch

from merch import execution_stats


def _order(*, duration=3600, filled=100, total=100):
    return {
        "id": 1,
        "name": "item1",
        "side": "buy",
        "state": "BOUGHT" if filled == total else "CANCELLED_BUY",
        "filled_qty": filled,
        "total_qty": total,
        "price": 100,
        "duration_seconds": duration,
        "before_login": False,
    }


def _flip(*, profit=1000, hours=2):
    return {
        "id": 1,
        "name": "item1",
        "bought": 100,
        "sold": 120,
        "bought_qty": 100,
        "sold_qty": 100,
        "profit": profit,
        "buy_ts": 1_000_000,
        "sell_ts": 1_000_000 + hours * 3_600_000,
    }


class ExecutionStatsTests(unittest.TestCase):
    def test_requires_four_relevant_orders_before_adjusting(self) -> None:
        with (
            patch("merch.execution_stats.runelite.read_offer_history",
                  return_value=[_order(duration=16 * 3600) for _ in range(3)]),
            patch("merch.execution_stats.runelite.read_flips", return_value=[]),
        ):
            stats = execution_stats.by_item()

        qty, evidence = execution_stats.adjusted_fillable_qty(1, 1000, stats)
        self.assertEqual(qty, 1000)
        self.assertIsNone(evidence)

    def test_slow_orders_reduce_four_hour_fill_estimate(self) -> None:
        with (
            patch("merch.execution_stats.runelite.read_offer_history",
                  return_value=[_order(duration=16 * 3600) for _ in range(4)]),
            patch("merch.execution_stats.runelite.read_flips", return_value=[]),
        ):
            stats = execution_stats.by_item()

        qty, evidence = execution_stats.adjusted_fillable_qty(1, 1000, stats)
        self.assertEqual(qty, 250)
        self.assertEqual(evidence["orders"], 4)
        self.assertEqual(evidence["window_fill_factor"], 0.25)

    def test_partial_cancellations_reduce_fill_estimate(self) -> None:
        with (
            patch("merch.execution_stats.runelite.read_offer_history",
                  return_value=[_order(filled=25, total=100) for _ in range(4)]),
            patch("merch.execution_stats.runelite.read_flips", return_value=[]),
        ):
            stats = execution_stats.by_item()

        qty, _ = execution_stats.adjusted_fillable_qty(1, 1000, stats)
        self.assertEqual(qty, 250)

    def test_full_fast_history_never_increases_market_quantity(self) -> None:
        with (
            patch("merch.execution_stats.runelite.read_offer_history",
                  return_value=[_order(duration=10) for _ in range(4)]),
            patch("merch.execution_stats.runelite.read_flips", return_value=[]),
        ):
            stats = execution_stats.by_item()

        qty, evidence = execution_stats.adjusted_fillable_qty(1, 1000, stats)
        self.assertEqual(qty, 1000)
        self.assertEqual(evidence["window_fill_factor"], 1.0)

    def test_staple_requires_five_profitable_timely_round_trips(self) -> None:
        stats = execution_stats._round_trip_stats([_flip() for _ in range(5)])

        self.assertTrue(stats["staple"])
        self.assertEqual(stats["profitable_trips"], 5)
        self.assertEqual(stats["net_profit"], 5000)

    def test_slow_or_insufficient_history_is_not_a_staple(self) -> None:
        insufficient = execution_stats._round_trip_stats([_flip() for _ in range(4)])
        slow = execution_stats._round_trip_stats([_flip(hours=16) for _ in range(5)])

        self.assertFalse(insufficient["staple"])
        self.assertFalse(slow["staple"])


if __name__ == "__main__":
    unittest.main()
