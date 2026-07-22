"""Compare a planner evaluation result with a frozen baseline."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def _violation_counts(result: dict) -> Counter:
    return Counter(row["rule"] for row in result.get("violations", []))


def compare(baseline: dict, candidate: dict, mode: str = "repair",
            minimum_utility_improvement_gp: int = 1) -> dict:
    blockers = []
    for key in ("contract_sha256", "evaluator_sha256", "oracle_sha256"):
        if baseline.get(key) != candidate.get(key):
            blockers.append(f"{key} differs; results are not comparable")
    base_fixtures = {
        (row["name"], row["source_sha256"]) for row in baseline.get("fixtures", [])
    }
    candidate_fixtures = {
        (row["name"], row["source_sha256"]) for row in candidate.get("fixtures", [])
    }
    if base_fixtures != candidate_fixtures:
        blockers.append("fixture set or hashes differ; results are not comparable")

    base_summary = baseline["summary"]
    candidate_summary = candidate["summary"]
    base_counts = _violation_counts(baseline)
    candidate_counts = _violation_counts(candidate)
    new_rules = sorted(set(candidate_counts) - set(base_counts))
    if new_rules:
        blockers.append(f"new violation classes: {', '.join(new_rules)}")
    if candidate_summary["worst_actual_profit_gp"] < base_summary["worst_actual_profit_gp"]:
        blockers.append("worst withheld-future profit regressed")
    if candidate_summary["aggregate_utility_gp"] < (
        base_summary["aggregate_utility_gp"] + minimum_utility_improvement_gp
    ):
        blockers.append("aggregate utility did not clear the required improvement")

    base_total = len(baseline.get("violations", []))
    candidate_total = len(candidate.get("violations", []))
    if mode == "repair":
        if candidate_total >= base_total:
            blockers.append("repair mode requires strictly fewer total violations")
    elif mode == "strict":
        if candidate_total:
            blockers.append("strict mode requires zero hard-invariant violations")
    else:
        raise ValueError("mode must be 'repair' or 'strict'")

    return {
        "accepted": not blockers,
        "mode": mode,
        "blockers": blockers,
        "baseline_revision": baseline.get("git_revision"),
        "candidate_revision": candidate.get("git_revision"),
        "baseline_violations": base_total,
        "candidate_violations": candidate_total,
        "violation_delta": candidate_total - base_total,
        "baseline_violation_classes": dict(sorted(base_counts.items())),
        "candidate_violation_classes": dict(sorted(candidate_counts.items())),
        "aggregate_utility_delta_gp": (
            candidate_summary["aggregate_utility_gp"]
            - base_summary["aggregate_utility_gp"]
        ),
        "worst_actual_profit_delta_gp": (
            candidate_summary["worst_actual_profit_gp"]
            - base_summary["worst_actual_profit_gp"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation.compare")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--mode", choices=("repair", "strict"), default="repair")
    parser.add_argument("--minimum-utility-improvement-gp", type=int, default=1)
    args = parser.parse_args()
    result = compare(
        json.loads(args.baseline.read_text()),
        json.loads(args.candidate.read_text()),
        args.mode,
        args.minimum_utility_improvement_gp,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
