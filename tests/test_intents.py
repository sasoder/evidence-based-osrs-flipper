from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flipper import intents

class IntentTests(unittest.TestCase):
    def test_intents_from_plan_are_exact_offer_signatures(self) -> None:
        plan_json = {
            "generated_at": "2026-06-27T12:00:00+00:00",
            "buys": [{
                "id": 7,
                "action": "buy",
                "qty": 3,
                "price": 100,
                "strategy": "patient-band",
                "reason": "exact reason",
                "predicted": {"direction": "up", "target": 120},
            }],
            "active_buys": [{
                "id": 8,
                "action": "buy",
                "qty": 1,
                "price": 1_000_000,
                "strategy": "active-margin",
                "reason": "active reason",
            }],
            "offer_triage": [{"id": 9, "verdict": "cancel"}],
        }

        rows = intents.intents_from_plan(plan_json)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["itemId"], 7)
        self.assertEqual(rows[0]["side"], "buy")
        self.assertEqual(rows[0]["qty"], 3)
        self.assertEqual(rows[0]["price"], 100)
        self.assertEqual(rows[0]["strategy"], "patient-band")
        self.assertEqual(rows[0]["note"], "exact reason")
        self.assertNotIn("status", rows[0])

    def test_write_intents_targets_plugin_queue(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = intents.write_intents(
                [{"intentId": "i", "itemId": 7, "side": "buy", "qty": 1, "price": 100}],
                rsn="Evidence",
                runelite_home=Path(d),
            )

            self.assertEqual(path, Path(d) / "flipping" / "merch-intents" / "Evidence.jsonl")
            self.assertEqual(json.loads(path.read_text()), {
                "intentId": "i",
                "itemId": 7,
                "side": "buy",
                "qty": 1,
                "price": 100,
            })



if __name__ == "__main__":
    unittest.main()
