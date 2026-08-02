"""Persist the planner's pending offer intents for harness-side reconciliation.

Item, side and quantity identify the next matching RuneLite offer. The intended
price breaks ties between otherwise identical pending orders but does not prevent
the harness from binding an offer placed at a different price.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .config import ROOT
from .runelite import profile_rsn

PLAN_SECTIONS = ("sell_fills", "buys", "patient_probes", "active_buys", "time_buys")
INTENT_DIR = ROOT / "state/intents"


def _intent_id(plan: dict, row: dict) -> str:
    raw = "|".join(str(v) for v in (
        plan.get("generated_at"),
        row.get("id"),
        row.get("action"),
        row.get("qty"),
        row.get("price"),
        row.get("strategy"),
    ))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def intents_from_plan(plan: dict) -> list[dict]:
    out = []
    for section in PLAN_SECTIONS:
        for row in plan.get(section, []) or []:
            action = row.get("action")
            if action not in {"buy", "sell"}:
                continue
            out.append({
                "intent_id": row.get("intent_id") or _intent_id(plan, row),
                "item_id": int(row["id"]),
                "side": action,
                "qty": int(row["qty"]),
                "price": int(row["price"]),
                "strategy": row.get("strategy"),
                "note": row.get("reason"),
                "prediction": row.get("predicted"),
                "hard_exit_at": row.get("hard_exit_at"),
                "created_at": plan.get("generated_at"),
            })
    return out


def intent_path(rsn: str) -> Path:
    return INTENT_DIR / f"{rsn}.jsonl"


def read_intents(rsn: str) -> list[dict]:
    path = intent_path(rsn)
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line]
    except FileNotFoundError:
        return []


def _save_intents(rsn: str, rows: list[dict]) -> Path:
    path = intent_path(rsn)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    tmp.replace(path)
    return path


def consume_intents(rsn: str, intent_ids: set[str]) -> None:
    if intent_ids:
        _save_intents(rsn, [
            row for row in read_intents(rsn) if row["intent_id"] not in intent_ids
        ])


def write_intents(intents: list[dict], *, rsn: str | None = None) -> Path:
    rsn = rsn or profile_rsn()
    if not rsn:
        raise ValueError("set config/settings.json rsn or keep exactly one RuneLite profile")
    return _save_intents(rsn, intents)


def _read_plan(path: str | None) -> dict:
    text = Path(path).read_text() if path else sys.stdin.read()
    return json.loads(text)


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="flipper.intents")
    ap.add_argument("plan", nargs="?", help="planner JSON file; defaults to stdin")
    args = ap.parse_args(argv)

    intents = intents_from_plan(_read_plan(args.plan))
    path = write_intents(intents)
    print(json.dumps({"path": str(path), "intents": len(intents)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
