"""Run the standalone evaluator-v2 contract against immutable market fixtures."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from unittest.mock import patch

from evaluation import selection_contract
from flipper import plan, prices, signals


ROOT = Path(__file__).resolve().parent
DEFAULT_CONTRACT = ROOT / "contract.json"
DEFAULT_FIXTURES = (
    ROOT / "fixtures/core_market.json",
    ROOT / "fixtures/observed_2026-07-27.json.gz",
    ROOT / "fixtures/observed_2026-07-29.json.gz",
)
PLAN_SECTIONS = {
    "buys": "patient",
    "patient_probes": "patient-probe",
    "active_buys": "active-margin",
    "time_buys": "time-of-day",
}


class FrozenDateTime(datetime):
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        value = cls.instant
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def _read_json(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as handle:
            return json.load(handle)
    return json.loads(path.read_text())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _combined_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(ROOT.parent)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _git_revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_fixtures(paths: list[Path]) -> list[dict]:
    fixtures = []
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise ValueError(f"required evaluator-v2 fixtures are missing: {', '.join(missing)}")
    for path in paths:
        payload = _read_json(path)
        if payload.get("version") != 2:
            raise ValueError(f"{path} is not an evaluator-v2 fixture archive")
        for fixture in payload.get("fixtures", []):
            fixtures.append({
                **fixture,
                "source": path.name,
                "source_sha256": _sha256(path),
            })
    if not fixtures:
        raise ValueError("no evaluator-v2 fixtures found")
    names = [fixture["name"] for fixture in fixtures]
    if len(names) != len(set(names)):
        raise ValueError("fixture names must be unique across sources")
    return fixtures


def _market_maps(fixture: dict) -> tuple[dict, dict, dict, dict]:
    items = {int(item["id"]): item for item in fixture["items"]}
    mapping = {
        item_id: {
            "id": item_id,
            "name": item["name"],
            "members": item.get("members", True),
            "limit": item.get("limit"),
        }
        for item_id, item in items.items()
    }
    latest = {str(item_id): item["latest"] for item_id, item in items.items()}
    one_hour = {str(item_id): item["one_hour"] for item_id, item in items.items()}
    return items, mapping, latest, one_hour


def _run_plan_visible(fixture: dict, cash: int, attendance: dict, slot_cap: int,
                      strategies: str) -> dict:
    items, mapping, latest, one_hour = _market_maps(fixture)
    FrozenDateTime.instant = datetime.fromisoformat(
        fixture["as_of"].replace("Z", "+00:00")
    )

    def timeseries(item_id: int, timestep: str) -> list[dict]:
        return list(
            (items.get(int(item_id)) or {}).get("history", {}).get(timestep, [])
        )

    with ExitStack() as stack:
        stack.enter_context(patch.object(prices, "mapping_by_id", return_value=mapping))
        stack.enter_context(patch.object(prices, "latest", return_value=latest))
        stack.enter_context(patch.object(prices, "one_hour", return_value=one_hour))
        stack.enter_context(patch.object(prices, "timeseries", side_effect=timeseries))
        stack.enter_context(patch.object(prices, "prefetch_timeseries", return_value=None))
        stack.enter_context(patch.object(plan, "_cost_basis", return_value={}))
        stack.enter_context(patch.object(plan, "_personal_execution_stats", return_value={}))
        stack.enter_context(patch.object(plan, "datetime", FrozenDateTime))
        stack.enter_context(patch.object(signals, "datetime", FrozenDateTime))
        return plan.plan(
            cash=cash,
            offers=[],
            seed_limit=0,
            candidate_limit=None,
            active_seed_limit=None,
            active_candidate_limit=100,
            time_seed_limit=0,
            time_candidate_limit=100,
            horizon=attendance["horizon"],
            away_hours=attendance["away_hours"],
            strategies=strategies,
            max_new_slots=slot_cap,
        )


def _run_plan(fixture: dict, decision_lane: str, cash: int, attendance: dict,
              slot_cap: int, strategies: str, contract: dict) -> dict:
    return _run_plan_visible(
        selection_contract.visible_fixture(fixture, decision_lane, contract),
        cash,
        attendance,
        slot_cap,
        strategies,
    )


def _orders(plan_result: dict, decision_lane: str | None = None,
            contract: dict | None = None) -> list[dict]:
    allowed = None
    if decision_lane is not None:
        if contract is None:
            raise ValueError("contract is required when filtering a decision lane")
        allowed = set(contract["decision_lanes"][decision_lane]["order_lanes"])
    return [
        {**row, "_evaluator_lane": lane}
        for section, lane in PLAN_SECTIONS.items()
        if allowed is None or lane in allowed
        for row in plan_result.get(section, [])
    ]


def evaluate(contract_path: Path = DEFAULT_CONTRACT,
             fixture_paths: list[Path] | None = None) -> dict:
    started = time.perf_counter()
    contract = _read_json(contract_path)
    if contract.get("version") != 2:
        raise ValueError("active evaluator requires contract version 2")
    paths = fixture_paths or list(DEFAULT_FIXTURES)
    fixtures = load_fixtures(paths)
    audits = [
        selection_contract.fixture_audit(fixture, contract)
        for fixture in fixtures
    ]
    coverage_rows = [
        row
        for fixture in fixtures
        for row in selection_contract.coverage_manifest(fixture, contract)
    ]
    visible_fixtures = {
        (fixture["name"], decision_lane):
            selection_contract.visible_fixture(fixture, decision_lane, contract)
        for fixture in fixtures
        for decision_lane in contract["decision_lanes"]
    }
    outcomes = {
        fixture["name"]: selection_contract.withheld_items(fixture)
        for fixture in fixtures
    }
    inputs_ready = time.perf_counter()

    cases = []
    replay_caches = {
        (fixture["name"], lane): {}
        for fixture in fixtures
        for lane in contract["decision_lanes"]
    }
    frontier_cache = {}
    for fixture in fixtures:
        for decision_lane, lane_contract in contract["decision_lanes"].items():
            visible = visible_fixtures[(fixture["name"], decision_lane)]
            item_map = {int(item["id"]): item for item in visible["items"]}
            attendances = [
                row for row in contract["attendance"]
                if row["name"] in lane_contract["attendance"]
            ]
            for attendance in attendances:
                for slot_cap in contract["slot_caps"]:
                    for cash in contract["bankrolls_gp"]:
                        plan_result = _run_plan_visible(
                            visible,
                            cash,
                            attendance,
                            slot_cap,
                            lane_contract["strategies"],
                        )
                        submitted = _orders(
                            plan_result, decision_lane, contract
                        )
                        normalized = [
                            {
                                **selection_contract.normalize_order(
                                    row, contract, attendance
                                ),
                                "name": item_map.get(int(row["id"]), {}).get(
                                    "name", str(row["id"])
                                ),
                            }
                            for row in submitted
                        ]
                        case = {
                            "fixture": fixture["name"],
                            "fixture_class": fixture["fixture_class"],
                            "source": fixture["source"],
                            "decision_lane": decision_lane,
                            "decision_cutoff": visible["as_of"],
                            "attendance": attendance["name"],
                            "away_hours": attendance["away_hours"],
                            "slot_cap": slot_cap,
                            "cash_gp": cash,
                            "normalized_orders": normalized,
                            "selected_item_ids": [
                                order["item_id"] for order in normalized
                            ],
                            "planned_capital_gp": sum(
                                order["quantity"] * order["buy_price"]
                                for order in normalized
                            ),
                            "planner_expected_profit_gp": sum(
                                order["expected_profit_gp"] for order in normalized
                            ),
                        }
                        frontier_key = (
                            fixture["name"], decision_lane, attendance["name"], cash
                        )
                        if frontier_key not in frontier_cache:
                            frontier_cache[frontier_key] = (
                                selection_contract.cash_aware_pareto_frontier(
                                    visible,
                                    decision_lane,
                                    cash,
                                    attendance,
                                    contract,
                                    replay_caches[(fixture["name"], decision_lane)],
                                )
                            )
                        case["analysis"] = selection_contract.analyze_case(
                            case,
                            frontier_cache[frontier_key],
                            visible,
                            outcomes[fixture["name"]],
                            coverage_rows,
                            contract,
                            replay_caches[(fixture["name"], decision_lane)],
                        )
                        cases.append(case)

    evaluated_ready = time.perf_counter()
    gate = selection_contract.summarize(cases, audits, coverage_rows, contract)
    validations = [
        case["analysis"]["withheld_validation"]
        for case in cases
        if case["analysis"]["withheld_validation"]["status"] == "covered"
    ]
    actual_profits = [row["actual_profit_gp"] for row in validations]
    actual_utilities = [row["actual_utility_gp"] for row in validations]
    effective_decision_counts = {
        fixture_class: {
            decision_lane: {
                "distinct_fixture_lane_snapshots": len({
                    case["fixture"]
                    for case in cases
                    if (
                        case["fixture_class"] == fixture_class
                        and case["decision_lane"] == decision_lane
                    )
                }),
                "matrix_case_evaluations": sum(
                    case["fixture_class"] == fixture_class
                    and case["decision_lane"] == decision_lane
                    for case in cases
                ),
                "admitted_items_by_fixture": {
                    audit["fixture"]:
                        audit["snapshot_universe_items_by_lane"][decision_lane]
                    for audit in audits
                    if audit["fixture_class"] == fixture_class
                },
            }
            for decision_lane in contract["decision_lanes"]
        }
        for fixture_class in contract["fixture_classes"]
    }
    threshold_names = [
        row["name"] for row in contract["challenger_materiality"]["candidates"]
    ]
    elapsed = time.perf_counter() - started
    summary = {
        "cases": len(cases),
        "nonempty_cases": sum(bool(case["normalized_orders"]) for case in cases),
        "fixtures": len(fixtures),
        "synthetic_fixtures": sum(
            fixture["fixture_class"] == "synthetic" for fixture in fixtures
        ),
        "observed_fixtures": sum(
            fixture["fixture_class"] == "observed" for fixture in fixtures
        ),
        "mean_actual_profit_gp": round(mean(actual_profits)) if actual_profits else 0,
        "median_actual_profit_gp": round(median(actual_profits)) if actual_profits else 0,
        "aggregate_actual_utility_gp": sum(actual_utilities),
        "visible_violation_count": sum(
            len(case["analysis"]["violations"]) for case in cases
        ),
        "qualifying_challenger_cases": sum(
            any(row["qualifies"] for row in case["analysis"]["challengers"])
            for case in cases
        ),
        "qualifying_challenger_cases_by_materiality": {
            name: sum(
                any(
                    row["qualifies_by_materiality"][name]
                    for row in case["analysis"]["challengers"]
                )
                for case in cases
            )
            for name in threshold_names
        },
        "observed_lane_local_gate_pass":
            gate["by_fixture_class"]["observed"]["lane_local_gate_pass"],
        "synthetic_lane_local_gate_pass":
            gate["by_fixture_class"]["synthetic"]["lane_local_gate_pass"],
        "lane_local_gates_pass": gate["lane_local_overall_pass"],
        "effective_decision_counts": effective_decision_counts,
        "complete_evaluator_runtime_seconds": round(elapsed, 3),
        "runtime_breakdown_seconds": {
            "input_and_corpus_audit": round(inputs_ready - started, 3),
            "planner_scoring_and_validation": round(evaluated_ready - inputs_ready, 3),
            "result_summary": round(time.perf_counter() - evaluated_ready, 3),
        },
    }
    return {
        "schema_version": 2,
        "evaluator_status": "frozen_v2",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "planner_revision": _git_revision(),
        "evaluator_revision": _git_revision(),
        "contract": str(contract_path.relative_to(ROOT.parent)),
        "contract_sha256": _sha256(contract_path),
        "evaluator_sha256": _combined_sha256([
            Path(__file__),
            ROOT / "selection_contract.py",
        ]),
        "oracle_sha256": _combined_sha256([
            contract_path,
            Path(__file__),
            ROOT / "selection_contract.py",
            ROOT / "compare.py",
            ROOT / "generate_fixtures.py",
            ROOT / "import_cache_fixture.py",
            ROOT.parent / "tests/test_evaluation.py",
            ROOT.parent / "tests/test_selection_contract.py",
        ]),
        "fixtures": [
            {
                "name": fixture["name"],
                "fixture_class": fixture["fixture_class"],
                "source": fixture["source"],
                "source_sha256": fixture["source_sha256"],
                "items": len(fixture["items"]),
                "decision_cutoffs": fixture["decision_cutoffs"],
                "source_provenance": fixture["source_provenance"],
            }
            for fixture in fixtures
        ],
        "fixture_audits": audits,
        "coverage_manifest": coverage_rows,
        "coverage_manifest_sha256":
            selection_contract.coverage_manifest_sha256(coverage_rows),
        "contract_notes": {
            "challenger_construction_inputs": "visible_history_only",
            "withheld_outcomes_role": "validation_only",
            "frontier_policy": "deterministic_cash_aware_pareto_no_cardinality_cap",
            "execution_policy": "chronological_bucket_inventory_simulation",
            "attendance_policy":
                "orders_cannot_be_cancelled_replaced_or_managed_before_return",
            "scope": "lane_local_shared_cash_and_slots_within_each_lane_only",
            "whole_planner_portfolio_optimality_assessed": False,
            "coverage_scope": "admitted_item",
            "selected_materiality": contract["challenger_materiality"],
            "cash_dominance_gate": False,
            "utilization_gate": False,
            "legacy_baseline_dependency": False,
        },
        "gate": gate,
        "summary": summary,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation.runner")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--fixtures", type=Path, nargs="*")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="return nonzero unless observed and synthetic v2 gates both pass",
    )
    args = parser.parse_args()
    result = evaluate(args.contract, args.fixtures)
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(json.dumps(result["summary"], indent=2))
    return 1 if args.enforce and not result["summary"]["lane_local_gates_pass"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
