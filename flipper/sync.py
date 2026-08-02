"""Snapshot RuneLite's local GE state and optional Flipping Utilities history."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT


FLIPPING_EXCLUDES = ("*.backup.json", "backupCheckpoints*.json", "accountwide.json")
PROFILE_RE = re.compile(r"^rsprofile\.rsprofile\.([^.]+)\.(displayName|type)$")
OFFER_RE = re.compile(r"^geoffer\.rsprofile\.([^.]+)\.(\d+)$")
TRADE_RE = re.compile(r"^grandexchange\.rsprofile\.([^.]+)\.tradeHistory$")
SELECTED_PROFILE_RE = re.compile(
    r"choosing RuneScapeProfile\([^)]*key=rsprofile\.([^)]+)\)"
)


def _runelite_home() -> Path:
    return Path(os.environ.get("RUNELITE_HOME", "~/.runelite")).expanduser()


def _property_value(value: str) -> str:
    """Decode the subset of Java-properties escapes RuneLite uses in these values."""
    return re.sub(
        r"\\u([0-9a-fA-F]{4})|\\(.)",
        lambda match: chr(int(match.group(1), 16)) if match.group(1) else match.group(2),
        value,
    )


def _selected_profile(runelite_home: Path) -> str | None:
    try:
        text = (runelite_home / "logs/client.log").read_text(errors="replace")
    except FileNotFoundError:
        return None
    matches = SELECTED_PROFILE_RE.findall(text)
    return matches[-1] if matches else None


def _profile(profiles: dict[str, dict], key: str, modified_at: str) -> dict:
    profile = profiles.setdefault(key, {"offers": {}, "tradeHistory": []})
    profile["modifiedAt"] = modified_at
    return profile


def _snapshot_profiles(runelite_home: Path, destination: Path) -> int:
    profiles: dict[str, dict] = {}
    for path in sorted((runelite_home / "profiles2").glob("*.properties")):
        modified_at = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
        for line in path.read_text(errors="replace").splitlines():
            if not line or line[0] in "#!" or "=" not in line:
                continue
            key, encoded = line.split("=", 1)
            value = _property_value(encoded)
            match = PROFILE_RE.match(key)
            if match:
                profile = _profile(profiles, match.group(1), modified_at)
                profile[match.group(2)] = value
                continue
            match = OFFER_RE.match(key)
            if match:
                profile = _profile(profiles, match.group(1), modified_at)
                profile["offers"][match.group(2)] = json.loads(value)
                continue
            match = TRADE_RE.match(key)
            if match:
                profile = _profile(profiles, match.group(1), modified_at)
                profile["tradeHistory"] = json.loads(value)

    destination.mkdir(parents=True, exist_ok=True)
    output = destination / "profiles.json"
    for stale in destination.glob("*.json"):
        if stale != output:
            stale.unlink()
    tmp = output.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selectedProfile": _selected_profile(runelite_home),
        "profiles": [{"key": key, **profile} for key, profile in sorted(profiles.items())],
    }, indent=2))
    tmp.replace(output)
    return len(profiles)


def _mirror_flipping(source: Path, destination: Path) -> int:
    """Mirror stock Flipping Utilities' account JSON files."""
    destination.mkdir(parents=True, exist_ok=True)
    wanted: set[str] = set()

    for path in source.glob("*.json"):
        if any(path.match(pattern) for pattern in FLIPPING_EXCLUDES):
            continue
        wanted.add(path.name)
        target = destination / path.name
        tmp = target.with_suffix(".tmp")
        tmp.write_text(path.read_text(errors="replace"))
        tmp.replace(target)

    for path in destination.glob("*.json"):
        if path.name not in wanted:
            path.unlink()

    return len(wanted)


def sync_exports(*, runelite_home: Path | None = None, root: Path = ROOT) -> dict:
    runelite_home = (runelite_home or _runelite_home()).expanduser()
    flipping_source = runelite_home / "flipping"
    flipping_destination = root / "data/incoming/flipping"
    runelite_destination = root / "data/incoming/runelite"

    if flipping_source.is_dir():
        flipping_count = _mirror_flipping(flipping_source, flipping_destination)
        flipping_status = "synced"
    else:
        # Flip history remains useful when RuneLite is temporarily unavailable.
        flipping_destination.mkdir(parents=True, exist_ok=True)
        flipping_count = 0
        flipping_status = "source missing; existing history preserved"

    if (runelite_home / "profiles2").is_dir():
        profile_count = _snapshot_profiles(runelite_home, runelite_destination)
        profile_status = "synced"
    else:
        runelite_destination.mkdir(parents=True, exist_ok=True)
        for path in runelite_destination.glob("*.json"):
            path.unlink()
        profile_count = 0
        profile_status = "source missing; stale snapshot cleared"

    return {
        "runelite_home": runelite_home,
        "root": root,
        "flipping_count": flipping_count,
        "flipping_status": flipping_status,
        "profile_count": profile_count,
        "profile_status": profile_status,
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
    print(f"runelite {result['profile_count']} profile(s), {result['profile_status']}")
    print(f"destination {result['root'] / 'data/incoming'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
