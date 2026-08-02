from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from flipper import intents, runelite


class RuneLiteTests(unittest.TestCase):
    def _snapshot(self, root: Path, *, offers: dict, selected: str | None = "current",
                  profiles: list[dict] | None = None,
                  trades: list[dict] | None = None,
                  generated_at: str = "2026-08-02T09:15:00+00:00") -> None:
        path = root / "runelite/profiles.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = profiles or [{
            "key": "current",
            "displayName": "Evidence",
            "type": "STANDARD",
            "offers": offers,
            "tradeHistory": trades or [],
        }]
        path.write_text(json.dumps({
            "generatedAt": generated_at,
            "selectedProfile": selected,
            "profiles": [
                {"modifiedAt": generated_at, **profile} for profile in rows
            ],
        }))

    def _read(self, root: Path, *, now: str | None = None) -> list[dict]:
        snapshot = json.loads((root / "runelite/profiles.json").read_text())
        now_ms = int(datetime.fromisoformat(now or snapshot["generatedAt"]).timestamp() * 1000)
        with (
            patch.object(runelite, "INCOMING", root),
            patch.object(runelite, "_STATE_PATH", root / "state.json"),
            patch.object(intents, "INTENT_DIR", root / "intents"),
            patch.object(runelite, "CONFIG", {**runelite.CONFIG, "rsn": "Evidence"}),
            patch.object(runelite, "_now_ms", return_value=now_ms),
        ):
            return runelite.read_open_offers()

    def test_reads_exact_current_offer_from_core_runelite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={"4": {
                "itemId": 209,
                "quantitySold": 116,
                "totalQuantity": 261,
                "price": 1235,
                "spent": 143260,
                "state": "SELLING",
            }})

            offers = self._read(root)

        self.assertEqual(offers, [{
            "slot": 4,
            "id": 209,
            "side": "sell",
            "qty": 261,
            "filled_qty": 116,
            "price": 1235,
            "price_source": "runelite",
            "source": "runelite",
            "intent_id": None,
            "strategy": None,
            "note": None,
            "hard_exit_at": None,
            "age_hours": 0.0,
            "age_is_floor": True,
            "last_fill_at": None,
            "last_fill_age_hours": None,
            "state": "ACTIVE",
        }])

    def test_selected_profile_resolves_duplicate_standard_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={}, profiles=[
                {"key": "old", "displayName": "Evidence", "type": "STANDARD", "offers": {}},
                {"key": "current", "displayName": "Evidence", "type": "STANDARD", "offers": {
                    "1": {"itemId": 7, "quantitySold": 0, "totalQuantity": 3,
                          "price": 100, "spent": 0, "state": "BUYING"},
                }},
                {"key": "beta", "displayName": "Evidence", "type": "BETA", "offers": {}},
            ])

            offers = self._read(root)

        self.assertEqual(offers[0]["id"], 7)

    def test_terminal_runelite_states_are_normalized_for_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={
                "0": {"itemId": 7, "quantitySold": 3, "totalQuantity": 3,
                      "price": 100, "spent": 300, "state": "BOUGHT"},
                "1": {"itemId": 8, "quantitySold": 0, "totalQuantity": 1,
                      "price": 200, "spent": 0, "state": "CANCELLED_BUY"},
            })

            offers = self._read(root)

        self.assertEqual([offer["state"] for offer in offers], ["FILLED", "CANCELLED"])

    def test_stock_flipping_utilities_enriches_placement_and_fill_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={"4": {
                "itemId": 209, "quantitySold": 116, "totalQuantity": 261,
                "price": 1235, "spent": 143260, "state": "SELLING",
            }}, generated_at="2026-08-02T07:15:00+00:00")
            flipping = root / "flipping"
            flipping.mkdir()
            flipping.joinpath("Evidence.json").write_text(json.dumps({
                "lastStoredAt": 1785654900000,
                "trades": [],
                "lastOffers": {"4": {
                    "id": 209, "st": "SELLING", "tQIT": 261, "cQIT": 116,
                    "tradeStartedAt": 1785654825414, "t": 1785654853000,
                    "beforeLogin": False,
                }},
            }))

            offer = self._read(root)[0]

        self.assertFalse(offer["age_is_floor"])
        self.assertEqual(offer["last_fill_at"], "2026-08-02T07:14:13+00:00")
        self.assertEqual(offer["source"], "flipping_utilities")
        self.assertEqual(offer["price_source"], "runelite")

    def test_fresh_flipping_utilities_binds_intent_before_core_flush(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={}, generated_at="2026-08-02T07:15:00+00:00")
            flipping = root / "flipping"
            flipping.mkdir()
            flipping.joinpath("Evidence.json").write_text(json.dumps({
                "lastStoredAt": 1785654900000,
                "trades": [],
                "lastOffers": {"1": {
                    "uuid": "new-offer", "id": 1127, "st": "BUYING",
                    "tQIT": 1, "cQIT": 0, "p": 0,
                    "tradeStartedAt": 1785654850000, "t": 1785654850000,
                    "beforeLogin": False,
                }},
            }))
            with patch.object(intents, "INTENT_DIR", root / "intents"):
                intents.write_intents([{
                    "intent_id": "fast", "item_id": 1127, "side": "buy", "qty": 1,
                    "price": 1, "strategy": "integration-test",
                    "created_at": "2026-08-02T07:14:00+00:00",
                }], rsn="Evidence")

            offer = self._read(root)[0]

        self.assertEqual(offer["intent_id"], "fast")
        self.assertEqual(offer["price"], 1)
        self.assertEqual(offer["price_source"], "intent")
        self.assertEqual(offer["source"], "flipping_utilities")

    def test_core_replaces_provisional_intent_price_when_it_catches_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={}, generated_at="2026-08-02T07:15:00+00:00")
            flipping = root / "flipping"
            flipping.mkdir()
            fu = {
                "lastStoredAt": 1785654900000,
                "trades": [],
                "lastOffers": {"1": {
                    "uuid": "new-offer", "id": 1127, "st": "BUYING",
                    "tQIT": 1, "cQIT": 0, "p": 0,
                    "tradeStartedAt": 1785654850000, "t": 1785654850000,
                    "beforeLogin": False,
                }},
            }
            flipping.joinpath("Evidence.json").write_text(json.dumps(fu))
            with patch.object(intents, "INTENT_DIR", root / "intents"):
                intents.write_intents([{
                    "intent_id": "fast", "item_id": 1127, "side": "buy", "qty": 1,
                    "price": 1, "created_at": "2026-08-02T07:14:00+00:00",
                }], rsn="Evidence")
            self._read(root)

            self._snapshot(root, offers={"1": {
                "itemId": 1127, "quantitySold": 0, "totalQuantity": 1,
                "price": 2, "spent": 0, "state": "BUYING",
            }}, generated_at="2026-08-02T07:16:00+00:00")
            fu["lastStoredAt"] = 1785654960000
            flipping.joinpath("Evidence.json").write_text(json.dumps(fu))

            confirmed = self._read(root)[0]

        self.assertEqual(confirmed["price"], 2)
        self.assertEqual(confirmed["price_source"], "runelite")
        self.assertEqual(confirmed["intent_id"], "fast")

    def test_fresh_empty_flipping_utilities_overrides_stale_core_offer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={"1": {
                "itemId": 1127, "quantitySold": 0, "totalQuantity": 1,
                "price": 1, "spent": 0, "state": "BUYING",
            }}, generated_at="2026-08-02T07:15:00+00:00")
            flipping = root / "flipping"
            flipping.mkdir()
            flipping.joinpath("Evidence.json").write_text(json.dumps({
                "lastStoredAt": 1785654900000,
                "trades": [],
                "lastOffers": {},
            }))

            offers = self._read(root)

        self.assertEqual(offers, [])

    def test_stale_flipping_utilities_does_not_override_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={"1": {
                "itemId": 1127, "quantitySold": 0, "totalQuantity": 1,
                "price": 1, "spent": 0, "state": "BUYING",
            }}, generated_at="2026-08-02T07:15:00+00:00")
            flipping = root / "flipping"
            flipping.mkdir()
            flipping.joinpath("Evidence.json").write_text(json.dumps({
                "lastStoredAt": 1785654000000,
                "trades": [],
                "lastOffers": {},
            }))

            offer = self._read(root)[0]

        self.assertEqual(offer["source"], "runelite")

    def test_snapshot_must_be_refreshed_before_reading_offers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={}, generated_at="2026-08-02T09:15:00+00:00")

            with self.assertRaisesRegex(RuntimeError, "snapshot is stale"):
                self._read(root, now="2026-08-02T09:18:00+00:00")

    def test_fresh_sync_rejects_stale_core_files_when_fu_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(
                root,
                offers={},
                generated_at="2026-08-02T09:15:00+00:00",
                profiles=[{
                    "key": "current",
                    "displayName": "Evidence",
                    "type": "STANDARD",
                    "offers": {},
                    "modifiedAt": "2026-08-02T08:15:00+00:00",
                }],
            )

            with self.assertRaisesRegex(RuntimeError, "GE state is stale"):
                self._read(root)

    def test_newer_core_snapshot_overrides_fresh_but_older_flipping_utilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={"1": {
                "itemId": 1127, "quantitySold": 0, "totalQuantity": 1,
                "price": 1, "spent": 0, "state": "BUYING",
            }}, generated_at="2026-08-02T07:15:00+00:00")
            flipping = root / "flipping"
            flipping.mkdir()
            flipping.joinpath("Evidence.json").write_text(json.dumps({
                "lastStoredAt": 1785654840000,
                "trades": [],
                "lastOffers": {},
            }))

            offers = self._read(root)

        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["source"], "runelite")

    def test_newer_core_snapshot_keeps_matching_flipping_utilities_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={"1": {
                "itemId": 1127, "quantitySold": 0, "totalQuantity": 1,
                "price": 1, "spent": 0, "state": "BUYING",
            }}, generated_at="2026-08-02T07:15:00+00:00")
            flipping = root / "flipping"
            flipping.mkdir()
            flipping.joinpath("Evidence.json").write_text(json.dumps({
                "lastStoredAt": 1785654840000,
                "trades": [],
                "lastOffers": {"1": {
                    "uuid": "same-offer", "id": 1127, "st": "BUYING",
                    "tQIT": 1, "cQIT": 0, "tradeStartedAt": 1785654780000,
                    "beforeLogin": False,
                }},
            }))

            offer = self._read(root)[0]

        self.assertEqual(offer["source"], "runelite")
        self.assertFalse(offer["age_is_floor"])
        self.assertEqual(offer["age_hours"], 0.03)

    def test_quantity_growth_is_dated_at_the_next_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offer = {
                "itemId": 209, "quantitySold": 1, "totalQuantity": 261,
                "price": 1235, "spent": 1235, "state": "SELLING",
            }
            self._snapshot(root, offers={"4": offer}, generated_at="2026-08-02T09:15:00+00:00")
            self._read(root)
            offer["quantitySold"] = 5
            self._snapshot(root, offers={"4": offer}, generated_at="2026-08-02T09:20:00+00:00")

            updated = self._read(root)[0]

        self.assertEqual(updated["last_fill_at"], "2026-08-02T09:20:00+00:00")
        self.assertEqual(updated["last_fill_age_hours"], 0.0)

    def test_new_offer_consumes_and_retains_matching_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={"0": {
                "itemId": 7, "quantitySold": 0, "totalQuantity": 3,
                "price": 105, "spent": 0, "state": "BUYING",
            }})
            with patch.object(intents, "INTENT_DIR", root / "intents"):
                intents.write_intents([{
                    "intent_id": "match", "item_id": 7, "side": "buy", "qty": 3,
                    "price": 100, "strategy": "patient-band", "note": "reason",
                    "hard_exit_at": "later", "created_at": "2026-08-02T09:14:00+00:00",
                }], rsn="Evidence")

            first = self._read(root)[0]
            second = self._read(root)[0]
            with patch.object(intents, "INTENT_DIR", root / "intents"):
                pending = intents.read_intents("Evidence")

        self.assertEqual(first["intent_id"], "match")
        self.assertEqual(first["strategy"], "patient-band")
        self.assertEqual(second["intent_id"], "match")
        self.assertEqual(pending, [])

    def test_old_intent_does_not_bind_to_a_later_manual_offer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={"0": {
                "itemId": 7, "quantitySold": 0, "totalQuantity": 3,
                "price": 105, "spent": 0, "state": "BUYING",
            }})
            with patch.object(intents, "INTENT_DIR", root / "intents"):
                intents.write_intents([{
                    "intent_id": "old", "item_id": 7, "side": "buy", "qty": 3,
                    "price": 100, "strategy": "patient-band",
                    "created_at": "2026-07-31T09:15:00+00:00",
                }], rsn="Evidence")

            offer = self._read(root)[0]
            with patch.object(intents, "INTENT_DIR", root / "intents"):
                pending = intents.read_intents("Evidence")

        self.assertIsNone(offer["intent_id"])
        self.assertEqual([row["intent_id"] for row in pending], ["old"])

    def test_collected_between_syncs_still_consumes_intent_from_trade_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={})
            self._read(root)
            with patch.object(intents, "INTENT_DIR", root / "intents"):
                intents.write_intents([{
                    "intent_id": "instant", "item_id": 7, "side": "buy", "qty": 3,
                    "price": 100, "strategy": "active-margin",
                    "created_at": "2026-08-02T07:15:00+00:00",
                }], rsn="Evidence")
            self._snapshot(root, offers={}, trades=[
                {"b": True, "i": 7, "q": 3, "p": 105, "t": 1785655000000},
            ], generated_at="2026-08-02T09:20:00+00:00")

            self._read(root)
            state = json.loads((root / "state.json").read_text())
            with patch.object(intents, "INTENT_DIR", root / "intents"):
                pending = intents.read_intents("Evidence")

        self.assertEqual(state["completed"][0]["intent"]["intent_id"], "instant")
        self.assertEqual(pending, [])

    def test_partially_filled_trade_history_matches_larger_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={})
            self._read(root)
            with patch.object(intents, "INTENT_DIR", root / "intents"):
                intents.write_intents([{
                    "intent_id": "partial", "item_id": 7, "side": "buy", "qty": 10,
                    "price": 100, "strategy": "active-margin",
                    "created_at": "2026-08-02T07:15:00+00:00",
                }], rsn="Evidence")
            self._snapshot(root, offers={}, trades=[
                {"b": True, "i": 7, "q": 4, "p": 99, "t": 1785655000000},
            ], generated_at="2026-08-02T09:20:00+00:00")

            self._read(root)
            with patch.object(runelite, "_STATE_PATH", root / "state.json"):
                recovered = runelite.read_recovered_buys()
            with patch.object(intents, "INTENT_DIR", root / "intents"):
                pending = intents.read_intents("Evidence")

        self.assertEqual(recovered[0]["filled_qty"], 4)
        self.assertEqual(recovered[0]["intent_id"], "partial")
        self.assertEqual(pending, [])

    def test_core_trade_history_supplies_flips_without_flipping_utilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={}, trades=[
                {"b": True, "i": 7, "q": 3, "p": 100, "t": 1},
                {"b": False, "i": 7, "q": 3, "p": 110, "t": 2},
            ])
            with (
                patch.object(runelite, "INCOMING", root),
                patch.object(runelite, "CONFIG", {**runelite.CONFIG, "rsn": "Evidence"}),
            ):
                flips = runelite.read_flips()

        self.assertEqual(flips[0]["bought"], 100)
        self.assertEqual(flips[0]["sold"], 110)
        self.assertEqual(flips[0]["sold_qty"], 3)

    def test_flipping_utilities_history_remains_optional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flipping = root / "flipping"
            flipping.mkdir()
            flipping.joinpath("Evidence.json").write_text(json.dumps({
                "trades": [{
                    "id": 5974,
                    "name": "Coconut",
                    "h": {"sO": [
                        {"b": True, "st": "BOUGHT", "p": 1734, "cQIT": 10, "t": 1},
                        {"b": False, "st": "SOLD", "p": 1850, "cQIT": 10, "t": 2},
                    ]},
                }],
                "lastOffers": {},
            }))
            with (
                patch.object(runelite, "INCOMING", root),
                patch.object(runelite, "CONFIG", {**runelite.CONFIG, "rsn": "Evidence"}),
            ):
                flips = runelite.read_flips()

        self.assertEqual(flips[0]["bought"], 1734)
        self.assertEqual(flips[0]["sold"], 1850)

    def test_core_extends_but_does_not_replace_flipping_utilities_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._snapshot(root, offers={}, trades=[
                {"b": False, "i": 5974, "q": 10, "p": 1850, "t": 2},
            ])
            flipping = root / "flipping"
            flipping.mkdir()
            flipping.joinpath("Evidence.json").write_text(json.dumps({
                "trades": [{
                    "id": 5974,
                    "name": "Coconut",
                    "h": {"sO": [
                        {"st": "BOUGHT", "p": 1734, "cQIT": 10, "t": 1},
                    ]},
                }],
            }))
            with (
                patch.object(runelite, "INCOMING", root),
                patch.object(runelite, "CONFIG", {**runelite.CONFIG, "rsn": "Evidence"}),
            ):
                history = runelite.read_offer_history()
                flips = runelite.read_flips()

        self.assertEqual([row["timestamp"] for row in history], [1, 2])
        self.assertEqual(flips[0]["name"], "Coconut")
        self.assertEqual(flips[0]["sold_qty"], 10)

    def test_recovers_tracked_buy_collected_between_syncs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(json.dumps({
                "offers": {},
                "completed": [{
                    "key": "buy",
                    "item_id": 8901,
                    "side": "buy",
                    "qty": 3,
                    "price": 1_000,
                    "timestamp": 1,
                    "intent": {
                        "intent_id": "tracked",
                        "strategy": "patient-band",
                        "hard_exit_at": None,
                    },
                }, {
                    "key": "sell",
                    "item_id": 8901,
                    "side": "sell",
                    "qty": 1,
                    "price": 1_100,
                    "timestamp": 2,
                    "intent": None,
                }],
            }))
            with patch.object(runelite, "_STATE_PATH", state_path):
                recovered = runelite.read_recovered_buys()

        self.assertEqual(recovered[0]["filled_qty"], 2)
        self.assertEqual(recovered[0]["price"], 1_000)
        self.assertEqual(recovered[0]["intent_id"], "tracked")

    def test_does_not_recover_inventory_already_on_offer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(json.dumps({
                "offers": {"2": {"item_id": 8901}},
                "completed": [{
                    "key": "buy",
                    "item_id": 8901,
                    "side": "buy",
                    "qty": 2,
                    "price": 1_000,
                    "timestamp": 1,
                    "intent": {"intent_id": "tracked", "strategy": "patient-band"},
                }],
            }))
            with patch.object(runelite, "_STATE_PATH", state_path):
                recovered = runelite.read_recovered_buys()

        self.assertEqual(recovered, [])


if __name__ == "__main__":
    unittest.main()
