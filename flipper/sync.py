"""Mirror RuneLite's Flipping Utilities exports into the local repo."""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT


FLIPPING_EXCLUDES = ("*.backup.json", "backupCheckpoints*.json", "accountwide.json")


def _runelite_home() -> Path:
    return Path(os.environ.get("RUNELITE_HOME", "~/.runelite")).expanduser()


def _excluded(relative: Path) -> bool:
    return (
        "current-slots" in relative.parts
        or any(fnmatch.fnmatch(relative.name, pattern) for pattern in FLIPPING_EXCLUDES)
    )


def _mirror(source: Path, destination: Path, *, exclude_flipping_noise: bool = False) -> int:
    """Copy source files and remove destination files no longer present in the mirror."""
    destination.mkdir(parents=True, exist_ok=True)
    wanted: set[Path] = set()

    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if exclude_flipping_noise and _excluded(relative):
            continue
        wanted.add(relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    for path in sorted(destination.rglob("*"), reverse=True):
        if path.is_file() and path.relative_to(destination) not in wanted:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()

    return len(wanted)


def sync_exports(*, runelite_home: Path | None = None, root: Path = ROOT) -> dict:
    runelite_home = (runelite_home or _runelite_home()).expanduser()
    flipping_source = runelite_home / "flipping"
    flipping_destination = root / "data/incoming/flipping"
    slots_source = flipping_source / "current-slots"
    slots_destination = root / "data/incoming/ge-slots"

    if flipping_source.is_dir():
        flipping_count = _mirror(
            flipping_source,
            flipping_destination,
            exclude_flipping_noise=True,
        )
        flipping_status = "synced"
    else:
        # Flip history remains useful when RuneLite is temporarily unavailable.
        flipping_destination.mkdir(parents=True, exist_ok=True)
        flipping_count = 0
        flipping_status = "source missing; existing history preserved"

    if slots_source.is_dir():
        slots_count = _mirror(slots_source, slots_destination)
        slots_status = "synced"
    else:
        # Never let a missing live export leave actionable stale offers behind.
        slots_destination.mkdir(parents=True, exist_ok=True)
        for path in slots_destination.glob("*.json"):
            path.unlink()
        slots_count = 0
        slots_status = "source missing; stale slots cleared"

    return {
        "runelite_home": runelite_home,
        "root": root,
        "flipping_count": flipping_count,
        "flipping_status": flipping_status,
        "slots_count": slots_count,
        "slots_status": slots_status,
    }


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="flipper.sync")
    parser.add_argument(
        "--runelite-home",
        type=Path,
        default=None,
        help="RuneLite home; defaults to RUNELITE_HOME or ~/.runelite",
    )
    args = parser.parse_args(argv)
    result = sync_exports(runelite_home=args.runelite_home)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(f"runelite-sync {generated_at}")
    print(f"source {result['runelite_home']}")
    print(f"flipping {result['flipping_count']} file(s), {result['flipping_status']}")
    print(f"ge-slots {result['slots_count']} file(s), {result['slots_status']}")
    print(f"destination {result['root'] / 'data/incoming'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
