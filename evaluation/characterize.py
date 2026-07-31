"""Run the current evaluator against planner code from another Git worktree."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from copy import deepcopy
from pathlib import Path


COMPACT_SUMMARY_KEYS = (
    "cases",
    "nonempty_cases",
    "fixtures",
    "synthetic_fixtures",
    "observed_fixtures",
    "mean_actual_profit_gp",
    "median_actual_profit_gp",
    "aggregate_actual_utility_gp",
    "visible_violation_count",
    "qualifying_challenger_cases",
    "qualifying_challenger_cases_by_materiality",
    "observed_lane_local_gate_pass",
    "synthetic_lane_local_gate_pass",
    "lane_local_gates_pass",
    "effective_decision_counts",
)


def baseline_projection(result: dict) -> dict:
    """Return the deterministic, comparison-complete baseline projection."""
    return {
        "schema_version": result["schema_version"],
        "evaluator_status": result["evaluator_status"],
        "planner_revision": result["planner_revision"],
        "contract_sha256": result["contract_sha256"],
        "evaluator_sha256": result["evaluator_sha256"],
        "oracle_sha256": result["oracle_sha256"],
        "fixtures": [
            {
                "name": row["name"],
                "fixture_class": row["fixture_class"],
                "source": row["source"],
                "source_sha256": row["source_sha256"],
            }
            for row in sorted(
                result["fixtures"],
                key=lambda row: (row["name"], row["source_sha256"]),
            )
        ],
        "coverage_manifest_sha256": result["coverage_manifest_sha256"],
        "contract_notes": deepcopy(result["contract_notes"]),
        "gate": deepcopy(result["gate"]),
        "summary": {
            key: deepcopy(result["summary"][key])
            for key in COMPACT_SUMMARY_KEYS
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation.characterize")
    parser.add_argument("--planner-root", type=Path, required=True)
    parser.add_argument("--planner-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--baseline-output",
        type=Path,
        help="also write a compact deterministic reproducibility baseline",
    )
    args = parser.parse_args()

    planner_root = str(args.planner_root.resolve())
    sys.path.insert(0, planner_root)
    importlib.import_module("flipper")
    sys.path.remove(planner_root)

    # Import only after the alternate flipper package is pinned in sys.modules.
    from evaluation import runner

    result = runner.evaluate()
    result["planner_revision"] = args.planner_revision
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.baseline_output:
        args.baseline_output.parent.mkdir(parents=True, exist_ok=True)
        args.baseline_output.write_text(
            json.dumps(baseline_projection(result), indent=2, sort_keys=True)
            + "\n"
        )
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
