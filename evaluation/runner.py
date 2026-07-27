"""Run the frozen planner evaluation contract against immutable market fixtures."""

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
    ROOT / "fixtures/real_market.json.gz",
    ROOT / "fixtures/real_market_2026-07-26.json.gz",
)
PLAN_SECTIONS = ("buys", "patient_probes", "active_buys", "time_buys")


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
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_fixtures(paths: list[Path]) -> list[dict]:
    fixtures = []
    for path in paths:
        if not path.exists():
            continue
        payload = _read_json(path)
        for fixture in payload.get("fixtures", []):
            fixtures.append({**fixture, "source": path.name, "source_sha256": _sha256(path)})
    if not fixtures:
        raise ValueError("no evaluation fixtures found")
    names = [fixture["name"] for fixture in fixtures]
    if len(names) != len(set(names)):
        raise ValueError("fixture names must be unique across sources")
    return fixtures


def _market_maps(fixture: dict) -> tuple[dict, dict, dict, dict]:
    items = {int(item["id"]): item for item in fixture["items"]}
    mapping = {
        iid: {
            "id": iid,
            "name": item["name"],
            "members": item.get("members", True),
            "limit": item.get("limit"),
        }
        for iid, item in items.items()
    }
    latest = {str(iid): item["latest"] for iid, item in items.items()}
    one_hour = {str(iid): item["one_hour"] for iid, item in items.items()}
    return items, mapping, latest, one_hour


def _run_plan(fixture: dict, cash: int, attendance: dict, slot_cap: int,
              strategies: str) -> dict:
    fixture = selection_contract.visible_fixture(fixture)
    items, mapping, latest, one_hour = _market_maps(fixture)
    FrozenDateTime.instant = datetime.fromisoformat(fixture["as_of"].replace("Z", "+00:00"))

    def timeseries(item_id: int, timestep: str) -> list[dict]:
        return list((items.get(int(item_id)) or {}).get("history", {}).get(timestep, []))

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


def _strategy(row: dict) -> str:
    return selection_contract.strategy(row)


def _simulate_order(row: dict, item: dict, contract: dict) -> dict:
    lane = contract["simulation"][_strategy(row)]
    future = list(item.get("future", {}).get(lane["timestep"], []))
    result = selection_contract.simulate_buckets(row, future, contract)
    result["capital_hours"] = float(result["capital_hours"])
    return result


def _orders(plan_result: dict) -> list[dict]:
    return [row for section in PLAN_SECTIONS for row in plan_result.get(section, [])]


def _case_violations(case: dict, contract: dict) -> list[dict]:
    violations = []
    cash = case["cash_gp"]
    orders = case["orders"]
    total_capital = sum(order["posted_capital_gp"] for order in orders)
    if total_capital > cash:
        violations.append({"rule": "budget", "actual": total_capital, "limit": cash})
    if len(orders) > case["slot_cap"]:
        violations.append({"rule": "slot_cap", "actual": len(orders), "limit": case["slot_cap"]})
    if case["away_hours"] is not None and case["away_hours"] >= 0.5:
        active = [order["id"] for order in orders if order["strategy"] == "active-margin"]
        if active:
            violations.append({"rule": "unattended_active", "items": active})

    min_return = contract["minimum_expected_return_per_posted_capital_hour"]
    max_position_loss = cash * contract["maximum_position_loss_pct"]
    total_loss = 0
    for order in orders:
        if order["expected_profit_gp"] <= 0:
            violations.append({"rule": "positive_expected_profit", "item": order["id"]})
        lane = contract["simulation"][order["strategy"]]
        denominator = order["posted_capital_gp"] * lane["hold_hours"]
        expected_return = order["expected_profit_gp"] / denominator if denominator else 0
        order["expected_return_per_posted_capital_hour"] = round(expected_return, 8)
        if expected_return < min_return:
            violations.append({
                "rule": "capital_efficiency", "item": order["id"],
                "actual": round(expected_return, 8), "minimum": min_return,
            })
        loss = max(0, -order["actual_profit_gp"])
        total_loss += loss
        if loss > max_position_loss:
            violations.append({
                "rule": "position_loss", "item": order["id"],
                "actual": loss, "limit": round(max_position_loss),
            })
    max_portfolio_loss = cash * contract["maximum_portfolio_loss_pct"]
    if total_loss > max_portfolio_loss:
        violations.append({
            "rule": "portfolio_loss", "actual": total_loss,
            "limit": round(max_portfolio_loss),
        })
    return violations


