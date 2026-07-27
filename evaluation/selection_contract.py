"""Report-only evaluator-v2 selection characterization.

The policy is intentionally bounded: its canonical frontier contains only executable
item/lane orders emitted by the planner somewhere in the configured evaluation matrix.
It is useful for local selection comparisons, but is not a globally optimal action frontier.
"""

from __future__ import annotations

import math
from fractions import Fraction

from flipper import ge_tax


STEP_SECONDS = {"5m": 300, "1h": 3_600, "6h": 21_600}
WITHHELD_ITEM_FIELDS = {"future", "coverage", "coverage_flags", "future_coverage"}
EXECUTABLE_FIELDS = (
    "item_id", "side", "quantity", "buy_price", "sell_target",
    "cancel_after", "hard_exit_after",
)


def strategy(row: dict) -> str:
    value = row.get("strategy")
    if value == "patient-band":
        return "patient"
    return value or {
        "flip": "patient",
        "flip-patient-probe": "patient-probe",
        "flip-active": "active-margin",
        "flip-time-of-day": "time-of-day",
    }.get(row.get("bucket"), "patient")


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
                if "coverage" in lowered or lowered == "future":
                    raise ValueError(f"withheld metadata reached visible input: {key}")
                reject_metadata(nested)
        elif isinstance(value, list):
            for nested in value:
                reject_metadata(nested)

    reject_metadata(fixture)
    for item in fixture.get("items", []):
        assert not WITHHELD_ITEM_FIELDS.intersection(item)


def withheld_items(fixture: dict) -> dict[int, dict]:
    """Project outcome data without copying it into any visible planner structure."""
    return {
        int(item["id"]): {"future": item.get("future", {})}
        for item in fixture["items"]
    }


def normalize_order(row: dict, contract: dict) -> dict:
    lane = strategy(row)
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
        "name": row.get("name") or str(row["id"]),
    }


def executable_order(order: dict) -> dict:
    return {field: order[field] for field in EXECUTABLE_FIELDS}


def order_signature(order: dict) -> tuple:
    return tuple(order[field] for field in EXECUTABLE_FIELDS)


def action_signature(order: dict) -> tuple:
    return tuple(order[field] for field in EXECUTABLE_FIELDS if field != "quantity")


def order_row(order: dict) -> dict:
    return {
        "id": order["item_id"],
        "name": order.get("name") or str(order["item_id"]),
        "action": order["side"],
        "strategy": order["lane"],
        "qty": order["quantity"],
        "price": order["buy_price"],
        "sell_target": order["sell_target"],
        "expected_profit": 0,
    }


def _non_null_price(rows: list[dict], key: str, fallback: int) -> int:
    return next((int(row[key]) for row in reversed(rows) if row.get(key)), fallback)


