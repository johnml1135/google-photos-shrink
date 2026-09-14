"""Validate a Google Takeout export against the live library. READ-ONLY.

Answers the two questions the whole Takeout plan rests on:

  1. Does Takeout hand back bytes that hash-match what Google stores?
     If yes, `find_uploaded()` is a sound join key and deletion can be targeted
     precisely. If no, we need a fuzzier join and much stronger guardrails.
  2. How complete is the export's metadata? Files without a resolvable sidecar
     would lose their timestamps and must be quarantined, not uploaded.

This script never uploads, trashes, or modifies anything, locally or remotely.
"""

from __future__ import annotations

import argparse
import collections
import json
import random
from pathlib import Path

from photos_shrink import takeout
from photos_shrink.config import load_config
from photos_shrink.remote import GooglePhotosRemote


def inventory(root: Path) -> list[takeout.TakeoutRecord]:
    records = []
    for record in takeout.scan(root, compute_hash=False):
        records.append(record)
        if len(records) % 500 == 0:
            print(f"  scanned {len(records)} files...", flush=True)
    return records


def summarize(records: list[takeout.TakeoutRecord]) -> dict:
    by_kind = collections.Counter(r.kind for r in records)
    bytes_by_kind: dict[str, int] = collections.defaultdict(int)
    for record in records:
        bytes_by_kind[record.kind] += record.size_bytes
    return {
        "files": len(records),
        "by_kind": dict(by_kind),
        "gb_by_kind": {k: round(v / 1e9, 2) for k, v in bytes_by_kind.items()},
        "total_gb": round(sum(bytes_by_kind.values()) / 1e9, 2),
        "with_sidecar": sum(1 for r in records if r.sidecar is not None),
        "with_timestamp": sum(1 for r in records if r.has_metadata),
        "edited_variants": sum(1 for r in records if r.edited),
        "with_geo": sum(1 for r in records if r.latitude is not None),
        "albums": len({r.album for r in records if r.album}),
    }


def sample(records: list[takeout.TakeoutRecord], count: int, seed: int = 0) -> list[takeout.TakeoutRecord]:
    """Pick a spread across kind / edited / sidecar-presence, not just the first N."""

    buckets: dict[tuple, list] = collections.defaultdict(list)
    for record in records:
        buckets[(record.kind, record.edited, record.sidecar is not None)].append(record)

    rng = random.Random(seed)
    chosen: list[takeout.TakeoutRecord] = []
    per_bucket = max(1, count // max(1, len(buckets)))
    for bucket in buckets.values():
        chosen.extend(rng.sample(bucket, min(per_bucket, len(bucket))))
    remaining = [r for r in records if r not in chosen]
    if len(chosen) < count and remaining:
        chosen.extend(rng.sample(remaining, min(count - len(chosen), len(remaining))))
    return chosen[:count]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Takeout/library match probe")
    parser.add_argument("root", type=Path, help="Extracted Takeout root (the folder holding 'Google Photos')")
    parser.add_argument("--sample", type=int, default=25, help="Files to hash-match against the library")
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument("--report", type=Path, default=Path(".photos-shrink/takeout-probe.json"))
    parser.add_argument("--offline", action="store_true", help="Inventory only; do not contact Google")
    args = parser.parse_args()

    print(f"Scanning {args.root} ...", flush=True)
    records = inventory(args.root)
    if not records:
        print("No media files found. Point --root at the extracted export.", flush=True)
        return 1

    stats = summarize(records)
    print("\n--- Export inventory ---", flush=True)
    for key, value in stats.items():
        print(f"  {key}: {value}", flush=True)

    missing = stats["files"] - stats["with_timestamp"]
    if missing:
        pct = 100 * missing / stats["files"]
        print(f"\n  WARNING: {missing} files ({pct:.1f}%) have no usable timestamp.", flush=True)
        print("  These would land in Google Photos dated 'today' and must be quarantined.", flush=True)

    report = {"root": str(args.root), "inventory": stats, "matches": []}

    if not args.offline:
        chosen = sample(records, args.sample)
        print(f"\n--- Hash-matching {len(chosen)} sampled files against the library ---", flush=True)
        remote = GooglePhotosRemote(load_config(args.config).as_dict())
        try:
            print(f"Account: {remote.login()}", flush=True)
            hits = 0
            for index, record in enumerate(chosen, 1):
                try:
                    match = remote.find_uploaded(record.path)
                except Exception as exc:  # noqa: BLE001 - probe reports, never raises
                    print(f"  [{index}/{len(chosen)}] {record.path.name}: ERROR {type(exc).__name__}", flush=True)
                    report["matches"].append({"file": str(record.path), "result": "error", "error": str(exc)})
                    continue
                hits += match is not None
                verdict = "MATCH" if match else "no match"
                print(f"  [{index}/{len(chosen)}] {record.path.name}: {verdict}", flush=True)
                report["matches"].append(
                    {
                        "file": str(record.path),
                        "kind": record.kind,
                        "edited": record.edited,
                        "result": "match" if match else "miss",
                        "item_id": (match or {}).get("id"),
                    }
                )
            attempted = len([m for m in report["matches"] if m["result"] in {"match", "miss"}])
            rate = 100 * hits / attempted if attempted else 0.0
            report["hit_rate_percent"] = round(rate, 1)
            print(f"\n  Hash match rate: {hits}/{attempted} ({rate:.1f}%)", flush=True)
            if rate >= 90:
                print("  => Takeout bytes match the library. The hash join is sound.", flush=True)
            elif rate > 0:
                print("  => Partial. Inspect the misses before trusting hash-targeted deletion.", flush=True)
            else:
                print("  => No matches. Takeout is rewriting bytes; hash join will NOT work.", flush=True)
        finally:
            remote.close()

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport written to {args.report}", flush=True)
    print("Nothing was uploaded, trashed, or modified.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
