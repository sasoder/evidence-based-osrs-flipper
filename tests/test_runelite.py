from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from flipper import runelite

# Fixture exports are always written and read under this name; tests must not
# depend on the machine-local config/settings.json rsn.
_TEST_RSN = "Tester"


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class FlipParsingTests(unittest.TestCase):
    """Flipping Utilities stores every offer (both sides) in h.sO; the side lives on each
    offer's b/st, not the list it came from. Regression guard for buys being miscounted as
    sells (bought=0), which silently breaks fill reconciliation."""

    def _read(self, tmp_path, records: list[dict]) -> list[dict]:
        flip_dir = tmp_path / "flipping"
        flip_dir.mkdir()
        (flip_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
            "trades": records,
            "lastOffers": {},
        }))
        with (
            patch.object(runelite, "INCOMING", tmp_path),
            patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}),
        ):
            return runelite.read_flips()

    def test_single_profile_export_can_supply_rsn(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            flip_dir = tmp / "flipping"
            flip_dir.mkdir()
            (flip_dir / "DetectedName.json").write_text(json.dumps({
                "trades": [{
                    "id": 5974,
                    "name": "Coconut",
                    "h": {"sO": [{
                        "b": True,
                        "st": "BOUGHT",
                        "id": 5974,
                        "p": 1734,
                        "cQIT": 6000,
                        "t": 1781769616000,
                    }]},
                }],
                "lastOffers": {},
            }))

            with (
                patch.object(runelite, "CONFIG", {**runelite.CONFIG, "rsn": ""}),
                patch.object(runelite, "INCOMING", tmp),
            ):
                self.assertEqual(runelite.profile_rsn(), "DetectedName")
                self.assertEqual(runelite.read_flips()[0]["name"], "Coconut")

    def test_buy_offer_in_sO_is_counted_as_a_buy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            flips = self._read(Path(d), [{
                "id": 5974, "name": "Coconut",
                "h": {"sO": [{"b": True, "st": "BOUGHT", "id": 5974, "p": 1734, "cQIT": 6000, "t": 1781769616000}]},
            }])
        self.assertEqual(len(flips), 1)
        self.assertEqual(flips[0]["bought"], 1734)
        self.assertEqual(flips[0]["sold"], 0)
        self.assertEqual(flips[0]["qty"], 6000)

    def test_sell_offer_in_sO_is_counted_as_a_sell(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            flips = self._read(Path(d), [{
                "id": 561, "name": "Nature rune",
                "h": {"sO": [{"b": False, "st": "SOLD", "id": 561, "p": 200, "cQIT": 1000, "t": 1781769616000}]},
            }])
        self.assertEqual(flips[0]["bought"], 0)
        self.assertEqual(flips[0]["sold"], 200)

    def test_round_trip_in_one_record_reports_both_sides_and_profit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            flips = self._read(Path(d), [{
                "id": 11212, "name": "Dragon arrow",
                "h": {"sO": [
                    {"b": True, "st": "BOUGHT", "p": 2850, "cQIT": 100, "t": 1},
                    {"b": False, "st": "SOLD", "p": 3400, "cQIT": 100, "t": 2},
                ]},
            }])
        self.assertEqual(flips[0]["bought"], 2850)
        self.assertEqual(flips[0]["sold"], 3400)
        self.assertEqual(flips[0]["net_sold"], 3332)
        self.assertEqual(flips[0]["profit"], (3332 - 2850) * 100)

    def test_multiple_buy_cycles_split_into_lots(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            flips = self._read(Path(d), [{
                "id": 12695, "name": "Super combat potion(4)",
                "h": {"sO": [
                    {"uuid": "old-buy", "b": True, "st": "BOUGHT", "p": 12795, "cQIT": 270, "t": 1},
                    {"uuid": "old-sell", "b": False, "st": "SOLD", "p": 12805, "cQIT": 270, "t": 2},
                    {"uuid": "new-buy", "b": True, "st": "BOUGHT", "p": 12499, "cQIT": 2000, "t": 3},
                    {"uuid": "new-sell", "b": False, "st": "SOLD", "p": 12804, "cQIT": 2000, "t": 4},
                ]},
            }])

        self.assertEqual(len(flips), 2)
        self.assertEqual(flips[0]["bought_qty"], 270)
        self.assertEqual(flips[0]["profit"], -66420)
        self.assertEqual(flips[1]["bought"], 12499)
        self.assertEqual(flips[1]["sold_qty"], 2000)
        self.assertEqual(flips[1]["profit"], 98000)

    def test_current_ge_slots_parse_non_empty_open_offers(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            (slot_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                "rsn": _TEST_RSN,
                "exportedAt": "2026-06-23T16:30:00Z",
                "slots": [
                    {
                        "slot": 0,
                        "state": "ACTIVE",
                        "side": "sell",
                        "itemId": 32032,
                        "offerQty": 261,
                        "filledQty": 0,
                        "offerPrice": 41324,
                        "filledPrice": 0,
                        "offerCreationTime": "2026-06-23T10:00:00Z",
                        "ageSeconds": 23400,
                        "beforeLogin": False,
                        "merchIntentId": "intent-1",
                        "merchStrategy": "active-margin",
                        "merchNote": "test note",
                        "merchHardExitAt": "2026-06-23T18:00:00Z",
                    },
                    {
                        "slot": 1,
                        "state": "EMPTY",
                        "side": None,
                        "itemId": None,
                        "offerQty": None,
                        "filledQty": None,
                        "offerPrice": None,
                        "filledPrice": None,
                        "offerCreationTime": None,
                        "ageSeconds": None,
                        "beforeLogin": False,
                    },
                    {
                        "slot": 2,
                        "state": "FILLED",
                        "side": "buy",
                        "itemId": 26219,
                        "offerQty": 1,
                        "filledQty": 1,
                        "offerPrice": 20050010,
                        "filledPrice": 20050010,
                        # Distinct from slot 0's placement time: identical
                        # cross-slot times are the batch-restamp signature and
                        # would be distrusted.
                        "offerCreationTime": "2026-06-23T09:00:00Z",
                        "ageSeconds": 27000,
                        "beforeLogin": False,
                    },
                ],
            }))

            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", tmp / "offer_ages.json"),
                patch.object(runelite, "_FILL_ANCHOR_PATH", tmp / "offer_fills.json"),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                offers = runelite.read_open_offers()

        self.assertEqual(offers, [
            {
                "slot": 0,
                "id": 32032,
                "side": "sell",
                "qty": 261,
                "filled_qty": 0,
                "price": 41324,
                "intent_id": "intent-1",
                "strategy": "active-margin",
                "note": "test note",
                "hard_exit_at": "2026-06-23T18:00:00Z",
                "age_hours": 6.5,
                "age_is_floor": None,
                "last_fill_at": None,
                "last_fill_age_hours": None,
                "state": "ACTIVE",
            },
            {
                "slot": 2,
                "id": 26219,
                "side": "buy",
                "qty": 1,
                "filled_qty": 1,
                "price": 20050010,
                "intent_id": None,
                "strategy": None,
                "note": None,
                "hard_exit_at": None,
                "age_hours": 7.5,
                "age_is_floor": None,
                "last_fill_at": None,
                "last_fill_age_hours": None,
                "state": "FILLED",
            },
        ])

    def test_current_ge_slots_derives_age_when_seconds_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            (slot_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                "rsn": _TEST_RSN,
                "exportedAt": "2026-06-23T16:30:00Z",
                "slots": [{
                    "slot": 4,
                    "state": "ACTIVE",
                    "side": "buy",
                    "itemId": 11090,
                    "offerQty": 4000,
                    "filledQty": 0,
                    "offerPrice": 1377,
                    "filledPrice": 0,
                    "offerCreationTime": "2026-06-23T14:00:00Z",
                    "ageSeconds": None,
                    "beforeLogin": False,
                }],
            }))

            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", tmp / "offer_ages.json"),
                patch.object(runelite, "_FILL_ANCHOR_PATH", tmp / "offer_fills.json"),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                offers = runelite.read_open_offers()

        self.assertEqual(offers[0]["age_hours"], 2.5)
        self.assertEqual(offers[0]["price"], 1377)
        self.assertEqual(offers[0]["state"], "ACTIVE")
        self.assertIsNone(offers[0]["hard_exit_at"])

    def test_current_ge_slots_include_time_since_last_fill(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            flip_dir = tmp / "flipping"
            flip_dir.mkdir()
            (slot_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                "rsn": _TEST_RSN,
                "exportedAt": "2026-06-23T16:30:00Z",
                "slots": [{
                    "slot": 2,
                    "state": "ACTIVE",
                    "side": "buy",
                    "itemId": 21902,
                    "offerQty": 25,
                    "filledQty": 4,
                    "offerPrice": 656421,
                    "offerCreationTime": "2026-06-23T14:00:00Z",
                    "ageSeconds": 9000,
                }],
            }))
            # last_fill_at comes from fill-anchored trade history, not the live snapshot's
            # rewritten `t`. The bogus live `t` (=export time) must be ignored.
            history_ms = int(_epoch("2026-06-23T16:00:00Z") * 1000)
            export_ms = int(_epoch("2026-06-23T16:30:00Z") * 1000)
            (flip_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                "trades": [{
                    "id": 21902,
                    "name": "item",
                    "h": {"sO": [{"uuid": "fill-offer", "st": "BUYING", "cQIT": 4,
                                   "p": 656421, "t": history_ms}]},
                }],
                "lastOffers": {
                    "2": {
                        "id": 21902,
                        "st": "BUYING",
                        "cQIT": 4,
                        "uuid": "fill-offer",
                        "t": export_ms,
                    },
                },
            }))

            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", tmp / "offer_ages.json"),
                patch.object(runelite, "_FILL_ANCHOR_PATH", tmp / "offer_fills.json"),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                offers = runelite.read_open_offers()

        self.assertEqual(offers[0]["last_fill_at"], "2026-06-23T16:00:00+00:00")
        self.assertEqual(offers[0]["last_fill_age_hours"], 0.5)

    def test_open_offer_partial_fill_dated_by_qty_growth_anchor(self) -> None:
        # A partial fill on a still-open offer is invisible to trade history; the qty-growth
        # anchor must date it once filled_qty grows between two exports.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            flip_dir = tmp / "flipping"
            flip_dir.mkdir()
            age_path = tmp / "offer_ages.json"
            anchor_path = tmp / "offer_fills.json"

            def write_export(exported_at: str, filled: int) -> None:
                (slot_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                    "rsn": _TEST_RSN,
                    "exportedAt": exported_at,
                    "slots": [{
                        "slot": 0,
                        "state": "ACTIVE",
                        "side": "sell",
                        "itemId": 30816,
                        "offerQty": 39,
                        "filledQty": filled,
                        "offerPrice": 49164,
                        "offerCreationTime": "2026-06-23T10:00:00Z",
                        "ageSeconds": 23400,
                    }],
                }))
                (flip_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                    "trades": [],  # no terminal history for this still-open offer
                    "lastOffers": {"0": {"id": 30816, "st": "SELLING", "cQIT": filled,
                                          "uuid": "abc", "t": 0}},
                }))

            # First sight at 16:00 with a partial: we can't know when it happened -> unknown.
            write_export("2026-06-23T16:00:00Z", 2)
            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", age_path),
                patch.object(runelite, "_FILL_ANCHOR_PATH", anchor_path),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                first = runelite.read_open_offers()
            self.assertIsNone(first[0]["last_fill_age_hours"])

            # Next export at 16:30 shows growth -> anchor stamps the fill at export time.
            write_export("2026-06-23T16:30:00Z", 5)
            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", age_path),
                patch.object(runelite, "_FILL_ANCHOR_PATH", anchor_path),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                grew = runelite.read_open_offers()
            self.assertEqual(grew[0]["last_fill_at"], "2026-06-23T16:30:00+00:00")
            self.assertEqual(grew[0]["last_fill_age_hours"], 0.0)

    def test_open_offer_last_fill_ignores_history_for_different_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            flip_dir = tmp / "flipping"
            flip_dir.mkdir()
            rsn = _TEST_RSN
            (slot_dir / f"{rsn}.json").write_text(json.dumps({
                "rsn": rsn,
                "exportedAt": "2026-06-23T16:30:00Z",
                "slots": [{
                    "slot": 0,
                    "state": "ACTIVE",
                    "side": "sell",
                    "itemId": 30816,
                    "offerQty": 39,
                    "filledQty": 5,
                    "offerPrice": 49164,
                    "offerCreationTime": "2026-06-23T10:00:00Z",
                    "ageSeconds": 23400,
                }],
            }))
            other_fill_ms = int(_epoch("2026-06-23T16:25:00Z") * 1000)
            (flip_dir / f"{rsn}.json").write_text(json.dumps({
                "trades": [{
                    "id": 30816,
                    "name": "item",
                    "h": {"sO": [{"uuid": "old-offer", "st": "SOLD", "cQIT": 5,
                                   "p": 49164, "t": other_fill_ms}]},
                }],
                "lastOffers": {"0": {"id": 30816, "st": "SELLING", "cQIT": 5,
                                      "uuid": "current-offer", "t": 0}},
            }))

            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.object(runelite, "_OFFER_AGE_PATH", tmp / "offer_ages.json"),
                patch.object(runelite, "_FILL_ANCHOR_PATH", tmp / "offer_fills.json"),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999999),
            ):
                offers = runelite.read_open_offers()

        self.assertIsNone(offers[0]["last_fill_at"])
        self.assertIsNone(offers[0]["last_fill_age_hours"])

    def test_current_ge_slots_reject_stale_export(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            slot_dir = tmp / "ge-slots"
            slot_dir.mkdir()
            (slot_dir / f"{_TEST_RSN}.json").write_text(json.dumps({
                "exportedAt": "2020-01-01T00:00:00Z",
                "slots": [],
            }))

            with (
                patch.object(runelite, "INCOMING", tmp),
                patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}),
            ):
                with self.assertRaisesRegex(RuntimeError, "GE slot export is stale"):
                    runelite.read_open_offers()

    def test_runelite_cli_reports_stale_export_without_traceback(self) -> None:
        err = io.StringIO()
        with (
            patch.object(runelite, "read_open_offers",
                         side_effect=RuntimeError("current GE slot export is stale")),
            redirect_stderr(err),
        ):
            code = runelite._main(["offers"])

        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "error: current GE slot export is stale\n")



