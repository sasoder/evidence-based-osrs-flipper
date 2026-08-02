from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flipper import intents


class IntentTests(unittest.TestCase):
    def test_intents_from_plan_include_order_identity_and_intended_price(self) -> None:
        plan_json = {
            "generated_at": "2026-06-27T12:00:00+00:00",
            "buys": [{
                "id": 7,
                "action": "buy",
                "qty": 3,
                "price": 100,
                "strategy": "patient-band",
                "reason": "exact reason",
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
        self.assertEqual(rows[0]["item_id"], 7)
        self.assertEqual(rows[0]["side"], "buy")
        self.assertEqual(rows[0]["qty"], 3)
        self.assertEqual(rows[0]["price"], 100)
        self.assertEqual(rows[0]["strategy"], "patient-band")
        self.assertEqual(rows[0]["note"], "exact reason")
        self.assertEqual(rows[0]["created_at"], "2026-06-27T12:00:00+00:00")
        self.assertNotIn("prediction", rows[0])
        self.assertNotIn("status", rows[0])

    def test_write_and_consume_harness_intents(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.object(intents, "INTENT_DIR", Path(d)):
                path = intents.write_intents([
                    {"intent_id": "i", "item_id": 7, "side": "buy", "qty": 1, "price": 100},
                    {"intent_id": "keep", "item_id": 8, "side": "buy", "qty": 1, "price": 200},
                ], rsn="Evidence")
                intents.consume_intents("Evidence", {"i"})
                remaining = intents.read_intents("Evidence")

            self.assertEqual(path, Path(d) / "Evidence.jsonl")
            self.assertEqual([row["intent_id"] for row in remaining], ["keep"])



if __name__ == "__main__":
    unittest.main()
