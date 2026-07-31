"""Compare a submission with the frozen lane-local V2 reproducibility baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def compare(baseline: dict, candidate: dict) -> dict:
    blockers = []
    for key in ("contract_sha256", "evaluator_sha256", "oracle_sha256"):
        if baseline.get(key) != candidate.get(key):
            blockers.append(f"{key} differs; results are not comparable")
    baseline_fixtures = {
        (row["name"], row["source_sha256"])
        for row in baseline.get("fixtures", [])
    }
    candidate_fixtures = {
        (row["name"], row["source_sha256"])
        for row in candidate.get("fixtures", [])
    }
    if baseline_fixtures != candidate_fixtures:
        blockers.append("fixture set or hashes differ; results are not comparable")
    if candidate.get("evaluator_status") != "frozen_v2":
        blockers.append("candidate was not produced by the frozen V2 runner")

    baseline_cohorts = baseline.get("gate", {}).get("by_fixture_class", {})
    candidate_cohorts = candidate.get("gate", {}).get("by_fixture_class", {})
    cohort_deltas = {}
    for fixture_class in ("observed", "synthetic"):
        before = baseline_cohorts.get(fixture_class, {})
        after = candidate_cohorts.get(fixture_class, {})
        if not after.get("lane_local_gate_pass", False):
            blockers.append(f"{fixture_class} lane-local v2 gate failed")
        cohort_deltas[fixture_class] = {
            "visible_violation_delta": (
                after.get("visible_violation_count", 0)
                - before.get("visible_violation_count", 0)
            ),
            "qualifying_challenger_case_delta": (
                after.get("qualifying_challenger_cases", 0)
                - before.get("qualifying_challenger_cases", 0)
            ),
            "aggregate_withheld_utility_delta_gp": (
                after.get("aggregate_withheld_utility_gp", 0)
                - before.get("aggregate_withheld_utility_gp", 0)
            ),
            "baseline_lane_local_gate_pass":
                before.get("lane_local_gate_pass", False),
            "candidate_lane_local_gate_pass":
                after.get("lane_local_gate_pass", False),
        }

    return {
        "accepted": not blockers,
        "blockers": blockers,
        "baseline_revision": baseline.get("planner_revision"),
        "candidate_revision": candidate.get("planner_revision"),
        "cohort_deltas": cohort_deltas,
        "acceptance_depends_on_baseline_performance": False,
        "scope": "lane_local",
        "whole_planner_portfolio_optimality_assessed": False,
        "cash_dominance_gate": False,
        "utilization_gate": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation.compare")
    parser.add_argument(
        "baseline",
        type=Path,
        nargs="?",
        default=Path("evaluation/baselines/v2-main.json"),
    )
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    result = compare(
        json.loads(args.baseline.read_text()),
        json.loads(args.candidate.read_text()),
    )
    print(json.dumps(result, indent=2))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