class OfferAgeAnchorTests(unittest.TestCase):
    """Age must survive a relog: the plugin forgets placement time, but the
    uuid-keyed anchor reconstructs it."""

    EXPORTED = "2026-06-25T12:00:00Z"

    def _write_export(self, tmp: Path, *, unknown_time: bool) -> None:
        slot_dir = tmp / "ge-slots"
        slot_dir.mkdir()
        flip_dir = tmp / "flipping"
        flip_dir.mkdir()
        rsn = _TEST_RSN
        # Plugin has lost the placement time — both fields null, like a relog.
        (slot_dir / f"{rsn}.json").write_text(json.dumps({
            "rsn": rsn,
            "exportedAt": self.EXPORTED,
            "slots": [{
                "slot": 0, "state": "ACTIVE", "side": "sell", "itemId": 565,
                "offerQty": 100, "filledQty": 0, "offerPrice": 5000,
                "filledPrice": 0, "offerCreationTime": None,
                "ageSeconds": None, "beforeLogin": False,
            }],
        }))
        (flip_dir / f"{rsn}.json").write_text(json.dumps({
            "trades": [],
            "lastOffers": {"0": {"id": 565, "st": "SELLING", "cQIT": 0,
                                 "t": int(_epoch(self.EXPORTED) * 1000),
                                 "uuid": "U-RELOG"}},
            "slotTimers": [{"slotIndex": 0,
                            "offerOccurredAtUnknownTime": unknown_time}],
        }))

    def _run(self, tmp: Path):
        with (
            patch.object(runelite, "INCOMING", tmp),
            patch.object(runelite, "_OFFER_AGE_PATH", tmp / "offer_ages.json"),
            patch.object(runelite, "_FILL_ANCHOR_PATH", tmp / "offer_fills.json"),
            patch.dict(runelite.CONFIG, {"rsn": _TEST_RSN}), patch.object(runelite, "OFFER_SNAPSHOT_STALE_MINUTES", 999_999),
        ):
            return runelite.read_open_offers()

    def test_persisted_anchor_survives_null_plugin_age(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_export(tmp, unknown_time=True)
            placed = int((_epoch(self.EXPORTED) - 24 * 3600) * 1000)
            (tmp / "offer_ages.json").write_text(json.dumps({
                "U-RELOG": {"anchor_ms": placed, "first_seen_ms": placed,
                            "item_id": 565, "side": "sell", "price": 5000,
                            "source": "anchor"},
            }))
            offers = self._run(tmp)
        # Plugin said "no age"; the anchor recovers the real ~24h.
        self.assertEqual(offers[0]["age_hours"], 24.0)

    def test_first_observation_is_used_when_no_plugin_age_or_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_export(tmp, unknown_time=True)
            offers = self._run(tmp)
            persisted = json.loads((tmp / "offer_ages.json").read_text())
        self.assertEqual(offers[0]["age_hours"], 0.0)
        self.assertEqual(persisted["U-RELOG"]["anchor_ms"],
                         int(_epoch(self.EXPORTED) * 1000))

    def test_first_observation_anchors_then_grows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_export(tmp, unknown_time=True)
            first = self._run(tmp)
            self.assertEqual(first[0]["age_hours"], 0.0)  # no evidence yet
            # A later snapshot of the same uuid must show real elapsed time.
            later = "2026-06-25T15:00:00Z"
            rsn = _TEST_RSN
            export = json.loads((tmp / "ge-slots" / f"{rsn}.json").read_text())
            export["exportedAt"] = later
            (tmp / "ge-slots" / f"{rsn}.json").write_text(json.dumps(export))
            second = self._run(tmp)
        self.assertEqual(second[0]["age_hours"], 3.0)

    def test_batch_restamped_times_are_distrusted(self) -> None:
        """FU restamps every open offer with the same placement time at a
        re-observation event while still claiming the times are known. Shared
        stamps must be discarded; a genuinely distinct one must survive."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rsn = _TEST_RSN
            (tmp / "ge-slots").mkdir()
            (tmp / "flipping").mkdir()
            stamp = int((_epoch(self.EXPORTED) - 10 * 3600) * 1000)
            genuine = int((_epoch(self.EXPORTED) - 2 * 3600) * 1000)

            def _slot(i: int, item: int) -> dict:
                return {"slot": i, "state": "ACTIVE", "side": "sell",
                        "itemId": item, "offerQty": 1, "filledQty": 0,
                        "offerPrice": 100, "filledPrice": 0,
                        "offerCreationTime": None, "ageSeconds": None,
                        "beforeLogin": False}

            (tmp / "ge-slots" / f"{rsn}.json").write_text(json.dumps({
                "rsn": rsn, "exportedAt": self.EXPORTED,
                "slots": [_slot(0, 565), _slot(1, 566), _slot(2, 567)],
            }))
            (tmp / "flipping" / f"{rsn}.json").write_text(json.dumps({
                "trades": [],
                "lastOffers": {
                    "0": {"id": 565, "st": "SELLING", "cQIT": 0, "uuid": "U-A"},
                    "1": {"id": 566, "st": "SELLING", "cQIT": 0, "uuid": "U-B"},
                    "2": {"id": 567, "st": "SELLING", "cQIT": 0, "uuid": "U-C"},
                },
                "slotTimers": [
                    {"slotIndex": 0, "offerOccurredAtUnknownTime": False,
                     "tradeStartTime": stamp},
                    {"slotIndex": 1, "offerOccurredAtUnknownTime": False,
                     "tradeStartTime": stamp + 2},
                    {"slotIndex": 2, "offerOccurredAtUnknownTime": False,
                     "tradeStartTime": genuine},
                ],
            }))
            offers = self._run(tmp)
        by_slot = {o["slot"]: o for o in offers}
        # Restamped pair falls back to first observation; the genuine time survives.
        self.assertEqual(by_slot[0]["age_hours"], 0.0)
        self.assertEqual(by_slot[1]["age_hours"], 0.0)
        self.assertEqual(by_slot[2]["age_hours"], 2.0)

    def test_trusted_plugin_time_used_when_not_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_export(tmp, unknown_time=False)
            rsn = _TEST_RSN
            flip = json.loads((tmp / "flipping" / f"{rsn}.json").read_text())
            flip["slotTimers"][0]["tradeStartTime"] = int(
                (_epoch(self.EXPORTED) - 5 * 3600) * 1000)
            (tmp / "flipping" / f"{rsn}.json").write_text(json.dumps(flip))
            offers = self._run(tmp)
        self.assertEqual(offers[0]["age_hours"], 5.0)
        self.assertFalse(offers[0]["age_is_floor"])

    def test_observation_anchored_age_stays_flagged_as_floor(self) -> None:
        # No trusted placement evidence: the age counts from when this harness first
        # saw the offer, so it is a lower bound — and stays one on re-observation,
        # because the persisted anchor was never better than an observation.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_export(tmp, unknown_time=True)
            first = self._run(tmp)
            second = self._run(tmp)
        self.assertTrue(first[0]["age_is_floor"])
        self.assertTrue(second[0]["age_is_floor"])



if __name__ == "__main__":
    unittest.main()
