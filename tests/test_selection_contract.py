from __future__ import annotations

import json
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from evaluation import runner, selection_contract as selection
from flipper import ge_tax


ROOT = Path(__file__).resolve().parents[1]


def bucket(timestamp: int, low: int = 100, high: int = 120,
           low_volume: int = 10, high_volume: int = 10) -> dict:
    return {
        "timestamp": timestamp,
        "avgLowPrice": low,
        "avgHighPrice": high,
        "lowPriceVolume": low_volume,
        "highPriceVolume": high_volume,
    }


def blocks(outcomes: list[str]) -> list[dict]:
    rows = []
    timestamp = 0
    for outcome in outcomes:
        rows.extend(bucket(timestamp + hour * 3_600) for hour in range(4))
        if outcome == "profit":
            rows.extend(
                bucket(timestamp + hour * 3_600, low=105, high=120)
                for hour in range(4, 16)
            )
        elif outcome == "loss":
            rows.extend(
                bucket(timestamp + hour * 3_600, low=50, high=90)
                for hour in range(4, 16)
            )
        else:
            rows[-4:] = [
                bucket(timestamp + hour * 3_600, low=110, high=120)
                for hour in range(4)
            ]
            rows.extend(
                bucket(timestamp + hour * 3_600, low=110, high=120)
                for hour in range(4, 16)
            )
        timestamp += 16 * 3_600
    return rows


class SelectionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.loads(
            (ROOT / "evaluation/contract.json").read_text()
        )

    def item(self, item_id: int = 1, history: list[dict] | None = None,
             future: list[dict] | None = None, limit: int = 20) -> dict:
        return {
            "id": item_id,
            "name": f"item-{item_id}",
            "members": True,
            "limit": limit,
            "latest": {"low": 100, "high": 120},
            "one_hour": {"lowPriceVolume": 10, "highPriceVolume": 10},
            "history": {"1h": history if history is not None else blocks(["profit"] * 3)},
            "future": {"1h": future if future is not None else blocks(["profit"])},
        }

    def fixture(self, items: list[dict]) -> dict:
        return {
            "name": "fixture",
            "as_of": "2026-01-01T00:00:00+00:00",
            "items": items,
        }

    def order(self, item_id: int = 1, quantity: int = 1,
              buy_price: int = 100, sell_target: int = 120,
              name: str | None = None) -> dict:
        return {
            "item_id": item_id,
            "side": "buy",
            "quantity": quantity,
            "buy_price": buy_price,
            "sell_target": sell_target,
            "cancel_after": 14_400,
            "hard_exit_after": 43_200,
            "lane": "patient",
            "name": name or f"item-{item_id}",
        }

    def coverage(self, item_id: int = 1, covered: bool = True) -> dict:
        return {
            "fixture": "fixture",
            "item_id": item_id,
            "lane": "patient",
            "as_of": "2026-01-01T00:00:00+00:00",
            "available_buckets": 16 if covered else 15,
            "required_buckets": 16,
            "covered": covered,
        }

    def case(self, orders: list[dict] | None = None, cash: int = 10_000) -> dict:
        return {
            "fixture": "fixture",
            "attendance": "attended",
            "slot_cap": 8,
            "cash_gp": cash,
            "normalized_orders": orders or [],
        }

    def test_visible_projection_excludes_future_and_coverage_metadata(self) -> None:
        item = self.item()
        item["coverage_flags"] = {"patient": True}
        visible = selection.visible_fixture(self.fixture([item]))

        self.assertNotIn("future", visible["items"][0])
        self.assertNotIn("coverage_flags", visible["items"][0])
        selection.assert_visible(visible)

        with self.assertRaisesRegex(ValueError, "withheld metadata"):
            selection.assert_visible({
                "items": [{**visible["items"][0], "history": {
                    "1h": [{**bucket(0), "future_coverage": True}]
                }}]
            })
        with self.assertRaisesRegex(ValueError, "withheld metadata"):
            selection.assert_visible({
                **visible,
                "universe": {"selection_coverage": True},
            })

    def test_planner_execution_receives_only_visible_input_shape(self) -> None:
        fixture = self.fixture([self.item()])
        seen = []
        original = runner._market_maps

        def inspect(value: dict) -> tuple:
            seen.append(value)
            return original(value)

        with (
            patch.object(runner, "_market_maps", side_effect=inspect),
            patch.object(runner.plan, "plan", return_value={}),
        ):
            runner._run_plan(
                fixture, 10_000, self.contract["attendance"][0], 1, "balanced"
            )

        self.assertEqual(len(seen), 1)
        self.assertEqual(
            set(seen[0]["items"][0]),
            {"id", "name", "members", "limit", "latest", "one_hour", "history"},
        )
        self.assertEqual(
            seen[0]["items"][0]["history"]["1h"],
            fixture["items"][0]["history"]["1h"],
        )

    def test_backtest_bucket_shape_matches_synthetic_and_observed_inputs(self) -> None:
        fixtures = runner.load_fixtures(list(runner.DEFAULT_FIXTURES))
        required = {
            "timestamp", "avgLowPrice", "avgHighPrice",
            "lowPriceVolume", "highPriceVolume",
        }
        shapes = {}
        for fixture in fixtures:
            row = next(
                bucket
                for item in fixture["items"]
                for bucket in item["history"]["1h"]
            )
            shapes["observed" if fixture["name"].startswith("wiki_cache_")
                   else "synthetic"] = set(row)

        self.assertEqual(shapes, {"synthetic": required, "observed": required})

    def test_signature_is_quantity_and_price_sensitive_but_label_proof(self) -> None:
        base = self.order(name="honest")
        spoofed = {**base, "name": "guaranteed winner", "reason": "perfect evidence"}

        self.assertEqual(selection.order_signature(base), selection.order_signature(spoofed))
        self.assertNotEqual(
            selection.order_signature(base),
            selection.order_signature({**base, "quantity": 2}),
        )
        self.assertNotEqual(
            selection.order_signature(base),
            selection.order_signature({**base, "buy_price": 99}),
        )
        self.assertEqual(
            tuple(selection.executable_order(base)),
            selection.EXECUTABLE_FIELDS,
        )

    def test_coverage_is_qualified_by_fixture_item_lane_and_as_of(self) -> None:
        manifest = selection.coverage_manifest(
            self.fixture([self.item()]), self.contract
        )
        patient = next(row for row in manifest if row["lane"] == "patient")

        self.assertEqual(
            selection.coverage_key(
                patient["fixture"], patient["item_id"],
                patient["lane"], patient["as_of"],
            ),
            ("fixture", 1, "patient", "2026-01-01T00:00:00+00:00"),
        )
        self.assertTrue(patient["covered"])

    def test_uncovered_action_gets_no_credit_without_suppressing_covered_cell(self) -> None:
        covered_order = self.order(1)
        uncovered_order = self.order(2)
        coverage_rows = [self.coverage(1, True), self.coverage(2, False)]
        coverage = {
            selection.coverage_key(
                row["fixture"], row["item_id"], row["lane"], row["as_of"]
            ): row
            for row in coverage_rows
        }
        record = {}

        selection._annotate_outcome(
            record,
            [],
            [covered_order, uncovered_order],
            "fixture",
            "2026-01-01T00:00:00+00:00",
            {
                1: {"future": {"1h": blocks(["profit"])}},
                2: {"future": {"1h": blocks(["profit"])}},
            },
            coverage,
            self.contract,
        )

        cells = {row["item_id"]: row for row in record["withheld_scoring_cells"]}
        self.assertGreater(cells[1]["performance_credit_gp"], 0)
        self.assertEqual(cells[2]["performance_credit_gp"], 0)
        self.assertIsNone(record["withheld_incremental_utility_gp"])
        self.assertEqual(coverage_rows, [self.coverage(1, True), self.coverage(2, False)])

    def test_replay_uses_non_overlapping_blocks_and_distinct_episodes(self) -> None:
        vector = selection.replay_vector(
            self.order(), self.item(history=blocks(["profit", "profit"])), self.contract
        )

        self.assertEqual(vector["evidence_count"], 2)
        self.assertGreater(vector["opportunity_episode_count"], 1)
        starts = sorted(vector["blocks"])
        self.assertEqual(starts[1] - starts[0], 16 * 3_600)

    def test_positive_mean_can_fail_all_window_dominance(self) -> None:
        order = self.order()
        vectors = {
            selection.order_signature(order): {
                "blocks": {
                    1: {"profit": 10, "utility": Fraction(10)},
                    2: {"profit": 10, "utility": Fraction(10)},
                    3: {"profit": -1, "utility": Fraction(-1)},
                },
                "opportunity_episode_count": 3,
            }
        }

        metrics = selection._metrics([], [order], vectors, 10_000, self.contract)

        self.assertTrue(metrics["raw_positive_mean_qualification"])
        self.assertFalse(metrics["all_window_dominance"])

    def test_portfolio_utility_is_accumulated_exactly_then_rounded_once(self) -> None:
        first = self.order(1)
        second = self.order(2)
        vectors = {
            selection.order_signature(first): {
                "blocks": {1: {"profit": 1, "utility": Fraction(1, 2)}},
                "opportunity_episode_count": 1,
            },
            selection.order_signature(second): {
                "blocks": {1: {"profit": 1, "utility": Fraction(1, 2)}},
                "opportunity_episode_count": 1,
            },
        }

        metrics = selection._metrics(
            [], [first, second], vectors, 10_000, self.contract
        )

        self.assertEqual(metrics["mean_incremental_visible_utility_gp"], 1)

    def test_repeated_tiny_portfolio_reports_visible_addition(self) -> None:
        item = self.item(limit=2)
        frontier = [{**self.order(), "observed_quantities": [1]}]
        analysis = selection.analyze_case(
            self.case(),
            frontier,
            selection.visible_fixture(self.fixture([item])),
            selection.withheld_items(self.fixture([item])),
            [self.coverage()],
            self.contract,
        )

        additions = [
            row for row in analysis["challengers"]
            if row["kind"] == "one_order_addition"
        ]
        self.assertTrue(additions)
        self.assertTrue(additions[0]["raw_positive_mean_qualification"])
        self.assertFalse(analysis["acceptance_gate"])

    def test_later_profit_without_visible_support_does_not_qualify(self) -> None:
        item = self.item(history=blocks(["no_touch"] * 3), future=blocks(["profit"]))
        frontier = [{**self.order(), "observed_quantities": [1]}]
        analysis = selection.analyze_case(
            self.case(),
            frontier,
            selection.visible_fixture(self.fixture([item])),
            selection.withheld_items(self.fixture([item])),
            [self.coverage()],
            self.contract,
        )
        addition = next(
            row for row in analysis["challengers"]
            if row["kind"] == "one_order_addition"
        )

        self.assertFalse(addition["raw_positive_mean_qualification"])
        self.assertGreater(addition["withheld_incremental_utility_gp"], 0)

    def test_visible_support_can_later_lose(self) -> None:
        item = self.item(history=blocks(["profit"] * 3), future=blocks(["loss"]))
        frontier = [{**self.order(), "observed_quantities": [1]}]
        analysis = selection.analyze_case(
            self.case(),
            frontier,
            selection.visible_fixture(self.fixture([item])),
            selection.withheld_items(self.fixture([item])),
            [self.coverage()],
            self.contract,
        )
        addition = next(
            row for row in analysis["challengers"]
            if row["kind"] == "one_order_addition"
        )

        self.assertTrue(addition["raw_positive_mean_qualification"])
        self.assertLess(addition["withheld_incremental_utility_gp"], 0)

    def test_breakpoints_include_capacity_constraints_and_integer_neighbors(self) -> None:
        item = self.item(limit=20)
        action = {**self.order(quantity=4), "observed_quantities": [4]}
        vector = selection.replay_vector(action, item, self.contract)
        points = selection.quantity_breakpoints(
            action, vector, item, cash=2_000, contract=self.contract
        )
        by_quantity = {row["quantity"]: row["sources"] for row in points}

        self.assertLessEqual(max(by_quantity), 20)
        self.assertTrue({"fill_capacity", "target_capacity"}.intersection(
            source for sources in by_quantity.values() for source in sources
        ))
        self.assertTrue({"affordability", "ge_limit"}.intersection(
            source for sources in by_quantity.values() for source in sources
        ))
        self.assertIn(3, by_quantity)
        self.assertIn(4, by_quantity)
        self.assertIn(5, by_quantity)
        self.assertIn("integer_neighbor", by_quantity[3])
        self.assertIn("integer_neighbor", by_quantity[5])
        self.assertTrue(any(
            "reservation_rounding" in sources for sources in by_quantity.values()
        ))

    def test_equality_based_bankroll_groups_ignore_labels_not_executable_changes(self) -> None:
        cases = []
        for index, cash in enumerate(self.contract["bankrolls_gp"]):
            order = self.order(name=f"label-{index}")
            cases.append({
                "fixture": "fixture",
                "attendance": "attended",
                "slot_cap": 8,
                "cash_gp": cash,
                "normalized_orders": [order],
                "selection_characterization": {"challengers": [{
                    "lane": "patient",
                    "all_window_dominance": False,
                    "raw_positive_mean_qualification": True,
                    "evidence_qualified": True,
                }]},
            })

        summary = selection.summarize(cases, {"fixture": [self.order()]})

        self.assertEqual(
            summary["unchanged_bankroll_transitions_with_qualifying_challengers"], 5
        )
        self.assertEqual(
            summary["flat_six_bankroll_groups_with_qualifying_challengers"], 1
        )

    @unittest.expectedFailure
    def test_characterization_evaluator_v1_honors_runtime_tax_exemption(self) -> None:
        row = {
            "id": 2347,
            "name": "Hammer",
            "strategy": "patient-band",
            "qty": 1,
            "price": 900,
            "sell_target": 1_000,
            "expected_profit": 100,
        }
        future = [
            bucket(hour * 3_600, low=900, high=950)
            for hour in range(4)
        ] + [
            bucket(hour * 3_600, low=950, high=1_000)
            for hour in range(4, 16)
        ]
        result = selection.simulate_buckets(row, future, self.contract)
        runtime_profit = ge_tax.net_sale_price(2347, "Hammer", 1_000) - 900

        self.assertEqual(result["actual_profit_gp"], runtime_profit)


if __name__ == "__main__":
    unittest.main()
