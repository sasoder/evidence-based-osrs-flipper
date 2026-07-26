from __future__ import annotations

import json
import unittest
from pathlib import Path

from evaluation import compare, generate_fixtures, runner


ROOT = Path(__file__).resolve().parents[1]


class FixtureTests(unittest.TestCase):
    def test_synthetic_fixture_generation_is_deterministic(self) -> None:
        first = json.dumps(generate_fixtures.build(), sort_keys=True)
        second = json.dumps(generate_fixtures.build(), sort_keys=True)

        self.assertEqual(first, second)
        self.assertEqual(len(generate_fixtures.build()["fixtures"]), 5)

    def test_checked_in_real_corpus_has_disjoint_history_and_future(self) -> None:
        fixtures = [
            fixture
            for fixture in runner.load_fixtures(list(runner.DEFAULT_FIXTURES))
            if fixture["name"].startswith("wiki_cache_")
        ]

        self.assertEqual(
            {fixture["name"]: len(fixture["items"]) for fixture in fixtures},
            {
                "wiki_cache_2026-07-11": 1_745,
                "wiki_cache_2026-07-22": 351,
                "wiki_cache_2026-07-26": 524,
            },
        )
        for fixture in fixtures:
            self.assertTrue(fixture["as_of"])
            self.assertEqual(
                fixture["universe"]["cohort_items"],
                fixture["universe"]["eligible_items"],
            )
            self.assertEqual(fixture["universe"]["selection"], "all eligible items")
            for item in fixture["items"][:10]:
                for timestep in ("1h", "6h"):
                    history = item["history"][timestep]
                    future = item["future"][timestep]
                    self.assertTrue(history)
                    self.assertTrue(future)
                    self.assertLess(history[-1]["timestamp"], future[0]["timestamp"])

    def test_raw_fixture_run_selects_from_market_items(self) -> None:
        fixture = runner.load_fixtures([
            ROOT / "evaluation/fixtures/core_market.json"
        ])[0]
        contract = json.loads((ROOT / "evaluation/contract.json").read_text())
        result = runner._run_plan(
            fixture,
            cash=100_000_000,
            attendance=contract["attendance"][0],
            slot_cap=8,
            strategies="balanced",
        )

        available = {item["id"] for item in fixture["items"]}
        selected = {row["id"] for row in runner._orders(result)}
        self.assertTrue(selected)
        self.assertLessEqual(selected, available)

    def test_baseline_records_dynamic_selection_across_cash_and_time(self) -> None:
        baseline = json.loads(
            (ROOT / "evaluation/baselines/main.json").read_text()
        )
        observed = [
            case for case in baseline["cases"]
            if case["fixture"].startswith("wiki_cache_")
            and case["attendance"] == "attended"
            and case["slot_cap"] == 8
        ]
        portfolios_by_fixture = {}
        for case in observed:
            portfolios_by_fixture.setdefault(case["fixture"], set()).add(
                tuple(case["selected_item_ids"])
            )

        self.assertGreaterEqual(len(portfolios_by_fixture), 2)
        self.assertTrue(any(len(rows) > 1 for rows in portfolios_by_fixture.values()))
        self.assertGreater(baseline["summary"]["cash_selection_changes"], 0)


class SimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.loads((ROOT / "evaluation/contract.json").read_text())

    def test_no_touch_means_no_fill_and_escrow_cost(self) -> None:
        row = {
            "id": 1, "name": "No touch", "strategy": "patient-band",
            "qty": 100, "price": 100, "sell_target": 120, "expected_profit": 1_000,
        }
        item = {"future": {"1h": [
            {"avgLowPrice": 110, "avgHighPrice": 120,
             "lowPriceVolume": 1_000, "highPriceVolume": 1_000}
            for _ in range(16)
        ]}}

        result = runner._simulate_order(row, item, self.contract)

        self.assertEqual(result["filled_qty"], 0)
        self.assertEqual(result["actual_profit_gp"], 0)
        self.assertEqual(result["capital_hours"], 40_000)

    def test_forced_exit_uses_low_side_price_and_tax(self) -> None:
        row = {
            "id": 1, "name": "Falls", "strategy": "patient-band",
            "qty": 10, "price": 100, "sell_target": 130, "expected_profit": 200,
        }
        future = [{
            "avgLowPrice": 100, "avgHighPrice": 110,
            "lowPriceVolume": 100, "highPriceVolume": 100,
        }] + [{
            "avgLowPrice": 80, "avgHighPrice": 90,
            "lowPriceVolume": 100, "highPriceVolume": 100,
        } for _ in range(12)]

        result = runner._simulate_order(row, {"future": {"1h": future}}, self.contract)

        self.assertEqual(result["filled_qty"], 10)
        self.assertEqual(result["forced_exit_qty"], 10)
        self.assertLess(result["actual_profit_gp"], 0)

    def test_capital_efficiency_uses_posted_not_expected_fill_capital(self) -> None:
        case = {
            "cash_gp": 100_000_000, "slot_cap": 8, "away_hours": None,
            "orders": [{
                "id": 1, "strategy": "patient", "posted_capital_gp": 20_000_000,
                "expected_profit_gp": 13_000, "actual_profit_gp": 13_000,
            }],
        }

        violations = runner._case_violations(case, self.contract)

        self.assertIn("capital_efficiency", {row["rule"] for row in violations})

    def test_cash_dominance_compares_selection_outputs(self) -> None:
        base = {"fixture": "x", "attendance": "attended", "slot_cap": 8}
        cases = [
            {**base, "cash_gp": 70_000_000,
             "expected_profit_gp": 100_000, "actual_profit_gp": 80_000},
            {**base, "cash_gp": 100_000_000,
             "expected_profit_gp": 90_000, "actual_profit_gp": 70_000},
        ]

        violations = runner._dominance_violations(cases, self.contract)

        self.assertEqual(len(violations), 2)


class ComparisonTests(unittest.TestCase):
    def _result(self, violations: list[dict], utility: int = 100,
                worst: int = -10) -> dict:
        return {
            "contract_sha256": "contract", "evaluator_sha256": "runner",
            "oracle_sha256": "oracle",
            "git_revision": "x",
            "fixtures": [{"name": "fixture", "source_sha256": "fixture-hash"}],
            "violations": violations,
            "summary": {
                "aggregate_utility_gp": utility,
                "worst_actual_profit_gp": worst,
            },
        }

    def test_repair_accepts_strictly_fewer_violations_without_regression(self) -> None:
        baseline = self._result([{"rule": "capital_efficiency"}, {"rule": "position_loss"}])
        candidate = self._result([{"rule": "capital_efficiency"}], utility=110, worst=-5)

        result = compare.compare(baseline, candidate, mode="repair")

        self.assertTrue(result["accepted"])

    def test_repair_rejects_oracle_changes(self) -> None:
        baseline = self._result([{"rule": "capital_efficiency"}])
        candidate = self._result([], utility=110, worst=-5)
        candidate["evaluator_sha256"] = "changed"

        result = compare.compare(baseline, candidate, mode="repair")

        self.assertFalse(result["accepted"])
        self.assertTrue(any("not comparable" in row for row in result["blockers"]))

    def test_repair_requires_utility_to_improve(self) -> None:
        baseline = self._result(
            [{"rule": "capital_efficiency"}, {"rule": "position_loss"}]
        )
        candidate = self._result([{"rule": "capital_efficiency"}])

        result = compare.compare(baseline, candidate, mode="repair")

        self.assertFalse(result["accepted"])
        self.assertTrue(any("utility" in row for row in result["blockers"]))


if __name__ == "__main__":
    unittest.main()
