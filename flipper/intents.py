"""Write the thin pending-intent queue consumed by the Flipping Utilities fork.

It records only the exact offer signatures the plugin needs to tag the next
matching RuneLite offer event, then the plugin removes the matched line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from .runelite import profile_rsn

PLAN_SECTIONS = ("sell_fills", "buys", "patient_probes", "active_buys", "time_buys")
INTENT_QUEUE_DIR = "merch-intents"  # RuneLite plugin compatibility path.


def _runelite_home() -> Path:
    return Path(os.environ.get("RUNELITE_HOME", "~/.runelite")).expanduser()


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
                "intentId": row.get("intent_id") or _intent_id(plan, row),
                "itemId": int(row["id"]),
                "side": action,
                "qty": int(row["qty"]),
                "price": int(row["price"]),
                "strategy": row.get("strategy"),
                "note": row.get("reason"),
                "prediction": row.get("predicted"),
                "hardExitAt": row.get("hard_exit_at"),
            })
    return out


def write_intents(intents: list[dict], *, rsn: str | None = None,
                  runelite_home: Path | None = None) -> Path:
    rsn = rsn or profile_rsn()
    if not rsn:
        raise ValueError("set config/settings.json rsn or keep exactly one FU profile export")
    path = (runelite_home or _runelite_home()) / "flipping" / INTENT_QUEUE_DIR / f"{rsn}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(i, separators=(",", ":")) + "\n" for i in intents))
    return path


def _read_plan(path: str | None) -> dict:
    text = Path(path).read_text() if path else sys.stdin.read()
    return json.loads(text)


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="flipper.intents")
    ap.add_argument("plan", nargs="?", help="planner JSON file; defaults to stdin")
    ap.add_argument("--runelite-home", type=Path, default=None,
                    help="RuneLite home; defaults to RUNELITE_HOME or ~/.runelite")
    args = ap.parse_args(argv)

    intents = intents_from_plan(_read_plan(args.plan))
    path = write_intents(intents, runelite_home=args.runelite_home)
    print(json.dumps({"path": str(path), "intents": len(intents)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
