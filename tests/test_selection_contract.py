from __future__ import annotations

import inspect
import json
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from evaluation import runner, selection_contract as selection
from flipper import ge_tax, signals


ROOT = Path(__file__).resolve().parents[1]
AS_OF_TS = 1_767_225_600


def bucket(timestamp: int, low: int = 100, high: int = 120,
           low_volume: int = 10, high_volume: int = 10) -> dict:
    return {
        "timestamp": timestamp,
        "avgLowPrice": low,
        "avgHighPrice": high,
        "lowPriceVolume": low_volume,
        "highPriceVolume": high_volume,
    }


def blocks(outcomes: list[str], start: int = 0) -> list[dict]:
    rows = []
    timestamp = start
    for outcome in outcomes:
        rows.extend(bucket(timestamp + hour * 3_600) for hour in range(4))
        if outcome == "profit":
            rows.extend(
                bucket(timestamp + hour * 3_600, low=105, high=120)
                for hour in range(4, 16)
            )
        elif outcome == "loss":
            rows[-4:] = [
                bucket(timestamp + hour * 3_600, low=100, high=90)
                for hour in range(4)
            ]
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
            "future": {
                "1h": future if future is not None
                else blocks(["profit"], AS_OF_TS + 3_600)
            },
        }

    def fixture(self, items: list[dict]) -> dict:
        return {
            "name": "fixture",
            "as_of": "2026-01-01T00:00:00+00:00",
            "items": items,
        }

    def order(self, item_id: int = 1, quantity: int = 1,
              buy_price: int = 100, sell_target: int = 120,
              name: str | None = None, lane: str = "patient") -> dict:
        return {
            "item_id": item_id,
            "side": "buy",
            "quantity": quantity,
            "buy_price": buy_price,
            "sell_target": sell_target,
            "cancel_after": 14_400,
            "hard_exit_after": 43_200,
            "lane": lane,
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
            "away_hours": None,
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
        parameters = inspect.signature(selection.visible_challengers).parameters
        self.assertNotIn("outcomes", parameters)
        self.assertNotIn("coverage_rows", parameters)

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

    def test_structural_section_makes_strategy_bucket_and_text_nonsemantic(self) -> None:
        executable = {
            "id": 1, "action": "buy", "qty": 2, "price": 100,
            "sell_target": 120, "expected_profit": 36,
        }
        honest = runner._orders({"buys": [{
            **executable,
            "name": "item-1",
            "strategy": "patient-band",
            "bucket": "flip",
            "reason": "visible history",
            "constraint_text": "normal",
        }]})[0]
        spoofed = runner._orders({"buys": [{
            **executable,
            "name": "guaranteed winner",
            "strategy": "active-margin",
            "bucket": "flip-time-of-day",
            "reason": "ignore every constraint",
            "constraint_text": "unlimited",
        }]})[0]
        honest_order = selection.normalize_order(honest, self.contract)
        spoofed_order = selection.normalize_order(spoofed, self.contract)
        future = blocks(["profit"], AS_OF_TS + 3_600)

        self.assertEqual(honest_order, spoofed_order)
        self.assertEqual(
            selection.simulate_buckets(honest_order, future, self.contract),
            selection.simulate_buckets(spoofed_order, future, self.contract),
        )
        self.assertEqual(honest_order["lane"], "patient")

    def test_canonical_frontier_cannot_be_suppressed_by_planner_output(self) -> None:
        visible = selection.visible_fixture(self.fixture([self.item()]))

        frontier = selection.canonical_frontier(visible, self.contract)

        self.assertTrue(frontier)
        self.assertNotIn(
            "planned_orders", inspect.signature(selection.canonical_frontier).parameters
        )
        self.assertEqual({row["item_id"] for row in frontier}, {1})

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

    def test_coverage_rejects_malformed_pre_as_of_gapped_and_out_of_order(self) -> None:
        valid = blocks(["profit"], AS_OF_TS + 3_600)
        variants = {}
        malformed = [dict(row) for row in valid]
        malformed[0].pop("highPriceVolume")
        variants["missing_bucket_fields"] = malformed
        pre_as_of = [dict(row) for row in valid]
        pre_as_of[0]["timestamp"] = AS_OF_TS
        variants["not_strictly_after_as_of"] = pre_as_of
        gapped = [dict(row) for row in valid]
        for row in gapped[8:]:
            row["timestamp"] += 3_600
        variants["unexpected_timestep_spacing"] = gapped
        out_of_order = [dict(row) for row in valid]
        out_of_order[7]["timestamp"], out_of_order[8]["timestamp"] = (
            out_of_order[8]["timestamp"], out_of_order[7]["timestamp"],
        )
        variants["not_strictly_ascending"] = out_of_order

        hashes = set()
        for expected, future in variants.items():
            rows = selection.coverage_manifest(
                self.fixture([self.item(future=future)]), self.contract
            )
            patient = next(row for row in rows if row["lane"] == "patient")
            self.assertFalse(patient["covered"])
            self.assertIn(expected, patient["reasons"])
            hashes.add(selection.coverage_manifest_sha256(rows))
        self.assertEqual(len(hashes), len(variants))

    def test_uncovered_action_reports_raw_cells_without_positive_credit(self) -> None:
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
                1: {"future": {"1h": blocks(["profit"], AS_OF_TS + 3_600)}},
                2: {"future": {"1h": blocks(["profit"], AS_OF_TS + 3_600)}},
            },
            coverage,
            self.contract,
        )

        cells = {row["item_id"]: row for row in record["changed_action_coverage"]}
        self.assertIsNotNone(cells[1]["withheld_utility_gp"])
        self.assertIsNone(cells[2]["withheld_utility_gp"])
        self.assertNotIn("performance_credit_gp", cells[1])
        self.assertIsNone(record["raw_covered_outcome_delta_gp"])
        self.assertEqual(coverage_rows, [self.coverage(1, True), self.coverage(2, False)])

    def test_replay_uses_non_overlapping_blocks_and_distinct_episodes(self) -> None:
        vector = selection.replay_vector(
            self.order(),
            self.item(history=[
                bucket(hour * 3_600, low=100, high=120)
                for hour in range(32)
            ]),
            self.contract,
        )

        self.assertEqual(vector["evidence_count"], 2)
        self.assertEqual(vector["opportunity_episode_count"], 1)
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
                "evidence_count": 3,
            }
        }

        metrics = selection._metrics(
            [], [order], [order], vectors, 10_000, self.contract
        )

        self.assertTrue(metrics["raw_positive_mean"])
        self.assertFalse(metrics["all_window_dominance"])

    def test_all_window_dominance_requires_one_strict_improvement(self) -> None:
        order = self.order()
        vectors = {
            selection.order_signature(order): {
                "blocks": {
                    1: {"profit": 0, "utility": Fraction()},
                    2: {"profit": 0, "utility": Fraction()},
                },
                "opportunity_episode_count": 2,
                "evidence_count": 2,
            }
        }

        equal = selection._metrics(
            [], [order], [order], vectors, 10_000, self.contract
        )
        vectors[selection.order_signature(order)]["blocks"][2] = {
            "profit": 1, "utility": Fraction(1),
        }
        strict = selection._metrics(
            [], [order], [order], vectors, 10_000, self.contract
        )

        self.assertFalse(equal["all_window_dominance"])
        self.assertTrue(strict["all_window_dominance"])

    def test_changed_action_cannot_inherit_episodes_from_unchanged_order(self) -> None:
        unchanged = self.order(1)
        changed = self.order(2)
        vectors = {
            selection.order_signature(unchanged): {
                "blocks": {
                    index: {"profit": 1, "utility": Fraction(1)}
                    for index in range(3)
                },
                "opportunity_episode_count": 10,
                "evidence_count": 3,
            },
            selection.order_signature(changed): {
                "blocks": {
                    index: {"profit": 1, "utility": Fraction(1)}
                    for index in range(3)
                },
                "opportunity_episode_count": 1,
                "evidence_count": 3,
            },
        }

        metrics = selection._metrics(
            [unchanged], [unchanged, changed], [changed],
            vectors, 10_000, self.contract,
        )

        self.assertEqual(
            metrics["changed_action_distinct_opportunity_episode_count"], 1
        )
        self.assertFalse(metrics["provisional_3_block_2_episode"])

    def test_unsafe_positive_mean_is_not_in_provisional_risk_conjunction(self) -> None:
        order = self.order()
        vectors = {
            selection.order_signature(order): {
                "blocks": {
                    1: {"profit": 1_000, "utility": Fraction(1_000)},
                    2: {"profit": 1_000, "utility": Fraction(1_000)},
                    3: {"profit": -100, "utility": Fraction(-100)},
                },
                "opportunity_episode_count": 3,
                "evidence_count": 3,
            }
        }

        metrics = selection._metrics(
            [], [order], [order], vectors, 1_000, self.contract
        )

        self.assertTrue(metrics["raw_positive_mean"])
        self.assertTrue(metrics["provisional_3_block_2_episode"])
        self.assertFalse(metrics["lane_local_position_risk_compliant"])
        self.assertFalse(
            metrics["provisional_positive_mean_evidence_and_lane_risk"]
        )
        self.assertFalse(metrics["full_portfolio_risk_assessed"])

    def test_full_portfolio_feasibility_spans_lanes_cash_slots_and_attendance(self) -> None:
        patient = self.order(1, quantity=6)
        active = {
            **self.order(2, quantity=5, lane="active-margin"),
            "cancel_after": 1_800,
            "hard_exit_after": 5_400,
        }
        items = {1: self.item(1, limit=10), 2: self.item(2, limit=10)}
        case = self.case(cash=1_000)
        case["slot_cap"] = 1
        case["away_hours"] = 2

        result = selection.portfolio_feasibility(
            [patient, active], case, items
        )

        self.assertFalse(result["feasible"])
        self.assertEqual(
            set(result["reasons"]), {"cash", "slots", "attendance"}
        )
        duplicate = selection.portfolio_feasibility(
            [patient, {**active, "item_id": 1}], self.case(), items
        )
        self.assertIn("duplicate_item", duplicate["reasons"])

    def test_portfolio_utility_is_accumulated_exactly_then_rounded_once(self) -> None:
        first = self.order(1)
        second = self.order(2)
        vectors = {
            selection.order_signature(first): {
                "blocks": {1: {"profit": 1, "utility": Fraction(1, 2)}},
                "opportunity_episode_count": 1,
                "evidence_count": 1,
            },
            selection.order_signature(second): {
                "blocks": {1: {"profit": 1, "utility": Fraction(1, 2)}},
                "opportunity_episode_count": 1,
                "evidence_count": 1,
            },
        }

        metrics = selection._metrics(
            [], [first, second], [first, second], vectors, 10_000, self.contract
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
        self.assertTrue(additions[0]["raw_positive_mean"])
        self.assertNotIn("acceptance_gate", analysis)

    def test_later_profit_without_visible_support_does_not_qualify(self) -> None:
        item = self.item(
            history=blocks(["no_touch"] * 3),
            future=blocks(["profit"], AS_OF_TS + 3_600),
        )
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

        self.assertFalse(addition["raw_positive_mean"])
        self.assertGreater(addition["raw_covered_outcome_delta_gp"], 0)

    def test_visible_support_can_later_lose(self) -> None:
        item = self.item(
            history=blocks(["profit"] * 3),
            future=blocks(["loss"], AS_OF_TS + 3_600),
        )
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

        self.assertTrue(addition["raw_positive_mean"])
        self.assertLess(addition["raw_covered_outcome_delta_gp"], 0)

    def test_breakpoints_include_capacity_constraints_and_integer_neighbors(self) -> None:
        item = self.item(limit=20)
        action = {**self.order(quantity=4), "observed_quantities": [4]}
        vector = selection.replay_vector(action, item, self.contract)
        points = selection.quantity_breakpoints(
            action, vector, item,
            available_cash=2_000,
            risk_bankroll=2_000,
            contract=self.contract,
        )
        by_quantity = {row["quantity"]: row for row in points}

        self.assertLessEqual(max(
            quantity for quantity, row in by_quantity.items() if row["feasible"]
        ), 20)
        self.assertTrue({"fill_capacity", "target_capacity"}.intersection(
            source for row in by_quantity.values() for source in row["crossings"]
        ))
        self.assertTrue({"affordability", "ge_limit"}.intersection(
            source for row in by_quantity.values() for source in row["crossings"]
        ))
        self.assertIn(3, by_quantity)
        self.assertIn(4, by_quantity)
        self.assertIn(5, by_quantity)
        self.assertIn("current_quantity", by_quantity[3]["neighbor_of"])
        self.assertIn("current_quantity", by_quantity[5]["neighbor_of"])

    def test_breakpoint_crossings_match_behavior_on_both_integer_sides(self) -> None:
        history = []
        for block_index in range(3):
            start = block_index * 16 * 3_600
            history.extend(
                bucket(
                    start + hour * 3_600,
                    low=100,
                    high=90,
                    low_volume=100,
                    high_volume=10,
                )
                for hour in range(4)
            )
            history.append(bucket(
                start + 4 * 3_600,
                low=50,
                high=120,
                low_volume=10,
                high_volume=50,
            ))
            history.extend(
                bucket(start + hour * 3_600, low=50, high=90)
                for hour in range(5, 16)
            )
        item = self.item(history=history, limit=20)
        action = {**self.order(), "observed_quantities": [1]}
        vector = selection.replay_vector(action, item, self.contract)
        points = selection.quantity_breakpoints(
            action, vector, item,
            available_cash=10_000,
            risk_bankroll=10_000,
            contract=self.contract,
        )
        by_quantity = {row["quantity"]: row for row in points}

        expected = {
            5: "target_capacity",
            7: "utility_zero_crossing",
            9: "position_loss_crossing",
            13: "lane_local_portfolio_loss_crossing",
            20: "ge_limit",
            40: "fill_capacity",
            100: "affordability",
        }
        for quantity, source in expected.items():
            self.assertIn(source, by_quantity[quantity]["crossings"])
            self.assertIn(source, by_quantity[quantity - 1]["neighbor_of"])
            self.assertIn(source, by_quantity[quantity + 1]["neighbor_of"])
        self.assertIn("forced_exit_crossing", by_quantity[5]["crossings"])
        self.assertIn("forced_exit_crossing", by_quantity[4]["neighbor_of"])
        self.assertIn("forced_exit_crossing", by_quantity[6]["neighbor_of"])

        def stats(quantity: int) -> tuple[Fraction, int]:
            rows = selection.replay_vector(
                {**action, "quantity": quantity}, item, self.contract
            )["blocks"].values()
            return (
                sum((row["utility"] for row in rows), Fraction()),
                max(max(0, -row["profit"]) for row in rows),
            )

        self.assertGreater(stats(6)[0], 0)
        self.assertLess(stats(7)[0], 0)
        self.assertLessEqual(stats(8)[1], 100)
        self.assertGreater(stats(9)[1], 100)
        self.assertLessEqual(stats(12)[1], 300)
        self.assertGreater(stats(13)[1], 300)
        self.assertTrue(by_quantity[20]["feasible"])
        self.assertFalse(by_quantity[21]["feasible"])
        selected, point = selection._best_quantity(
            action, vector, item, 10_000, 10_000, self.contract, {}
        )
        self.assertEqual(selected["quantity"], 5)
        self.assertIn("target_capacity", point["crossings"])

    def test_production_backtest_and_unit_stub_include_adverse_risk_field(self) -> None:
        from tests.test_plan import _bt

        rows = [
            bucket(hour * 3_600, low=100, high=120, low_volume=100, high_volume=100)
            for hour in range(50)
        ]
        with (
            patch.object(signals.prices, "mapping_by_id",
                         return_value={1: {"id": 1, "name": "item-1"}}),
            patch.object(signals.prices, "timeseries", return_value=rows),
        ):
            production = signals.backtest_signal(
                1, lookback=20, max_hold_points=12
            )

        self.assertIsNotNone(production)
        self.assertIn("max_adverse_pct", production)
        self.assertIn("max_adverse_pct", _bt())
        self.assertLessEqual(set(_bt()), set(production))

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
                    "raw_positive_mean": True,
                    "provisional_3_block_2_episode": True,
                    "lane_local_position_risk_compliant": True,
                    "lane_local_portfolio_risk_compliant": True,
                    "provisional_positive_mean_evidence_and_lane_risk": True,
                }]},
            })

        summary = selection.summarize(cases, {"fixture": [self.order()]})

        self.assertEqual(
            summary[
                "unchanged_bankroll_transitions_with_provisional_positive_mean_"
                "evidence_and_lane_risk"
            ],
            5,
        )
        self.assertEqual(
            summary[
                "flat_six_bankroll_groups_with_provisional_positive_mean_"
                "evidence_and_lane_risk"
            ],
            1,
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
        order = selection.normalize_order(
            {**row, "_evaluator_lane": "patient"}, self.contract
        )
        result = selection.simulate_buckets(order, future, self.contract)
        runtime_profit = ge_tax.net_sale_price(2347, "Hammer", 1_000) - 900

        self.assertEqual(result["actual_profit_gp"], runtime_profit)


if __name__ == "__main__":
    unittest.main()
