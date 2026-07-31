from __future__ import annotations

import inspect
import json
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from evaluation import runner, selection_contract as selection


ROOT = Path(__file__).resolve().parents[1]
STEP = 3_600
START = 1_767_052_800


def bucket(timestamp: int, low: int = 100, high: int = 120,
           low_volume: int = 100, high_volume: int = 100) -> dict:
    return {
        "timestamp": timestamp,
        "avgLowPrice": low,
        "avgHighPrice": high,
        "lowPriceVolume": low_volume,
        "highPriceVolume": high_volume,
    }


def outcome_block(kind: str, start: int) -> list[dict]:
    if kind == "profit":
        return [
            bucket(
                start + index * STEP,
                low=100 if index < 4 or index == 15 else 110,
                high=120,
            )
            for index in range(16)
        ]
    if kind == "loss":
        return [
            bucket(
                start + index * STEP,
                low=100 if index < 4 else 50,
                high=90,
            )
            for index in range(16)
        ]
    return [
        bucket(start + index * STEP, low=110, high=120)
        for index in range(16)
    ]


def history_blocks(kinds: list[str]) -> list[dict]:
    rows = []
    timestamp = START
    for kind in kinds:
        rows.extend(outcome_block(kind, timestamp))
        timestamp += 16 * STEP
    return rows


class SelectionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.loads(
            (ROOT / "evaluation/contract.json").read_text()
        )

    def item(self, item_id: int = 1, history: list[dict] | None = None,
             future: list[dict] | None = None, limit: int = 100,
             name: str | None = None) -> dict:
        history = history if history is not None else history_blocks(["profit"] * 5)
        future = future if future is not None else outcome_block(
            "profit", history[-1]["timestamp"] + STEP
        )
        return {
            "id": item_id,
            "name": name or f"item-{item_id}",
            "members": True,
            "limit": limit,
            "latest": {"low": history[-1]["avgLowPrice"],
                       "high": history[-1]["avgHighPrice"]},
            "one_hour": {"lowPriceVolume": 100, "highPriceVolume": 100},
            "history": {"1h": history},
            "future": {"1h": future},
        }

    def fixture(self, items: list[dict], fixture_class: str = "synthetic") -> dict:
        cutoff = items[0]["history"]["1h"][-1]["timestamp"]
        cutoff_iso = selection._iso(cutoff)
        return {
            "name": "fixture",
            "fixture_class": fixture_class,
            "as_of": cutoff_iso,
            "decision_cutoffs": {
                "patient": cutoff_iso,
                "active": cutoff_iso,
                "time": cutoff_iso,
            },
            "source_provenance": {
                "kind": "deterministic_generator",
                "generator": "test",
                "seed": 1,
            },
            "items": items,
        }

    def order(self, item_id: int = 1, quantity: int = 1,
              buy_price: int = 100, sell_target: int = 120,
              lane: str = "patient", name: str | None = None) -> dict:
        policy = self.contract["simulation"][lane]
        return {
            "item_id": item_id,
            "side": "buy",
            "quantity": quantity,
            "buy_price": buy_price,
            "sell_target": sell_target,
            "cancel_after": round(policy["entry_hours"] * 3_600),
            "hard_exit_after": round(
                (policy["entry_hours"] + policy["hold_hours"]) * 3_600
            ),
            "management_after": 0,
            "lane": lane,
            "name": name or f"item-{item_id}",
        }

    def case(self, orders: list[dict] | None = None, cash: int = 10_000,
             slot_cap: int = 8) -> dict:
        return {
            "fixture": "fixture",
            "fixture_class": "synthetic",
            "decision_lane": "patient",
            "attendance": "attended",
            "away_hours": None,
            "cash_gp": cash,
            "slot_cap": slot_cap,
            "normalized_orders": orders or [],
        }

    def test_visible_projection_is_lane_specific_and_withheld_proof(self) -> None:
        fixture = self.fixture([self.item()])
        visible = selection.visible_fixture(fixture, "patient", self.contract)

        self.assertEqual(visible["decision_lane"], "patient")
        self.assertEqual(set(visible["items"][0]["history"]), {"1h"})
        self.assertNotIn("future", visible["items"][0])
        selection.assert_visible(visible)
        with self.assertRaisesRegex(ValueError, "withheld metadata"):
            selection.assert_visible({**visible, "future_coverage": True})

        parameters = inspect.signature(selection.visible_challengers).parameters
        self.assertNotIn("outcomes", parameters)
        self.assertNotIn("coverage_rows", parameters)

    def test_snapshot_universe_is_evaluator_owned_and_requires_visible_blocks(self) -> None:
        complete = self.item(1)
        short = self.item(2, history=history_blocks(["profit"] * 4))
        fixture = self.fixture([complete, short])
        fixture["decision_cutoffs"]["patient"] = selection._iso(
            complete["history"]["1h"][-1]["timestamp"]
        )

        selected = selection.snapshot_universe(fixture, "patient", self.contract)

        self.assertEqual(selected, [1])
        self.assertNotIn(
            "planned_orders",
            inspect.signature(selection.snapshot_universe).parameters,
        )

    def test_structural_output_section_controls_simulation_lane(self) -> None:
        executable = {
            "id": 1,
            "action": "buy",
            "qty": 2,
            "price": 100,
            "sell_target": 120,
            "expected_profit": 36,
        }
        honest = runner._orders({"buys": [{**executable, "strategy": "patient"}]})[0]
        spoofed = runner._orders({"buys": [{
            **executable,
            "strategy": "active-margin",
            "bucket": "time",
            "reason": "spoof",
        }]})[0]

        self.assertEqual(
            selection.normalize_order(honest, self.contract),
            selection.normalize_order(spoofed, self.contract),
        )
        self.assertEqual(
            selection.normalize_order(honest, self.contract)["lane"],
            "patient",
        )

    def test_chronological_simulation_tracks_partial_inventory_bucket_by_bucket(self) -> None:
        order = self.order(quantity=10)
        rows = [
            bucket(START, low=100, high=110, low_volume=50, high_volume=0),
            bucket(START + STEP, low=100, high=120, low_volume=50, high_volume=50),
            bucket(START + 2 * STEP, low=110, high=120, low_volume=0, high_volume=100),
        ] + [
            bucket(START + index * STEP, low=110, high=110, low_volume=0, high_volume=0)
            for index in range(3, 16)
        ]

        result = selection.simulate_buckets(order, rows, self.contract)

        self.assertEqual(result["filled_qty"], 10)
        self.assertEqual(result["target_sold_qty"], 10)
        self.assertEqual(result["forced_exit_qty"], 0)
        self.assertEqual(
            [(row["bucket"], row["event"], row["quantity"]) for row in result["events"]],
            [
                (0, "entry_fill", 5),
                (1, "entry_fill", 5),
                (2, "target_sale", 10),
            ],
        )

    def test_overnight_patient_offer_cannot_be_managed_before_return(self) -> None:
        overnight = next(
            row for row in self.contract["attendance"]
            if row["name"] == "overnight"
        )
        executable = {
            "id": 1,
            "action": "buy",
            "qty": 10,
            "price": 100,
            "sell_target": 120,
        }
        row = runner._orders({"buys": [executable]})[0]
        order = selection.normalize_order(row, self.contract, overnight)
        rows = [
            bucket(START + index * STEP, low=100, high=120)
            for index in range(24)
        ]

        result = selection.simulate_buckets(order, rows, self.contract)

        self.assertEqual(order["management_after"], 12 * STEP)
        self.assertEqual(order["cancel_after"], 12 * STEP)
        self.assertEqual(order["hard_exit_after"], 24 * STEP)
        sales = [
            event for event in result["events"]
            if event["event"] == "target_sale"
        ]
        self.assertEqual(sales[0]["bucket"], 12)
        self.assertFalse(any(event["bucket"] < 12 for event in sales))

    def test_same_bucket_fill_cannot_use_same_bucket_target_capacity(self) -> None:
        order = self.order(quantity=10)
        rows = [
            bucket(START, low=100, high=120, low_volume=100, high_volume=100)
        ] + [
            bucket(START + index * STEP, low=110, high=110,
                   low_volume=0, high_volume=0)
            for index in range(1, 16)
        ]

        result = selection.simulate_buckets(order, rows, self.contract)

        self.assertEqual(result["filled_qty"], 10)
        self.assertEqual(result["target_sold_qty"], 0)
        self.assertEqual(result["forced_exit_qty"], 10)

    def test_no_touch_charges_posted_cash_until_cancellation(self) -> None:
        order = self.order(quantity=100)
        rows = [
            bucket(START + index * STEP, low=110, high=120)
            for index in range(16)
        ]

        result = selection.simulate_buckets(order, rows, self.contract)

        self.assertEqual(result["filled_qty"], 0)
        self.assertEqual(result["actual_profit_gp"], 0)
        self.assertEqual(result["capital_hours"], 40_000)

    def test_evaluator_tax_policy_is_immutable_and_honors_exemption(self) -> None:
        order = self.order(
            item_id=2347,
            buy_price=900,
            sell_target=1_000,
            name="Hammer",
        )
        rows = [
            bucket(START + index * STEP, low=900, high=1_000)
            for index in range(16)
        ]

        result = selection.simulate_buckets(order, rows, self.contract)

        self.assertEqual(result["actual_profit_gp"], 100)
        self.assertEqual(
            selection.evaluator_sale_tax(999, "taxed", 1_000, self.contract),
            20,
        )

    def test_replay_uses_nonoverlapping_blocks_and_distinct_episodes(self) -> None:
        vector = selection.replay_vector(
            self.order(),
            self.item(history=history_blocks(["profit"] * 3)),
            self.contract,
        )

        self.assertEqual(vector["evidence_count"], 3)
        self.assertEqual(vector["opportunity_episode_count"], 4)
        starts = sorted(vector["blocks"])
        self.assertEqual(starts[1] - starts[0], 16 * STEP)

    def test_cash_aware_frontier_has_no_eight_item_cap(self) -> None:
        items = []
        for item_id in range(1, 13):
            buy = item_id * 100
            high = buy + item_id * 30
            rows = [
                bucket(START + index * STEP, low=buy, high=high,
                       low_volume=10_000, high_volume=10_000)
                for index in range(80)
            ]
            items.append(self.item(item_id, history=rows, limit=1))
        fixture = self.fixture(items)
        visible = selection.visible_fixture(fixture, "patient", self.contract)

        frontier = selection.cash_aware_pareto_frontier(
            visible, "patient", 1_000_000,
            self.contract["attendance"][0], self.contract
        )

        self.assertGreater(len({row["item_id"] for row in frontier}), 8)
        self.assertTrue(all(row["posted_capital_gp"] <= 1_000_000 for row in frontier))

    def test_frontier_is_deterministic_and_cash_aware(self) -> None:
        visible = selection.visible_fixture(
            self.fixture([self.item()]), "patient", self.contract
        )

        low_cash = selection.cash_aware_pareto_frontier(
            visible, "patient", 99, self.contract["attendance"][0], self.contract
        )
        first = selection.cash_aware_pareto_frontier(
            visible, "patient", 10_000,
            self.contract["attendance"][0], self.contract
        )
        second = selection.cash_aware_pareto_frontier(
            visible, "patient", 10_000,
            self.contract["attendance"][0], self.contract
        )

        self.assertEqual(low_cash, [])
        self.assertEqual(first, second)

    def test_frontier_matches_exhaustive_generated_breakpoint_reduction(self) -> None:
        visible = selection.visible_fixture(
            self.fixture([self.item(limit=100)]), "patient", self.contract
        )
        attendance = self.contract["attendance"][0]
        generated = selection.generated_breakpoint_candidates(
            visible, "patient", 10_000, attendance, self.contract
        )
        exhaustive = [
            candidate
            for candidate in generated
            if not any(
                selection._dominates_candidate(other, candidate)
                for other in generated
                if other is not candidate
            )
        ]
        frontier = selection.cash_aware_pareto_frontier(
            visible, "patient", 10_000, attendance, self.contract
        )

        self.assertEqual(
            {selection.order_signature(row) for row in frontier},
            {selection.order_signature(row) for row in exhaustive},
        )
        self.assertGreater(
            len([
                row for row in frontier
                if row["item_id"] == 1 and row["lane"] == "patient"
            ]),
            1,
        )

    def test_coverage_is_keyed_by_fixture_item_lane_and_cutoff(self) -> None:
        fixture = self.fixture([self.item()])
        # This unit fixture only carries patient data; restrict the audit contract accordingly.
        contract = json.loads(json.dumps(self.contract))
        contract["decision_lanes"] = {"patient": contract["decision_lanes"]["patient"]}
        contract["decision_lanes"]["patient"]["order_lanes"] = ["patient"]
        contract["decision_lanes"]["patient"]["attendance"] = ["attended"]

        rows = selection.coverage_manifest(fixture, contract)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["covered"])
        self.assertEqual(
            selection.coverage_key(
                rows[0]["fixture"],
                rows[0]["item_id"],
                rows[0]["order_lane"],
                rows[0]["attendance"],
                rows[0]["decision_cutoff"],
            ),
            (
                "fixture", 1, "patient", "attended",
                fixture["decision_cutoffs"]["patient"],
            ),
        )

    def test_coverage_rejects_gap_immediately_after_cutoff(self) -> None:
        item = self.item()
        item["future"]["1h"][0]["timestamp"] += STEP
        fixture = self.fixture([item])
        contract = json.loads(json.dumps(self.contract))
        contract["decision_lanes"] = {"patient": contract["decision_lanes"]["patient"]}
        contract["decision_lanes"]["patient"]["order_lanes"] = ["patient"]
        contract["decision_lanes"]["patient"]["attendance"] = ["attended"]

        row = selection.coverage_manifest(fixture, contract)[0]

        self.assertFalse(row["covered"])
        self.assertIn("not_immediately_after_cutoff", row["reasons"])

    def test_coverage_horizon_is_attendance_specific(self) -> None:
        item = self.item()
        start = item["history"]["1h"][-1]["timestamp"] + STEP
        item["future"]["1h"] = [
            bucket(start + index * STEP) for index in range(24)
        ]
        contract = json.loads(json.dumps(self.contract))
        contract["decision_lanes"] = {
            "patient": contract["decision_lanes"]["patient"]
        }
        contract["decision_lanes"]["patient"]["order_lanes"] = ["patient"]

        rows = selection.coverage_manifest(self.fixture([item]), contract)
        required = {row["attendance"]: row["required_buckets"] for row in rows}

        self.assertEqual(
            required,
            {"attended": 16, "away_2h": 16, "overnight": 24},
        )
        self.assertTrue(all(row["coverage_scope"] == "admitted_item" for row in rows))

    def test_portfolio_feasibility_checks_full_cash_slots_duplicates_and_universe(self) -> None:
        item_map = {1: self.item(1), 2: self.item(2)}
        first = self.order(1, quantity=6)
        second = self.order(2, quantity=5)
        case = self.case(cash=1_000, slot_cap=1)

        result = selection.portfolio_feasibility(
            [first, second], case, item_map, self.contract
        )
        duplicate = selection.portfolio_feasibility(
            [first, {**second, "item_id": 1}], self.case(), item_map, self.contract
        )
        outside = selection.portfolio_feasibility(
            [self.order(3)], self.case(), item_map, self.contract
        )

        self.assertEqual(set(result["reasons"]), {"cash", "slots"})
        self.assertIn("duplicate_item", duplicate["reasons"])
        self.assertIn("outside_snapshot_universe", outside["reasons"])

    def test_feasibility_rejects_attendance_timing_spoof(self) -> None:
        order = self.order()
        case = self.case(orders=[order])
        spoofed = {**order, "management_after": STEP}

        result = selection.portfolio_feasibility(
            [spoofed], case, {1: self.item()}, self.contract
        )

        self.assertIn("attendance_semantics", result["reasons"])

    def test_positive_fractional_delta_below_materiality_does_not_qualify(self) -> None:
        current = self.order(quantity=1)
        alternative = self.order(quantity=2)
        score = {
            "non_overlapping_block_count": 3,
            "distinct_opportunity_episode_count": 3,
        }
        portfolio = {
            "position_risk_compliant": True,
            "portfolio_risk_compliant": True,
            "maximum_visible_portfolio_loss_gp": 0,
        }
        with (
            patch.object(selection, "_ensure_vector"),
            patch.object(
                selection,
                "_portfolio_rows",
                side_effect=[
                    {1: {"utility": Fraction()}},
                    {1: {"utility": Fraction(1, 2)}},
                ],
            ),
            patch.object(selection, "score_order", return_value=score),
            patch.object(selection, "portfolio_score", return_value=portfolio),
        ):
            metrics = selection._challenger_metrics(
                [current], [alternative], [alternative],
                {1: self.item()}, 10_000_000, self.contract, {},
            )

        self.assertTrue(metrics["positive_fractional_visible_delta"])
        self.assertFalse(metrics["qualifies"])
        self.assertEqual(
            metrics["selected_materiality"]["threshold_gp"], 500
        )

    def test_withheld_profit_never_turns_unsupported_action_into_visible_edge(self) -> None:
        item = self.item(
            history=history_blocks(["no_touch"] * 5),
            future=outcome_block("profit", START + 80 * STEP),
        )
        fixture = self.fixture([item])
        visible = selection.visible_fixture(fixture, "patient", self.contract)
        manual = self.order(buy_price=100, sell_target=120)
        case = self.case()
        frontier_score = selection.score_order(
            manual, visible["items"][0], 10_000, self.contract
        )
        frontier = [{
            **manual,
            "posted_capital_gp": 100,
            "frontier_score": frontier_score,
            "selected_breakpoint": {},
        }]
        coverage_contract = json.loads(json.dumps(self.contract))
        coverage_contract["decision_lanes"] = {
            "patient": coverage_contract["decision_lanes"]["patient"]
        }
        coverage_contract["decision_lanes"]["patient"]["order_lanes"] = ["patient"]
        coverage_contract["decision_lanes"]["patient"]["attendance"] = ["attended"]
        coverage = selection.coverage_manifest(fixture, coverage_contract)

        analysis = selection.analyze_case(
            case,
            frontier,
            visible,
            selection.withheld_items(fixture),
            coverage,
            self.contract,
        )
        addition = next(row for row in analysis["challengers"]
                        if row["kind"] == "pareto_addition")

        self.assertFalse(addition["qualifies"])
        self.assertGreater(
            addition["withheld_validation"]["raw_outcome_delta_gp"], 0
        )

    def test_visible_edge_can_fail_withheld_validation_without_leaking_back(self) -> None:
        item = self.item(
            history=history_blocks(["profit"] * 5),
            future=outcome_block("loss", START + 80 * STEP),
        )
        fixture = self.fixture([item])
        visible = selection.visible_fixture(fixture, "patient", self.contract)
        frontier = selection.cash_aware_pareto_frontier(
            visible, "patient", 10_000,
            self.contract["attendance"][0], self.contract
        )
        coverage_contract = json.loads(json.dumps(self.contract))
        coverage_contract["decision_lanes"] = {
            "patient": coverage_contract["decision_lanes"]["patient"]
        }
        coverage_contract["decision_lanes"]["patient"]["order_lanes"] = ["patient"]
        coverage_contract["decision_lanes"]["patient"]["attendance"] = ["attended"]
        coverage = selection.coverage_manifest(fixture, coverage_contract)

        analysis = selection.analyze_case(
            self.case(),
            frontier,
            visible,
            selection.withheld_items(fixture),
            coverage,
            self.contract,
        )
        qualifying = [row for row in analysis["challengers"] if row["qualifies"]]

        self.assertTrue(qualifying)
        self.assertTrue(all(
            row["withheld_validation"]["raw_outcome_delta_gp"] < 0
            for row in qualifying
        ))

    def test_observed_and_synthetic_gates_are_independent(self) -> None:
        def case(fixture_class: str, violations: list[dict]) -> dict:
            return {
                "fixture_class": fixture_class,
                "analysis": {
                    "violations": violations,
                    "challengers": [],
                    "withheld_validation": {
                        "status": "covered",
                        "orders": [],
                        "actual_utility_gp": 0,
                    },
                },
            }

        audits = [
            {"fixture_class": "observed", "passed": True},
            {"fixture_class": "synthetic", "passed": True},
        ]
        coverage = [
            {"fixture_class": "observed", "covered": True},
            {"fixture_class": "synthetic", "covered": True},
        ]
        summary = selection.summarize(
            [case("observed", [{"rule": "x"}]), case("synthetic", [])],
            audits,
            coverage,
            self.contract,
        )

        self.assertFalse(
            summary["by_fixture_class"]["observed"]["lane_local_gate_pass"]
        )
        self.assertTrue(
            summary["by_fixture_class"]["synthetic"]["lane_local_gate_pass"]
        )
        self.assertEqual(summary["scope"], "lane_local")
        self.assertFalse(summary["whole_planner_portfolio_optimality_assessed"])
        self.assertFalse(summary["cash_dominance_gate"])
        self.assertFalse(summary["utilization_gate"])

    def test_planner_execution_receives_only_lane_visible_shape(self) -> None:
        fixture = self.fixture([self.item()])
        visible = selection.visible_fixture(fixture, "patient", self.contract)
        seen = []
        original = runner._market_maps

        def inspect_fixture(value: dict) -> tuple:
            seen.append(value)
            return original(value)

        with (
            patch.object(runner, "_market_maps", side_effect=inspect_fixture),
            patch.object(runner.plan, "plan", return_value={}),
        ):
            runner._run_plan_visible(
                visible, 10_000, self.contract["attendance"][0], 1, "patient"
            )

        self.assertEqual(len(seen), 1)
        self.assertEqual(set(seen[0]["items"][0]["history"]), {"1h"})
        self.assertNotIn("future", seen[0]["items"][0])


if __name__ == "__main__":
    unittest.main()
