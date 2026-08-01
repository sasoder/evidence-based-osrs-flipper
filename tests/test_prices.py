from __future__ import annotations

import unittest
from unittest.mock import patch

from flipper import prices

class TimeseriesMemoTests(unittest.TestCase):
    def setUp(self) -> None:
        prices._TS_MEMO.clear()
        self.addCleanup(prices._TS_MEMO.clear)

    def test_prefetch_then_timeseries_serves_from_memo_without_refetch(self) -> None:
        calls: list[tuple[int, str]] = []

        def fake_fetch(item_id: int, timestep: str) -> list[dict]:
            calls.append((item_id, timestep))
            return [{"timestamp": 0, "id": item_id, "step": timestep}]

        with patch("flipper.prices._fetch_timeseries", side_effect=fake_fetch):
            prices.prefetch_timeseries([1, 2], ("1h", "6h"))
            # Every (item, timestep) pair fetched exactly once, concurrently.
            self.assertEqual(sorted(calls), [(1, "1h"), (1, "6h"), (2, "1h"), (2, "6h")])
            # Reads now come from the memo — no further fetches.
            self.assertEqual(prices.timeseries(1, "1h"), [{"timestamp": 0, "id": 1, "step": "1h"}])
            prices.timeseries(2, "6h")
            prices.prefetch_timeseries([1, 2], ("1h", "6h"))  # idempotent
            self.assertEqual(len(calls), 4)

    def test_timeseries_falls_back_to_single_fetch_when_not_prefetched(self) -> None:
        with patch("flipper.prices._fetch_timeseries",
                   return_value=[{"timestamp": 0}]) as fetch:
            self.assertEqual(prices.timeseries(99, "1h"), [{"timestamp": 0}])
            prices.timeseries(99, "1h")  # second read is memoized
        fetch.assert_called_once_with(99, "1h")


if __name__ == "__main__":
    unittest.main()

