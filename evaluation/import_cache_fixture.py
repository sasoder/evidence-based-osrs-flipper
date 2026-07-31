"""Import an integrity-checked cache archive as an evaluator-v2 observed fixture.

The output is written only after the archive manifest, evaluator-owned lane universes, visible
history, and full withheld horizons all pass. The importer is never used during evaluation.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from evaluation import selection_contract


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "evaluation/contract.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(archive_dir: Path) -> dict:
    manifest = archive_dir / "SHA256SUMS"
    cache_dir = archive_dir / "cache"
    if not manifest.is_file() or not cache_dir.is_dir():
        raise ValueError("archive must contain SHA256SUMS and cache/")
    expected = {}
    for line in manifest.read_text().splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or len(digest) != 64:
            raise ValueError(f"malformed archive manifest row: {line!r}")
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative_path.parts[:1] != ("cache",)
        ):
            raise ValueError(f"unsafe archive manifest path: {relative}")
        expected[relative] = digest
    actual_paths = sorted(
        str(path.relative_to(archive_dir))
        for path in cache_dir.glob("*.json")
    )
    if sorted(expected) != actual_paths:
        raise ValueError("archive manifest does not exactly cover cache JSON files")
    mismatches = [
        relative
        for relative, digest in expected.items()
        if _sha256(archive_dir / relative) != digest
    ]
    if mismatches:
        raise ValueError(f"archive checksum mismatch: {mismatches[0]}")
    return {
        "kind": "immutable_cache_archive",
        "archive_id": archive_dir.name,
        "manifest_sha256": _sha256(manifest),
        "manifest_file_count": len(expected),
        "integrity_verified": True,
    }


def _query(key: str) -> tuple[int, str] | None:
    if "/timeseries" not in key:
        return None
    parsed = urllib.parse.urlsplit(key.split(" ", 1)[-1])
    query = urllib.parse.parse_qs(parsed.query)
    try:
        return int(query["id"][0]), query["timestep"][0]
    except (KeyError, TypeError, ValueError):
        return None


def _load(cache_dir: Path, day: str) -> tuple[dict[int, dict], dict[tuple[int, str], list[dict]]]:
    mapping_candidates = []
    series_candidates: dict[tuple[int, str], tuple[float, list[dict]]] = {}
    for path in sorted(cache_dir.glob("*.json")):
        try:
            envelope = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        stored_at = envelope.get("stored_at")
        if not isinstance(stored_at, (int, float)):
            continue
        stored_day = datetime.fromtimestamp(stored_at, timezone.utc).date().isoformat()
        key = envelope.get("key", "")
        value = envelope.get("value")
        if "/mapping" in key and isinstance(value, list):
            mapping_candidates.append((stored_at, value))
            continue
        if stored_day != day:
            continue
        parsed = _query(key)
        rows = value.get("data") if isinstance(value, dict) else None
        if parsed and isinstance(rows, list) and rows:
            ordered = sorted(rows, key=lambda row: row["timestamp"])
            prior = series_candidates.get(parsed)
            if prior is None or stored_at > prior[0]:
                series_candidates[parsed] = (stored_at, ordered)
    if not mapping_candidates:
        raise ValueError(f"archive has no mapping response: {cache_dir}")
    mapping_rows = max(mapping_candidates, key=lambda row: row[0])[1]
    mapping = {int(row["id"]): row for row in mapping_rows}
    series = {key: value[1] for key, value in series_candidates.items()}
    if not series:
        raise ValueError(f"archive has no {day} time series")
    return mapping, series


def _series_is_contiguous(rows: list[dict], timestep: str) -> bool:
    return (
        bool(rows)
        and all(selection_contract.REQUIRED_BUCKET_FIELDS.issubset(row) for row in rows)
        and all(
            right["timestamp"] - left["timestamp"]
            == selection_contract.STEP_SECONDS[timestep]
            for left, right in zip(rows, rows[1:])
        )
    )


def _best_lane_cutoff(series: dict[tuple[int, str], list[dict]], ids: set[int],
                      timestep: str, minimum_history: int,
                      required_future: int, minimum_items: int) -> int:
    """Choose the latest cutoff meeting the reviewed lane-coverage floor."""
    candidates = Counter()
    width = minimum_history + required_future
    for item_id in ids:
        rows = series.get((item_id, timestep), [])
        for start in range(0, len(rows) - width + 1):
            window = rows[start:start + width]
            if _series_is_contiguous(window, timestep):
                candidates[window[minimum_history - 1]["timestamp"]] += 1
    if not candidates:
        raise ValueError(
            f"archive has no contiguous {timestep} window with "
            f"{minimum_history} visible and {required_future} withheld buckets"
        )
    eligible = [
        cutoff for cutoff, count in candidates.items()
        if count >= minimum_items
    ]
    if not eligible:
        raise ValueError(
            f"archive has no {timestep} cutoff covering at least {minimum_items} items"
        )
    return max(eligible)


def build_archive_fixture(archive_dir: Path, day: str, contract: dict) -> dict:
    provenance = verify_archive(archive_dir)
    mapping, series = _load(archive_dir / "cache", day)
    member_ids = {
        item_id for item_id, meta in mapping.items()
        if meta.get("members") and int(meta.get("limit") or 0) > 0
    }
    cutoffs = {}
    lane_staging: dict[str, dict[int, tuple[list[dict], list[dict]]]] = {}
    lane_report = {}
    for decision_lane, lane in contract["decision_lanes"].items():
        timestep = lane["timestep"]
        required = selection_contract.maximum_lane_horizon_buckets(
            decision_lane, contract
        )
        minimum_history = required * contract["minimum_non_overlapping_blocks"]
        cutoff = _best_lane_cutoff(
            series,
            member_ids,
            timestep,
            minimum_history,
            required,
            int(lane["minimum_observed_snapshot_items"]),
        )
        staged = {}
        for item_id in sorted(member_ids):
            rows = series.get((item_id, timestep), [])
            history = [row for row in rows if row["timestamp"] <= cutoff][-minimum_history:]
            future = [row for row in rows if row["timestamp"] > cutoff][:required]
            if (
                len(history) >= minimum_history
                and len(future) == required
                and history[-1]["timestamp"] == cutoff
                and future[0]["timestamp"]
                == cutoff + selection_contract.STEP_SECONDS[timestep]
                and _series_is_contiguous(history, timestep)
                and _series_is_contiguous(future, timestep)
            ):
                staged[item_id] = (history, future)
        if not staged:
            raise ValueError(f"{day} archive has no fully covered {decision_lane} items")
        lane_staging[decision_lane] = staged
        cutoffs[decision_lane] = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        lane_report[decision_lane] = {
            "timestep": timestep,
            "decision_cutoff": cutoffs[decision_lane],
            "required_visible_history_buckets": minimum_history,
            "required_withheld_buckets": required,
            "fully_covered_items": len(staged),
        }

    union_ids = sorted({
        item_id for staged in lane_staging.values() for item_id in staged
    })
    items = []
    for item_id in union_ids:
        meta = mapping[item_id]
        history = {}
        future = {}
        for decision_lane, staged in lane_staging.items():
            if item_id not in staged:
                continue
            timestep = contract["decision_lanes"][decision_lane]["timestep"]
            history[timestep], future[timestep] = staged[item_id]
        current_rows = next(iter(history.values()))
        current = current_rows[-1]
        items.append({
            "id": item_id,
            "name": meta["name"],
            "members": True,
            "limit": int(meta["limit"]),
            "latest": {
                "low": current.get("avgLowPrice"),
                "high": current.get("avgHighPrice"),
                "lowTime": current["timestamp"],
                "highTime": current["timestamp"],
            },
            "one_hour": {
                "lowPriceVolume": current.get("lowPriceVolume") or 0,
                "highPriceVolume": current.get("highPriceVolume") or 0,
            },
            "history": history,
            "future": future,
        })
    fixture = {
        "name": f"wiki_archive_{day}",
        "fixture_class": "observed",
        "regime": "observed",
        "as_of": cutoffs["patient"],
        "decision_cutoffs": cutoffs,
        "source_provenance": provenance,
        "lane_import_report": lane_report,
        "items": items,
    }
    audit = selection_contract.fixture_audit(fixture, contract)
    coverage = selection_contract.coverage_manifest(fixture, contract)
    if not audit["passed"]:
        raise ValueError(f"fixture audit failed: {audit['reasons']}")
    uncovered = [row for row in coverage if not row["covered"]]
    if uncovered:
        raise ValueError(
            f"fixture coverage failed: {uncovered[0]['decision_lane']} "
            f"item {uncovered[0]['item_id']} {uncovered[0]['reasons']}"
        )
    return fixture

def _write_gzip(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    with path.open("wb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as compressed:
            compressed.write(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    args = parser.parse_args()
    contract = json.loads(args.contract.read_text())
    fixture = build_archive_fixture(args.archive_dir, args.date, contract)
    payload = {"version": 2, "fixtures": [fixture]}
    _write_gzip(args.output, payload)
    print(json.dumps({
        "path": str(args.output),
        "fixture": fixture["name"],
        "archive": fixture["source_provenance"],
        "lane_import_report": fixture["lane_import_report"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
