"""Deterministic planner: turn signals + budget + your open offers into a final instruction
table. **Zero LLM tokens** — selection, the survival gate, offer triage, sizing, reasons and
predictions are all mechanical. The only LLM step in a session is `merch.research`, which produces
a small boost/avoid overlay that this planner consumes; see AGENTS.md.

Pipeline:
  1. scan() for live-low intraday candidates, then gate each on 12h backtest survival.
  2. apply the research overlay: avoid drops a survivor; boost re-ranks survivors without
     bypassing the gate.
  3. triage the open offers you pass in (hold / reprice / cancel).
  4. size survivors to fillable_qty / budget / GE limit across the free GE slots.

CLI:
    python -m merch.plan --cash 76000000 \
        --offers '[{"id":11212,"side":"sell","qty":11000,"price":3390}]' \
        [--overlay overlay.json] [--markdown]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Mapping

from . import execution_stats, ge_tax, runelite, signals

MAX_SLOTS = 8                 # Members GE offer slots
SURVIVAL_MIN_TRADES = 4       # backtest round-trips required to trust a pattern
REPRICE_TOLERANCE = 0.02      # band drift past this fraction triggers a reprice verdict
STALE_BUY_HOURS = 4           # an intraday entry that did not fill has missed its window
STALE_SELL_HOURS = 6          # stale active sells should clear, not chase the sell band
OVERPRICED_ASK_TOLERANCE = 0.05  # a no-band ask this far above the live bid has no evidence
                                 # behind the premium; reprice instead of waiting out staleness
OUTLIER_BID_DROP = 0.10       # a live bid this far below the recent low band is a bad tick, not the market
PATIENT_PROBE_CAP_PCT = 0.05  # experimental near-band bid; measured, never treated as validated
TIME_OF_DAY_BUY_CANCEL_HOURS = 6
# Liquid gp is the amount the user explicitly wants deployed this run, so the budget is all of
# it. Leaving more than 1% unspent gets an explanation instead of silently under-deploying.
DEPLOYMENT_SHORTFALL_PCT = 0.01
MIN_FLIP_PROFIT_PCT = 0.0002  # a slot must net >= this fraction of net worth (backtested realized),
                              # else it is too small to be worth the slot at this bankroll
PATIENT_MIN_NET_MARGIN_GP = 10  # a patient flip's after-tax per-unit spread must clear this many
                                # coins; thinner than this is bid-ask tick noise, not a real edge
                                # (e.g. Ancient essence 16->17 = 1gp), so its modelled profit is
                                # phantom. Absolute coins only — low ROI with decent coins is fine.
ACTIVE_HORIZON_MINUTES = 90
ACTIVE_CANCEL_MINUTES = 30
PLAN_HORIZONS = {"intraday", "overnight"}
OVERNIGHT_FILL_WINDOW_HOURS = signals.MAX_HOLD_HOURS
OVERNIGHT_AWAY_HOURS = 8      # away this long or more is the overnight horizon, not a short absence
PERSONAL_CANDIDATE_LIMIT = 25
DEFAULT_STRATEGIES = frozenset({"patient", "probe", "time", "active"})
STRATEGY_ALIASES = {
    "all": DEFAULT_STRATEGIES,
    "balanced": DEFAULT_STRATEGIES,
    "conservative": frozenset({"patient", "time", "active"}),
    "none": frozenset(),
    "triage": frozenset(),
    "patient": frozenset({"patient"}),
    "patient-band": frozenset({"patient"}),
    "probe": frozenset({"probe"}),
    "patient-probe": frozenset({"probe"}),
    "time": frozenset({"time"}),
    "time-of-day": frozenset({"time"}),
    "active": frozenset({"active"}),
    "active-margin": frozenset({"active"}),
}


def _normalize_strategies(
    strategies: str | list[str] | set[str] | tuple[str, ...] | None,
) -> set[str]:
    if strategies is None:
        return set(DEFAULT_STRATEGIES)
    raw = [strategies] if isinstance(strategies, str) else list(strategies)
    chosen: set[str] = set()
    for value in raw:
        for part in str(value).split(","):
            key = part.strip().lower()
            if not key:
                continue
            if key not in STRATEGY_ALIASES:
                raise ValueError(
                    f"unknown strategy {part!r}; expected one of {sorted(STRATEGY_ALIASES)}"
                )
            chosen.update(STRATEGY_ALIASES[key])
    return chosen


def _survival(item_id: int) -> dict | None:
    """Backtest under the honest time-stop. Returns None if it can't be trusted as a flip."""
    bt = signals.backtest_signal(
        item_id,
        timestep=signals.EXECUTION_TIMESTEP,
        max_hold_points=signals.MAX_HOLD_HOURS,
    )
    if not bt or bt.get("error"):
        return None
    if (
        (bt.get("total_profit_per_unit") or 0) > 0
        and (bt.get("trades") or 0) >= SURVIVAL_MIN_TRADES
        and bt["median_hold_hours"] <= signals.MAX_HOLD_HOURS
    ):
        return bt
    return None


def _by_hours(hours: float = signals.MAX_HOLD_HOURS) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).isoformat(timespec="minutes")