def simulate_buckets(row: dict, buckets: list[dict], contract: dict) -> dict:
    """Simulate one executable order against a supplied, already-separated bucket vector."""
    lane_name = strategy(row)
    lane = contract["simulation"][lane_name]
    timestep = lane["timestep"]
    step_hours = Fraction(STEP_SECONDS[timestep], 3_600)
    entry_points = max(1, math.ceil(lane["entry_hours"] / float(step_hours)))
    hold_points = max(1, math.ceil(lane["hold_hours"] / float(step_hours)))
    qty = int(row["qty"])
    buy = int(row["price"])
    target = int(row["sell_target"])
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
    expected_profit = int(row.get("expected_profit") or 0)
    if not filled:
        capital_hours = Fraction(posted_capital) * Fraction(str(lane["entry_hours"]))
        return {
            "id": row["id"], "name": row["name"], "strategy": lane_name,
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
        "id": row["id"], "name": row["name"], "strategy": lane_name,
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
    for item in fixture["items"]:
        for lane_name, lane in contract["simulation"].items():
            future = item.get("future", {}).get(lane["timestep"], [])
            required = _required_buckets(lane)
            manifest.append({
                "fixture": fixture["name"],
                "item_id": int(item["id"]),
                "lane": lane_name,
                "as_of": fixture["as_of"],
                "available_buckets": len(future),
                "required_buckets": required,
                "covered": len(future) >= required,
            })
    return manifest


def coverage_key(fixture: str, item_id: int, lane: str, as_of: str) -> tuple:
    return fixture, int(item_id), lane, as_of


def canonical_frontier(visible: dict, planned_orders: list[dict], contract: dict) -> list[dict]:
    """Build the explicit bounded planner-emission item/lane frontier."""
    assert_visible(visible)
    available = {int(item["id"]) for item in visible["items"]}
    actions: dict[tuple, dict] = {}
    for row in planned_orders:
        order = normalize_order(row, contract)
        if order["item_id"] not in available:
            raise ValueError("planner emitted an item outside visible input")
        key = action_signature(order)
        actions.setdefault(key, {**order, "observed_quantities": set()})
        actions[key]["observed_quantities"].add(order["quantity"])
    return [
        {**action, "observed_quantities": sorted(action["observed_quantities"])}
        for _, action in sorted(actions.items())
    ]


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
        simulation = simulate_buckets(order_row(order), buckets, contract)
        rows[block_id] = {
            "profit": simulation["actual_profit_gp"],
            "capital_hours": simulation["capital_hours"],
            "utility": _utility(simulation, contract),
        }
        capacities.add(simulation["entry_capacity"])
        target_capacities.add(simulation["target_capacity"])

    history = list(item.get("history", {}).get(lane["timestep"], []))
    touched = [
        int(bucket.get("timestamp") or index * STEP_SECONDS[lane["timestep"]])
        for index, bucket in enumerate(history)
        if bucket.get("avgLowPrice")
        and bucket["avgLowPrice"] <= order["buy_price"]
        and math.floor(
            (bucket.get("lowPriceVolume") or 0)
            * float(lane["participation_rate"])
        ) > 0
    ]
    separation = order["hard_exit_after"]
    episodes = 0
    episode_start = None
    for timestamp in touched:
        if episode_start is None or timestamp - episode_start >= separation:
            episodes += 1
            episode_start = timestamp
    return {
        "blocks": rows,
        "evidence_count": len(rows),
        "opportunity_episode_count": episodes,
        "entry_capacities": sorted(capacities),
        "target_capacities": sorted(target_capacities),
    }


def quantity_breakpoints(order: dict, vector: dict, item: dict, cash: int,
                         contract: dict, replay_cache: dict | None = None) -> list[dict]:
    """Return reviewed piecewise-change candidates plus both integer neighbors."""
    pivots: dict[int, set[str]] = {}

    def add(value: int | float, source: str) -> None:
        quantity = int(value)
        if quantity > 0:
            pivots.setdefault(quantity, set()).add(source)

    add(1, "minimum_integer")
    for quantity in order.get("observed_quantities", [order["quantity"]]):
        add(quantity, "planner_quantity")
    for capacity in vector["entry_capacities"]:
        add(capacity, "fill_capacity")
    for capacity in vector["target_capacities"]:
        add(capacity, "target_capacity")
    for entry in vector["entry_capacities"]:
        for target in vector["target_capacities"]:
            add(entry - target, "forced_exit_capacity")

    ge_limit = int(item.get("limit") or 10**9)
    affordable = cash // order["buy_price"] if order["buy_price"] else 0
    add(ge_limit, "ge_limit")
    add(affordable, "affordability")

    def replay(candidate: dict) -> dict:
        key = order_signature(candidate)
        if replay_cache is not None:
            if key not in replay_cache:
                replay_cache[key] = replay_vector(candidate, item, contract)
            return replay_cache[key]
        return replay_vector(candidate, item, contract)

    one = {**order, "quantity": 1}
    one_vector = replay(one)
    losses = [max(0, -row["profit"]) for row in one_vector["blocks"].values()]
    worst_unit_loss = max(losses, default=0)
    if worst_unit_loss:
        add(
            cash * contract["maximum_position_loss_pct"] / worst_unit_loss,
            "position_loss_crossing",
        )
        add(
            cash * contract["maximum_portfolio_loss_pct"] / worst_unit_loss,
            "portfolio_loss_crossing",
        )

    utilities = [row["utility"] for row in one_vector["blocks"].values()]
    if utilities and sum(utilities) == 0:
        add(1, "profit_zero_crossing")
    reservation = (
        Fraction(order["buy_price"] * order["cancel_after"], 3_600)
        * Fraction(str(contract["minimum_expected_return_per_posted_capital_hour"]))
    )
    if reservation:
        add(Fraction(1, 2) / reservation, "reservation_rounding")

    profit_points = sorted(pivots)
    prior_profit = None
    for quantity in profit_points:
        candidate = {**order, "quantity": quantity}
        profits = [
            row["profit"]
            for row in replay(candidate)["blocks"].values()
        ]
        total_profit = sum(profits)
        if prior_profit is not None and (prior_profit <= 0 < total_profit
                                         or prior_profit >= 0 > total_profit):
            pivots[quantity].add("profit_zero_crossing")
        prior_profit = total_profit

    expanded: dict[int, set[str]] = {}
    upper = min(ge_limit, affordable)
    for pivot, sources in pivots.items():
        for quantity in (pivot - 1, pivot, pivot + 1):
            if 0 < quantity <= upper:
                expanded.setdefault(quantity, set()).update(sources)
                if quantity != pivot:
                    expanded[quantity].add("integer_neighbor")
    return [
        {"quantity": quantity, "sources": sorted(sources)}
        for quantity, sources in sorted(expanded.items())
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


def _metrics(current: list[dict], alternative: list[dict],
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
    episodes = max(
        (vectors[order_signature(order)]["opportunity_episode_count"]
         for order in alternative),
        default=0,
    )
    mean_delta = _round_fraction(sum(deltas, Fraction()) / len(deltas)) if deltas else 0
    return {
        "mean_incremental_visible_utility_gp": mean_delta,
        "non_overlapping_evidence_count": len(common),
        "distinct_opportunity_episode_count": episodes,
        "visible_worst_loss_gp": min(0, min(profits, default=0)),
        "maximum_position_loss_gp": max(position_losses, default=0),
        "maximum_portfolio_loss_gp": max((max(0, -value) for value in profits), default=0),
        "position_risk_ok": max(position_losses, default=0)
        <= cash * contract["maximum_position_loss_pct"],
        "portfolio_risk_ok": max((max(0, -value) for value in profits), default=0)
        <= cash * contract["maximum_portfolio_loss_pct"],
        "all_window_dominance": bool(deltas) and all(delta >= 0 for delta in deltas),
        "raw_positive_mean_qualification": bool(deltas) and mean_delta > 0,
        "evidence_qualified": len(common) >= 3 and episodes >= 2,
}


def _withheld_utility(order: dict, outcome: dict, contract: dict) -> Fraction:
    lane = contract["simulation"][order["lane"]]
    buckets = outcome.get("future", {}).get(lane["timestep"], [])
    simulation = simulate_buckets(order_row(order), buckets, contract)
    return _utility(simulation, contract)


def _covered(order: dict, fixture_name: str, as_of: str, coverage: dict[tuple, dict]) -> bool:
    entry = coverage.get(coverage_key(
        fixture_name, order["item_id"], order["lane"], as_of
    ))
    return bool(entry and entry["covered"])


def _annotate_outcome(record: dict, current: list[dict], alternative: list[dict],
                      fixture_name: str, as_of: str, outcomes: dict[int, dict],
                      coverage: dict[tuple, dict], contract: dict) -> None:
    changed = [
        order for order in alternative
        if order_signature(order) not in {order_signature(row) for row in current}
    ]
    scoring_cells = []
    for order in changed:
        covered = _covered(order, fixture_name, as_of, coverage)
        utility = (
            _round_fraction(_withheld_utility(
                order, outcomes[order["item_id"]], contract
            ))
            if covered else None
        )
        scoring_cells.append({
            "item_id": order["item_id"],
            "lane": order["lane"],
            "covered": covered,
            "withheld_utility_gp": utility,
            "performance_credit_gp": max(0, utility) if utility is not None else 0,
        })
    record["withheld_scoring_cells"] = scoring_cells
    changed_covered = bool(changed) and all(row["covered"] for row in scoring_cells)
    record["withheld_coverage"] = changed_covered
    if not changed_covered:
        record["withheld_incremental_utility_gp"] = None
        record["withheld_performance_credit_gp"] = sum(
            row["performance_credit_gp"] for row in scoring_cells
        )
        return
    current_value = sum(
        (_withheld_utility(order, outcomes[order["item_id"]], contract)
         for order in current
         if _covered(order, fixture_name, as_of, coverage)),
        Fraction(),
    )
    alternative_value = sum(
        (_withheld_utility(order, outcomes[order["item_id"]], contract)
         for order in alternative
         if _covered(order, fixture_name, as_of, coverage)),
        Fraction(),
    )
    delta = _round_fraction(alternative_value - current_value)
    record["withheld_incremental_utility_gp"] = delta
    record["withheld_performance_credit_gp"] = max(0, delta)


def _candidate(order: dict, quantity: int) -> dict:
    return {**order, "quantity": quantity}


def _bounded_benchmark(actions: list[dict], vectors: dict[tuple, dict],
                       item_map: dict[int, dict], cash: int, slots: int,
                       contract: dict) -> list[dict]:
    """Exact sparse knapsack over one best visible quantity per bounded action."""
    candidates = []
    for action in actions:
        points = quantity_breakpoints(
            action, vectors[order_signature(action)],
            item_map[action["item_id"]], cash, contract, vectors,
        )
        choices = [_candidate(action, point["quantity"]) for point in points]
        choices = [choice for choice in choices if choice["quantity"] * choice["buy_price"] <= cash]
        if not choices:
            continue
        for choice in choices:
            if order_signature(choice) not in vectors:
                vectors[order_signature(choice)] = replay_vector(
                    choice, item_map[choice["item_id"]], contract
                )
        best = max(
            choices,
            key=lambda choice: (
                sum((row["utility"] for row in
                     vectors[order_signature(choice)]["blocks"].values()), Fraction()),
                -choice["quantity"] * choice["buy_price"],
            ),
        )
        candidates.append(best)

    states: list[dict[tuple[int, frozenset], tuple[Fraction, list[dict]]]] = [
        {(0, frozenset()): (Fraction(), [])}
    ] + [{} for _ in range(slots)]
    for candidate in candidates:
        cost = candidate["quantity"] * candidate["buy_price"]
        utility = sum(
            (row["utility"] for row in
             vectors[order_signature(candidate)]["blocks"].values()),
            Fraction(),
        )
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


def analyze_case(case: dict, frontier: list[dict], visible: dict,
                 outcomes: dict[int, dict], coverage_rows: list[dict],
                 contract: dict, prior_case: dict | None = None,
                 replay_cache: dict | None = None) -> dict:
    """Characterize local challengers without producing acceptance failures."""
    assert_visible(visible)
    item_map = {int(item["id"]): item for item in visible["items"]}
    coverage = {
        coverage_key(row["fixture"], row["item_id"], row["lane"], row["as_of"]): row
        for row in coverage_rows
    }
    current = case["normalized_orders"]
    vectors = replay_cache if replay_cache is not None else {}

    def ensure(order: dict) -> None:
        key = order_signature(order)
        if key not in vectors:
            vectors[key] = replay_vector(order, item_map[order["item_id"]], contract)

    for order in current + frontier:
        ensure(order)

    records = []

    def record(kind: str, lane: str, alternative: list[dict], details: dict | None = None) -> None:
        if any(order["lane"] != lane for order in alternative):
            return
        for order in alternative:
            ensure(order)
        row = {
            "kind": kind,
            "lane": lane,
            **(details or {}),
            **_metrics(
                [order for order in current if order["lane"] == lane],
                alternative,
                vectors,
                case["cash_gp"],
                contract,
            ),
        }
        _annotate_outcome(
            row,
            [order for order in current if order["lane"] == lane],
            alternative,
            case["fixture"],
            visible["as_of"],
            outcomes,
            coverage,
            contract,
        )
        records.append(row)

    for lane_name in contract["simulation"]:
        lane_current = [order for order in current if order["lane"] == lane_name]
        lane_actions = [order for order in frontier if order["lane"] == lane_name]
        spent = sum(order["quantity"] * order["buy_price"] for order in current)

        if len(current) < case["slot_cap"]:
            current_items = {order["item_id"] for order in current}
            for action in lane_actions:
                if action["item_id"] in current_items:
                    continue
                available = case["cash_gp"] - spent
                points = quantity_breakpoints(
                    action, vectors[order_signature(action)],
                    item_map[action["item_id"]], available, contract, vectors,
                )
                if points:
                    addition = _candidate(action, points[-1]["quantity"])
                    record("one_order_addition", lane_name, lane_current + [addition], {
                        "item_id": addition["item_id"], "quantity": addition["quantity"],
                    })

        for index, order in enumerate(lane_current):
            available = case["cash_gp"] - spent + order["quantity"] * order["buy_price"]
            base_action = next(
                (action for action in lane_actions
                 if action_signature(action) == action_signature(order)),
                {**order, "observed_quantities": [order["quantity"]]},
            )
            points = quantity_breakpoints(
                base_action, vectors[order_signature(base_action)],
                item_map[order["item_id"]], available, contract, vectors,
            )
            next_points = [point for point in points if point["quantity"] > order["quantity"]]
            if next_points:
                resized = _candidate(order, next_points[0]["quantity"])
                alternative = lane_current[:index] + [resized] + lane_current[index + 1:]
                record("next_breakpoint_resize", lane_name, alternative, {
                    "item_id": order["item_id"],
                    "from_quantity": order["quantity"],
                    "to_quantity": resized["quantity"],
                    "breakpoint_sources": next_points[0]["sources"],
                })

        for index, removed in enumerate(lane_current):
            available = (
                case["cash_gp"] - spent
                + removed["quantity"] * removed["buy_price"]
            )
            for action in lane_actions:
                if action["item_id"] in {row["item_id"] for row in current if row is not removed}:
                    continue
                if action_signature(action) == action_signature(removed):
                    continue
                points = quantity_breakpoints(
                    action, vectors[order_signature(action)],
                    item_map[action["item_id"]], available, contract, vectors,
                )
                if not points:
                    continue
                replacement = _candidate(action, points[-1]["quantity"])
                alternative = (
                    lane_current[:index] + [replacement] + lane_current[index + 1:]
                )
                record("one_for_one_replacement", lane_name, alternative, {
                    "removed_item_id": removed["item_id"],
                    "added_item_id": replacement["item_id"],
                    "quantity": replacement["quantity"],
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
            if scaled and sum(
                order["quantity"] * order["buy_price"] for order in scaled
            ) <= case["cash_gp"]:
                record("adjacent_bankroll_scaling", lane_name, scaled, {
                    "lower_cash_gp": prior_case["cash_gp"],
                })

        benchmark = _bounded_benchmark(
            lane_actions, vectors, item_map, case["cash_gp"], case["slot_cap"], contract
        )
        record("bounded_benchmark", lane_name, benchmark, {
            "frontier_actions": len(lane_actions),
        })

    counts = {}
    for row in records:
        key = f"{row['lane']}:{row['kind']}"
        counts[key] = counts.get(key, 0) + 1
    return {
        "acceptance_gate": False,
        "cross_lane_aggregation": "unresolved; results remain lane-separated",
        "challengers": records,
        "counts": counts,
    }


def summarize(cases: list[dict], frontiers: dict[str, list[dict]]) -> dict:
    challengers = [
        (case, row)
        for case in cases
        for row in case["selection_characterization"]["challengers"]
    ]
    by_lane_fixture: dict[str, dict] = {}
    unchanged_with_qualifying = 0
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
                    "positive_mean": 0,
                    "evidence_qualified": 0,
                },
            )
            cell["challengers"] += 1
            cell["all_window"] += row["all_window_dominance"]
            cell["positive_mean"] += row["raw_positive_mean_qualification"]
            cell["evidence_qualified"] += row["evidence_qualified"]

    flat_groups = 0
    for rows in groups.values():
        ordered = sorted(rows, key=lambda row: row["cash_gp"])
        signatures = [
            tuple(sorted(order_signature(order) for order in row["normalized_orders"]))
            for row in ordered
        ]
        if len(ordered) == 6 and len(set(signatures)) == 1 and any(
            challenger["evidence_qualified"]
            and challenger["raw_positive_mean_qualification"]
            for row in ordered
            for challenger in row["selection_characterization"]["challengers"]
        ):
            flat_groups += 1
        for prior, current in zip(ordered, ordered[1:]):
            if (
                tuple(sorted(order_signature(order) for order in prior["normalized_orders"]))
                == tuple(sorted(order_signature(order) for order in current["normalized_orders"]))
                and any(
                    challenger["evidence_qualified"]
                    and challenger["raw_positive_mean_qualification"]
                    for challenger in current["selection_characterization"]["challengers"]
                )
            ):
                unchanged_with_qualifying += 1
    return {
        "acceptance_gate": False,
        "all_window_challenger_count": sum(
            row["all_window_dominance"] for _, row in challengers
        ),
        "raw_positive_mean_challenger_count": sum(
            row["raw_positive_mean_qualification"] for _, row in challengers
        ),
        "evidence_qualified_challenger_count": sum(
            row["evidence_qualified"] for _, row in challengers
        ),
        "counts_by_lane_and_fixture": by_lane_fixture,
        "unchanged_bankroll_transitions_with_qualifying_challengers":
            unchanged_with_qualifying,
        "flat_six_bankroll_groups_with_qualifying_challengers": flat_groups,
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
