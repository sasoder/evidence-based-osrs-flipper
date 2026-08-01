from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flipper import sync


class SyncTests(unittest.TestCase):
    def test_syncs_profile_and_slots_while_excluding_flipping_noise(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            runelite = base / "runelite"
            root = base / "repo"
            flipping = runelite / "flipping"
            slots = flipping / "current-slots"
            slots.mkdir(parents=True)
            (flipping / "Evidence.json").write_text("history")
            (flipping / "Evidence.backup.json").write_text("backup")
            (flipping / "backupCheckpoints-Evidence.json").write_text("checkpoints")
            (flipping / "accountwide.json").write_text("account")
            (slots / "Evidence.json").write_text("slots")

            result = sync.sync_exports(runelite_home=runelite, root=root)

            incoming = root / "data/incoming"
            self.assertEqual((incoming / "flipping/Evidence.json").read_text(), "history")
            self.assertEqual((incoming / "ge-slots/Evidence.json").read_text(), "slots")
            self.assertFalse((incoming / "flipping/Evidence.backup.json").exists())
            self.assertFalse((incoming / "flipping/backupCheckpoints-Evidence.json").exists())
            self.assertFalse((incoming / "flipping/accountwide.json").exists())
            self.assertFalse((incoming / "flipping/current-slots").exists())
            self.assertEqual(result["flipping_count"], 1)
            self.assertEqual(result["slots_count"], 1)

    def test_existing_source_is_a_true_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            runelite = base / "runelite"
            root = base / "repo"
            flipping = runelite / "flipping"
            slots = flipping / "current-slots"
            slots.mkdir(parents=True)
            (flipping / "Evidence.json").write_text("new")
            (slots / "Evidence.json").write_text("new slots")
            incoming = root / "data/incoming"
            (incoming / "flipping").mkdir(parents=True)
            (incoming / "flipping/Stale.json").write_text("stale")
            (incoming / "flipping/Evidence.backup.json").write_text("old excluded file")
            (incoming / "ge-slots").mkdir(parents=True)
            (incoming / "ge-slots/Stale.json").write_text("stale")

            sync.sync_exports(runelite_home=runelite, root=root)

            self.assertEqual(
                sorted(p.name for p in (incoming / "flipping").iterdir()),
                ["Evidence.json"],
            )
            self.assertEqual(
                sorted(p.name for p in (incoming / "ge-slots").iterdir()),
                ["Evidence.json"],
            )

    def test_missing_sources_preserve_history_and_clear_slot_json(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root = base / "repo"
            incoming = root / "data/incoming"
            (incoming / "flipping").mkdir(parents=True)
            (incoming / "flipping/Evidence.json").write_text("history")
            (incoming / "ge-slots").mkdir(parents=True)
            (incoming / "ge-slots/Evidence.json").write_text("stale")
            (incoming / "ge-slots/.gitkeep").write_text("")

            result = sync.sync_exports(runelite_home=base / "missing", root=root)

            self.assertTrue((incoming / "flipping/Evidence.json").exists())
            self.assertFalse((incoming / "ge-slots/Evidence.json").exists())
            self.assertTrue((incoming / "ge-slots/.gitkeep").exists())
            self.assertIn("preserved", result["flipping_status"])
            self.assertIn("cleared", result["slots_status"])

    def test_runelite_home_environment_override_expands_user(self) -> None:
        with mock.patch.dict(os.environ, {"RUNELITE_HOME": "~/custom-runelite"}):
            self.assertEqual(sync._runelite_home(), Path("~/custom-runelite").expanduser())


if __name__ == "__main__":
    unittest.main()