def _deadline_due(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False


def _buy_row(sig: dict, bt: dict, qty: int, execution_evidence: dict | None = None,
             staple: dict | None = None) -> dict:
    personal = ""
    if execution_evidence:
        personal = (
            f"; personal FU cap {execution_evidence['adjusted_fillable_qty']} from "
            f"{execution_evidence['orders']} orders "
            f"({execution_evidence['window_fill_factor']:.0%} window factor)"
        )
    staple_note = ""
    if staple and staple.get("staple"):
        staple_note = (
            f"; personal staple ({staple['profitable_trips']} profitable trips, "
            f"{staple['median_hours']}h median)"
        )
    return {
        "id": sig["id"], "name": sig["name"], "action": "buy", "bucket": "flip",
        "qty": qty, "price": sig["entry_price"], "sell_target": sig["exit_price"],
        "live_low": sig.get("current_low"), "live_high": sig.get("current_high"),
        "horizon": "0-12h",
        "reason": (f"live low entry @ {sig['entry_price']} within {int(signals.BUY_QUANTILE*100)}th-pctile band; "
                   f"6h regime {sig['regime']['level']}; survives 12h reprice-to-clear "
                   f"(+{bt['total_profit_per_unit']:,}/u over {bt['trades']} trips); "
                   f"fillable ~{sig['fillable_qty']}/{int(sig['fill_window_hours'])}h"
                   f"{personal}{staple_note}"),
        "predicted": {"direction": "up", "target": sig["exit_price"],
                      "by": _by_hours()},
        "confidence": 0.65 if staple and staple.get("staple") else 0.60,
        "strategy": "patient-band",
        **({"staple_evidence": staple} if staple and staple.get("staple") else {}),
        **({"execution_stats": execution_evidence} if execution_evidence else {}),
    }


def _time_buy_row(sig: dict, qty: int) -> dict:
    return {
        "id": sig["id"],
        "name": sig["name"],
        "action": "buy",
        "bucket": "flip-time-of-day",
        "strategy": "time-of-day",
        "qty": qty,
        "price": sig["entry_price"],
        "sell_target": sig["exit_price"],
        "live_low": sig.get("current_low"),
        "live_high": sig.get("current_high"),
        "horizon": "6-24h",
        "reason": (
            f"UTC pattern {sig['entry_window_utc']} buy → {sig['exit_window_utc']} sell; "
            f"newest holdout {sig['test']['win_rate']:.0%} wins over "
            f"{sig['test']['trades']} samples, "
            f"{sig['test']['median_profit_per_unit']:,}gp/u median after tax; "
            "sized to fillability/GE limit, cancel zero-fill after "
            f"{TIME_OF_DAY_BUY_CANCEL_HOURS}h"
        ),
        "predicted": {
            "direction": "up",
            "target": sig["exit_price"],
            "by": _by_hours(sig["hold_hours"]),
        },
        "hard_exit_at": _by_hours(24),
        "expected_profit": sig["expected_profit_per_unit"] * qty,
        "pattern_evidence": {
            "entry_window_utc": sig["entry_window_utc"],
            "exit_window_utc": sig["exit_window_utc"],
            "hold_hours": sig["hold_hours"],
            "train": sig["train"],
            "test": sig["test"],
        },
    }


def _active_buy_row(sig: dict, qty: int, expected_profit: int) -> dict:
    return {
        "id": sig["id"],
        "name": sig["name"],
        "action": "buy",
        "bucket": "flip-active",
        "qty": qty,
        "price": sig["entry_price"],
        "sell_target": sig["exit_price"],
        "live_low": sig.get("current_low"),
        "live_high": sig.get("current_high"),
        "horizon": "0-90m",
        "reason": (
            f"active margin probe: fresh two-sided prints "
            f"({sig['high_age_minutes']:.1f}/{sig['low_age_minutes']:.1f}m old); "
            f"net spread {sig['net_margin']:,}/u after tax ({sig['roi_pct']:.2f}%); "
            f"1h flow {sig['high_vol_1h']} buys/{sig['low_vol_1h']} sells; "
            f"90m fillable ~{sig['fillable_qty']}, forced-exit EV "
            f"{sig['expected_value_per_unit']:,}/u; "
            f"cancel unfilled after {ACTIVE_CANCEL_MINUTES}m, hard exit by "
            f"{ACTIVE_HORIZON_MINUTES}m"
        ),
        "predicted": {
            "direction": "up",
            "target": sig["exit_price"],
            "by": _by_hours(ACTIVE_HORIZON_MINUTES / 60),
        },
        "confidence": 0.45 if sig.get("short_drift_pct") is None else 0.55,
        "strategy": "active-margin",
        "expected_profit": expected_profit,
    }


def _sell_fill_row(offer: dict, triage: dict) -> dict | None:
    if offer.get("side") != "buy" or triage.get("verdict") not in {"cancel", "collect"}:
        return None
    qty = int(_num(offer.get("filled_qty")))
    if qty <= 0:
        return None
    market = signals.item_signal(offer["id"]) or signals.live_quote(offer["id"])
    price = (market or {}).get("exit_price") or _sane_bid(market)
    if not price:
        raise ValueError(f"cannot price filled buy for resale: {offer}")
    reason = f"sell {qty} filled unit(s) after {triage['verdict']}ing the buy offer"
    if not offer.get("intent_id") and not offer.get("strategy"):
        reason += " — untracked buy, resale will not be strategy-graded"
    return {
        "id": offer["id"],
        "name": triage.get("name") or (market or {}).get("name"),
        "action": "sell",
        "bucket": "sell-fill",
        "strategy": offer.get("strategy") or "sell-fill",
        "qty": qty,
        "price": price,
        "sell_target": price,
        "live_low": (market or {}).get("current_low"),
        "live_high": (market or {}).get("current_high"),
        "horizon": "0-12h",
        "reason": reason,
        "predicted": {"direction": "up", "target": price, "by": _by_hours()},
        "confidence": 0.40,
    }


def _project_after_triage(offers: list[dict], triage: list[dict], budget: int) -> dict:
    free_slots = MAX_SLOTS
    released_buy_gp = 0
    locked_buy_gp = 0
    sell_fills = []
    for offer, row in zip(offers, triage):
        verdict = row.get("verdict")
        filled = int(_num(offer.get("filled_qty")))
        qty = int(_num(offer.get("qty")))
        price = int(_num(offer.get("price")))
        unfilled_gp = max(0, qty - filled) * price

        if verdict in {"cancel", "collect"}:
            if offer.get("side") == "buy":
                released_buy_gp += unfilled_gp
            sell = _sell_fill_row(offer, row)
            if sell:
                sell_fills.append(sell)
            continue

        free_slots -= 1
        if offer.get("side") == "buy":
            locked_buy_gp += unfilled_gp

    return {
        "free_slots": max(0, free_slots),
        "budget_left": budget + released_buy_gp,
        "released_buy_gp": released_buy_gp,
        "locked_buy_gp": locked_buy_gp,
        "sell_fills": sell_fills,
    }


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _fmt_live(row: dict) -> str:
    low = row.get("live_low")
    high = row.get("live_high")
    if high is None and low is None:
        return ""
    if high is None:
        return f"{_fmt(low)}/-"
    if low is None:
        return f"-/{_fmt(high)}"
    return f"{_fmt(low)}/{_fmt(high)}"


def _action_rows(p: dict) -> list[dict]:
    rows = []

    def add(**row) -> None:
        rows.append({"step": len(rows) + 1, **row})

    for o in p.get("offer_triage", []):
        action = o["verdict"]
        price = o.get("new_price") if action == "reprice" else o.get("price")
        add(
            strategy="open-offer",
            action=action,
            item=o.get("name"),
            side=o.get("side"),
            qty=o.get("qty"),
            price=price,
            live_low=o.get("live_low"),
            live_high=o.get("live_high"),
            sell_target="",
            deadline="",
            reason=o.get("note"),
        )
    for b in p.get("sell_fills", []):
        add(
            strategy="sell-fill",
            action="sell",
            item=b.get("name"),
            side="sell",
            qty=b.get("qty"),
            price=b.get("price"),
            live_low=b.get("live_low"),
            live_high=b.get("live_high"),
            sell_target=b.get("sell_target"),
            deadline=b.get("predicted", {}).get("by", ""),
            reason=b.get("reason"),
        )
    for section, strategy in (
        ("buys", "patient"),
        ("patient_probes", "probe"),
        ("time_buys", "time-of-day"),
        ("active_buys", "active"),
    ):
        for b in p.get(section, []):
            add(
                strategy=strategy,
                action=b.get("action"),
                item=b.get("name"),
                side=b.get("action"),
                qty=b.get("qty"),
                price=b.get("price"),
                live_low=b.get("live_low"),
                live_high=b.get("live_high"),
                sell_target=b.get("sell_target"),
                deadline=b.get("hard_exit_at") or b.get("predicted", {}).get("by", ""),
                reason=b.get("reason"),
            )
    return rows


def _num(value, default=0):
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _sane_bid(market: dict | None) -> int | None:
    """Live insta-buy price (current_high) with single-tick outliers filtered out.

    The wiki `latest` high is the last insta-buy print and can be a lone anomalous tick
    (observed 2026-06-24: Ext super antifire printed 15000 against a ~20086 recent low band
    and a 19000 live insta-sell). Clearing a stale sell against such a bid books a phantom
    loss. A real bid sits near the recent low band; one that has crashed far below it — while
    the robust `buy_band` percentile of recent lows has not — is noise, so we drop it and let
    the caller hold rather than dump.
    """
    if not market:
        return None
    bid = market.get("current_high")
    if not bid:
        return None
    ref = market.get("buy_band") or market.get("current_low")
    if ref and bid < ref * (1 - OUTLIER_BID_DROP):
        return None
    return bid


def _clear_price(current_price: int, current_high: int | None) -> int | None:
    if not current_high:
        return current_price or None
    if not current_price:
        return current_high
    return min(current_price, current_high)


def _cost_basis() -> dict[int, int]:
    """Per-item weighted-average buy price of units still held (empty if no FU export)."""
    held_qty: dict[int, int] = {}
    cost_sum: dict[int, int] = {}
    for f in runelite.read_flips():
        iid = f.get("id")
        buy = f.get("bought") or 0
        held = (f.get("bought_qty") or 0) - (f.get("sold_qty") or 0)
        if not iid or buy <= 0 or held <= 0:
            continue
        held_qty[iid] = held_qty.get(iid, 0) + held
        cost_sum[iid] = cost_sum.get(iid, 0) + buy * held
    return {iid: round(cost_sum[iid] / held_qty[iid]) for iid in held_qty}


def _personal_execution_stats(window_hours: float = signals.FILL_WINDOW_HOURS) -> dict[int, dict]:
    # Personal stats are an optional sizing cap: missing/unconfigured FU exports must not
    # block planning, so any failure degrades to "no personal evidence".
    try:
        return execution_stats.by_item(window_hours)
    except Exception:
        return {}


def _break_even(cost: int) -> int:
    """Lowest sell price that still nets >= cost after GE's 2% sell tax (ceil of cost/0.98)."""
    return -(-(cost * 100) // 98)


def _apply_cost_guard(res: dict, offer: dict, sig: dict | None, cost: int | None,
                      strategy: str | None = None, hard_exit_due: bool = False) -> dict:
    """Loss-minimising layer for sells whose clear price sits below what we paid.

    The clamp-to-bid fix is right about *unfillable* asks but blind to cost. Before the
    strategy's hard stop, a below-break-even clear becomes the lowest ask that still
    recovers cost after tax: an ask above break-even is lowered to it (strictly more
    fillable, gives up nothing) and one already at/below break-even holds. Each row
    quantifies the clear-now alternative so the user can choose to book the loss early;
    at the hard stop the planner books it itself, matching the backtest's forced exit.
    """
    if cost is None or res.get("verdict") != "reprice" or res.get("side") != "sell":
        return res
    new_price = res.get("new_price")
    be = _break_even(cost)
    if not new_price or new_price >= be:
        return res
    age = _num(offer.get("age_hours"))
    stops: dict[str, tuple[float, str]] = {
        "active-margin": (ACTIVE_HORIZON_MINUTES / 60, "90m active stop-loss"),
        "time-of-day": (24, "24h time-of-day stop-loss"),
    }
    hard_stop, label = stops.get(strategy or "", (signals.MAX_HOLD_HOURS, "12h stop-loss"))
    if age >= hard_stop or hard_exit_due:
        res = dict(res)
        res["note"] = (f"{label}: clearing {new_price} below "
                       f"break-even {be} (cost {cost})")
        return res
    bid = (sig or {}).get("current_high")
    remaining = max(0, int(_num(offer.get("qty"))) - int(_num(offer.get("filled_qty"))))
    alt = ""
    if bid and remaining:
        proceeds = ge_tax.net_sale_price(res.get("id") or 0, res.get("name") or "", bid) * remaining
        loss = cost * remaining - proceeds
        alt = f"; clear now at bid {bid} = -{loss:,}gp realized, frees {proceeds:,}gp"
    base = {k: res[k] for k in res if k != "new_price"}
    ask = int(_num(offer.get("price")))
    if ask > be:
        return {**base, "verdict": "reprice", "new_price": be, "cost_floor": True,
                "note": (f"bid {bid} below break-even {be} (cost {cost}) — lower ask to "
                         f"break-even; below-cost clear waits for the {label}{alt}")}
    return {**base, "verdict": "hold",
            "note": (f"bid {bid} below break-even {be} (cost {cost}) — ask already at/below "
                     f"break-even; below-cost clear waits for the {label}{alt}")}


def _enforce_fillable(res: dict, sig: dict | None) -> dict:
    """Safety net: a sell reprice must never post an ask above the live bid.

    The band-top `sell` target only clears when current_high has actually reached
    it (signals.ready_to_sell). Posting above the live bid sits forever — the bug
    that leaked margin on keel parts / vial of blood / ext antifire. Any reprice
    that would do so is a logic error, not a recoverable state, so we raise rather
    than ship it into the GE. Buys may sit below market on purpose (patient
    accumulation), so only the sell side is gated here.
    """
    if not sig or res.get("verdict") != "reprice":
        return res
    # A cost-floored ask sits above the bid on purpose: it is the lowest price that
    # still recovers cost, chosen over holding an even higher fantasy ask.
    if res.get("cost_floor"):
        return res
    bid = _sane_bid(sig)
    new_price = res.get("new_price")
    if res.get("side") == "sell" and bid and new_price and new_price > bid * (1 + REPRICE_TOLERANCE):
        raise AssertionError(
            f"sell reprice {res.get('id')} -> {new_price} is above live bid {bid}; "
            "would not fill (clamp to current_high)"
        )
    return res


def _open_strategy_by_item(offers: list[dict]) -> dict[int, dict]:
    """Strategy tags come from FU current-slot exports."""
    strategies = {}
    for offer in offers:
        strategy = offer.get("strategy")
        if strategy:
            strategies[offer.get("slot", offer["id"])] = {
                "strategy": strategy,
                "hard_exit_at": offer.get("hard_exit_at"),
            }
    return strategies


def _triage_offer(offer: dict, cost_map: dict[int, int] | None = None,
                  strategy_by_item: Mapping[int, dict] | None = None) -> dict:
    """hold / reprice / cancel verdict for one open GE offer, vs the current band."""
    sig = signals.item_signal(offer["id"])
    quote = sig or signals.live_quote(offer["id"])
    cost = (cost_map or {}).get(offer["id"])
    strategy_context = (strategy_by_item or {}).get(offer.get("slot", offer["id"])) or {}
    strategy = strategy_context.get("strategy")
    hard_exit_due = _deadline_due(strategy_context.get("hard_exit_at"))
    res = _apply_cost_guard(
        _decide_triage(
            offer,
            sig,
            quote,
            strategy=strategy,
            hard_exit_at=strategy_context.get("hard_exit_at"),
        ),
        offer, quote, cost, strategy=strategy, hard_exit_due=hard_exit_due,
    )
    res = _enforce_fillable(res, quote)
    # No live intent and no strategy tag: origin unknown — a manual offer, a call whose
    # intent was already reconciled, or one the harness failed to link. The advice applies
    # either way; the tag only means the outcome will not be strategy-graded. Collect
    # stays unqualified because collecting is always correct.
    if not offer.get("intent_id") and not strategy and res.get("verdict") != "collect":
        res["untracked"] = True
        note = res.get("note")
        res["note"] = (
            f"untracked offer — {note}" if note else "untracked offer"
        )
    # An observation-anchored age is a lower bound, so freshness/staleness reasoning
    # above may have treated a long-parked offer as young. Say so.
    if offer.get("age_is_floor") and res.get("verdict") != "collect":
        res["age_is_floor"] = True
        res["note"] = (f"{res.get('note') or ''} — age ≥{_num(offer.get('age_hours')):g}h "
                       f"(first observed then; placement time unknown)").lstrip(" —")
    return res


def _decide_triage(offer: dict, sig: dict | None, quote: dict | None,
                   strategy: str | None = None, hard_exit_at: str | None = None) -> dict:
    market = sig or quote
    base = {"id": offer["id"], "name": (market or {}).get("name"), "side": offer.get("side"),
            "qty": offer.get("qty"), "price": offer.get("price"),
            "live_low": (market or {}).get("current_low"),
            "live_high": (market or {}).get("current_high"),
            "age_hours": offer.get("age_hours"), "filled_qty": offer.get("filled_qty"),
            "last_fill_age_hours": offer.get("last_fill_age_hours"),
            "state": offer.get("state")}
    state = str(offer.get("state") or "ACTIVE").upper()
    if state == "FILLED":
        return {**base, "verdict": "collect", "note": "filled but uncollected — collect to free the slot"}
    if state == "CANCELLED":
        return {**base, "verdict": "collect", "note": "cancelled but uncollected — collect to free the slot"}
    side = offer.get("side")
    if side in ("buy", "sell") and (offer.get("price") or 0) <= 0:
        raise ValueError(f"open offer has no limit price: {offer}")
    # Backstop for the rare case where age can't be anchored at all (no uuid join):
    # an unknown age must never read as "freshly placed". age_known gates the
    # branches that would otherwise hold a long-parked offer on a phantom 0h.
    age_known = offer.get("age_hours") is not None
    age_hours = _num(offer.get("age_hours"))
    filled_qty = _num(offer.get("filled_qty"))
    last_fill_age = offer.get("last_fill_age_hours")
    idle_hours = _num(last_fill_age) if filled_qty > 0 and last_fill_age is not None else age_hours
    if strategy == "time-of-day":
        if side == "buy":
            if age_hours >= TIME_OF_DAY_BUY_CANCEL_HOURS:
                return {
                    **base,
                    "verdict": "cancel",
                    "note": (
                        f"{TIME_OF_DAY_BUY_CANCEL_HOURS}h UTC entry window expired"
                        if filled_qty <= 0 else
                        f"collect {int(filled_qty)} filled; cancel unfilled remainder after "
                        f"{TIME_OF_DAY_BUY_CANCEL_HOURS}h UTC entry window"
                    ),
                }
            return {**base, "verdict": "hold",
                    "note": "inside scheduled UTC entry window; never reprice upward"}
        if side == "sell":
            if (hard_exit_at and _deadline_due(hard_exit_at)) or age_hours >= 24:
                bid = _sane_bid(quote)
                return {**base, "verdict": "reprice", "new_price": _clear_price(
                    offer.get("price") or 0, bid
                ), "note": "24h time-of-day hard exit — clear at live bid"}
            return {**base, "verdict": "hold",
                    "note": f"scheduled time-of-day exit remains live until {hard_exit_at}"}
    if strategy == "active-margin" and side == "buy":
        if age_hours >= ACTIVE_CANCEL_MINUTES / 60:
            fill_note = (
                f"; collect {int(filled_qty)} filled unit(s) and place the sell on the next run"
                if filled_qty > 0 else ""
            )
            return {**base, "verdict": "cancel",
                    "note": (f"active probe {age_hours * 60:.0f}m old — cancel unfilled "
                             f"remainder{fill_note}")}
        return {**base, "verdict": "hold",
                "note": "active probe inside 30m entry window; never reprice upward"}
    if (
        strategy is None
        and side == "buy"
        and (offer.get("price") or 0) >= signals.ACTIVE_MIN_PRICE
        and age_known
        and age_hours < ACTIVE_CANCEL_MINUTES / 60
    ):
        return {
            **base,
            "verdict": "hold",
            "note": (
                "fresh high-value buy has no FU strategy tag; preserve it through "
                "the 30m active entry window instead of applying patient-band cancellation"
            ),
        }
    hard_exit_due = _deadline_due(hard_exit_at)
    if (
        strategy == "active-margin"
        and side == "sell"
        and (hard_exit_due or age_hours >= ACTIVE_HORIZON_MINUTES / 60)
    ):
        bid = _sane_bid(quote)
        clear = _clear_price(offer.get("price") or 0, bid)
        return {**base, "verdict": "reprice", "new_price": clear,
                "note": f"90m active hard exit — clear at market {clear}"}
    if not sig:
        if side == "buy":
            return {**base, "verdict": "cancel", "note": "no intraday signal — free the slot"}
        price = offer.get("price") or 0
        bid = _sane_bid(quote)
        unproven_fresh = not age_known and filled_qty <= 0
        if (idle_hours >= STALE_SELL_HOURS or unproven_fresh) and bid and bid < price:
            note = (f"no fills for {idle_hours:g}h — clear remaining units at live bid {bid}"
                    if not unproven_fresh else
                    f"age unknown (plugin lost creation time) — clear at live bid {bid}")
            return {**base, "verdict": "reprice", "new_price": bid, "note": note}
        if bid and price > bid * (1 + OVERPRICED_ASK_TOLERANCE):
            return {**base, "verdict": "reprice", "new_price": bid,
                    "note": (f"ask {price} is {100 * (price / bid - 1):.0f}% above live bid "
                             f"{bid} — no band evidence supports the premium; clear at bid")}
        if bid and bid < price:
            return {**base, "verdict": "hold",
                    "note": (f"no intraday band; ask {price} above live bid {bid} but not "
                             f"stale yet — clears after {STALE_SELL_HOURS}h without fills")}
        return {**base, "verdict": "hold", "note": "no intraday band; already at/below live bid"}
    regime_high = sig["regime"]["level"] == "high"
    target = sig["entry_price"] if side == "buy" else sig["exit_price"]
    price = offer.get("price") or 0
    if side == "buy" and regime_high:
        return {**base, "verdict": "cancel", "note": f"regime high ({sig['regime']['reason']}) — free the slot"}
    if side == "buy" and age_known and age_hours >= STALE_BUY_HOURS and filled_qty <= 0:
        return {**base, "verdict": "cancel",
                "note": f"stale {age_hours:g}h with no fills — entry window expired"}
    if (
        side == "buy"
        and strategy != "patient-probe"
        and (not sig.get("price_fresh") or not sig.get("ready_to_buy"))
    ):
        return {**base, "verdict": "cancel", "note": "live market is outside the buy band — do not chase"}
    if side == "buy" and strategy == "patient-probe" and not sig.get("price_fresh"):
        return {**base, "verdict": "cancel", "note": "patient probe lost fresh market data — free the slot"}
    if side == "buy":
        live_low = sig["entry_price"]
        if price > live_low * (1 + REPRICE_TOLERANCE):
            return {**base, "verdict": "reprice", "new_price": live_low,
                    "note": f"lower to live low {live_low}"}
        return {**base, "verdict": "hold",
                "note": f"at or below live low {live_low}; never reprice upward"}
    if side == "sell" and regime_high:
        clear = _clear_price(price, _sane_bid(sig))
        return {**base, "verdict": "reprice", "new_price": clear,
                "note": f"regime high — clear at market {clear}"}
    if side == "sell" and filled_qty > 0 and last_fill_age is not None and idle_hours < STALE_SELL_HOURS:
        return {**base, "verdict": "hold",
                "note": f"last fill {idle_hours:g}h ago — offer is still moving"}
    if side == "sell" and idle_hours >= STALE_SELL_HOURS:
        clear = _clear_price(price, _sane_bid(sig))
        if clear and clear < price:
            return {**base, "verdict": "reprice", "new_price": clear,
                    "note": f"no fills for {idle_hours:g}h — clear remaining units at market {clear}"}
        return {**base, "verdict": "hold",
                "note": f"no fills for {idle_hours:g}h — do not raise above current {price}"}
    if side == "sell" and target and abs(price - target) / target > REPRICE_TOLERANCE:
        # Never raise an ask above the live bid: the band-top sell only fills once
        # current_high reaches it. Clamp to what actually clears, else hold at market.
        bid = _sane_bid(sig)
        achievable = min(target, bid) if bid else target
        if not price or abs(price - achievable) / achievable <= REPRICE_TOLERANCE:
            return {**base, "verdict": "hold",
                    "note": f"already at market bid {bid}; band-top {target} unreachable"}
        return {**base, "verdict": "reprice", "new_price": achievable,
                "note": (f"clear at market {achievable}; band-top {target} above live bid {bid}"
                         if achievable < target else f"band moved; sell target now {target}")}
    if target and abs(price - target) / target > REPRICE_TOLERANCE:
        return {**base, "verdict": "reprice", "new_price": target,
                "note": f"band moved; {side} target now {target}"}
    return {**base, "verdict": "hold", "note": f"inside band (target {target})"}


def _personal_candidate_ids(stats: dict[int, dict]) -> list[int]:
    rows = []
    for iid, item in stats.items():
        rt = item.get("round_trip") or {}
        if (rt.get("net_profit") or 0) <= 0 or (rt.get("profitable_trips") or 0) <= 0:
            continue
        rows.append((
            bool(rt.get("staple")),
            rt.get("median_gp_per_capital_hour") or 0,
            rt.get("net_profit") or 0,
            rt.get("profitable_trips") or 0,
            iid,
        ))
    rows.sort(reverse=True)
    return [iid for *_, iid in rows[:PERSONAL_CANDIDATE_LIMIT]]


def _add_personal_candidates(rows: list[dict], stats: dict[int, dict],
                             fill_window_hours: float) -> list[dict]:
    seen = {row["id"] for row in rows}
    out = list(rows)
    for iid in _personal_candidate_ids(stats):
        if iid in seen:
            continue
        sig = signals.item_signal(iid, fill_window_hours=fill_window_hours)
        if sig:
            out.append(sig)
            seen.add(iid)
    out.sort(key=lambda row: row["score"], reverse=True)
    return out


def plan(cash: int, offers: list[dict] | None = None,
         overlay: dict | None = None, seed_limit: int = 80, candidate_limit: int | None = None,
         active_seed_limit: int | None = None, active_candidate_limit: int = 20,
         time_seed_limit: int = 80, time_candidate_limit: int = 10,
         horizon: str = "intraday", lanes: str | list[str] | set[str] | tuple[str, ...] | None = None,
         max_new_slots: int | None = None, away_hours: float | None = None,
         strategies: str | list[str] | set[str] | tuple[str, ...] | None = None) -> dict:
    if horizon not in PLAN_HORIZONS:
        raise ValueError(f"unknown plan horizon {horizon!r}; expected one of {sorted(PLAN_HORIZONS)}")
    if cash is None:
        raise ValueError("cash is required; pass the liquid gp you want the planner to size against")
    if cash < 0:
        raise ValueError("cash must be non-negative")
    if max_new_slots is not None and not 0 <= max_new_slots <= MAX_SLOTS:
        raise ValueError(f"max_new_slots must be between 0 and {MAX_SLOTS}")
    if away_hours is not None and away_hours < 0:
        raise ValueError("away_hours must be non-negative")
    if lanes is not None and strategies is not None:
        raise ValueError("pass either strategies or lanes, not both")
    strategy_input = strategies if strategies is not None else lanes
    # Attendance is one concept: overnight is just "away long enough". Either input implies the
    # other so no strategy can see a horizon that contradicts the stated absence.
    if horizon == "overnight":
        away_hours = max(away_hours or 0, OVERNIGHT_FILL_WINDOW_HOURS)
    if away_hours is not None and away_hours >= OVERNIGHT_AWAY_HOURS:
        horizon = "overnight"
    enabled_strategies = _normalize_strategies(strategy_input)
    # A strategy is unattendable when its first required management action lands inside the absence.
    active_unattended = away_hours is not None and away_hours * 60 >= ACTIVE_CANCEL_MINUTES
    if active_unattended and enabled_strategies and enabled_strategies <= {"active"}:
        raise ValueError(
            f"contradiction: only the active strategy is requested but you are away "
            f"{away_hours:g}h; active offers need management within {ACTIVE_CANCEL_MINUTES}m — "
            "reduce the absence or allow other strategies"
        )
    offers = offers or []
    overlay = overlay or {}
    boost = {b["id"] for b in overlay.get("boost", [])}
    avoid = {a["id"] for a in overlay.get("avoid", [])}

    liquid = cash
    fill_window_hours = (
        OVERNIGHT_FILL_WINDOW_HOURS if horizon == "overnight" else signals.FILL_WINDOW_HOURS
    )
    cost_map = _cost_basis()
    strategy_by_item = _open_strategy_by_item(offers)
    personal_stats = _personal_execution_stats(fill_window_hours)
    # Minimum worthwhile profit for a slot, scaled to the gp the user explicitly makes available.
    # Backtested realized profit, not paper margin.
    profit_floor = int(liquid * MIN_FLIP_PROFIT_PCT)

    offer_triage = [_triage_offer(o, cost_map, strategy_by_item) for o in offers]
    projection = _project_after_triage(offers, offer_triage, liquid)
    out: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {"liquid_gp": liquid,
                   "open_offers": len(offers),
                   "profit_floor_gp": profit_floor,
                   "horizon": horizon,
                   "away_hours": away_hours,
                   "strategies": sorted(enabled_strategies),
                   "max_new_slots": max_new_slots},
        "offer_triage": offer_triage,
        "sell_fills": projection["sell_fills"],
        "projection": {k: v for k, v in projection.items() if k != "sell_fills"},
        "buys": [], "patient_probes": [], "active_buys": [], "time_buys": [],
        "staples": [
            {"id": iid, "name": item.get("name"), **item["round_trip"]}
            for iid, item in personal_stats.items()
            if item.get("round_trip", {}).get("staple")
        ],
        "skipped": [], "active_skipped": [], "active_filter_summary": {},
        "time_skipped": [], "time_filter_summary": {},
    }

    on_offer = {
        o["id"] for o, row in zip(offers, offer_triage)
        if row.get("verdict") in {"hold", "reprice"}
    }
    free_slots = projection["free_slots"]
    if max_new_slots is not None:
        free_slots = min(free_slots, max_new_slots)
    budget_left = projection["budget_left"]

    # Daily UTC-pattern experiment. This strategy is selected on older 6h history and must remain
    # profitable on the newest 30% before it can compete for slots.
    time_scan = (
        signals.time_of_day_scan(seed_limit=time_seed_limit, limit=time_candidate_limit)
        if "time" in enabled_strategies and time_candidate_limit > 0 else
        {"candidates": [], "evaluated": 0, "rejected_count": 0}
    )
    out["time_filter_summary"] = {
        "evaluated": time_scan.get("evaluated", 0),
        "no_positive_holdout_window": time_scan.get("rejected_count", 0),
    }
    time_survivors = []
    for sig in time_scan.get("candidates", []):
        iid = sig["id"]
        if iid in avoid:
            out["time_skipped"].append({"id": iid, "name": sig["name"],
                                        "reason": "research avoid"})
            continue
        if iid in on_offer:
            out["time_skipped"].append({"id": iid, "name": sig["name"],
                                        "reason": "already on offer"})
            continue
        time_survivors.append(sig)

    # 1+2: scan -> survival gate -> overlay (avoid drops; boost re-ranks survivors).
    survivors = []
    probe_survivors = []
    patient_candidates = []
    if {"patient", "probe"} & enabled_strategies:
        patient_scan = signals.scan(
            seed_limit=seed_limit,
            limit=candidate_limit,
            fill_window_hours=fill_window_hours,
        )
        patient_candidates = _add_personal_candidates(patient_scan, personal_stats, fill_window_hours)
    for sig in patient_candidates:
        iid = sig["id"]
        if iid in avoid:
            out["skipped"].append({"id": iid, "name": sig["name"], "reason": "research avoid"})
            continue
        if iid in on_offer:
            out["skipped"].append({"id": iid, "name": sig["name"], "reason": "already on offer"})
            continue
        if not sig["price_fresh"]:
            out["skipped"].append({"id": iid, "name": sig["name"],
                                   "reason": "stale live quote"})
            continue
        patient_margin = sig.get("margin")
        if patient_margin is not None and patient_margin < PATIENT_MIN_NET_MARGIN_GP:
            out["skipped"].append({"id": iid, "name": sig["name"],
                                   "reason": (f"after-tax margin {patient_margin}gp/u < "
                                              f"{PATIENT_MIN_NET_MARGIN_GP}gp floor — single-coin "
                                              "spread, no real edge")})
            continue
        strategy_group = "production" if sig["ready_to_buy"] else (
            "patient-probe" if sig.get("patient_probe_ready") else None
        )
        if strategy_group == "production" and "patient" not in enabled_strategies:
            out["skipped"].append({"id": iid, "name": sig["name"],
                                   "reason": "patient strategy disabled"})
            continue
        if strategy_group == "patient-probe" and "probe" not in enabled_strategies:
            out["skipped"].append({"id": iid, "name": sig["name"],
                                   "reason": "patient-probe strategy disabled"})
            continue
        if strategy_group is None:
            out["skipped"].append({"id": iid, "name": sig["name"],
                                   "reason": (f"live low {sig.get('distance_to_buy_pct')}% above band "
                                              f"— outside the {signals.PATIENT_PROBE_MAX_DISTANCE_PCT:g}% "
                                              "probe window")})
            continue
        # Regime high = the market is currently breaking; only buy into it with a research thesis
        # (an overlay boost), never on the deterministic signal alone.
        if sig["regime"]["level"] == "high" and iid not in boost:
            out["skipped"].append({"id": iid, "name": sig["name"],
                                   "reason": "regime high — needs research thesis"})
            continue
        bt = _survival(iid)
        if not bt:
            out["skipped"].append({"id": iid, "name": sig["name"], "reason": "fails survival gate"})
            continue
        (survivors if strategy_group == "production" else probe_survivors).append((sig, bt))

    # Active high-value strategy: current after-tax spread probes, not percentile-band holds. Scanned
    # up-front so it competes for the same free slots as the patient/time strategies by expected realized
    # gp/hour, rather than only inheriting whatever slots they leave behind.
    if "active" not in enabled_strategies:
        active_scan = {"candidates": [], "rejected": []}
    elif active_unattended:
        active_scan = {
            "candidates": [],
            "rejected": [{"reason": (
                f"active strategy disabled: away {away_hours:g}h, but active offers need "
                f"management within {ACTIVE_CANCEL_MINUTES}m"
            )}],
        }
    else:
        active_scan = signals.active_margin_scan(
            seed_limit=active_seed_limit,
            limit=active_candidate_limit,
        )
    active_summary: dict[str, int] = {}
    for row in active_scan.get("rejected", []):
        reason = row["reason"]
        active_summary[reason] = active_summary.get(reason, 0) + 1
    out["active_filter_summary"] = active_summary
    active_survivors = []
    for sig in active_scan.get("candidates", []):
        iid = sig["id"]
        if iid in avoid:
            out["active_skipped"].append({"id": iid, "name": sig["name"], "reason": "research avoid"})
        elif iid in on_offer:
            out["active_skipped"].append({"id": iid, "name": sig["name"],
                                          "reason": "already on offer"})
        else:
            active_survivors.append(sig)

    allocations = [
        (
            sig["id"] not in boost,
            -(bt["avg_profit_per_unit"] * sig["fillable_qty"]
              / max(bt["median_hold_hours"], 1)),
            "patient",
            sig,
            bt,
        )
        for sig, bt in survivors
    ] + [
        (
            sig["id"] not in boost,
            -sig["score"],
            "time-of-day",
            sig,
            None,
        )
        for sig in time_survivors
    ] + [
        (
            sig["id"] not in boost,
            -(sig.get("expected_gp_per_hour") or 0),
            "active",
            sig,
            None,
        )
        for sig in active_survivors
    ]
    allocations.sort(key=lambda row: row[:2])

    # Validated patient, time-of-day, and active strategies compete for the free slots by expected
    # realized gp/hour.
    selected_ids = set()
    for _, _, strategy_type, sig, bt in allocations:
        if free_slots <= 0:
            break
        if sig["id"] in selected_ids:
            continue
        skip_target = (
            out["skipped"] if strategy_type == "patient"
            else out["active_skipped"] if strategy_type == "active"
            else out["time_skipped"]
        )
        buy = sig["entry_price"] or 0
        evidence = None
        if strategy_type == "patient":
            personal_fillable, evidence = execution_stats.adjusted_fillable_qty(
                sig["id"], sig["fillable_qty"] or 0, personal_stats
            )
            qty = min(personal_fillable, (budget_left // buy) if buy else 0,
                      sig["ge_limit"] or 10**9)
            per_unit = bt["avg_profit_per_unit"]
        elif strategy_type == "active":
            # active scan already caps fillable_qty by GE limit and two-sided flow.
            qty = min(sig["fillable_qty"] or 0, (budget_left // buy) if buy else 0)
            per_unit = sig["expected_value_per_unit"]
        else:  # time-of-day
            qty = min(sig["fillable_qty"] or 0, (budget_left // buy) if buy else 0,
                      sig["ge_limit"] or 10**9)
            per_unit = sig["expected_profit_per_unit"]
        expected_profit = int(per_unit * qty)
        if qty <= 0:
            skip_target.append({"id": sig["id"], "name": sig["name"],
                                "reason": "no budget/liquidity for a slot"})
            continue
        if expected_profit < profit_floor:
            skip_target.append({
                "id": sig["id"], "name": sig["name"],
                "reason": f"expected profit {expected_profit:,}gp < floor {profit_floor:,}gp",
            })
            continue
        if strategy_type == "patient":
            staple = (personal_stats.get(sig["id"]) or {}).get("round_trip")
            out["buys"].append(_buy_row(sig, bt, qty, evidence, staple))
        elif strategy_type == "active":
            out["active_buys"].append(_active_buy_row(sig, qty, expected_profit))
        else:
            out["time_buys"].append(_time_buy_row(sig, qty))
        budget_left -= qty * buy
        free_slots -= 1
        selected_ids.add(sig["id"])

    # Experimental near-band strategy. It uses the same survival gate but does not pretend that the
    # backtest validates fills above the band. The total capital cap creates the execution evidence
    # needed to calibrate or reject the distance rule later without letting probes dominate.
    probe_survivors.sort(key=lambda sb: -(
        sb[1]["avg_profit_per_unit"] * sb[0]["fillable_qty"]
        / max(sb[1]["median_hold_hours"], 1)
    ))
    probe_cap = min(int(liquid * PATIENT_PROBE_CAP_PCT), budget_left)
    for sig, bt in probe_survivors:
        if free_slots <= 0:
            break
        buy = sig["entry_price"] or 0
        personal_fillable, evidence = execution_stats.adjusted_fillable_qty(
            sig["id"], sig["fillable_qty"] or 0, personal_stats
        )
        qty = min(
            personal_fillable,
            probe_cap // buy if buy else 0,
            sig["ge_limit"] or 10**9,
        )
        expected_profit = int(bt["avg_profit_per_unit"] * qty)
        if qty <= 0 or expected_profit < profit_floor:
            out["skipped"].append({
                "id": sig["id"], "name": sig["name"],
                "reason": (
                    "patient probe has no affordable/liquid quantity"
                    if qty <= 0 else
                    f"patient probe expected profit {expected_profit:,}gp < floor {profit_floor:,}gp"
                ),
            })
            continue
        out["patient_probes"].append({
            "id": sig["id"],
            "name": sig["name"],
            "action": "buy",
            "bucket": "flip-patient-probe",
            "strategy": "patient-probe",
            "qty": qty,
            "price": buy,
            "sell_target": sig["exit_price"],
            "live_low": sig.get("current_low"),
            "live_high": sig.get("current_high"),
            "horizon": "0-12h",
            "reason": (
                f"experimental patient bid at {sig['buy_band']:,}, "
                f"{sig['distance_to_buy_pct']:.2f}% below latest instant-sell print; "
                f"survives band backtest (+{bt['total_profit_per_unit']:,}/u over "
                f"{bt['trades']} trips), but fill reachability is unvalidated; "
                f"cancel zero-fill after {STALE_BUY_HOURS}h"
            ),
            "predicted": {"direction": "up", "target": sig["exit_price"], "by": _by_hours()},
            "confidence": 0.45,
            "expected_profit": expected_profit,
            **({"execution_stats": evidence} if evidence else {}),
        })
        spent = qty * buy
        budget_left -= spent
        probe_cap -= spent
        free_slots -= 1

    out["slots"] = {
        "max": MAX_SLOTS,
        "open_offers": len(offers),
        "new_buys": len(out["buys"]),
        "patient_probes": len(out["patient_probes"]),
        "active_buys": len(out["active_buys"]),
        "time_buys": len(out["time_buys"]),
        "free": free_slots,
        "new_slot_cap": max_new_slots,
    }
    out["budget_left_gp"] = budget_left
    planned_buys = out["buys"] + out["patient_probes"] + out["active_buys"] + out["time_buys"]
    planned_buy_gp = sum(b["qty"] * b["price"] for b in planned_buys)
    deployed = projection["locked_buy_gp"] + planned_buy_gp
    utilization = deployed / liquid if liquid else 0
    unspent = max(0, liquid - deployed)
    out["deployment"] = {
        "planned_gp": deployed,
        "planned_buy_gp": planned_buy_gp,
        "unspent_gp": unspent,
        "utilization_pct": round(utilization * 100, 1),
        "constraint": (
            None if unspent <= int(liquid * DEPLOYMENT_SHORTFALL_PCT) else
            "eligible fillability, profit floor, budget, or slot constraints"
        ),
    }
    return out


def _render_md(p: dict) -> str:
    L = [f"# Plan — {p['generated_at']}",
         f"liquid {p['inputs']['liquid_gp']:,}gp (manual) · "
         f"slots {p['slots']['new_buys']+p['slots']['patient_probes']+p['slots']['active_buys']+p['slots']['time_buys']}"
         f"+{p['slots']['open_offers']} used / {p['slots']['max']}"]
    deployment = p.get("deployment")
    if deployment:
        L.append(
            f"deployment {deployment['utilization_pct']:.1f}% "
            f"({deployment['unspent_gp']:,}gp unspent)"
        )
    rows = _action_rows(p)
    if rows:
        L += [
            "",
            "## Actions",
            "| action | item | qty | price | live lo/hi | sell target | deadline | reason |",
            "|---|---|---:|---:|---:|---:|---|---|",
        ]
        for r in rows:
            # Fold the offer side into the action where it isn't implied: new
            # placements are already "buy"/"sell", and collect targets the slot.
            action = r["action"]
            if action not in ("buy", "sell", "collect") and r.get("side"):
                action = f"{action} {r['side']}"
            L.append(
                f"| **{action}** | {_fmt(r['item'])} | "
                f"{_fmt(r['qty'])} | {_fmt(r['price'])} | "
                f"{_fmt_live(r)} | "
                f"{_fmt(r['sell_target'])} | {_fmt(r['deadline'])} | {_fmt(r['reason'])} |"
            )
    return "\n".join(L)


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="merch.plan")
    ap.add_argument("--cash", type=int, required=True, help="liquid gp to size against")
    ap.add_argument(
        "--offers",
        default=None,
        help='JSON override: [{"id","side","qty","price"}]; defaults to live RuneLite export',
    )
    ap.add_argument("--overlay", default=None, help="path to research overlay JSON")
    ap.add_argument("--seed-limit", type=int, default=80)
    ap.add_argument("--limit", type=int, default=0,
                    help="cap evaluated patient candidates (0 evaluates the full seed pool)")
    ap.add_argument(
        "--active-seed-limit",
        type=int,
        default=None,
        help="optional cap after filtering to high-value items; default scans all",
    )
    ap.add_argument("--active-limit", type=int, default=20)
    ap.add_argument("--time-seed-limit", type=int, default=80)
    ap.add_argument("--time-limit", type=int, default=10)
    ap.add_argument("--horizon", choices=sorted(PLAN_HORIZONS), default="intraday",
                    help=("overnight means away 12h+: sizes patient to the 12h window and, like "
                          "any sufficient --away-hours, excludes keyboard-dependent strategies"))
    ap.add_argument("--away-hours", type=float, default=None,
                    help=("hours the user will be away from the keyboard; disables strategies whose "
                          f"offers need management sooner (active: {ACTIVE_CANCEL_MINUTES}m); "
                          f"{OVERNIGHT_AWAY_HOURS}h+ implies --horizon overnight"))
    ap.add_argument(
        "--strategies",
        default=None,
        help=("comma-separated strategies to allow: balanced/all, conservative, patient, probe, "
              "time, active, or triage/none"),
    )
    ap.add_argument("--lanes", dest="lanes", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--max-new-slots", type=int, default=None,
                    help="maximum number of new buy offers to recommend after checking open offers")
    ap.add_argument("--write-intents", action="store_true",
                    help="write pending FU intent tags for new recommendations")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args(argv)

    if args.offers is None:
        try:
            offers = runelite.read_open_offers()
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    else:
        offers = json.loads(args.offers)
    overlay = json.loads(open(args.overlay).read()) if args.overlay else {}
    try:
        p = plan(cash=args.cash, offers=offers, overlay=overlay,
                 seed_limit=args.seed_limit, candidate_limit=(args.limit or None),
                 active_seed_limit=args.active_seed_limit,
                 active_candidate_limit=args.active_limit,
                 time_seed_limit=args.time_seed_limit,
                 time_candidate_limit=args.time_limit,
                 horizon=args.horizon,
                 strategies=args.strategies,
                 lanes=args.lanes,
                 max_new_slots=args.max_new_slots,
                 away_hours=args.away_hours)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.write_intents:
        from . import intents

        written = intents.write_intents(intents.intents_from_plan(p))
        p["intent_queue"] = {"path": str(written)}
    print(_render_md(p) if args.markdown else json.dumps(p, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
