"""Ingest RuneLite exports mirrored by ``flipper.sync`` into data/incoming/.

Inputs produced by RuneLite on the trading machine:

  data/incoming/flipping/*.json           — Flipping Utilities autosave files
  data/incoming/runelite/profiles.json    — built-in RuneLite GE state

CLI:
    python -m flipper.runelite flips       # normalized realized flips
    python -m flipper.runelite offers      # reconciled non-empty GE slots
    python -m flipper.runelite status      # profile and live-source verification
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, load_config

CONFIG = load_config()
INCOMING = ROOT / "data/incoming"


def _configured_rsn() -> str | None:
    rsn = str(CONFIG.get("rsn") or "").strip()
    return rsn or None


def _single_profile_stem(directory: Path) -> str | None:
    if not directory.exists():
        return None
    profiles = sorted(
        p.stem for p in directory.glob("*.json")
        if not p.name.endswith(".backup.json")
        and not p.name.startswith("backupCheckpoints")
        and p.name != "accountwide.json"
    )
    return profiles[0] if len(profiles) == 1 else None


def profile_rsn() -> str | None:
    """Configured RSN, or the only profile visible in FU/core local data."""
    if rsn := _configured_rsn():
        return rsn
    candidates = {stem for stem in (_single_profile_stem(INCOMING / "flipping"),) if stem}
    try:
        snapshot = json.loads((INCOMING / "runelite/profiles.json").read_text())
        names = {
            profile["displayName"] for profile in snapshot["profiles"]
            if profile.get("type") == "STANDARD" and profile.get("displayName")
        }
        if len(names) == 1:
            candidates.update(names)
    except FileNotFoundError:
        pass
    return next(iter(candidates)) if len(candidates) == 1 else None


def _to_int(x) -> int:
    if x is None:
        return 0
    try:
        return int(float(x))
    except ValueError:
        return 0


def _offer_is_buy(offer: dict) -> bool:
    """Classify a current Flipping Utilities offer from its state."""
    state = offer["st"].upper()
    if "BUY" in state or state == "BOUGHT":
        return True
    if "SELL" in state or "SOLD" in state:
        return False
    raise ValueError(f"unknown Flipping Utilities offer state: {state}")


def _offer_qty(offer: dict) -> int:
    return _to_int(offer.get("cQIT"))


def _offer_price(offer: dict) -> int:
    return _to_int(offer.get("p"))


def _offer_ts(offer: dict):
    return offer.get("t")


def _offer_sort_ts(offer: dict) -> int:
    return _to_int(offer.get("t") or offer.get("tradeStartedAt"))


def _fifo_lots(record: dict, offers: list[dict]) -> list[dict]:
    """Split Flipping Utilities' per-item history into FIFO lots.

    FU stores all offers for an item together. Reconciliation needs order-sized lots so a new
    2,000-unit buy is not accidentally graded with an older 270-unit buy in the same item row.
    """
    iid = _to_int(record["id"])
    name = record["name"]
    from .ge_tax import net_sale_price

    lots: list[dict] = []
    for offer in sorted(offers, key=_offer_sort_ts):
        qty = _offer_qty(offer)
        price = _offer_price(offer)
        if qty <= 0 or price <= 0:
            continue
        if _offer_is_buy(offer):
            lots.append({
                "id": iid,
                "name": name,
                "bought": price,
                "sold": 0,
                "bought_qty": qty,
                "sold_qty": 0,
                "qty": qty,
                "profit": 0,
                "closed": _offer_ts(offer),
                "buy_ts": _offer_ts(offer),
                "sell_ts": None,
                "offer_ids": {"buy": offer.get("uuid"), "sells": []},
            })
            continue

        remaining = qty
        net_price = net_sale_price(iid, name, price)
        for lot in lots:
            if remaining <= 0:
                break
            if lot["bought_qty"] <= lot["sold_qty"]:
                continue
            if lot.get("buy_ts") and _offer_ts(offer) and _offer_ts(offer) < lot["buy_ts"]:
                continue
            take = min(remaining, lot["bought_qty"] - lot["sold_qty"])
            sold_total = lot.get("_sold_total", lot["sold"] * lot["sold_qty"]) + take * price
            net_sold_total = lot.get("_net_sold_total", 0) + take * net_price
            lot["_sold_total"] = sold_total
            lot["_net_sold_total"] = net_sold_total
            lot["sold_qty"] += take
            lot["sold"] = sold_total // lot["sold_qty"]
            lot["profit"] += (net_price - lot["bought"]) * take
            lot["net_sold"] = net_sold_total // lot["sold_qty"]
            lot["closed"] = _offer_ts(offer)
            lot["sell_ts"] = _offer_ts(offer)
            lot["offer_ids"]["sells"].append(offer.get("uuid"))
            remaining -= take
        if remaining > 0:
            lots.append({
                "id": iid,
                "name": name,
                "bought": 0,
                "sold": price,
                "bought_qty": 0,
                "sold_qty": remaining,
                "qty": remaining,
                "profit": 0,
                "closed": _offer_ts(offer),
                "buy_ts": None,
                "sell_ts": _offer_ts(offer),
                "offer_ids": {"buy": None, "sells": [offer.get("uuid")]},
            })
    for lot in lots:
        lot.pop("_sold_total", None)
        lot.pop("_net_sold_total", None)
    return lots


def read_offer_history() -> list[dict]:
    """Return FU or core RuneLite trades in a stable, analysis-friendly shape.

    These are terminal/partial snapshots, not a tick-by-tick fill tape. In particular, FU does
    not retain zero-fill cancelled orders, so callers must use the data only as a conservative
    cap and never infer that absent failures did not happen.
    """
    flip_dir = INCOMING / "flipping"
    out: list[dict] = []
    path = _flip_file(flip_dir) if flip_dir.exists() else None
    if path:
        raw = json.loads(path.read_text())
        records = raw["trades"]
        for record in records:
            iid = _to_int(record["id"])
            name = record["name"]
            offers = record["h"]["sO"]
            for offer in offers:
                filled_qty = _offer_qty(offer)
                total_qty = _to_int(offer.get("tQIT")) or filled_qty
                started = _to_int(offer.get("tradeStartedAt"))
                ended = _to_int(_offer_ts(offer))
                duration = max(0, (ended - started) / 1000) if started and ended else None
                out.append({
                    "id": iid,
                    "name": name,
                    "side": "buy" if _offer_is_buy(offer) else "sell",
                    "state": str(offer.get("st") or "").upper(),
                    "filled_qty": filled_qty,
                    "total_qty": total_qty,
                    "price": _offer_price(offer),
                    "timestamp": _offer_ts(offer),
                    "started_at": offer.get("tradeStartedAt"),
                    "duration_seconds": duration,
                    "before_login": bool(offer.get("beforeLogin")),
                    "slot": _to_int(offer.get("s")),
                    "uuid": offer.get("uuid"),
                })
    if out:
        return out
    for trade in _core_trades():
        out.append({
            "id": _to_int(trade["i"]),
            "name": f"item_{_to_int(trade['i'])}",
            "side": "buy" if trade["b"] else "sell",
            "state": "BOUGHT" if trade["b"] else "SOLD",
            "filled_qty": _to_int(trade["q"]),
            "total_qty": _to_int(trade["q"]),
            "price": _to_int(trade["p"]),
            "timestamp": trade["t"],
            "started_at": None,
            "duration_seconds": None,
            "before_login": True,
            "slot": None,
            "uuid": None,
        })
    return out


def read_flips() -> list[dict]:
    """Normalize Flipping Utilities records into order-sized FIFO lots.

    Each row is [{id, name, bought, sold, bought_qty, sold_qty, qty, profit, closed}].
    `bought_qty`/`sold_qty` stay distinct so partial positions remain open instead of being
    graded from FU's item-wide aggregate.
    """
    flip_dir = INCOMING / "flipping"
    out: list[dict] = []
    path = _flip_file(flip_dir) if flip_dir.exists() else None
    if path:
        raw = json.loads(path.read_text())
        for record in raw["trades"]:
            out.extend(_fifo_lots(record, record["h"]["sO"]))
    if out:
        return out
    records: dict[int, dict] = {}
    for trade in _core_trades():
        iid = _to_int(trade["i"])
        record = records.setdefault(iid, {
            "id": iid,
            "name": f"item_{iid}",
            "h": {"sO": []},
        })
        record["h"]["sO"].append({
            "b": bool(trade["b"]),
            "st": "BOUGHT" if trade["b"] else "SOLD",
            "p": _to_int(trade["p"]),
            "cQIT": _to_int(trade["q"]),
            "t": trade["t"],
        })
    for record in records.values():
        out.extend(_fifo_lots(record, record["h"]["sO"]))
    return out


_STATE_PATH = ROOT / "state/ge_state.json"
FU_LIVE_MAX_AGE_MINUTES = 2


def _core_snapshot() -> dict:
    try:
        return json.loads((INCOMING / "runelite/profiles.json").read_text())
    except FileNotFoundError as error:
        raise RuntimeError("RuneLite GE snapshot is missing; run flipper.sync") from error


def _core_profile() -> tuple[dict, datetime, datetime]:
    snapshot = _core_snapshot()
    rsn = profile_rsn()
    if not rsn:
        raise RuntimeError("set config/settings.json rsn or keep exactly one RuneLite profile")
    profiles = [
        profile for profile in snapshot["profiles"]
        if profile.get("displayName", "").casefold() == rsn.casefold()
        and profile.get("type") == "STANDARD"
    ]
    selected = CONFIG.get("runelite_profile") or snapshot.get("selectedProfile")
    if selected_profile := next((profile for profile in profiles if profile["key"] == selected), None):
        profile = selected_profile
    elif len(profiles) == 1:
        profile = profiles[0]
    else:
        raise RuntimeError(
            f"RuneLite profile for {rsn!r} is ambiguous; set config/settings.json runelite_profile"
        )
    source_at = datetime.fromisoformat(profile["modifiedAt"].replace("Z", "+00:00"))
    synced_at = datetime.fromisoformat(snapshot["generatedAt"].replace("Z", "+00:00"))
    return profile, source_at, synced_at


def _core_trades() -> list[dict]:
    try:
        profile, _, _ = _core_profile()
    except RuntimeError:
        return []
    return profile.get("tradeHistory", [])


def _trade_key(trade: dict) -> str:
    return ":".join(str(trade[field]) for field in ("b", "i", "q", "p", "t"))


def _state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text())
    except FileNotFoundError:
        return {}


def _save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(_STATE_PATH)


def _flip_file(flip_dir: Path) -> Path | None:
    rsn = profile_rsn()
    if not rsn:
        return None
    profile = flip_dir / f"{rsn}.json"
    return profile if profile.exists() else None


def _fu_snapshot(synced_ms: int) -> tuple[dict[int, dict], int] | None:
    path = _flip_file(INCOMING / "flipping")
    if not path:
        return None
    raw = json.loads(path.read_text())
    stored_ms = _to_int(raw.get("lastStoredAt"))
    if not stored_ms:
        return None
    if synced_ms - stored_ms > FU_LIVE_MAX_AGE_MINUTES * 60_000:
        return None
    return ({int(slot): offer for slot, offer in raw.get("lastOffers", {}).items()}, stored_ms)


def _side(state: str) -> str:
    if state in {"BUYING", "BOUGHT", "CANCELLED_BUY"}:
        return "buy"
    if state in {"SELLING", "SOLD", "CANCELLED_SELL"}:
        return "sell"
    raise ValueError(f"unknown RuneLite GE state: {state}")


def _matches(core: dict | None, item_id: int, side: str, qty: int) -> bool:
    return bool(
        core
        and _to_int(core.get("itemId")) == item_id
        and _side(str(core.get("state")).upper()) == side
        and _to_int(core.get("totalQuantity")) == qty
    )


def _match_intent(
    item_id: int,
    side: str,
    qty: int,
    price: int | None,
    pending: list[dict],
    *,
    partial: bool = False,
) -> dict | None:
    candidates = [
        intent for intent in pending
        if _to_int(intent["item_id"]) == item_id
        and intent["side"] == side
        and (_to_int(intent["qty"]) >= qty if partial else _to_int(intent["qty"]) == qty)
    ]
    if not candidates:
        return None
    if len(candidates) > 1 and not price:
        return None
    return min(
        candidates,
        key=lambda row: abs(_to_int(row["price"]) - (price or 0)),
    )


def read_open_offers() -> list[dict]:
    """Reconcile fresh FU slots with exact core prices, history, and intents."""
    from . import intents

    profile, observed_at, synced_at = _core_profile()
    core_ms = int(observed_at.timestamp() * 1000)
    synced_ms = int(synced_at.timestamp() * 1000)
    rsn = profile["displayName"]
    previous = _state()
    same_profile = previous.get("profile_key") == profile["key"]
    previous_offers = previous.get("offers", {}) if same_profile else {}
    pending = intents.read_intents(rsn)
    consumed: set[str] = set()
    fu_snapshot = _fu_snapshot(synced_ms)
    updates, fu_ms = fu_snapshot or ({}, 0)
    core_offers = profile.get("offers", {})
    prefer_fu = fu_snapshot is not None and fu_ms >= core_ms
    slots = sorted(updates) if prefer_fu else sorted(int(slot) for slot in core_offers)
    current: dict[str, dict] = {}
    out: list[dict] = []

    for slot in slots:
        update = updates.get(slot)
        core = core_offers.get(str(slot))
        if prefer_fu:
            assert update is not None
            state = str(update["st"]).upper()
            item_id = _to_int(update["id"])
            side = "buy" if _offer_is_buy(update) else "sell"
            qty = _to_int(update["tQIT"])
            filled_qty = _to_int(update["cQIT"])
            source = "flipping_utilities"
            observed_ms = fu_ms
            uuid = update.get("uuid")
            identity = f"fu:{uuid}" if uuid else f"fu:{slot}:{item_id}:{side}:{qty}"
        else:
            assert core is not None
            state = str(core["state"]).upper()
            item_id = _to_int(core["itemId"])
            side = _side(state)
            qty = _to_int(core["totalQuantity"])
            filled_qty = _to_int(core["quantitySold"])
            source = "runelite"
            observed_ms = core_ms
            if update and not _matches(core, _to_int(update["id"]),
                                       "buy" if _offer_is_buy(update) else "sell",
                                       _to_int(update["tQIT"])):
                update = None
            uuid = update.get("uuid") if update else None
            identity = (
                f"fu:{uuid}" if uuid else
                f"core:{slot}:{item_id}:{side}:{qty}:{_to_int(core['price'])}"
            )

        old = previous_offers.get(str(slot))
        same_offer = bool(old and old["identity"] == identity)

        placed_ms = old["placed_ms"] if same_offer else observed_ms
        age_is_floor = old["age_is_floor"] if same_offer else True
        if update and not update.get("beforeLogin") and _to_int(update.get("tradeStartedAt")):
            placed_ms = min(placed_ms, _to_int(update["tradeStartedAt"]))
            age_is_floor = False

        last_fill_ms = old["last_fill_ms"] if same_offer else None
        if same_offer and filled_qty > _to_int(old["filled_qty"]):
            last_fill_ms = _to_int((update or {}).get("t")) or observed_ms
        elif not same_offer and filled_qty > 0 and update and not update.get("beforeLogin"):
            last_fill_ms = _to_int(update.get("t")) or None

        intent = old.get("intent") if same_offer else None
        if not intent:
            intent = _match_intent(
                item_id,
                side,
                qty,
                _to_int((core or {}).get("price")) or None,
                [row for row in pending if row["intent_id"] not in consumed],
            )
            if intent:
                consumed.add(intent["intent_id"])

        core_matches = _matches(core, item_id, side, qty)
        started_ms = (
            _to_int(update.get("tradeStartedAt"))
            if update and not update.get("beforeLogin") else 0
        )
        core_is_current = core_matches and (
            not started_ms or core_ms >= started_ms
        )
        if core_is_current:
            price = _to_int(core["price"])
            price_source = "runelite"
        elif same_offer and old.get("price"):
            price = old["price"]
            price_source = old["price_source"]
        elif intent:
            price = _to_int(intent["price"])
            price_source = "intent"
        else:
            price = None
            price_source = "unknown"

        current[str(slot)] = {
            "identity": identity,
            "item_id": item_id,
            "side": side,
            "qty": qty,
            "price": price,
            "price_source": price_source,
            "filled_qty": filled_qty,
            "placed_ms": placed_ms,
            "age_is_floor": age_is_floor,
            "last_fill_ms": last_fill_ms,
            "intent": intent,
        }
        last_fill = (
            datetime.fromtimestamp(last_fill_ms / 1000, tz=timezone.utc)
            if last_fill_ms else None
        )
        planner_state = (
            "FILLED" if state in {"BOUGHT", "SOLD"}
            else "CANCELLED" if state in {"CANCELLED_BUY", "CANCELLED_SELL"}
            else "ACTIVE"
        )
        out.append({
            "slot": slot,
            "id": item_id,
            "side": side,
            "qty": qty,
            "filled_qty": filled_qty,
            "price": price,
            "price_source": price_source,
            "source": source,
            "intent_id": intent.get("intent_id") if intent else None,
            "strategy": intent.get("strategy") if intent else None,
            "note": intent.get("note") if intent else None,
            "hard_exit_at": intent.get("hard_exit_at") if intent else None,
            "age_hours": round(max(0, synced_ms - placed_ms) / 3_600_000, 2),
            "age_is_floor": age_is_floor,
            "last_fill_at": last_fill.isoformat(timespec="seconds") if last_fill else None,
            "last_fill_age_hours": (
                round(max(0, synced_ms - last_fill_ms) / 3_600_000, 2)
                if last_fill_ms else None
            ),
            "state": planner_state,
        })

    trades = profile.get("tradeHistory", [])
    trade_keys = {_trade_key(trade) for trade in trades}
    completed = list(previous.get("completed", [])) if same_profile else []
    if same_profile:
        previous_trade_keys = set(previous.get("trade_keys", []))
        for trade in trades:
            key = _trade_key(trade)
            if key in previous_trade_keys:
                continue
            side = "buy" if trade["b"] else "sell"
            iid = _to_int(trade["i"])
            qty = _to_int(trade["q"])
            linked = next((
                row.get("intent") for row in [*previous_offers.values(), *current.values()]
                if row.get("intent") and row.get("item_id") == iid and row.get("side") == side
                and (row.get("qty") == qty or row.get("filled_qty") == qty)
            ), None)
            if not linked:
                linked = _match_intent(
                    iid,
                    side,
                    qty,
                    _to_int(trade["p"]),
                    [row for row in pending if row["intent_id"] not in consumed],
                    partial=True,
                )
            if linked:
                consumed.add(linked["intent_id"])
            completed.append({
                "key": key,
                "item_id": iid,
                "side": side,
                "qty": qty,
                "price": _to_int(trade["p"]),
                "timestamp": trade["t"],
                "intent": linked,
            })

    _save_state({
        "profile_key": profile["key"],
        "rsn": rsn,
        "slot_source": "flipping_utilities" if prefer_fu else "runelite",
        "flipping_utilities_stored_at": (
            datetime.fromtimestamp(fu_ms / 1000, tz=timezone.utc).isoformat()
            if fu_snapshot else None
        ),
        "observed_at": observed_at.isoformat(),
        "synced_at": synced_at.isoformat(),
        "offers": current,
        "trade_keys": sorted(trade_keys),
        "completed": completed[-1024:],
    })
    intents.consume_intents(rsn, consumed)
    return out


def read_recovered_buys() -> list[dict]:
    """Tracked bought inventory whose GE offer disappeared before observation."""
    state = _state()
    current_items = {offer["item_id"] for offer in state.get("offers", {}).values()}
    lots: list[dict] = []
    for trade in sorted(state.get("completed", []), key=lambda row: _to_int(row["timestamp"])):
        item_id = trade["item_id"]
        if trade["side"] == "buy":
            if trade.get("intent"):
                lots.append({**trade, "remaining_qty": trade["qty"]})
            continue
        remaining = trade["qty"]
        for lot in lots:
            if lot["item_id"] != item_id or lot["remaining_qty"] <= 0:
                continue
            sold = min(remaining, lot["remaining_qty"])
            lot["remaining_qty"] -= sold
            remaining -= sold
            if remaining == 0:
                break

    grouped: dict[int, dict] = {}
    for lot in lots:
        qty = lot["remaining_qty"]
        item_id = lot["item_id"]
        if qty <= 0 or item_id in current_items:
            continue
        row = grouped.setdefault(item_id, {
            "id": item_id,
            "side": "buy",
            "qty": 0,
            "filled_qty": 0,
            "cost": 0,
            "intent_id": lot["intent"]["intent_id"],
            "strategy": lot["intent"].get("strategy"),
            "hard_exit_at": lot["intent"].get("hard_exit_at"),
            "source": "recovered_trade",
        })
        row["qty"] += qty
        row["filled_qty"] += qty
        row["cost"] += qty * lot["price"]

    for row in grouped.values():
        row["price"] = round(row.pop("cost") / row["qty"])
    return list(grouped.values())


def _main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    try:
        if cmd == "flips":
            out = read_flips()
        elif cmd == "offers":
            out = read_open_offers()
        elif cmd == "status":
            offers = read_open_offers()
            state = _state()
            out = {
                "profile": state["rsn"],
                "slot_source": state["slot_source"],
                "flipping_utilities_stored_at": state["flipping_utilities_stored_at"],
                "offers": len(offers),
                "recovered_buys": len(read_recovered_buys()),
                "synced_at": state["synced_at"],
            }
        else:
            print(__doc__)
            return 2
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
