from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import json

from flipper import sync


class SyncTests(unittest.TestCase):
    def _write_profiles(self, runelite: Path) -> None:
        profiles = runelite / "profiles2"
        profiles.mkdir(parents=True)
        (profiles / "$rsprofile--1.properties").write_text("\n".join([
            "rsprofile.rsprofile.current.displayName=Evidence",
            "rsprofile.rsprofile.current.type=STANDARD",
            r'geoffer.rsprofile.current.4={"itemId"\:209.0,"quantitySold"\:116.0,"totalQuantity"\:261.0,"price"\:1235.0,"spent"\:143260.0,"state"\:"SELLING"}',
            r'grandexchange.rsprofile.current.tradeHistory=[{"b"\:false,"i"\:209,"q"\:21,"p"\:1505,"t"\:1781691276570}]',
        ]))

    def test_syncs_flipping_history_and_core_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            runelite = base / "runelite"
            root = base / "repo"
            flipping = runelite / "flipping"
            flipping.mkdir(parents=True)
            (flipping / "Evidence.json").write_text("history")
            (flipping / "Evidence.backup.json").write_text("backup")
            (flipping / "backupCheckpoints-Evidence.json").write_text("checkpoints")
            (flipping / "accountwide.json").write_text("account")
            self._write_profiles(runelite)

            result = sync.sync_exports(runelite_home=runelite, root=root)

            incoming = root / "data/incoming"
            self.assertEqual((incoming / "flipping/Evidence.json").read_text(), "history")
            self.assertFalse((incoming / "flipping/Evidence.backup.json").exists())
            self.assertFalse((incoming / "flipping/backupCheckpoints-Evidence.json").exists())
            self.assertFalse((incoming / "flipping/accountwide.json").exists())
            profiles = json.loads((incoming / "runelite/profiles.json").read_text())
            self.assertEqual(profiles["profiles"][0]["displayName"], "Evidence")
            self.assertIn("modifiedAt", profiles["profiles"][0])
            self.assertEqual(profiles["profiles"][0]["offers"]["4"]["price"], 1235.0)
            self.assertEqual(profiles["profiles"][0]["tradeHistory"][0]["i"], 209)
            self.assertEqual(result["flipping_count"], 1)
            self.assertEqual(result["profile_count"], 1)

    def test_existing_source_is_a_true_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            runelite = base / "runelite"
            root = base / "repo"
            flipping = runelite / "flipping"
            flipping.mkdir(parents=True)
            (flipping / "Evidence.json").write_text("new")
            self._write_profiles(runelite)
            incoming = root / "data/incoming"
            (incoming / "flipping").mkdir(parents=True)
            (incoming / "flipping/Stale.json").write_text("stale")
            (incoming / "flipping/Evidence.backup.json").write_text("old excluded file")
            (incoming / "runelite").mkdir(parents=True)
            (incoming / "runelite/Stale.json").write_text("stale")

            sync.sync_exports(runelite_home=runelite, root=root)

            self.assertEqual(
                sorted(p.name for p in (incoming / "flipping").iterdir()),
                ["Evidence.json"],
            )
            self.assertEqual(sorted(p.name for p in (incoming / "runelite").iterdir()), ["profiles.json"])

    def test_missing_sources_preserve_history_and_clear_core_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root = base / "repo"
            incoming = root / "data/incoming"
            (incoming / "flipping").mkdir(parents=True)
            (incoming / "flipping/Evidence.json").write_text("history")
            (incoming / "runelite").mkdir(parents=True)
            (incoming / "runelite/profiles.json").write_text("stale")
            (incoming / "runelite/.gitkeep").write_text("")

            result = sync.sync_exports(runelite_home=base / "missing", root=root)

            self.assertTrue((incoming / "flipping/Evidence.json").exists())
            self.assertFalse((incoming / "runelite/profiles.json").exists())
            self.assertTrue((incoming / "runelite/.gitkeep").exists())
            self.assertIn("preserved", result["flipping_status"])
            self.assertIn("cleared", result["profile_status"])

    def test_uses_runelite_selected_profile_from_log(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            runelite = base / "runelite"
            self._write_profiles(runelite)
            logs = runelite / "logs"
            logs.mkdir()
            (logs / "client.log").write_text(
                "choosing RuneScapeProfile(displayName=Evidence, type=STANDARD, "
                "key=rsprofile.current), ignoring [RuneScapeProfile(displayName=Evidence, "
                "type=STANDARD, key=rsprofile.ignored)]\n"
            )

            sync.sync_exports(runelite_home=runelite, root=base / "repo")

            snapshot = json.loads(
                (base / "repo/data/incoming/runelite/profiles.json").read_text()
            )
            self.assertEqual(snapshot["selectedProfile"], "current")

    def test_runelite_home_environment_override_expands_user(self) -> None:
        with mock.patch.dict(os.environ, {"RUNELITE_HOME": "~/custom-runelite"}):
            self.assertEqual(sync._runelite_home(), Path("~/custom-runelite").expanduser())

if __name__ == "__main__":
    unittest.main()
