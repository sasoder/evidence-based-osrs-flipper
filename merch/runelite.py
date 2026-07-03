"""Ingest RuneLite exports that arrive (via git) in data/incoming/.

Inputs produced by RuneLite on the trading machine:

  data/incoming/flipping/*.json           — Flipping Utilities autosave files
  data/incoming/ge-slots/*.json           — FU fork current GE slot export

Formats vary slightly by plugin version, so parsing here is deliberately tolerant: we
look for the columns/keys we need and ignore the rest.

CLI:
    python -m merch.runelite flips       # normalized realized flips
    python -m merch.runelite offers      # non-empty GE slots from FU current-slot export
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, load_config

CONFIG = load_config()
INCOMING = ROOT / CONFIG["incoming_dir"]


def _configured_rsn() -> str | None:
    rsn = str(CONFIG.get("rsn") or "").strip()
    return rsn if rsn and rsn != "YOUR_RSN" else None


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
    """Configured RSN, or the only FU profile visible in local incoming exports."""
    if rsn := _configured_rsn():
        return rsn
    candidates = {
        stem for stem in (
            _single_profile_stem(INCOMING / "ge-slots"),
            _single_profile_stem(INCOMING / "flipping"),
        )
        if stem
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def _first(d: dict, *keys, default=None):
    for k in keys:
        for actual in d:
            if actual.lower().replace(" ", "").replace("_", "") == k:
                return d[actual]
    return default


def _to_int(x) -> int:
    if x is None:
        return 0
    s = str(x).replace(",", "").strip()
    try:
        return int(float(s))
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
    iid = _to_int(_first(record, "itemid", "id", default=0))
    name = _first(record, "itemname", "name", "item", default="?")
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
        net_price = net_sale_price(iid, name, price, _offer_ts(offer))
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
    """Return FU's persisted order events in a stable, analysis-friendly shape.

    These are terminal/partial snapshots, not a tick-by-tick fill tape. In particular, FU does
    not retain zero-fill cancelled orders, so callers must use the data only as a conservative
    cap and never infer that absent failures did not happen.
    """
    flip_dir = INCOMING / "flipping"
    out: list[dict] = []
    if not flip_dir.exists():
        return out
    for path in _flip_files(flip_dir):
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
    return out


def read_flips() -> list[dict]:
    """Normalize Flipping Utilities records into order-sized FIFO lots.

    Each row is [{id, name, bought, sold, bought_qty, sold_qty, qty, profit, closed}].
    `bought_qty`/`sold_qty` stay distinct so partial positions remain open instead of being
    graded from FU's item-wide aggregate.
    """
    flip_dir = INCOMING / "flipping"
    out: list[dict] = []
    if not flip_dir.exists():
        return out
    for path in _flip_files(flip_dir):
        raw = json.loads(path.read_text())
        for record in raw["trades"]:
            out.extend(_fifo_lots(record, record["h"]["sO"]))
    return out


def read_open_offers() -> list[dict]:
    """Current GE slots in merch.plan's open-offer shape.

    Uses the patched Flipping Utilities current-slot export because it is the
    authoritative live GE slot snapshot: item, side, quantity, filled quantity,
    listing price, state, and age metadata all come from the client-side slot
    view.
    """
    return _read_open_offers_from_export()


def _read_open_offers_from_export() -> list[dict]:
    """Read FU's current-slot JSON export into merch.plan's open-offer shape.

    Returns non-empty GE slots:
    [{"slot": 0, "id": 32032, "side": "sell", "qty": 261,
      "filled_qty": 0, "price": 41324, "age_hours": 6.5, "state": "ACTIVE"}]
    """
    path = _ge_slots_file()
    if not path:
        raise RuntimeError(
            "current GE slot export is missing; log in with the FU fork running and verify "
            "Export current GE slots is enabled"
        )
    raw = json.loads(path.read_text())

    exported_at = _parse_iso(raw["exportedAt"])
    if not exported_at:
        raise RuntimeError("current GE slot export has no valid exportedAt timestamp")
    snapshot_age = datetime.now(timezone.utc) - exported_at.astimezone(timezone.utc)
    stale_minutes = CONFIG.get("offer_snapshot_stale_minutes", 5)
    if snapshot_age.total_seconds() > stale_minutes * 60:
        age_minutes = snapshot_age.total_seconds() / 60
        raise RuntimeError(
            f"current GE slot export is stale ({age_minutes:.1f}m old; "
            f"limit {stale_minutes}m); the export heartbeats every 10s while logged in, "
            f"so log in to RuneLite (or toggle 'Export current GE slots' off/on) and re-sync"
        )

    updates = _current_offer_updates()
    timers = _current_slot_timers()
    anchors = _load_offer_anchors()
    fill_anchors = _load_fill_anchors()
    fill_index = _last_fill_index()
    exported_ms = int(exported_at.timestamp() * 1000)
    slot_claims = {
        _to_int(slot.get("slot")): _placement_claims_ms(
            slot, updates.get(_to_int(slot.get("slot"))),
            timers.get(_to_int(slot.get("slot"))), exported_ms)
        for slot in raw["slots"] if slot["state"].upper() != "EMPTY"
    }
    suspects = _batch_restamps_ms(slot_claims)
    open_uuids: set[str] = set()
    offers = []
    for slot in raw["slots"]:
        state = slot["state"].upper()
        if state == "EMPTY":
            continue
        side = slot["side"]
        item_id = _to_int(slot["itemId"])
        if side not in ("buy", "sell") or item_id <= 0:
            raise ValueError(f"invalid current GE slot: {slot}")

        slot_idx = _to_int(slot.get("slot"))
        update = updates.get(slot_idx)
        # Only trust the autosave join when item and side agree with the GE slot.
        joined = (
            update is not None
            and _to_int(update.get("id")) == item_id
            and _offer_is_buy(update) == (side == "buy")
        )
        if joined and update.get("uuid"):
            open_uuids.add(update["uuid"])
            age_seconds = _resolve_age_seconds(
                slot, update, timers.get(slot_idx), exported_at, anchors, suspects)
        else:
            # No reliable uuid join (e.g. missing autosave): fall back to the
            # plugin's own age, which may be None.
            age_seconds = _age_seconds(slot, exported_at, suspects)
        filled_qty = _to_int(slot.get("filledQty"))
        last_fill_at = None
        last_fill_age_hours = None
        # Source last-fill time from fill-anchored trade history, not the live snapshot's
        # rewritten `t`. Unknown stays None so the staleness gate treats it as unknown, not fresh.
        offer_uuid = update.get("uuid") if joined else None
        anchor_ms = (
            _resolve_fill_ms(offer_uuid, filled_qty, exported_ms, fill_anchors)
            if offer_uuid else None
        )
        if filled_qty > 0:
            # History dates closed offers; the qty-growth anchor catches partial fills on the
            # still-open offer that history can't see. Newest wins; None only if neither knows.
            fill_ms = anchor_ms
            history_ms = fill_index.get((item_id, side, offer_uuid))
            if history_ms and (not fill_ms or history_ms > fill_ms):
                fill_ms = history_ms
            if fill_ms:
                fill_time = datetime.fromtimestamp(fill_ms / 1000, tz=timezone.utc)
                last_fill_at = fill_time.isoformat(timespec="seconds")
                last_fill_age_hours = round(
                    max(0, (exported_at - fill_time).total_seconds()) / 3600,
                    2,
                )
        offers.append({
            "slot": _to_int(slot.get("slot")),
            "id": item_id,
            "side": side,
            "qty": _to_int(slot.get("offerQty")),
            "filled_qty": filled_qty,
            "price": _to_int(slot.get("offerPrice")),
            "intent_id": slot.get("merchIntentId"),
            "strategy": slot.get("merchStrategy"),
            "note": slot.get("merchNote"),
            "hard_exit_at": (
                slot.get("merchHardExitAt")
                or slot.get("hardExitAt")
                or slot.get("hard_exit_at")
            ),
            "age_hours": round(age_seconds / 3600, 2) if age_seconds is not None else None,
            "last_fill_at": last_fill_at,
            "last_fill_age_hours": last_fill_age_hours,
            "state": state,
        })
    # Drop anchors for offers that are no longer open; keeps the store bounded
    # to the current slots and lets a reused uuid start fresh.
    for stale_uuid in [u for u in anchors if u not in open_uuids]:
        del anchors[stale_uuid]
    if anchors or _OFFER_AGE_PATH.exists():
        _save_offer_anchors(anchors)
    for stale_uuid in [u for u in fill_anchors if u not in open_uuids]:
        del fill_anchors[stale_uuid]
    if fill_anchors or _FILL_ANCHOR_PATH.exists():
        _save_fill_anchors(fill_anchors)
    return sorted(offers, key=lambda o: o["slot"])


def _ge_slots_file() -> Path | None:
    slot_dir = INCOMING / "ge-slots"
    rsn = profile_rsn()
    if not rsn:
        raise ValueError("set config/settings.json rsn or keep exactly one current-slot export")
    profile = slot_dir / f"{rsn}.json"
    return profile if profile.exists() else None


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_seconds(slot: dict, exported_at: datetime | None,
                 suspects: set[int] | None = None) -> float | None:
    if not exported_at:
        return None
    exported_ms = int(exported_at.timestamp() * 1000)
    raw_age = slot.get("ageSeconds")
    if raw_age is not None:
        placed_ms = exported_ms - max(0, _to_int(raw_age)) * 1000
        if not _is_restamped(placed_ms, suspects):
            return max(0, _to_int(raw_age))
    created = _parse_iso(slot.get("offerCreationTime"))
    if not created:
        return None
    created_ms = int(created.timestamp() * 1000)
    if _is_restamped(created_ms, suspects):
        return None
    return max(0, (exported_at - created).total_seconds())


# --- Offer-age anchoring ----------------------------------------------------
# RuneLite's GrandExchangeOffer carries no original placement tick, so Flipping
# Utilities' age resets to ~0 (or null) whenever an offer is re-observed across a
# relog. We instead anchor each offer by its stable `uuid` to the earliest
# *trustworthy* placement evidence and persist it, so age survives relogs.

_STATE_DIR = ROOT / "state"
_OFFER_AGE_PATH = _STATE_DIR / "offer_ages.json"
_FILL_ANCHOR_PATH = _STATE_DIR / "offer_fills.json"
def _load_offer_anchors() -> dict:
    try:
        return json.loads(_OFFER_AGE_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_offer_anchors(anchors: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _OFFER_AGE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(anchors, indent=2, sort_keys=True))
    tmp.replace(_OFFER_AGE_PATH)


def _load_fill_anchors() -> dict:
    try:
        return json.loads(_FILL_ANCHOR_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_fill_anchors(anchors: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _FILL_ANCHOR_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(anchors, indent=2, sort_keys=True))
    tmp.replace(_FILL_ANCHOR_PATH)


def _resolve_fill_ms(uuid: str, filled_qty: int, exported_ms: int,
                     fill_anchors: dict) -> int | None:
    """Last-fill time (ms epoch) for the *currently open* offer, from observed qty growth.

    History only dates terminal offers, so a partial fill on a still-open offer is invisible
    there. We persist the offer's filled_qty by uuid and stamp ``exported_ms`` whenever it
    grows between exports. On first sight we cannot know when an existing partial happened, so
    we record the qty but return None (unknown) rather than claim it just filled.
    """
    stored = fill_anchors.get(uuid)
    prev_qty = _to_int(stored.get("filled_qty")) if stored else None
    fill_ms = _to_int(stored.get("fill_ms")) if stored and stored.get("fill_ms") else None
    if prev_qty is not None and filled_qty > prev_qty:
        fill_ms = exported_ms
    fill_anchors[uuid] = {"filled_qty": filled_qty, "fill_ms": fill_ms}
    return fill_ms


def _current_slot_timers() -> dict[int, dict]:
    files = _flip_files(INCOMING / "flipping")
    if not files:
        return {}
    raw = json.loads(files[0].read_text())
    return {_to_int(t.get("slotIndex")): t for t in (raw.get("slotTimers") or [])}


# Distinct offers are never placed within the same few seconds; when 2+ slots claim
# placement times this close together, FU restamped every open offer at a
# re-observation event (relog/world hop) while still asserting the times are known.
_RESTAMP_EPSILON_MS = 5_000


def _is_restamped(ms: int, suspects: set[int] | None) -> bool:
    return bool(suspects) and any(abs(ms - s) <= _RESTAMP_EPSILON_MS for s in suspects)


def _placement_claims_ms(slot: dict, offer: dict | None, timer: dict | None,
                         exported_ms: int) -> set[int]:
    """Every placement time this slot claims, trusted or not."""
    claims: set[int] = set()
    raw_age = slot.get("ageSeconds")
    if raw_age is not None:
        claims.add(exported_ms - max(0, _to_int(raw_age)) * 1000)
    created = _parse_iso(slot.get("offerCreationTime"))
    if created:
        claims.add(int(created.timestamp() * 1000))
    for source in ((timer or {}).get("tradeStartTime"),
                   (offer or {}).get("tradeStartedAt")):
        ms = _to_int(source)
        if ms > 0:
            claims.add(ms)
    return claims


def _batch_restamps_ms(slot_claims: dict[int, set[int]]) -> set[int]:
    """Placement times claimed by two or more different slots — batch restamps."""
    suspects: set[int] = set()
    for idx, claims in slot_claims.items():
        others = [ms for other_idx, other in slot_claims.items()
                  if other_idx != idx for ms in other]
        suspects.update(
            ms for ms in claims
            if any(abs(ms - o) <= _RESTAMP_EPSILON_MS for o in others)
        )
    return suspects


def _trusted_placements_ms(slot: dict, offer: dict, timer: dict | None,
                           exported_ms: int, suspects: set[int] | None = None) -> list[int]:
    """Placement timestamps (ms epoch) we are willing to trust for one offer.

    FU's `offerOccurredAtUnknownTime` / `beforeLogin` are the plugin admitting its
    own timing is unreliable; when either is set we discard its placement values.
    Batch-restamped times (shared across slots) are discarded the same way.
    """
    out: list[int] = []
    raw_age = slot.get("ageSeconds")
    if raw_age is not None:
        out.append(exported_ms - max(0, _to_int(raw_age)) * 1000)
    created = _parse_iso(slot.get("offerCreationTime"))
    if created:
        out.append(int(created.timestamp() * 1000))
    unknown = bool((timer or {}).get("offerOccurredAtUnknownTime"))
    if not unknown and not offer.get("beforeLogin"):
        for source in ((timer or {}).get("tradeStartTime"),
                       offer.get("tradeStartedAt")):
            ms = _to_int(source)
            if ms > 0:
                out.append(ms)
    return [ms for ms in out
            if ms and ms <= exported_ms + 60_000 and not _is_restamped(ms, suspects)]


def _resolve_age_seconds(slot: dict, offer: dict, timer: dict | None,
                         exported_at: datetime, anchors: dict,
                         suspects: set[int] | None = None) -> float:
    """Age in seconds from the earliest trusted placement, persisted by uuid.

    Once an offer has an anchor it can only get *older* across runs, never reset
    younger — which is exactly the relog failure mode the plugin exhibits.
    """
    exported_ms = int(exported_at.timestamp() * 1000)
    uuid = offer["uuid"]
    stored = anchors.get(uuid)

    candidates = _trusted_placements_ms(slot, offer, timer, exported_ms, suspects)
    if stored:
        candidates.append(_to_int(stored.get("anchor_ms")))

    if candidates:
        anchor_ms = min(c for c in candidates if c)
        source = "anchor"
    else:
        anchor_ms = exported_ms
        source = "observed"

    first_seen = _to_int(stored.get("first_seen_ms")) if stored else exported_ms
    anchors[uuid] = {
        "anchor_ms": anchor_ms,
        "first_seen_ms": min(first_seen or exported_ms, exported_ms),
        "item_id": _to_int(slot["itemId"]),
        "side": slot["side"],
        "price": _to_int(slot.get("offerPrice")),
        "source": source,
    }
    return max(0, (exported_ms - anchor_ms) / 1000)


def _flip_files(flip_dir: Path) -> list[Path]:
    rsn = profile_rsn()
    if not rsn:
        raise ValueError("set config/settings.json rsn or keep exactly one Flipping Utilities export")
    profile = flip_dir / f"{rsn}.json"
    return [profile] if profile.exists() else []


def _last_fill_index() -> dict[tuple[int, str, str], int]:
    """Most recent real fill time (ms epoch) per (item id, side) from FU trade history.

    FU's live ``lastOffers[slot].t`` is rewritten to the snapshot/export time on every
    re-export (login/offer-change/config-toggle), so it cannot date a fill. The persisted
    trade history records each terminal/partial offer's own event time, which is fill-anchored.
    We take the newest matching event as the open offer's last-fill time; absent any match the
    caller reports the age as unknown (None) rather than a fake just-now value.
    """
    flip_dir = INCOMING / "flipping"
    index: dict[tuple[int, str, str], int] = {}
    if not flip_dir.exists():
        return index
    for path in _flip_files(flip_dir):
        raw = json.loads(path.read_text())
        for record in raw["trades"]:
            iid = _to_int(_first(record, "itemid", "id", default=0))
            for offer in record["h"]["sO"]:
                uuid = offer.get("uuid")
                if not uuid:
                    continue
                if _offer_qty(offer) <= 0:
                    continue
                side = "buy" if _offer_is_buy(offer) else "sell"
                ts = _to_int(_offer_ts(offer))
                if ts <= 0:
                    continue
                key = (iid, side, uuid)
                if ts > index.get(key, 0):
                    index[key] = ts
    return index


def _current_offer_updates() -> dict[int, dict]:
    files = _flip_files(INCOMING / "flipping")
    if not files:
        return {}
    raw = json.loads(files[0].read_text())
    return {int(slot): offer for slot, offer in raw["lastOffers"].items()}


def _main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    try:
        if cmd == "flips":
            out = read_flips()
        elif cmd in ("offers", "open-offers"):
            out = read_open_offers()
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