def _dominance_violations(cases: list[dict], contract: dict) -> list[dict]:
    tolerance = contract["dominance_tolerance_gp"]
    groups = {}
    for case in cases:
        key = (case["fixture"], case["attendance"], case["slot_cap"])
        groups.setdefault(key, []).append(case)
    violations = []
    for key, rows in groups.items():
        ordered = sorted(rows, key=lambda row: row["cash_gp"])
        for prior, current in zip(ordered, ordered[1:]):
            for metric in ("expected_profit_gp", "actual_profit_gp"):
                if current[metric] + tolerance < prior[metric]:
                    violations.append({
                        "rule": f"cash_dominance_{metric}",
                        "fixture": key[0], "attendance": key[1], "slot_cap": key[2],
                        "lower_cash": prior["cash_gp"], "higher_cash": current["cash_gp"],
                        "lower_value": prior[metric], "higher_value": current[metric],
                    })
    return violations


def evaluate(contract_path: Path = DEFAULT_CONTRACT,
             fixture_paths: list[Path] | None = None) -> dict:
    started = time.perf_counter()
    contract = _read_json(contract_path)
    paths = fixture_paths or list(DEFAULT_FIXTURES)
    fixtures = load_fixtures(paths)
    visible_fixtures = {
        fixture["name"]: selection_contract.visible_fixture(fixture)
        for fixture in fixtures
    }
    outcomes = {
        fixture["name"]: selection_contract.withheld_items(fixture)
        for fixture in fixtures
    }
    coverage_rows = [
        row
        for fixture in fixtures
        for row in selection_contract.coverage_manifest(fixture, contract)
    ]
    cases = []
    planned_by_fixture: dict[str, list[dict]] = {}
    for fixture in fixtures:
        visible = visible_fixtures[fixture["name"]]
        item_map = {int(item["id"]): item for item in fixture["items"]}
        for attendance in contract["attendance"]:
            for slot_cap in contract["slot_caps"]:
                for cash in contract["bankrolls_gp"]:
                    result = _run_plan(
                        visible, cash, attendance, slot_cap, contract["strategies"])
                    planner_orders = _orders(result)
                    planned_by_fixture.setdefault(fixture["name"], []).extend(planner_orders)
                    normalized = [
                        selection_contract.normalize_order(row, contract)
                        for row in planner_orders
                    ]
                    simulations = []
                    for row, order in zip(planner_orders, normalized):
                        simulation = _simulate_order(
                            row, item_map[int(row["id"])], contract
                        )
                        simulation["executable_order"] = (
                            selection_contract.executable_order(order)
                        )
                        simulations.append(simulation)
                    expected_profit = sum(row["expected_profit_gp"] for row in simulations)
                    actual_profit = sum(row["actual_profit_gp"] for row in simulations)
                    capital_hours = sum(row["capital_hours"] for row in simulations)
                    case = {
                        "fixture": fixture["name"], "source": fixture["source"],
                        "seed": fixture.get("seed"), "regime": fixture.get("regime"),
                        "attendance": attendance["name"], "away_hours": attendance["away_hours"],
                        "slot_cap": slot_cap, "cash_gp": cash,
                        "selected_item_ids": [row["id"] for row in simulations],
                        "selected_items": [row["name"] for row in simulations],
                        "normalized_orders": normalized,
                        "orders": simulations,
                        "planned_capital_gp": sum(row["posted_capital_gp"] for row in simulations),
                        "expected_profit_gp": expected_profit,
                        "actual_profit_gp": actual_profit,
                        "capital_hours": capital_hours,
                        "utility_gp": round(
                            actual_profit
                            - capital_hours
                            * contract["minimum_expected_return_per_posted_capital_hour"]
                        ),
                    }
                    case["violations"] = _case_violations(case, contract)
                    cases.append(case)

    frontiers = {
        fixture["name"]: selection_contract.canonical_frontier(
            visible_fixtures[fixture["name"]],
            planned_by_fixture.get(fixture["name"], []),
            contract,
        )
        for fixture in fixtures
    }
    selection_groups: dict[tuple, list[dict]] = {}
    for case in cases:
        key = (case["fixture"], case["attendance"], case["slot_cap"])
        selection_groups.setdefault(key, []).append(case)
    replay_caches = {fixture["name"]: {} for fixture in fixtures}
    for rows in selection_groups.values():
        prior = None
        for case in sorted(rows, key=lambda row: row["cash_gp"]):
            case["selection_characterization"] = selection_contract.analyze_case(
                case,
                frontiers[case["fixture"]],
                visible_fixtures[case["fixture"]],
                outcomes[case["fixture"]],
                coverage_rows,
                contract,
                prior_case=prior,
                replay_cache=replay_caches[case["fixture"]],
            )
            prior = case

    dominance = _dominance_violations(cases, contract)
    case_violations = [
        {"fixture": case["fixture"], "attendance": case["attendance"],
         "slot_cap": case["slot_cap"], "cash_gp": case["cash_gp"], **violation}
        for case in cases for violation in case["violations"]
    ]
    unique_portfolios = len({
        (case["fixture"], case["attendance"], case["slot_cap"],
         tuple(sorted(case["selected_item_ids"])))
        for case in cases
    })
    selection_transitions = 0
    changed_selections = 0
    for rows in selection_groups.values():
        ordered = sorted(rows, key=lambda row: row["cash_gp"])
        for prior, current in zip(ordered, ordered[1:]):
            selection_transitions += 1
            changed_selections += (
                set(prior["selected_item_ids"]) != set(current["selected_item_ids"])
            )
    nonempty = [case for case in cases if case["orders"]]
    portfolios_by_fixture = {
        fixture["name"]: len({
            (case["attendance"], case["slot_cap"],
             tuple(sorted(case["selected_item_ids"])))
            for case in cases if case["fixture"] == fixture["name"]
        })
        for fixture in fixtures
    }
    selection_summary = selection_contract.summarize(cases, frontiers)
    elapsed = time.perf_counter() - started
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_revision": _git_revision(),
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
            ROOT.parent / "tests/test_evaluation.py",
            ROOT.parent / "tests/test_selection_contract.py",
        ]),
        "fixtures": [
            {"name": fixture["name"], "source": fixture["source"],
             "source_sha256": fixture["source_sha256"], "items": len(fixture["items"]),
             "as_of": fixture["as_of"], "universe": fixture.get("universe")}
            for fixture in fixtures
        ],
        "summary": {
            "cases": len(cases), "nonempty_cases": len(nonempty),
            "nonempty_rate": round(len(nonempty) / len(cases), 4) if cases else 0,
            "unique_selection_portfolios": unique_portfolios,
            "unique_selection_portfolios_by_fixture": portfolios_by_fixture,
            "cash_selection_transitions": selection_transitions,
            "cash_selection_changes": changed_selections,
            "cash_selection_change_rate": round(
                changed_selections / selection_transitions, 4
            ) if selection_transitions else 0,
            "mean_expected_profit_gp": round(mean(case["expected_profit_gp"] for case in cases)),
            "mean_actual_profit_gp": round(mean(case["actual_profit_gp"] for case in cases)),
            "median_actual_profit_gp": round(median(case["actual_profit_gp"] for case in cases)),
            "worst_actual_profit_gp": min(case["actual_profit_gp"] for case in cases),
            "aggregate_utility_gp": sum(case["utility_gp"] for case in cases),
            "case_violations": len(case_violations),
            "dominance_violations": len(dominance),
            "hard_invariants_pass": not case_violations and not dominance,
            "complete_evaluator_runtime_seconds": round(elapsed, 3),
        },
        "selection_contract_v2": {
            "status": "report_only_characterization",
            "frontier_policy":
                "deduplicated executable planner emissions per fixture/item/lane",
            "frontier_is_globally_optimal": False,
            "coverage_manifest": coverage_rows,
            "characterization": selection_summary,
            "uncertainty_calibration": selection_contract.uncertainty_report(
                list(visible_fixtures.values()), contract
            ),
            "tax_contract":
                "evaluator-v1 per-unit sale_tax retained pending exemption review",
            "sealed_holdout_boundary":
                "must be a separately permissioned service or principal whose outcome "
                "data is inaccessible to the planner agent; another readable directory "
                "is not sealed",
        },
        "violations": case_violations + dominance,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation.runner")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--fixtures", type=Path, nargs="*")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--enforce", action="store_true",
                        help="return nonzero when any hard invariant fails")
    args = parser.parse_args()
    result = evaluate(args.contract, args.fixtures)
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(json.dumps(result["summary"], indent=2))
    return 1 if args.enforce and not result["summary"]["hard_invariants_pass"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
