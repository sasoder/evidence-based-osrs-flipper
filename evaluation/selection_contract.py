"""Report-only selection characterization over a bounded visible-data frontier."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from fractions import Fraction

from flipper import ge_tax


STEP_SECONDS = {"5m": 300, "1h": 3_600, "6h": 21_600}
EXECUTABLE_FIELDS = (
    "item_id", "side", "quantity", "buy_price", "sell_target",
    "cancel_after", "hard_exit_after",
)
FRONTIER_ITEMS_PER_LANE = 8
REQUIRED_BUCKET_FIELDS = {
    "timestamp", "avgLowPrice", "avgHighPrice", "lowPriceVolume", "highPriceVolume",
}


def visible_fixture(fixture: dict) -> dict:
    """Return the exact planner/oracle-visible projection of a fixture."""
    visible_items = []
    for item in fixture["items"]:
        visible_items.append({
            key: item[key]
            for key in ("id", "name", "members", "limit", "latest", "one_hour", "history")
            if key in item
        })
    visible = {
        key: fixture[key]
        for key in ("name", "as_of", "seed", "regime", "source", "source_sha256")
        if key in fixture
    }
    visible["items"] = visible_items
    assert_visible(visible)
    return visible


def assert_visible(fixture: dict) -> None:
    def reject_metadata(value) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                lowered = key.lower()
                if "coverage" in lowered or lowered == "future" or lowered.startswith("future_"):
                    raise ValueError(f"withheld metadata reached visible input: {key}")
                reject_metadata(nested)
        elif isinstance(value, list):
            for nested in value:
                reject_metadata(nested)

    reject_metadata(fixture)


def withheld_items(fixture: dict) -> dict[int, dict]:
    """Project outcome data without copying it into any visible planner structure."""
    return {
        int(item["id"]): {"future": item.get("future", {})}
        for item in fixture["items"]
    }


def normalize_order(row: dict, contract: dict) -> dict:
    lane = row["_evaluator_lane"]
    policy = contract["simulation"][lane]
    order = {
        "item_id": int(row["id"]),
        "side": row.get("action") or "buy",
        "quantity": int(row["qty"]),
        "buy_price": int(row["price"]),
        "sell_target": int(row["sell_target"]),
        "cancel_after": round(float(policy["entry_hours"]) * 3_600),
        "hard_exit_after": round(float(policy["hold_hours"]) * 3_600),
    }
    return {
        **order,
        "lane": lane,
        "name": str(row["id"]),
        "expected_profit_gp": int(row.get("expected_profit") or 0),
    }


def executable_order(order: dict) -> dict:
    return {field: order[field] for field in EXECUTABLE_FIELDS}


def order_signature(order: dict) -> tuple:
    return tuple(order[field] for field in EXECUTABLE_FIELDS)


def action_signature(order: dict) -> tuple:
    return tuple(order[field] for field in EXECUTABLE_FIELDS if field != "quantity")


def _non_null_price(rows: list[dict], key: str, fallback: int) -> int:
    return next((int(row[key]) for row in reversed(rows) if row.get(key)), fallback)


def simulate_buckets(order: dict, buckets: list[dict], contract: dict) -> dict:
    """Simulate one executable order against a supplied, already-separated bucket vector."""
    lane_name = order["lane"]
    lane = contract["simulation"][lane_name]
    timestep = lane["timestep"]
    step_hours = Fraction(STEP_SECONDS[timestep], 3_600)
    entry_points = max(1, math.ceil(lane["entry_hours"] / float(step_hours)))
    hold_points = max(1, math.ceil(lane["hold_hours"] / float(step_hours)))
    qty = order["quantity"]
    buy = order["buy_price"]
    target = order["sell_target"]
    participation = Fraction(str(lane["participation_rate"]))

    entry_rows = buckets[:entry_points]
    entry_touches = [
        (index, bucket)
        for index, bucket in enumerate(entry_rows)
        if bucket.get("avgLowPrice") and bucket["avgLowPrice"] <= buy
    ]
    entry_capacity = sum(
        max(0, math.floor((bucket.get("lowPriceVolume") or 0) * participation))
        for _, bucket in entry_touches
    )
    filled = min(qty, entry_capacity)
    posted_capital = qty * buy
    expected_profit = order.get("expected_profit_gp", 0)
    if not filled:
        capital_hours = Fraction(posted_capital) * Fraction(str(lane["entry_hours"]))
        return {
            "id": order["item_id"], "name": order["name"], "strategy": lane_name,
            "posted_capital_gp": posted_capital, "expected_profit_gp": expected_profit,
            "filled_qty": 0, "target_sold_qty": 0, "forced_exit_qty": 0,
            "actual_profit_gp": 0, "capital_hours": capital_hours,
            "fill_rate": 0.0, "entry_capacity": entry_capacity, "target_capacity": 0,
        }

    entry_index = entry_touches[0][0]
    exit_rows = buckets[entry_index + 1: entry_index + 1 + hold_points]
    target_rows = [
        bucket for bucket in exit_rows
        if bucket.get("avgHighPrice") and bucket["avgHighPrice"] >= target
    ]
    target_capacity = sum(
        max(0, math.floor((bucket.get("highPriceVolume") or 0) * participation))
        for bucket in target_rows
    )
    target_sold = min(filled, target_capacity)
    forced = filled - target_sold
    forced_price = _non_null_price(exit_rows, "avgLowPrice", buy)
    # This deliberately retains evaluator-v1 tax economics pending tax-contract review.
    target_profit = target_sold * (target - buy - ge_tax.sale_tax(target))
    forced_profit = forced * (forced_price - buy - ge_tax.sale_tax(forced_price))
    actual_profit = target_profit + forced_profit
    filled_hours = (
        Fraction(str(lane["hold_hours"]))
        if forced else max(step_hours, step_hours * len(exit_rows))
    )
    capital_hours = (
        Fraction(filled * buy) * filled_hours
        + Fraction((qty - filled) * buy) * Fraction(str(lane["entry_hours"]))
    )
    return {
        "id": order["item_id"], "name": order["name"], "strategy": lane_name,
        "posted_capital_gp": posted_capital, "expected_profit_gp": expected_profit,
        "filled_qty": filled, "target_sold_qty": target_sold, "forced_exit_qty": forced,
        "actual_profit_gp": actual_profit,
        "forced_exit_price": forced_price if forced else None,
        "capital_hours": capital_hours,
        "fill_rate": round(filled / qty, 4) if qty else 0.0,
        "entry_capacity": entry_capacity, "target_capacity": target_capacity,
    }


def _required_buckets(lane: dict) -> int:
    step_hours = STEP_SECONDS[lane["timestep"]] / 3_600
    return (
        max(1, math.ceil(lane["entry_hours"] / step_hours))
        + max(1, math.ceil(lane["hold_hours"] / step_hours))
    )


def coverage_manifest(fixture: dict, contract: dict) -> list[dict]:
    manifest = []
    as_of = int(datetime.fromisoformat(
        fixture["as_of"].replace("Z", "+00:00")
    ).timestamp())
    for item in fixture["items"]:
        for lane_name, lane in contract["simulation"].items():
            future = item.get("future", {}).get(lane["timestep"], [])
            required = _required_buckets(lane)
            reasons = []
            if len(future) < required:
                reasons.append("incomplete_horizon")
            if any(not REQUIRED_BUCKET_FIELDS.issubset(bucket) for bucket in future):
                reasons.append("missing_bucket_fields")
            timestamps = [bucket.get("timestamp") for bucket in future]
            if any(not isinstance(timestamp, int) for timestamp in timestamps):
                reasons.append("invalid_timestamp")
            else:
                if any(timestamp <= as_of for timestamp in timestamps):
                    reasons.append("not_strictly_after_as_of")
                if any(current <= prior for prior, current
                       in zip(timestamps, timestamps[1:])):
                    reasons.append("not_strictly_ascending")
                step = STEP_SECONDS[lane["timestep"]]
                if any(current - prior != step for prior, current
                       in zip(timestamps, timestamps[1:])):
                    reasons.append("unexpected_timestep_spacing")
            manifest.append({
                "fixture": fixture["name"],
                "item_id": int(item["id"]),
                "lane": lane_name,
                "as_of": fixture["as_of"],
                "available_buckets": len(future),
                "required_buckets": required,
                "covered": not reasons,
                "reasons": reasons,
            })
    return manifest


def coverage_manifest_sha256(rows: list[dict]) -> str:
    ordered = sorted(
        rows,
        key=lambda row: (
            row["fixture"], row["item_id"], row["lane"], row["as_of"],
        ),
    )
    payload = json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def coverage_key(fixture: str, item_id: int, lane: str, as_of: str) -> tuple:
    return fixture, int(item_id), lane, as_of


def canonical_frontier(visible: dict, contract: dict) -> list[dict]:
    """Build a bounded visible-data frontier independent of planner output."""
    assert_visible(visible)
    frontier = []
    for lane_name, lane in contract["simulation"].items():
        ranked = []
        for item in visible["items"]:
            buy = int(item.get("latest", {}).get("low") or 0)
            target = int(item.get("latest", {}).get("high") or 0)
            if buy <= 0 or target - buy - ge_tax.sale_tax(target) <= 0:
                continue
            action = {
                "item_id": int(item["id"]),
                "side": "buy",
                "quantity": 1,
                "buy_price": buy,
                "sell_target": target,
                "cancel_after": round(float(lane["entry_hours"]) * 3_600),
                "hard_exit_after": round(float(lane["hold_hours"]) * 3_600),
                "lane": lane_name,
                "name": item["name"],
                "observed_quantities": [1],
            }
            vector = replay_vector(action, item, contract)
            if not vector["blocks"]:
                continue
            visible_utility = sum(
                (row["utility"] for row in vector["blocks"].values()),
                Fraction(),
            ) / len(vector["blocks"])
            ranked.append((visible_utility, vector["opportunity_episode_count"],
                           -action["item_id"], action))
        frontier.extend(
            row[-1] for row in sorted(ranked, reverse=True)[:FRONTIER_ITEMS_PER_LANE]
        )
    return sorted(frontier, key=lambda row: (row["lane"], row["item_id"]))


def _history_blocks(item: dict, lane: dict) -> list[tuple[int, list[dict]]]:
    rows = list(item.get("history", {}).get(lane["timestep"], []))
    size = _required_buckets(lane)
    remainder = len(rows) % size
    rows = rows[remainder:]
    return [
        (int(rows[index].get("timestamp") or index), rows[index:index + size])
        for index in range(0, len(rows), size)
        if len(rows[index:index + size]) == size
    ]


def _round_fraction(value: Fraction) -> int:
    if value >= 0:
        return math.floor(value + Fraction(1, 2))
    return math.ceil(value - Fraction(1, 2))


def _utility(simulation: dict, contract: dict) -> Fraction:
    rate = Fraction(str(contract["minimum_expected_return_per_posted_capital_hour"]))
    return Fraction(simulation["actual_profit_gp"]) - simulation["capital_hours"] * rate


def replay_vector(order: dict, item: dict, contract: dict) -> dict:
    lane = contract["simulation"][order["lane"]]
    blocks = _history_blocks(item, lane)
    rows = {}
    capacities = set()
    target_capacities = set()
    for block_id, buckets in blocks:
        simulation = simulate_buckets(order, buckets, contract)
        rows[block_id] = {
            "profit": simulation["actual_profit_gp"],
            "capital_hours": simulation["capital_hours"],
            "utility": _utility(simulation, contract),
        }
        capacities.add(simulation["entry_capacity"])
        target_capacities.add(simulation["target_capacity"])

    history = list(item.get("history", {}).get(lane["timestep"], []))
    touches = [
        bool(
            bucket.get("avgLowPrice")
            and bucket["avgLowPrice"] <= order["buy_price"]
            and math.floor(
                (bucket.get("lowPriceVolume") or 0)
                * float(lane["participation_rate"])
            ) > 0
        )
        for bucket in history
    ]
    episodes = sum(
        touched and (index == 0 or not touches[index - 1])
        for index, touched in enumerate(touches)
    )
    return {
        "blocks": rows,
        "evidence_count": len(rows),
        "opportunity_episode_count": episodes,
        "entry_capacities": sorted(capacities),
        "target_capacities": sorted(target_capacities),
    }


def quantity_breakpoints(order: dict, vector: dict, item: dict,
                         available_cash: int, risk_bankroll: int,
                         contract: dict, replay_cache: dict | None = None) -> list[dict]:
    """Return calculated quantity crossings and their integer neighbors."""
    pivots: dict[int, set[str]] = {}

    def add(quantity: int, source: str) -> None:
        if quantity > 0:
            pivots.setdefault(quantity, set()).add(source)

    add(1, "minimum_integer")
    for quantity in order.get("observed_quantities", [order["quantity"]]):
        add(quantity, "current_quantity")
    for capacity in vector["entry_capacities"]:
        add(capacity, "fill_capacity")
    for capacity in vector["target_capacities"]:
        add(max(1, capacity), "target_capacity")
    for entry in vector["entry_capacities"]:
        for target in vector["target_capacities"]:
            if entry > target:
                add(max(1, target), "forced_exit_crossing")

    ge_limit = int(item.get("limit") or 10**9)
    affordable = available_cash // order["buy_price"] if order["buy_price"] else 0
    add(ge_limit, "ge_limit")
    add(affordable, "affordability")
    upper = min(ge_limit, affordable)

    def replay(candidate: dict) -> dict:
        key = order_signature(candidate)
        if replay_cache is not None:
            if key not in replay_cache:
                replay_cache[key] = replay_vector(candidate, item, contract)
            return replay_cache[key]
        return replay_vector(candidate, item, contract)

    def stats(quantity: int) -> tuple[Fraction, int]:
        rows = replay({**order, "quantity": quantity})["blocks"].values()
        return (
            sum((row["utility"] for row in rows), Fraction()),
            max((max(0, -row["profit"]) for row in rows), default=0),
        )

    if upper > 0:
        structural = sorted({
            1, upper,
            *(min(upper, max(1, value)) for value in pivots),
        })
        for left, right in zip(structural, structural[1:]):
            left_utility = stats(left)[0]
            right_utility = stats(right)[0]
            if left_utility == 0:
                add(left, "utility_zero_crossing")
            if right_utility == 0:
                add(right, "utility_zero_crossing")
            if left_utility * right_utility < 0:
                negative = left_utility < 0
                low, high = left, right
                while low + 1 < high:
                    middle = (low + high) // 2
                    if (stats(middle)[0] < 0) == negative:
                        low = middle
                    else:
                        high = middle
                add(high, "utility_zero_crossing")

        for limit_key, source in (
            ("maximum_position_loss_pct", "position_loss_crossing"),
            ("maximum_portfolio_loss_pct", "lane_local_portfolio_loss_crossing"),
        ):
            limit = Fraction(str(contract[limit_key])) * risk_bankroll
            if stats(upper)[1] > limit:
                low, high = 0, upper
                while low + 1 < high:
                    middle = (low + high) // 2
                    if stats(middle)[1] > limit:
                        high = middle
                    else:
                        low = middle
                add(high, source)

    expanded: dict[int, dict[str, set[str]]] = {}
    for pivot, sources in pivots.items():
        for quantity in (pivot - 1, pivot, pivot + 1):
            if quantity <= 0:
                continue
            row = expanded.setdefault(quantity, {"crossings": set(), "neighbor_of": set()})
            if quantity == pivot:
                row["crossings"].update(sources)
            else:
                row["neighbor_of"].update(sources)
    return [
        {
            "quantity": quantity,
            "crossings": sorted(values["crossings"]),
            "neighbor_of": sorted(values["neighbor_of"]),
            "feasible": quantity <= upper,
        }
        for quantity, values in sorted(expanded.items())
    ]


def _portfolio_vector(orders: list[dict], vectors: dict[tuple, dict]) -> dict:
    if not orders:
        return {}
    block_sets = [set(vectors[order_signature(order)]["blocks"]) for order in orders]
    common = set.intersection(*block_sets) if block_sets else set()
    return {
        block_id: {
            "profit": sum(
                vectors[order_signature(order)]["blocks"][block_id]["profit"]
                for order in orders
            ),
            "utility": sum(
                (vectors[order_signature(order)]["blocks"][block_id]["utility"]
                 for order in orders),
                Fraction(),
            ),
        }
        for block_id in sorted(common)
    }


def _metrics(current: list[dict], alternative: list[dict], changed: list[dict],
             vectors: dict[tuple, dict], cash: int, contract: dict) -> dict:
    current_rows = _portfolio_vector(current, vectors)
    alternative_rows = _portfolio_vector(alternative, vectors)
    common = sorted(set(current_rows).intersection(alternative_rows))
    if not current:
        common = sorted(alternative_rows)
    deltas = [
        alternative_rows[key]["utility"]
        - (current_rows[key]["utility"] if current else Fraction())
        for key in common
    ]
    profits = [alternative_rows[key]["profit"] for key in common]
    position_losses = []
    for order in alternative:
        rows = vectors[order_signature(order)]["blocks"].values()
        position_losses.extend(max(0, -row["profit"]) for row in rows)
    changed_evidence = [{
        "item_id": order["item_id"],
        "lane": order["lane"],
        "quantity": order["quantity"],
        "non_overlapping_block_count":
            vectors[order_signature(order)]["evidence_count"],
        "distinct_opportunity_episode_count":
            vectors[order_signature(order)]["opportunity_episode_count"],
    } for order in changed]
    blocks = min(
        (row["non_overlapping_block_count"] for row in changed_evidence),
        default=0,
    )
    episodes = min(
        (row["distinct_opportunity_episode_count"] for row in changed_evidence),
        default=0,
    )
    mean_delta = _round_fraction(sum(deltas, Fraction()) / len(deltas)) if deltas else 0
    position_risk = (
        max(position_losses, default=0)
        <= cash * contract["maximum_position_loss_pct"]
    )
    lane_risk = (
        max((max(0, -value) for value in profits), default=0)
        <= cash * contract["maximum_portfolio_loss_pct"]
    )
    positive_mean = bool(deltas) and mean_delta > 0
    provisional_evidence = blocks >= 3 and episodes >= 2
    return {
        "mean_incremental_visible_utility_gp": mean_delta,
        "comparable_replay_block_count": len(common),
        "changed_action_non_overlapping_block_count": blocks,
        "changed_action_distinct_opportunity_episode_count": episodes,
        "changed_action_evidence": changed_evidence,
        "lane_local_visible_worst_loss_gp": min(0, min(profits, default=0)),
        "lane_local_maximum_position_loss_gp": max(position_losses, default=0),
        "lane_local_maximum_portfolio_loss_gp":
            max((max(0, -value) for value in profits), default=0),
        "lane_local_position_risk_compliant": position_risk,
        "lane_local_portfolio_risk_compliant": lane_risk,
        "full_portfolio_risk_assessed": False,
        "all_window_dominance": (
            bool(deltas)
            and all(delta >= 0 for delta in deltas)
            and any(delta > 0 for delta in deltas)
        ),
        "raw_positive_mean": positive_mean,
        "provisional_3_block_2_episode": provisional_evidence,
        "provisional_positive_mean_evidence_and_lane_risk": (
            positive_mean and provisional_evidence and position_risk and lane_risk
        ),
    }


def _withheld_utility(order: dict, outcome: dict, contract: dict) -> Fraction:
    lane = contract["simulation"][order["lane"]]
    buckets = outcome.get("future", {}).get(lane["timestep"], [])
    simulation = simulate_buckets(order, buckets, contract)
    return _utility(simulation, contract)


def _covered(order: dict, fixture_name: str, as_of: str, coverage: dict[tuple, dict]) -> bool:
    entry = coverage.get(coverage_key(
        fixture_name, order["item_id"], order["lane"], as_of
    ))
    return bool(entry and entry["covered"])


def _annotate_outcome(record: dict, current: list[dict], alternative: list[dict],
                      fixture_name: str, as_of: str, outcomes: dict[int, dict],
                      coverage: dict[tuple, dict], contract: dict) -> None:
    current_signatures = {order_signature(order) for order in current}
    alternative_signatures = {order_signature(order) for order in alternative}
    changed = [
        ("before", order) for order in current
        if order_signature(order) not in alternative_signatures
    ] + [
        ("after", order) for order in alternative
        if order_signature(order) not in current_signatures
    ]
    cells = []
    for role, order in changed:
        covered = _covered(order, fixture_name, as_of, coverage)
        utility = None
        if covered:
            utility = _round_fraction(_withheld_utility(
                order, outcomes[order["item_id"]], contract
            ))
        cells.append({
            "role": role,
            "item_id": order["item_id"],
            "lane": order["lane"],
            "quantity": order["quantity"],
            "covered": covered,
            "withheld_utility_gp": utility,
        })
    record["changed_action_coverage"] = cells
    record["withheld_outcome_status"] = (
        "covered" if cells and all(row["covered"] for row in cells) else "uncovered"
    )
    record["raw_covered_outcome_delta_gp"] = (
        sum(row["withheld_utility_gp"] for row in cells if row["role"] == "after")
        - sum(row["withheld_utility_gp"] for row in cells if row["role"] == "before")
        if record["withheld_outcome_status"] == "covered" else None
    )


def _candidate(order: dict, quantity: int) -> dict:
    return {**order, "quantity": quantity}


def portfolio_feasibility(orders: list[dict], case: dict,
                          item_map: dict[int, dict]) -> dict:
    capital = sum(order["quantity"] * order["buy_price"] for order in orders)
    reasons = []
    if capital > case["cash_gp"]:
        reasons.append("cash")
    if len(orders) > case["slot_cap"]:
        reasons.append("slots")
    item_ids = [order["item_id"] for order in orders]
    if len(item_ids) != len(set(item_ids)):
        reasons.append("duplicate_item")
    if case["away_hours"] is not None and case["away_hours"] >= 0.5:
        if any(order["lane"] == "active-margin" for order in orders):
            reasons.append("attendance")
    for order in orders:
        item = item_map[order["item_id"]]
        if (
            order["side"] != "buy"
            or order["quantity"] <= 0
            or order["quantity"] > int(item.get("limit") or 10**9)
        ):
            reasons.append("order_constraint")
            break
    return {
        "feasible": not reasons,
        "reasons": reasons,
        "capital_gp": capital,
        "slots": len(orders),
    }


def _best_quantity(action: dict, vector: dict, item: dict,
                   available_cash: int, risk_bankroll: int,
                   contract: dict, vectors: dict) -> tuple[dict, dict] | None:
    points = [
        point for point in quantity_breakpoints(
            action, vector, item, available_cash, risk_bankroll, contract, vectors
        )
        if point["feasible"]
    ]
    choices = []
    for point in points:
        candidate = _candidate(action, point["quantity"])
        key = order_signature(candidate)
        if key not in vectors:
            vectors[key] = replay_vector(candidate, item, contract)
        rows = vectors[key]["blocks"].values()
        score = (
            sum((row["utility"] for row in rows), Fraction()) / len(rows)
            if rows else Fraction()
        )
        choices.append((
            score,
            -candidate["quantity"] * candidate["buy_price"],
            -candidate["quantity"],
            -candidate["item_id"],
            candidate,
            point,
        ))
    if not choices:
        return None
    best = max(choices)
    return best[-2], best[-1]


def _bounded_benchmark(actions: list[dict], vectors: dict[tuple, dict],
                       item_map: dict[int, dict], cash: int, slots: int,
                       excluded_items: set[int], risk_bankroll: int,
                       contract: dict) -> list[dict]:
    """Exact sparse knapsack over one best visible quantity per bounded action."""
    candidates = []
    for action in actions:
        if action["item_id"] in excluded_items:
            continue
        selected = _best_quantity(
            action, vectors[order_signature(action)],
            item_map[action["item_id"]], cash, risk_bankroll, contract, vectors,
        )
        if selected:
            candidates.append(selected[0])

    states: list[dict[tuple[int, frozenset], tuple[Fraction, list[dict]]]] = [
        {(0, frozenset()): (Fraction(), [])}
    ] + [{} for _ in range(slots)]
    for candidate in candidates:
        cost = candidate["quantity"] * candidate["buy_price"]
        utility = sum(
            (row["utility"] for row in
             vectors[order_signature(candidate)]["blocks"].values()),
            Fraction(),
        ) / max(1, vectors[order_signature(candidate)]["evidence_count"])
        item_id = candidate["item_id"]
        for used in range(slots - 1, -1, -1):
            for (spent, item_ids), (score, orders) in list(states[used].items()):
                if item_id in item_ids or spent + cost > cash:
                    continue
                key = (spent + cost, item_ids | {item_id})
                value = (score + utility, orders + [candidate])
                prior = states[used + 1].get(key)
                if prior is None or value[0] > prior[0]:
                    states[used + 1][key] = value
    choices = [value for state in states for value in state.values()]
    return max(choices, key=lambda value: value[0], default=(Fraction(), []))[1]


def visible_challengers(case: dict, frontier: list[dict], visible: dict,
                        contract: dict, prior_case: dict | None = None,
                        replay_cache: dict | None = None) -> list[dict]:
    """Construct local challengers with visible inputs only."""
    assert_visible(visible)
    item_map = {int(item["id"]): item for item in visible["items"]}
    current = case["normalized_orders"]
    vectors = replay_cache if replay_cache is not None else {}

    def ensure(order: dict) -> None:
        key = order_signature(order)
        if key not in vectors:
            vectors[key] = replay_vector(order, item_map[order["item_id"]], contract)

    for order in current + frontier:
        ensure(order)

    records = []

    def record(kind: str, lane: str, full_alternative: list[dict],
               details: dict | None = None) -> None:
        feasibility = portfolio_feasibility(full_alternative, case, item_map)
        if not feasibility["feasible"]:
            return
        lane_current = [order for order in current if order["lane"] == lane]
        alternative = [
            order for order in full_alternative if order["lane"] == lane
        ]
        if (
            sorted(order_signature(order) for order in lane_current)
            == sorted(order_signature(order) for order in alternative)
        ):
            return
        for order in alternative:
            ensure(order)
        current_signatures = {order_signature(order) for order in lane_current}
        changed = [
            order for order in alternative
            if order_signature(order) not in current_signatures
        ]
        row = {
            "kind": kind,
            "lane": lane,
            "portfolio_feasibility": feasibility,
            "_current_orders": lane_current,
            "_alternative_orders": alternative,
            **(details or {}),
            **_metrics(
                lane_current,
                alternative,
                changed,
                vectors,
                case["cash_gp"],
                contract,
            ),
        }
        records.append(row)

    for lane_name in contract["simulation"]:
        lane_current = [order for order in current if order["lane"] == lane_name]
        lane_actions = [order for order in frontier if order["lane"] == lane_name]
        spent = sum(order["quantity"] * order["buy_price"] for order in current)
        fixed = [order for order in current if order["lane"] != lane_name]

        if len(current) < case["slot_cap"]:
            current_items = {order["item_id"] for order in current}
            for action in lane_actions:
                if action["item_id"] in current_items:
                    continue
                available = case["cash_gp"] - spent
                selected = _best_quantity(
                    action, vectors[order_signature(action)],
                    item_map[action["item_id"]],
                    available,
                    case["cash_gp"],
                    contract,
                    vectors,
                )
                if selected:
                    addition, point = selected
                    record("one_order_addition", lane_name, current + [addition], {
                        "item_id": addition["item_id"], "quantity": addition["quantity"],
                        "quantity_policy":
                            "max_visible_mean_utility_then_lowest_cost",
                        "selected_breakpoint": point,
                    })

        for order in lane_current:
            available = case["cash_gp"] - spent + order["quantity"] * order["buy_price"]
            base_action = next(
                (action for action in lane_actions
                 if action_signature(action) == action_signature(order)),
                {**order, "observed_quantities": [order["quantity"]]},
            )
            points = quantity_breakpoints(
                base_action, vectors[order_signature(base_action)],
                item_map[order["item_id"]],
                available,
                case["cash_gp"],
                contract,
                vectors,
            )
            next_points = [
                point for point in points
                if point["feasible"] and point["quantity"] > order["quantity"]
            ]
            if next_points:
                resized = _candidate(order, next_points[0]["quantity"])
                alternative = [
                    resized if row is order else row for row in current
                ]
                record("next_breakpoint_resize", lane_name, alternative, {
                    "item_id": order["item_id"],
                    "from_quantity": order["quantity"],
                    "to_quantity": resized["quantity"],
                    "selected_breakpoint": next_points[0],
                })

        for removed in lane_current:
            available = (
                case["cash_gp"] - spent
                + removed["quantity"] * removed["buy_price"]
            )
            for action in lane_actions:
                if action["item_id"] in {row["item_id"] for row in current if row is not removed}:
                    continue
                if action_signature(action) == action_signature(removed):
                    continue
                selected = _best_quantity(
                    action, vectors[order_signature(action)],
                    item_map[action["item_id"]],
                    available,
                    case["cash_gp"],
                    contract,
                    vectors,
                )
                if not selected:
                    continue
                replacement, point = selected
                alternative = [
                    replacement if row is removed else row for row in current
                ]
                record("one_for_one_replacement", lane_name, alternative, {
                    "removed_item_id": removed["item_id"],
                    "added_item_id": replacement["item_id"],
                    "quantity": replacement["quantity"],
                    "quantity_policy":
                        "max_visible_mean_utility_then_lowest_cost",
                    "selected_breakpoint": point,
                })

        if prior_case:
            prior_lane = [
                order for order in prior_case["normalized_orders"]
                if order["lane"] == lane_name
            ]
            scaled = []
            ratio = Fraction(case["cash_gp"], prior_case["cash_gp"])
            for order in prior_lane:
                quantity = max(1, math.floor(order["quantity"] * ratio))
                quantity = min(
                    quantity,
                    int(item_map[order["item_id"]].get("limit") or 10**9),
                    case["cash_gp"] // order["buy_price"],
                )
                scaled.append(_candidate(order, quantity))
            if scaled:
                record("adjacent_bankroll_scaling", lane_name, fixed + scaled, {
                    "lower_cash_gp": prior_case["cash_gp"],
                })

        fixed_cash = sum(order["quantity"] * order["buy_price"] for order in fixed)
        available_slots = case["slot_cap"] - len(fixed)
        benchmark = _bounded_benchmark(
            lane_actions,
            vectors,
            item_map,
            case["cash_gp"] - fixed_cash,
            available_slots,
            {order["item_id"] for order in fixed},
            case["cash_gp"],
            contract,
        )
        record("bounded_benchmark", lane_name, fixed + benchmark, {
            "frontier_actions": len(lane_actions),
            "available_cash_gp": case["cash_gp"] - fixed_cash,
            "available_slots": available_slots,
        })

    return records


def analyze_case(case: dict, frontier: list[dict], visible: dict,
                 outcomes: dict[int, dict], coverage_rows: list[dict],
                 contract: dict, prior_case: dict | None = None,
                 replay_cache: dict | None = None) -> dict:
    """Add withheld characterization after visible challenger construction."""
    records = visible_challengers(
        case, frontier, visible, contract, prior_case, replay_cache
    )
    coverage = {
        coverage_key(row["fixture"], row["item_id"], row["lane"], row["as_of"]): row
        for row in coverage_rows
    }
    for row in records:
        current = row.pop("_current_orders")
        alternative = row.pop("_alternative_orders")
        _annotate_outcome(
            row, current, alternative, case["fixture"], visible["as_of"],
            outcomes, coverage, contract,
        )
    return {"challengers": records}


def summarize(cases: list[dict], frontiers: dict[str, list[dict]]) -> dict:
    challengers = [
        (case, row)
        for case in cases
        for row in case["selection_characterization"]["challengers"]
    ]
    by_lane_fixture: dict[str, dict] = {}
    unchanged_with_provisional_conjunction = 0
    groups: dict[tuple, list[dict]] = {}
    for case in cases:
        key = (case["fixture"], case["attendance"], case["slot_cap"])
        groups.setdefault(key, []).append(case)
        for _, row in [(case, row) for row in
                       case["selection_characterization"]["challengers"]]:
            cell = by_lane_fixture.setdefault(
                f"{case['fixture']}:{row['lane']}",
                {
                    "challengers": 0,
                    "all_window": 0,
                    "raw_positive_mean": 0,
                    "provisional_3_block_2_episode": 0,
                    "lane_local_position_risk_compliant": 0,
                    "lane_local_portfolio_risk_compliant": 0,
                    "provisional_positive_mean_evidence_and_lane_risk": 0,
                },
            )
            cell["challengers"] += 1
            cell["all_window"] += row["all_window_dominance"]
            for metric in (
                "raw_positive_mean",
                "provisional_3_block_2_episode",
                "lane_local_position_risk_compliant",
                "lane_local_portfolio_risk_compliant",
                "provisional_positive_mean_evidence_and_lane_risk",
            ):
                cell[metric] += row[metric]

    flat_groups = 0
    for rows in groups.values():
        ordered = sorted(rows, key=lambda row: row["cash_gp"])
        signatures = [
            tuple(sorted(order_signature(order) for order in row["normalized_orders"]))
            for row in ordered
        ]
        if len(ordered) == 6 and len(set(signatures)) == 1 and any(
            challenger["provisional_positive_mean_evidence_and_lane_risk"]
            for row in ordered
            for challenger in row["selection_characterization"]["challengers"]
        ):
            flat_groups += 1
        for prior, current in zip(ordered, ordered[1:]):
            if (
                tuple(sorted(order_signature(order) for order in prior["normalized_orders"]))
                == tuple(sorted(order_signature(order) for order in current["normalized_orders"]))
                and any(
                    challenger["provisional_positive_mean_evidence_and_lane_risk"]
                    for challenger in current["selection_characterization"]["challengers"]
                )
            ):
                unchanged_with_provisional_conjunction += 1
    return {
        "acceptance_gate": False,
        "all_window_challenger_count": sum(
            row["all_window_dominance"] for _, row in challengers
        ),
        "raw_positive_mean_challenger_count": sum(
            row["raw_positive_mean"] for _, row in challengers
        ),
        "provisional_3_block_2_episode_challenger_count": sum(
            row["provisional_3_block_2_episode"] for _, row in challengers
        ),
        "lane_local_position_risk_compliant_challenger_count": sum(
            row["lane_local_position_risk_compliant"] for _, row in challengers
        ),
        "lane_local_portfolio_risk_compliant_challenger_count": sum(
            row["lane_local_portfolio_risk_compliant"] for _, row in challengers
        ),
        "provisional_positive_mean_evidence_and_lane_risk_challenger_count": sum(
            row["provisional_positive_mean_evidence_and_lane_risk"]
            for _, row in challengers
        ),
        "counts_by_lane_and_fixture": by_lane_fixture,
        "unchanged_bankroll_transitions_with_provisional_positive_mean_"
        "evidence_and_lane_risk": unchanged_with_provisional_conjunction,
        "flat_six_bankroll_groups_with_provisional_positive_mean_"
        "evidence_and_lane_risk": flat_groups,
        "frontier_cardinalities": {
            fixture: len(frontier) for fixture, frontier in frontiers.items()
        },
        "cross_lane_benchmarking_resolved": False,
    }


def uncertainty_report(visible_fixtures: list[dict], contract: dict) -> dict:
    pseudo_counts: dict[str, int] = {}
    by_fixture_lane: dict[str, int] = {}
    for fixture in visible_fixtures:
        for lane_name, lane in contract["simulation"].items():
            item_counts = [
                len(_history_blocks(item, lane)) for item in fixture["items"]
            ]
            count = max(item_counts, default=0)
            by_fixture_lane[f"{fixture['name']}:{lane_name}"] = count
            pseudo_counts[lane_name] = pseudo_counts.get(lane_name, 0) + count
    required = math.ceil(math.log(0.05) / math.log(0.99))
    return {
        "supports_99th_percentile_calibration": False,
        "effective_independent_sample_count": None,
        "pseudo_checkpoint_upper_bound_by_lane": pseudo_counts,
        "pseudo_checkpoints_by_fixture_and_lane": by_fixture_lane,
        "minimum_iid_samples_for_95pct_chance_of_one_1pct_tail_observation": required,
        "required_assumptions": [
            "non-overlapping blocks are stationary and independent",
            "items within a time block are not counted as independent checkpoints",
            "overlapping dated fixtures are deduplicated before aggregation",
        ],
        "additional_data_needed":
            "prospective, time-indexed checkpoints spanning at least 299 independent "
            "lane horizons, with overlap and regime labels retained",
    }
