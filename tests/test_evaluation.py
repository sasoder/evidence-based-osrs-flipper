from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from evaluation import (
    characterize,
    compare,
    generate_fixtures,
    import_cache_fixture,
    runner,
)


ROOT = Path(__file__).resolve().parents[1]


class FixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.loads(
            (ROOT / "evaluation/contract.json").read_text()
        )

    def test_synthetic_fixture_generation_is_deterministic_v2(self) -> None:
        first = json.dumps(generate_fixtures.build(), sort_keys=True)
        second = json.dumps(generate_fixtures.build(), sort_keys=True)

        self.assertEqual(first, second)
        payload = generate_fixtures.build()
        self.assertEqual(payload["version"], 2)
        self.assertEqual(len(payload["fixtures"]), 5)
        self.assertEqual(
            set(payload["fixtures"][0]["decision_cutoffs"]),
            {"patient", "active", "time"},
        )

    def test_active_defaults_exclude_legacy_observed_fixtures(self) -> None:
        self.assertEqual(
            {path.name for path in runner.DEFAULT_FIXTURES},
            {
                "core_market.json",
                "observed_2026-07-27.json.gz",
                "observed_2026-07-29.json.gz",
            },
        )
        self.assertNotIn("real_market.json.gz", {
            path.name for path in runner.DEFAULT_FIXTURES
        })

    def test_imported_archives_pass_provenance_and_lane_coverage(self) -> None:
        fixtures = runner.load_fixtures(list(runner.DEFAULT_FIXTURES))
        observed = [
            fixture for fixture in fixtures
            if fixture["fixture_class"] == "observed"
        ]

        self.assertEqual(
            {fixture["name"] for fixture in observed},
            {"wiki_archive_2026-07-27", "wiki_archive_2026-07-29"},
        )
        for fixture in observed:
            audit = runner.selection_contract.fixture_audit(
                fixture, self.contract
            )
            coverage = runner.selection_contract.coverage_manifest(
                fixture, self.contract
            )
            self.assertTrue(audit["passed"], audit)
            self.assertTrue(all(row["covered"] for row in coverage))
            self.assertTrue(
                fixture["source_provenance"]["integrity_verified"]
            )
            for lane, policy in self.contract["decision_lanes"].items():
                self.assertGreaterEqual(
                    audit["snapshot_universe_items_by_lane"][lane],
                    policy["minimum_observed_snapshot_items"],
                )
            self.assertEqual(
                audit["snapshot_universe_items_by_lane"]["active"],
                5,
                "the active-lane observed universe is only five admitted items "
                "per date",
            )

    def test_archive_verification_rejects_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            payload = cache / "one.json"
            payload.write_text("{}")
            digest = hashlib.sha256(payload.read_bytes()).hexdigest()
            (root / "SHA256SUMS").write_text(f"{digest}  cache/one.json\n")

            verified = import_cache_fixture.verify_archive(root)
            payload.write_text('{"changed":true}')

            self.assertTrue(verified["integrity_verified"])
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                import_cache_fixture.verify_archive(root)

    def test_plan_sections_are_filtered_by_decision_lane(self) -> None:
        result = {
            section: [{"id": index, "qty": 1, "price": 1, "sell_target": 2}]
            for index, section in enumerate(runner.PLAN_SECTIONS, 1)
        }

        patient = runner._orders(result, "patient", self.contract)
        active = runner._orders(result, "active", self.contract)
        time = runner._orders(result, "time", self.contract)

        self.assertEqual(
            {row["_evaluator_lane"] for row in patient},
            {"patient", "patient-probe"},
        )
        self.assertEqual(
            {row["_evaluator_lane"] for row in active},
            {"active-margin"},
        )
        self.assertEqual(
            {row["_evaluator_lane"] for row in time},
            {"time-of-day"},
        )


class ComparisonTests(unittest.TestCase):
    def result(self, observed: bool, synthetic: bool,
               observed_utility: int = 0) -> dict:
        cohorts = {
            "observed": {
                "lane_local_gate_pass": observed,
                "visible_violation_count": 0 if observed else 1,
                "qualifying_challenger_cases": 0,
                "aggregate_withheld_utility_gp": observed_utility,
            },
            "synthetic": {
                "lane_local_gate_pass": synthetic,
                "visible_violation_count": 0 if synthetic else 1,
                "qualifying_challenger_cases": 0,
                "aggregate_withheld_utility_gp": 0,
            },
        }
        return {
            "evaluator_status": "frozen_v2",
            "contract_sha256": "contract",
            "evaluator_sha256": "runner",
            "oracle_sha256": "oracle",
            "planner_revision": "revision",
            "fixtures": [{"name": "fixture", "source_sha256": "fixture-hash"}],
            "gate": {"by_fixture_class": cohorts},
        }

    def test_acceptance_uses_candidate_v2_gates_not_baseline_performance(self) -> None:
        baseline = self.result(False, False, observed_utility=1_000_000)
        candidate = self.result(True, True, observed_utility=1)

        result = compare.compare(baseline, candidate)

        self.assertTrue(result["accepted"])
        self.assertFalse(result["acceptance_depends_on_baseline_performance"])
        self.assertFalse(result["cash_dominance_gate"])
        self.assertFalse(result["utilization_gate"])

    def test_compact_baseline_compares_with_full_candidate(self) -> None:
        full_baseline = self.result(False, False, observed_utility=100)
        full_baseline.update({
            "schema_version": 2,
            "coverage_manifest_sha256": "coverage",
            "contract_notes": {"scope": "lane_local"},
            "summary": {
                key: index
                for index, key in enumerate(
                    characterize.COMPACT_SUMMARY_KEYS
                )
            },
            "cases": [{"large": "full evaluator output is not projected"}],
        })
        full_baseline["fixtures"][0].update({
            "fixture_class": "observed",
            "source": "fixture.json",
        })
        compact_baseline = characterize.baseline_projection(full_baseline)
        full_candidate = self.result(True, True, observed_utility=150)
        full_candidate["cases"] = [{"large": "candidate remains full"}]

        result = compare.compare(compact_baseline, full_candidate)

        self.assertTrue(result["accepted"])
        self.assertNotIn("cases", compact_baseline)
        self.assertEqual(
            result["cohort_deltas"]["observed"][
                "aggregate_withheld_utility_delta_gp"
            ],
            50,
        )

    def test_observed_and_synthetic_failures_are_separate_blockers(self) -> None:
        baseline = self.result(False, False)
        candidate = self.result(False, True)

        result = compare.compare(baseline, candidate)

        self.assertFalse(result["accepted"])
        self.assertIn("observed lane-local v2 gate failed", result["blockers"])
        self.assertNotIn("synthetic lane-local v2 gate failed", result["blockers"])

    def test_oracle_change_is_not_comparable(self) -> None:
        baseline = self.result(True, True)
        candidate = self.result(True, True)
        candidate["oracle_sha256"] = "changed"

        result = compare.compare(baseline, candidate)

        self.assertFalse(result["accepted"])
        self.assertTrue(any("not comparable" in row for row in result["blockers"]))


if __name__ == "__main__":
    unittest.main()
