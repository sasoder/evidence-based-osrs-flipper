"""Standalone evaluator-v2 selection, replay, and validation contract.

This module deliberately does not import planner or signal code. Planner submissions and
evaluator-owned challengers are reduced to executable orders, then scored against the same visible
history. Withheld buckets enter only through the validation functions near the end of the file.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from fractions import Fraction


STEP_SECONDS = {"5m": 300, "1h": 3_600, "6h": 21_600}
EXECUTABLE_FIELDS = (
    "item_id",
    "side",
    "quantity",
    "buy_price",
    "sell_target",
    "cancel_after",
    "hard_exit_after",
    "management_after",
)
REQUIRED_BUCKET_FIELDS = {
    "timestamp",
    "avgLowPrice",
    "avgHighPrice",
    "lowPriceVolume",
    "highPriceVolume",
}


def _timestamp(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _round_fraction(value: Fraction) -> int:
    if value >= 0:
        return math.floor(value + Fraction(1, 2))
    return math.ceil(value - Fraction(1, 2))


def _policy_horizon_buckets(policy: dict) -> int:
    step_hours = Fraction(STEP_SECONDS[policy["timestep"]], 3_600)
    return max(
        1,
        math.ceil(
            (Fraction(str(policy["entry_hours"])) + Fraction(str(policy["hold_hours"])))
            / step_hours
        ),
    )


def _attendance_by_name(contract: dict, name: str) -> dict:
    try:
        return next(row for row in contract["attendance"] if row["name"] == name)
    except StopIteration as exc:
        raise ValueError(f"unknown evaluator attendance {name!r}") from exc


def execution_timing(order_lane: str, attendance: dict, contract: dict) -> dict:
    """Return evaluator-owned executable timing for one attendance state."""
    policy = contract["simulation"][order_lane]
    away_seconds = round(float(attendance.get("away_hours") or 0) * 3_600)
    entry_seconds = round(float(policy["entry_hours"]) * 3_600)
    hold_seconds = round(float(policy["hold_hours"]) * 3_600)
    cancel_after = max(entry_seconds, away_seconds)
    return {
        "management_after": away_seconds,
        "cancel_after": cancel_after,
        "hard_exit_after": cancel_after + hold_seconds,
    }


def maximum_lane_horizon_buckets(decision_lane: str, contract: dict) -> int:
    lane = contract["decision_lanes"][decision_lane]
    attendances = [
        _attendance_by_name(contract, name) for name in lane["attendance"]
    ]
    return max(
        math.ceil(
            execution_timing(order_lane, attendance, contract)["hard_exit_after"]
            / STEP_SECONDS[contract["simulation"][order_lane]["timestep"]]
        )
        for order_lane in lane["order_lanes"]
        for attendance in attendances
    )


def _entry_buckets(policy: dict) -> int:
    step_hours = Fraction(STEP_SECONDS[policy["timestep"]], 3_600)
    return max(1, math.ceil(Fraction(str(policy["entry_hours"])) / step_hours))


def decision_cutoff(fixture: dict, decision_lane: str) -> int:
    try:
        return _timestamp(fixture["decision_cutoffs"][decision_lane])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"fixture {fixture.get('name', '<unnamed>')} has no v2 cutoff for {decision_lane}"
        ) from exc


def evaluator_sale_tax(item_id: int, name: str, gross_price: int, contract: dict) -> int:
    """Apply the evaluator-owned immutable tax policy."""
    tax = contract["tax"]
    if (
        int(item_id) in {int(value) for value in tax["exempt_item_ids"]}
        or name.lower() in {value.lower() for value in tax["exempt_item_names"]}
    ):
        return 0
    if gross_price <= 0:
        return 0
    return min(
        math.floor(Fraction(str(tax["rate"])) * gross_price),
        int(tax["per_item_cap_gp"]),
    )


def _valid_bucket_series(rows: list[dict], timestep: str, cutoff: int | None = None) -> bool:
    if not rows or any(not REQUIRED_BUCKET_FIELDS.issubset(row) for row in rows):
        return False
    timestamps = [row["timestamp"] for row in rows]
    if any(not isinstance(value, int) for value in timestamps):
        return False
    if any(right - left != STEP_SECONDS[timestep] for left, right in zip(
        timestamps, timestamps[1:]
    )):
        return False
    return cutoff is None or (timestamps[-1] == cutoff and all(value <= cutoff for value in timestamps))


def snapshot_universe(fixture: dict, decision_lane: str, contract: dict) -> list[int]:
    """Select the lane snapshot universe using evaluator-owned data rules only."""
    lane = contract["decision_lanes"][decision_lane]
    timestep = lane["timestep"]
    cutoff = decision_cutoff(fixture, decision_lane)
    minimum_rows = (
        maximum_lane_horizon_buckets(decision_lane, contract)
        * int(contract["minimum_non_overlapping_blocks"])
    )
    selected = []
    for item in fixture["items"]:
        rows = [
            row for row in item.get("history", {}).get(timestep, [])
            if isinstance(row.get("timestamp"), int) and row["timestamp"] <= cutoff
        ]
        if (
            item.get("members", True)
            and int(item.get("limit") or 0) > 0
            and len(rows) >= minimum_rows
            and _valid_bucket_series(rows, timestep, cutoff)
        ):
            selected.append(int(item["id"]))
    return sorted(selected)


def visible_fixture(fixture: dict, decision_lane: str, contract: dict) -> dict:
    """Return the exact lane-specific projection visible to both planner and evaluator."""
    lane = contract["decision_lanes"][decision_lane]
    timestep = lane["timestep"]
    cutoff = decision_cutoff(fixture, decision_lane)
    selected = set(snapshot_universe(fixture, decision_lane, contract))
    visible_items = []
    for item in fixture["items"]:
        if int(item["id"]) not in selected:
            continue
        history = [
            row for row in item["history"][timestep]
            if row["timestamp"] <= cutoff
        ]
        current = history[-1]
        visible_items.append({
            "id": int(item["id"]),
            "name": item["name"],
            "members": bool(item.get("members", True)),
            "limit": int(item.get("limit") or 0),
            "latest": {
                "low": current.get("avgLowPrice"),
                "high": current.get("avgHighPrice"),
                "lowTime": cutoff,
                "highTime": cutoff,
            },
            "one_hour": {
                "lowPriceVolume": current.get("lowPriceVolume") or 0,
                "highPriceVolume": current.get("highPriceVolume") or 0,
            },
            "history": {timestep: history},
        })
    visible = {
        "name": fixture["name"],
        "as_of": fixture["decision_cutoffs"][decision_lane],
        "decision_lane": decision_lane,
        "fixture_class": fixture["fixture_class"],
        "source": fixture.get("source"),
        "source_sha256": fixture.get("source_sha256"),
        "items": visible_items,
    }
    assert_visible(visible)
    return visible


def assert_visible(fixture: dict) -> None:
    def reject(value) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                lowered = key.lower()
                if (
                    lowered == "future"
                    or lowered.startswith("future_")
                    or "coverage" in lowered
                    or "outcome" in lowered
                ):
                    raise ValueError(f"withheld metadata reached visible input: {key}")
                reject(nested)
        elif isinstance(value, list):
            for nested in value:
                reject(nested)

    reject(fixture)


def withheld_items(fixture: dict) -> dict[int, dict]:
    return {
        int(item["id"]): {"future": item.get("future", {})}
        for item in fixture["items"]
    }


def fixture_audit(fixture: dict, contract: dict) -> dict:
    reasons = []
    if fixture.get("fixture_class") not in contract["fixture_classes"]:
        reasons.append("unknown_fixture_class")
    provenance = fixture.get("source_provenance") or {}
    if fixture.get("fixture_class") == "observed":
        if provenance.get("kind") != "immutable_cache_archive":
            reasons.append("observed_archive_provenance_missing")
        if provenance.get("integrity_verified") is not True:
            reasons.append("archive_integrity_not_verified")
        if not provenance.get("manifest_sha256") or not provenance.get("manifest_file_count"):
            reasons.append("archive_manifest_identity_missing")
    elif fixture.get("fixture_class") == "synthetic":
        if provenance.get("kind") != "deterministic_generator":
            reasons.append("synthetic_generator_provenance_missing")

    lane_counts = {}
    for decision_lane in contract["decision_lanes"]:
        try:
            lane_counts[decision_lane] = len(
                snapshot_universe(fixture, decision_lane, contract)
            )
        except ValueError:
            lane_counts[decision_lane] = 0
            reasons.append(f"{decision_lane}_cutoff_missing")
        if lane_counts[decision_lane] == 0:
            reasons.append(f"{decision_lane}_universe_empty")
        if (
            fixture.get("fixture_class") == "observed"
            and lane_counts[decision_lane]
            < contract["decision_lanes"][decision_lane][
                "minimum_observed_snapshot_items"
            ]
        ):
            reasons.append(f"{decision_lane}_archive_lane_floor_not_met")
    return {
        "fixture": fixture.get("name"),
        "fixture_class": fixture.get("fixture_class"),
        "passed": not reasons,
        "reasons": sorted(set(reasons)),
        "snapshot_universe_items_by_lane": lane_counts,
        "provenance": provenance,
    }


def coverage_manifest(fixture: dict, contract: dict) -> list[dict]:
    manifest = []
    item_map = {int(item["id"]): item for item in fixture["items"]}
    for decision_lane, lane in contract["decision_lanes"].items():
        cutoff = decision_cutoff(fixture, decision_lane)
        for item_id in snapshot_universe(fixture, decision_lane, contract):
            item = item_map[item_id]
            for order_lane in lane["order_lanes"]:
                policy = contract["simulation"][order_lane]
                timestep = policy["timestep"]
                for attendance_name in lane["attendance"]:
                    attendance = _attendance_by_name(contract, attendance_name)
                    timing = execution_timing(order_lane, attendance, contract)
                    required = math.ceil(
                        timing["hard_exit_after"] / STEP_SECONDS[timestep]
                    )
                    future = list(item.get("future", {}).get(timestep, []))
                    reasons = []
                    if len(future) < required:
                        reasons.append("incomplete_horizon")
                    horizon = future[:required]
                    if not _valid_bucket_series(horizon, timestep):
                        reasons.append("invalid_bucket_series")
                    if (
                        horizon
                        and horizon[0].get("timestamp")
                        != cutoff + STEP_SECONDS[timestep]
                    ):
                        reasons.append("not_immediately_after_cutoff")
                    manifest.append({
                        "fixture": fixture["name"],
                        "fixture_class": fixture["fixture_class"],
                        "coverage_scope": "admitted_item",
                        "decision_lane": decision_lane,
                        "order_lane": order_lane,
                        "attendance": attendance_name,
                        "item_id": item_id,
                        "decision_cutoff": fixture["decision_cutoffs"][decision_lane],
                        "available_buckets": len(future),
                        "required_buckets": required,
                        "covered": not reasons,
                        "reasons": reasons,
                    })
    return manifest


def coverage_key(fixture: str, item_id: int, order_lane: str,
                 attendance: str, cutoff: str) -> tuple:
    return fixture, int(item_id), order_lane, attendance, cutoff


def coverage_manifest_sha256(rows: list[dict]) -> str:
    payload = json.dumps(
        sorted(
            rows,
            key=lambda row: (
                row["fixture"],
                row["decision_lane"],
                row["order_lane"],
                row["attendance"],
                row["item_id"],
                row["decision_cutoff"],
            ),
        ),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def normalize_order(row: dict, contract: dict,
                    attendance: dict | None = None) -> dict:
    lane = row["_evaluator_lane"]
    attendance = attendance or _attendance_by_name(contract, "attended")
    timing = execution_timing(lane, attendance, contract)
    return {
        "item_id": int(row["id"]),
        "side": row.get("action") or "buy",
        "quantity": int(row["qty"]),
        "buy_price": int(row["price"]),
        "sell_target": int(row["sell_target"]),
        **timing,
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


def _last_price(rows: list[dict], field: str, fallback: int) -> int:
    return next((int(row[field]) for row in reversed(rows) if row.get(field)), fallback)


def simulate_buckets(order: dict, buckets: list[dict], contract: dict) -> dict:
    """Replay fills, inventory, target sales, and liquidation in chronological order."""
    policy = contract["simulation"][order["lane"]]
    timestep = policy["timestep"]
    step_seconds = STEP_SECONDS[timestep]
    step_hours = Fraction(step_seconds, 3_600)
    entry_points = max(1, math.ceil(order["cancel_after"] / step_seconds))
    hard_points = max(entry_points + 1, math.ceil(order["hard_exit_after"] / step_seconds))
    management_after = int(order.get("management_after") or 0)
    rows = list(buckets[:hard_points])
    participation = Fraction(str(policy["participation_rate"]))
    quantity = int(order["quantity"])
    buy = int(order["buy_price"])
    target = int(order["sell_target"])
    name = order.get("name", str(order["item_id"]))

    remaining = quantity
    inventory = 0
    filled = 0
    target_sold = 0
    entry_capacity = 0
    target_capacity = 0
    target_profit = 0
    capital_hours = Fraction()
    events = []

    for index, bucket in enumerate(rows):
        elapsed_hours = step_hours * (index + 1)

        # A single GE slot cannot sell partial inventory while its buy offer remains open. Target
        # selling starts in the bucket after a full fill, or after the entry offer is cancelled.
        sell_offer_open = (
            (remaining == 0 or index >= entry_points)
            and index * step_seconds >= management_after
        )
        if (
            sell_offer_open
            and bucket.get("avgHighPrice")
            and int(bucket["avgHighPrice"]) >= target
        ):
            capacity = max(
                0,
                math.floor((bucket.get("highPriceVolume") or 0) * participation),
            )
            target_capacity += capacity
            sold = min(inventory, capacity) if inventory else 0
            if sold:
                inventory -= sold
                target_sold += sold
                tax = evaluator_sale_tax(order["item_id"], name, target, contract)
                target_profit += sold * (target - buy - tax)
                capital_hours += Fraction(sold * buy) * elapsed_hours
                events.append({"bucket": index, "event": "target_sale", "quantity": sold})

        if (
            index < entry_points
            and bucket.get("avgLowPrice")
            and int(bucket["avgLowPrice"]) <= buy
        ):
            capacity = max(
                0,
                math.floor((bucket.get("lowPriceVolume") or 0) * participation),
            )
            entry_capacity += capacity
            bought = min(remaining, capacity) if remaining else 0
            if bought:
                remaining -= bought
                inventory += bought
                filled += bought
                events.append({"bucket": index, "event": "entry_fill", "quantity": bought})

    forced = inventory
    forced_price = _last_price(rows, "avgLowPrice", buy)
    forced_profit = 0
    if forced:
        tax = evaluator_sale_tax(order["item_id"], name, forced_price, contract)
        forced_profit = forced * (forced_price - buy - tax)
        liquidation_hours = step_hours * max(len(rows), hard_points)
        capital_hours += Fraction(forced * buy) * liquidation_hours
        events.append({
            "bucket": max(0, len(rows) - 1),
            "event": "forced_exit",
            "quantity": forced,
            "price": forced_price,
        })

    # Posted cash is reserved from decision time. Unfilled quantity is released at cancellation.
    capital_hours += Fraction(remaining * buy) * step_hours * entry_points
    actual_profit = target_profit + forced_profit
    return {
        "id": order["item_id"],
        "name": name,
        "strategy": order["lane"],
        "posted_capital_gp": quantity * buy,
        "expected_profit_gp": int(order.get("expected_profit_gp", 0)),
        "filled_qty": filled,
        "target_sold_qty": target_sold,
        "forced_exit_qty": forced,
        "actual_profit_gp": actual_profit,
        "forced_exit_price": forced_price if forced else None,
        "capital_hours": capital_hours,
        "fill_rate": round(filled / quantity, 4) if quantity else 0.0,
        "entry_capacity": entry_capacity,
        "target_capacity": target_capacity,
        "events": events,
    }


def _utility(simulation: dict, contract: dict) -> Fraction:
    rate = Fraction(str(contract["minimum_visible_return_per_posted_capital_hour"]))
    return Fraction(simulation["actual_profit_gp"]) - simulation["capital_hours"] * rate


def _history_blocks(item: dict, order: dict, policy: dict) -> list[tuple[int, list[dict]]]:
    rows = list(item.get("history", {}).get(policy["timestep"], []))
    size = max(
        1,
        math.ceil(
            order["hard_exit_after"] / STEP_SECONDS[policy["timestep"]]
        ),
    )
    rows = rows[len(rows) % size:]
    return [
        (int(rows[index]["timestamp"]), rows[index:index + size])
        for index in range(0, len(rows), size)
        if len(rows[index:index + size]) == size
    ]


def replay_vector(order: dict, item: dict, contract: dict) -> dict:
    policy = contract["simulation"][order["lane"]]
    blocks = {}
    entry_capacities = set()
    target_capacities = set()
    for block_id, buckets in _history_blocks(item, order, policy):
        simulation = simulate_buckets(order, buckets, contract)
        blocks[block_id] = {
            "profit": simulation["actual_profit_gp"],
            "capital_hours": simulation["capital_hours"],
            "utility": _utility(simulation, contract),
        }
        entry_capacities.add(simulation["entry_capacity"])
        target_capacities.add(simulation["target_capacity"])

    participation = Fraction(str(policy["participation_rate"]))
    history = item.get("history", {}).get(policy["timestep"], [])
    touches = [
        bool(
            row.get("avgLowPrice")
            and row["avgLowPrice"] <= order["buy_price"]
            and math.floor((row.get("lowPriceVolume") or 0) * participation) > 0
        )
        for row in history
    ]
    episodes = sum(
        touched and (index == 0 or not touches[index - 1])
        for index, touched in enumerate(touches)
    )
    return {
        "blocks": blocks,
        "evidence_count": len(blocks),
        "opportunity_episode_count": episodes,
        "entry_capacities": sorted(entry_capacities),
        "target_capacities": sorted(target_capacities),
    }


def _ensure_vector(order: dict, item_map: dict[int, dict], contract: dict, cache: dict) -> dict:
    key = order_signature(order)
    if key not in cache:
        cache[key] = replay_vector(order, item_map[order["item_id"]], contract)
    return cache[key]


def score_order(order: dict, item: dict, cash: int, contract: dict,
                replay_cache: dict | None = None) -> dict:
    cache = replay_cache if replay_cache is not None else {}
    vector = _ensure_vector(order, {order["item_id"]: item}, contract, cache)
    rows = list(vector["blocks"].values())
    utilities = [row["utility"] for row in rows]
    profits = [row["profit"] for row in rows]
    mean_utility = sum(utilities, Fraction()) / len(utilities) if utilities else Fraction()
    mean_profit = Fraction(sum(profits), len(profits)) if profits else Fraction()
    maximum_loss = max((max(0, -value) for value in profits), default=0)
    evidence_ok = (
        vector["evidence_count"] >= contract["minimum_non_overlapping_blocks"]
        and vector["opportunity_episode_count"] >= contract["minimum_opportunity_episodes"]
    )
    risk_ok = maximum_loss <= Fraction(str(contract["maximum_position_loss_pct"])) * cash
    positive = bool(rows) and mean_utility > 0
    return {
        "mean_visible_utility_gp": _round_fraction(mean_utility),
        "mean_visible_utility_fraction": [
            mean_utility.numerator, mean_utility.denominator
        ],
        "mean_visible_profit_gp": _round_fraction(mean_profit),
        "visible_worst_profit_gp": min(profits, default=0),
        "maximum_visible_loss_gp": maximum_loss,
        "non_overlapping_block_count": vector["evidence_count"],
        "distinct_opportunity_episode_count": vector["opportunity_episode_count"],
        "positive_visible_utility": positive,
        "evidence_sufficient": evidence_ok,
        "position_risk_compliant": risk_ok,
        "qualifies": positive and evidence_ok and risk_ok,
    }


def quantity_breakpoints(order: dict, vector: dict, item: dict, available_cash: int,
                         risk_bankroll: int, contract: dict,
                         replay_cache: dict | None = None) -> list[dict]:
    """Return deterministic capacity, cash, limit, utility, and risk crossings."""
    cache = replay_cache if replay_cache is not None else {}
    cache_key = ("breakpoints", order_signature(order), available_cash, risk_bankroll)
    if cache_key in cache:
        return cache[cache_key]
    pivots: dict[int, set[str]] = {}

    def add(value: int, source: str) -> None:
        if value > 0:
            pivots.setdefault(int(value), set()).add(source)

    add(1, "minimum_integer")
    add(int(order.get("quantity") or 1), "current_quantity")
    for value in vector["entry_capacities"]:
        add(value, "fill_capacity")
    for value in vector["target_capacities"]:
        add(max(1, value), "target_capacity")
    for entry in vector["entry_capacities"]:
        for target in vector["target_capacities"]:
            if entry > target:
                add(max(1, target), "forced_exit_crossing")
    ge_limit = int(item.get("limit") or 0)
    affordable = available_cash // order["buy_price"] if order["buy_price"] else 0
    add(ge_limit, "ge_limit")
    add(affordable, "affordability")
    upper = min(ge_limit, affordable)

    def stats(quantity: int) -> tuple[Fraction, int]:
        candidate = {**order, "quantity": quantity}
        rows = _ensure_vector(candidate, {order["item_id"]: item}, contract, cache)["blocks"].values()
        return (
            sum((row["utility"] for row in rows), Fraction()),
            max((max(0, -row["profit"]) for row in rows), default=0),
        )

    structural = sorted({1, upper, *(min(upper, value) for value in pivots if upper > 0)})
    if upper > 0:
        for left, right in zip(structural, structural[1:]):
            left_utility = stats(left)[0]
            right_utility = stats(right)[0]
            if left_utility == 0:
                add(left, "utility_zero_crossing")
            if right_utility == 0:
                add(right, "utility_zero_crossing")
            if left_utility * right_utility < 0:
                sign = left_utility < 0
                low, high = left, right
                while low + 1 < high:
                    middle = (low + high) // 2
                    if (stats(middle)[0] < 0) == sign:
                        low = middle
                    else:
                        high = middle
                add(high, "utility_zero_crossing")
        limit = Fraction(str(contract["maximum_position_loss_pct"])) * risk_bankroll
        if stats(upper)[1] > limit:
            low, high = 0, upper
            while low + 1 < high:
                middle = (low + high) // 2
                if stats(middle)[1] > limit:
                    high = middle
                else:
                    low = middle
            add(high, "position_loss_crossing")

    expanded: dict[int, dict[str, set[str]]] = {}
    for pivot, sources in pivots.items():
        for quantity in (pivot - 1, pivot, pivot + 1):
            if quantity <= 0:
                continue
            row = expanded.setdefault(quantity, {"crossings": set(), "neighbor_of": set()})
            (row["crossings"] if quantity == pivot else row["neighbor_of"]).update(sources)
    result = [
        {
            "quantity": quantity,
            "crossings": sorted(values["crossings"]),
            "neighbor_of": sorted(values["neighbor_of"]),
            "feasible": quantity <= upper,
        }
        for quantity, values in sorted(expanded.items())
    ]
    cache[cache_key] = result
    return result


def _canonical_action(item: dict, order_lane: str, attendance: dict,
                      contract: dict) -> dict | None:
    low = int(item.get("latest", {}).get("low") or 0)
    high = int(item.get("latest", {}).get("high") or 0)
    if low <= 0 or high <= low:
        return None
    if high - low - evaluator_sale_tax(item["id"], item["name"], high, contract) <= 0:
        return None
    return {
        "item_id": int(item["id"]),
        "side": "buy",
        "quantity": 1,
        "buy_price": low,
        "sell_target": high,
        **execution_timing(order_lane, attendance, contract),
        "lane": order_lane,
        "name": item["name"],
    }


def _dominates_candidate(left: dict, right: dict) -> bool:
    left_score = left["frontier_score"]
    right_score = right["frontier_score"]
    left_utility = Fraction(*left_score["mean_visible_utility_fraction"])
    right_utility = Fraction(*right_score["mean_visible_utility_fraction"])
    comparisons = (
        left["posted_capital_gp"] <= right["posted_capital_gp"],
        left_utility >= right_utility,
        left_score["non_overlapping_block_count"] >= right_score["non_overlapping_block_count"],
        left_score["distinct_opportunity_episode_count"]
        >= right_score["distinct_opportunity_episode_count"],
        left_score["maximum_visible_loss_gp"] <= right_score["maximum_visible_loss_gp"],
    )
    strict = (
        left["posted_capital_gp"] < right["posted_capital_gp"]
        or left_utility > right_utility
        or left_score["non_overlapping_block_count"] > right_score["non_overlapping_block_count"]
        or left_score["distinct_opportunity_episode_count"]
        > right_score["distinct_opportunity_episode_count"]
        or left_score["maximum_visible_loss_gp"] < right_score["maximum_visible_loss_gp"]
    )
    return all(comparisons) and strict


def _candidate_sort_key(row: dict) -> tuple:
    return (
        row["posted_capital_gp"],
        -Fraction(*row["frontier_score"]["mean_visible_utility_fraction"]),
        row["frontier_score"]["maximum_visible_loss_gp"],
        -row["frontier_score"]["non_overlapping_block_count"],
        -row["frontier_score"]["distinct_opportunity_episode_count"],
        row["item_id"],
        row["lane"],
        row["quantity"],
        order_signature(row),
    )


def pareto_reduce_candidates(candidates: list[dict]) -> list[dict]:
    """Return the complete deterministic nondominated subset."""
    unique = {}
    for candidate in candidates:
        unique.setdefault(order_signature(candidate), candidate)
    ordered = sorted(unique.values(), key=_candidate_sort_key)
    return [
        candidate
        for candidate in ordered
        if not any(
            _dominates_candidate(other, candidate)
            for other in ordered
            if other is not candidate
        )
    ]


def generated_breakpoint_candidates(visible: dict, decision_lane: str, cash: int,
                                    attendance: dict, contract: dict,
                                    replay_cache: dict | None = None) -> list[dict]:
    """Score every feasible integer breakpoint candidate before Pareto reduction."""
    assert_visible(visible)
    cache = replay_cache if replay_cache is not None else {}
    item_map = {int(item["id"]): item for item in visible["items"]}
    candidates = []
    for order_lane in contract["decision_lanes"][decision_lane]["order_lanes"]:
        for item in visible["items"]:
            action = _canonical_action(item, order_lane, attendance, contract)
            if not action or action["buy_price"] > cash:
                continue
            vector = _ensure_vector(action, item_map, contract, cache)
            points = [
                point for point in quantity_breakpoints(
                    action, vector, item, cash, cash, contract, cache
                )
                if point["feasible"]
            ]
            for point in points:
                order = {**action, "quantity": point["quantity"]}
                score = score_order(order, item, cash, contract, cache)
                candidates.append({
                    **order,
                    "posted_capital_gp": order["quantity"] * order["buy_price"],
                    "frontier_score": score,
                    "selected_breakpoint": point,
                })
    return sorted(candidates, key=_candidate_sort_key)


def cash_aware_pareto_frontier(visible: dict, decision_lane: str, cash: int,
                               attendance: dict, contract: dict,
                               replay_cache: dict | None = None) -> list[dict]:
    """Preserve every nondominated feasible quantity through both Pareto stages."""
    candidates = generated_breakpoint_candidates(
        visible, decision_lane, cash, attendance, contract, replay_cache
    )
    per_item = []
    groups: dict[tuple[int, str], list[dict]] = {}
    for candidate in candidates:
        groups.setdefault((candidate["item_id"], candidate["lane"]), []).append(
            candidate
        )
    for key in sorted(groups):
        per_item.extend(pareto_reduce_candidates(groups[key]))
    return pareto_reduce_candidates(per_item)


def portfolio_feasibility(orders: list[dict], case: dict,
                          item_map: dict[int, dict], contract: dict) -> dict:
    capital = sum(order["quantity"] * order["buy_price"] for order in orders)
    reasons = []
    if capital > case["cash_gp"]:
        reasons.append("cash")
    if len(orders) > case["slot_cap"]:
        reasons.append("slots")
    ids = [order["item_id"] for order in orders]
    if len(ids) != len(set(ids)):
        reasons.append("duplicate_item")
    allowed_lanes = set(contract["decision_lanes"][case["decision_lane"]]["order_lanes"])
    attendance = _attendance_by_name(contract, case["attendance"])
    for order in orders:
        item = item_map.get(order["item_id"])
        if item is None:
            reasons.append("outside_snapshot_universe")
            continue
        if order["lane"] not in allowed_lanes:
            reasons.append("wrong_decision_lane")
        elif any(
            order.get(field) != value
            for field, value in execution_timing(
                order["lane"], attendance, contract
            ).items()
        ):
            reasons.append("attendance_semantics")
        if (
            order["side"] != "buy"
            or order["quantity"] <= 0
            or order["buy_price"] <= 0
            or order["sell_target"] <= 0
            or order["quantity"] > int(item.get("limit") or 0)
        ):
            reasons.append("order_constraint")
    return {
        "feasible": not reasons,
        "reasons": sorted(set(reasons)),
        "capital_gp": capital,
        "slots": len(orders),
    }


def _portfolio_rows(orders: list[dict], vectors: dict) -> dict[int, dict]:
    if not orders:
        return {}
    sets = [set(vectors[order_signature(order)]["blocks"]) for order in orders]
    common = set.intersection(*sets) if sets else set()
    return {
        block: {
            "profit": sum(
                vectors[order_signature(order)]["blocks"][block]["profit"]
                for order in orders
            ),
            "utility": sum(
                (
                    vectors[order_signature(order)]["blocks"][block]["utility"]
                    for order in orders
                ),
                Fraction(),
            ),
        }
        for block in sorted(common)
    }


def portfolio_score(orders: list[dict], item_map: dict[int, dict], cash: int,
                    contract: dict, replay_cache: dict | None = None) -> dict:
    cache = replay_cache if replay_cache is not None else {}
    order_scores = []
    for order in orders:
        if order["item_id"] not in item_map:
            continue
        _ensure_vector(order, item_map, contract, cache)
        order_scores.append({
            "executable_order": executable_order(order),
            **score_order(order, item_map[order["item_id"]], cash, contract, cache),
        })
    rows = list(_portfolio_rows(
        [order for order in orders if order["item_id"] in item_map], cache
    ).values())
    utilities = [row["utility"] for row in rows]
    profits = [row["profit"] for row in rows]
    mean_utility = sum(utilities, Fraction()) / len(utilities) if utilities else Fraction()
    portfolio_loss = max((max(0, -value) for value in profits), default=0)
    position_loss = max(
        (row["maximum_visible_loss_gp"] for row in order_scores),
        default=0,
    )
    return {
        "orders": order_scores,
        "mean_visible_utility_gp": _round_fraction(mean_utility),
        "visible_worst_profit_gp": min(profits, default=0),
        "maximum_visible_position_loss_gp": position_loss,
        "maximum_visible_portfolio_loss_gp": portfolio_loss,
        "position_risk_compliant":
            position_loss <= Fraction(str(contract["maximum_position_loss_pct"])) * cash,
        "portfolio_risk_compliant":
            portfolio_loss <= Fraction(str(contract["maximum_portfolio_loss_pct"])) * cash,
        "comparable_replay_block_count": len(rows),
    }


def _challenger_metrics(current: list[dict], alternative: list[dict], changed: list[dict],
                        item_map: dict[int, dict], cash: int, contract: dict,
                        cache: dict) -> dict:
    for order in current + alternative:
        _ensure_vector(order, item_map, contract, cache)
    current_rows = _portfolio_rows(current, cache)
    alternative_rows = _portfolio_rows(alternative, cache)
    common = sorted(set(current_rows).intersection(alternative_rows))
    if not current:
        common = sorted(alternative_rows)
    deltas = [
        alternative_rows[key]["utility"]
        - (current_rows[key]["utility"] if current else Fraction())
        for key in common
    ]
    changed_scores = [
        score_order(order, item_map[order["item_id"]], cash, contract, cache)
        for order in changed
    ]
    alternative_score = portfolio_score(alternative, item_map, cash, contract, cache)
    blocks = min(
        (score["non_overlapping_block_count"] for score in changed_scores),
        default=0,
    )
    episodes = min(
        (score["distinct_opportunity_episode_count"] for score in changed_scores),
        default=0,
    )
    mean_delta = sum(deltas, Fraction()) / len(deltas) if deltas else Fraction()
    evidence_ok = (
        blocks >= contract["minimum_non_overlapping_blocks"]
        and episodes >= contract["minimum_opportunity_episodes"]
    )
    base_qualifies = (
        bool(deltas)
        and mean_delta > 0
        and evidence_ok
        and alternative_score["position_risk_compliant"]
        and alternative_score["portfolio_risk_compliant"]
    )
    materiality = contract["challenger_materiality"]
    thresholds = {
        row["name"]: max(
            1,
            math.ceil(Fraction(str(row["fraction_of_bankroll"])) * cash),
        )
        for row in materiality["candidates"]
    }
    selected_fraction = Fraction(str(materiality["selected_fraction_of_bankroll"]))
    selected = next(
        row["name"]
        for row in materiality["candidates"]
        if Fraction(str(row["fraction_of_bankroll"])) == selected_fraction
    )
    qualifies_by_materiality = {
        name: base_qualifies and mean_delta >= threshold
        for name, threshold in thresholds.items()
    }
    return {
        "mean_incremental_visible_utility_gp": _round_fraction(mean_delta),
        "mean_incremental_visible_utility_fraction": [
            mean_delta.numerator, mean_delta.denominator
        ],
        "positive_fractional_visible_delta": bool(deltas) and mean_delta > 0,
        "comparable_replay_block_count": len(common),
        "changed_action_non_overlapping_block_count": blocks,
        "changed_action_distinct_opportunity_episode_count": episodes,
        "evidence_sufficient": evidence_ok,
        "position_risk_compliant": alternative_score["position_risk_compliant"],
        "portfolio_risk_compliant": alternative_score["portfolio_risk_compliant"],
        "alternative_visible_score": alternative_score,
        "materiality_thresholds_gp": thresholds,
        "qualifies_by_materiality": qualifies_by_materiality,
        "selected_materiality": {
            "name": selected,
            "fraction_of_bankroll": float(selected_fraction),
            "threshold_gp": thresholds[selected],
            "status": materiality["status"],
        },
        "qualifies": qualifies_by_materiality[selected],
    }


def _record_dominates(left: dict, right: dict) -> bool:
    keys_max = (
        "changed_action_non_overlapping_block_count",
        "changed_action_distinct_opportunity_episode_count",
    )
    left_utility = Fraction(*left["mean_incremental_visible_utility_fraction"])
    right_utility = Fraction(*right["mean_incremental_visible_utility_fraction"])
    left_loss = left["alternative_visible_score"]["maximum_visible_portfolio_loss_gp"]
    right_loss = right["alternative_visible_score"]["maximum_visible_portfolio_loss_gp"]
    left_capital = left["portfolio_feasibility"]["capital_gp"]
    right_capital = right["portfolio_feasibility"]["capital_gp"]
    weak = (
        all(left[key] >= right[key] for key in keys_max)
        and left_utility >= right_utility
        and left_loss <= right_loss
        and left_capital <= right_capital
    )
    strict = (
        any(left[key] > right[key] for key in keys_max)
        or left_utility > right_utility
        or left_loss < right_loss
        or left_capital < right_capital
    )
    return weak and strict


def visible_challengers(case: dict, frontier: list[dict], visible: dict,
                        contract: dict, replay_cache: dict | None = None) -> list[dict]:
    """Build and score feasible challengers without receiving withheld inputs."""
    assert_visible(visible)
    cache = replay_cache if replay_cache is not None else {}
    item_map = {int(item["id"]): item for item in visible["items"]}
    current = list(case["normalized_orders"])
    current_signatures = {order_signature(order) for order in current}
    current_ids = {order["item_id"] for order in current}
    alternatives: list[tuple[str, list[dict], dict]] = []

    if len(current) < case["slot_cap"]:
        for candidate in frontier:
            if candidate["item_id"] not in current_ids:
                alternatives.append((
                    "pareto_addition",
                    current + [{key: candidate[key] for key in candidate if key not in {
                        "posted_capital_gp", "frontier_score", "selected_breakpoint"
                    }}],
                    {"added_item_id": candidate["item_id"]},
                ))

    for index, removed in enumerate(current):
        for candidate in frontier:
            if candidate["item_id"] in current_ids - {removed["item_id"]}:
                continue
            replacement = {
                key: candidate[key] for key in candidate
                if key not in {"posted_capital_gp", "frontier_score", "selected_breakpoint"}
            }
            if order_signature(replacement) == order_signature(removed):
                continue
            alternative = list(current)
            alternative[index] = replacement
            alternatives.append((
                "pareto_replacement",
                alternative,
                {
                    "removed_item_id": removed["item_id"],
                    "added_item_id": replacement["item_id"],
                },
            ))

    # A deterministic cash-aware benchmark uses the same Pareto set and never truncates it.
    benchmark = []
    for candidate in sorted(
        frontier,
        key=lambda row: (
            -Fraction(*row["frontier_score"]["mean_visible_utility_fraction"]),
            row["posted_capital_gp"],
            row["item_id"],
            row["lane"],
            row["quantity"],
        ),
    ):
        plain = {
            key: candidate[key] for key in candidate
            if key not in {"posted_capital_gp", "frontier_score", "selected_breakpoint"}
        }
        proposal = benchmark + [plain]
        if portfolio_feasibility(proposal, case, item_map, contract)["feasible"]:
            benchmark = proposal
    if benchmark:
        alternatives.append(("pareto_benchmark", benchmark, {
            "frontier_cardinality": len(frontier),
        }))

    records = []
    seen = set()
    for kind, alternative, details in alternatives:
        signature = tuple(sorted(order_signature(order) for order in alternative))
        if signature in seen:
            continue
        seen.add(signature)
        feasibility = portfolio_feasibility(alternative, case, item_map, contract)
        if not feasibility["feasible"]:
            continue
        alternative_signatures = {order_signature(order) for order in alternative}
        changed = [
            order for order in alternative
            if order_signature(order) not in current_signatures
        ]
        if not changed or alternative_signatures == current_signatures:
            continue
        records.append({
            "kind": kind,
            "portfolio_feasibility": feasibility,
            "_current_orders": current,
            "_alternative_orders": alternative,
            **details,
            **_challenger_metrics(
                current, alternative, changed, item_map,
                case["cash_gp"], contract, cache,
            ),
        })

    # Report the complete nondominated challenger set; this is a semantic Pareto reduction, not a
    # cardinality cap.
    pareto = [
        row for row in records
        if not any(_record_dominates(other, row) for other in records if other is not row)
    ]
    return sorted(
        pareto,
        key=lambda row: (
            not row["qualifies"],
            -row["mean_incremental_visible_utility_gp"],
            row["portfolio_feasibility"]["capital_gp"],
            row["kind"],
            row.get("added_item_id", -1),
            row.get("removed_item_id", -1),
        ),
    )


def _coverage_lookup(rows: list[dict]) -> dict[tuple, dict]:
    return {
        coverage_key(
            row["fixture"],
            row["item_id"],
            row["order_lane"],
            row["attendance"],
            row["decision_cutoff"],
        ): row
        for row in rows
    }


def _withheld_order_score(order: dict, outcome: dict, covered: bool,
                          contract: dict) -> dict:
    if not covered:
        return {
            "covered": False,
            "actual_profit_gp": None,
            "actual_utility_gp": None,
            "maximum_loss_gp": None,
        }
    policy = contract["simulation"][order["lane"]]
    simulation = simulate_buckets(
        order,
        outcome.get("future", {}).get(policy["timestep"], []),
        contract,
    )
    return {
        "covered": True,
        "actual_profit_gp": simulation["actual_profit_gp"],
        "actual_utility_gp": _round_fraction(_utility(simulation, contract)),
        "maximum_loss_gp": max(0, -simulation["actual_profit_gp"]),
    }


def validate_orders(orders: list[dict], fixture_name: str, attendance: str, cutoff: str,
                    outcomes: dict[int, dict], coverage_rows: list[dict],
                    contract: dict) -> dict:
    coverage = _coverage_lookup(coverage_rows)
    rows = []
    for order in orders:
        covered = bool(coverage.get(coverage_key(
            fixture_name, order["item_id"], order["lane"], attendance, cutoff
        ), {}).get("covered"))
        rows.append({
            "item_id": order["item_id"],
            "lane": order["lane"],
            "quantity": order["quantity"],
            **_withheld_order_score(
                order, outcomes.get(order["item_id"], {}), covered, contract
            ),
        })
    covered = all(row["covered"] for row in rows)
    return {
        "status": "covered" if covered else "uncovered",
        "orders": rows,
        "actual_profit_gp":
            sum(row["actual_profit_gp"] for row in rows) if covered else None,
        "actual_utility_gp":
            sum(row["actual_utility_gp"] for row in rows) if covered else None,
        "maximum_position_loss_gp":
            max((row["maximum_loss_gp"] for row in rows), default=0) if covered else None,
    }


def _annotate_challenger_outcome(record: dict, fixture_name: str, attendance: str,
                                 cutoff: str,
                                 outcomes: dict[int, dict], coverage_rows: list[dict],
                                 contract: dict) -> None:
    current = record.pop("_current_orders")
    alternative = record.pop("_alternative_orders")
    before = validate_orders(
        current, fixture_name, attendance, cutoff, outcomes, coverage_rows, contract
    )
    after = validate_orders(
        alternative, fixture_name, attendance, cutoff, outcomes, coverage_rows, contract
    )
    covered = before["status"] == after["status"] == "covered"
    record["withheld_validation"] = {
        "status": "covered" if covered else "uncovered",
        "submitted_utility_gp": before["actual_utility_gp"] if covered else None,
        "challenger_utility_gp": after["actual_utility_gp"] if covered else None,
        "raw_outcome_delta_gp": (
            after["actual_utility_gp"] - before["actual_utility_gp"]
            if covered else None
        ),
    }


def analyze_case(case: dict, frontier: list[dict], visible: dict,
                 outcomes: dict[int, dict], coverage_rows: list[dict],
                 contract: dict, replay_cache: dict | None = None) -> dict:
    """Score a submission and visible challengers, then attach withheld validation."""
    cache = replay_cache if replay_cache is not None else {}
    item_map = {int(item["id"]): item for item in visible["items"]}
    feasibility = portfolio_feasibility(
        case["normalized_orders"], case, item_map, contract
    )
    submitted_score = portfolio_score(
        case["normalized_orders"], item_map, case["cash_gp"], contract, cache
    )
    violations = [
        {"rule": f"portfolio_{reason}"}
        for reason in feasibility["reasons"]
    ]
    for row in submitted_score["orders"]:
        item_id = row["executable_order"]["item_id"]
        if not row["positive_visible_utility"]:
            violations.append({"rule": "submitted_nonpositive_visible_utility", "item_id": item_id})
        if not row["evidence_sufficient"]:
            violations.append({"rule": "submitted_insufficient_evidence", "item_id": item_id})
        if not row["position_risk_compliant"]:
            violations.append({"rule": "submitted_position_risk", "item_id": item_id})
    if not submitted_score["portfolio_risk_compliant"]:
        violations.append({"rule": "submitted_portfolio_risk"})

    challengers = visible_challengers(case, frontier, visible, contract, cache)
    if any(row["qualifies"] for row in challengers):
        violations.append({"rule": "lane_local_visible_qualifying_challenger"})
    for row in challengers:
        _annotate_challenger_outcome(
            row, case["fixture"], case["attendance"], visible["as_of"],
            outcomes, coverage_rows, contract
        )
    withheld = validate_orders(
        case["normalized_orders"], case["fixture"], case["attendance"], visible["as_of"],
        outcomes, coverage_rows, contract,
    )
    return {
        "frontier_cardinality": len(frontier),
        "portfolio_feasibility": feasibility,
        "submitted_visible_score": submitted_score,
        "challengers": challengers,
        "withheld_validation": withheld,
        "violations": violations,
        "lane_local_visible_gate_pass": not violations,
    }


def summarize(cases: list[dict], fixture_audits: list[dict],
              coverage_rows: list[dict], contract: dict) -> dict:
    """Gate lane-local observed and synthetic cohorts independently."""
    result = {}
    threshold_names = [
        row["name"] for row in contract["challenger_materiality"]["candidates"]
    ]
    for fixture_class in contract["fixture_classes"]:
        cohort = [case for case in cases if case["fixture_class"] == fixture_class]
        audits = [row for row in fixture_audits if row["fixture_class"] == fixture_class]
        coverage = [row for row in coverage_rows if row["fixture_class"] == fixture_class]
        validations = [
            case["analysis"]["withheld_validation"]
            for case in cohort
        ]
        order_rows = [row for validation in validations for row in validation["orders"]]
        covered_orders = sum(row["covered"] for row in order_rows)
        covered_rate = covered_orders / len(order_rows) if order_rows else 1.0
        covered_cases = [
            validation for validation in validations
            if validation["status"] == "covered"
        ]
        aggregate_utility = sum(
            validation["actual_utility_gp"] for validation in covered_cases
        )
        visible_violations = [
            violation
            for case in cohort
            for violation in case["analysis"]["violations"]
        ]
        provenance_pass = bool(audits) and all(row["passed"] for row in audits)
        admitted_item_coverage_pass = (
            bool(coverage) and all(row["covered"] for row in coverage)
        )
        visible_pass = not visible_violations
        withheld_pass = (
            covered_rate >= contract["minimum_cohort_covered_order_rate"]
            and aggregate_utility >= 0
        )
        result[fixture_class] = {
            "fixtures": len(audits),
            "cases": len(cohort),
            "provenance_pass": provenance_pass,
            "admitted_item_coverage_pass": admitted_item_coverage_pass,
            "lane_local_visible_submission_and_selection_pass": visible_pass,
            "withheld_validation_pass": withheld_pass,
            "covered_order_rate": round(covered_rate, 6),
            "aggregate_withheld_utility_gp": aggregate_utility,
            "visible_violation_count": len(visible_violations),
            "qualifying_challenger_cases": sum(
                any(row["qualifies"] for row in case["analysis"]["challengers"])
                for case in cohort
            ),
            "qualifying_challenger_cases_by_materiality": {
                name: sum(
                    any(
                        row["qualifies_by_materiality"][name]
                        for row in case["analysis"]["challengers"]
                    )
                    for case in cohort
                )
                for name in threshold_names
            },
            "lane_local_gate_pass": (
                provenance_pass
                and admitted_item_coverage_pass
                and visible_pass
                and withheld_pass
            ),
        }
    return {
        "by_fixture_class": result,
        "scope": "lane_local",
        "whole_planner_portfolio_optimality_assessed": False,
        "lane_local_overall_pass": bool(result) and all(
            row["lane_local_gate_pass"] for row in result.values()
        ),
        "cash_dominance_gate": False,
        "utilization_gate": False,
    }
