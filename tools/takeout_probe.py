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
import sys
from contextlib import ExitStack
from pathlib import Path

from photos_shrink import takeout
from photos_shrink.config import load_config
from photos_shrink.remote import COOKIE_HINT, RemoteProtocolError, open_session


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


def bucket_of(record: takeout.TakeoutRecord) -> str:
    """Group records by the traits that plausibly affect whether bytes match."""

    if record.edited:
        return f"{record.kind}/edited"
    if "(" in record.path.stem:
        return f"{record.kind}/duplicate-counter"
    return f"{record.kind}/plain"


def sample(
    records: list[takeout.TakeoutRecord],
    count: int,
    *,
    stratified: bool = False,
    seed: int = 0,
) -> list[takeout.TakeoutRecord]:
    """Pick files to hash-match.

    The default is a uniform random draw, because an equal draw from each
    bucket over-represents rare categories -- edited variants, duplicate
    counters, videos -- and a rate computed from it is not the library's rate.
    Use stratified only to probe coverage of the odd categories deliberately.
    """

    rng = random.Random(seed)
    if not stratified:
        return rng.sample(records, min(count, len(records)))

    buckets: dict[str, list] = collections.defaultdict(list)
    for record in records:
        buckets[bucket_of(record)].append(record)
    chosen: list[takeout.TakeoutRecord] = []
    per_bucket = max(1, count // max(1, len(buckets)))
    for bucket in buckets.values():
        chosen.extend(rng.sample(bucket, min(per_bucket, len(bucket))))
    return chosen[:count]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Takeout/library match probe")
    parser.add_argument("root", type=Path, help="Extracted Takeout root (the folder holding 'Google Photos')")
    parser.add_argument("--sample", type=int, default=25, help="Files to hash-match against the library")
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument("--report", type=Path, default=Path(".photos-shrink/takeout-probe.json"))
    parser.add_argument("--offline", action="store_true", help="Inventory only; do not contact Google")
    parser.add_argument(
        "--stratified",
        action="store_true",
        help="Draw evenly from each category instead of uniformly. Probes odd "
        "categories deliberately; the resulting rate is NOT the library's rate.",
    )
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
        chosen = sample(records, args.sample, stratified=args.stratified)
        print(f"\n--- Hash-matching {len(chosen)} sampled files against the library ---", flush=True)
        with ExitStack() as stack:
            # enter_context, not a bare call: open_session is a generator
            # context manager, so the login only runs on __enter__ and a bare
            # call would let an expired cookie escape this handler.
            try:
                remote = stack.enter_context(open_session(load_config(args.config)))
            except RemoteProtocolError as exc:
                print(f"\n{exc}", file=sys.stderr)
                print(COOKIE_HINT, file=sys.stderr)
                return 2
            print(f"Account: {remote.account_id()}", flush=True)
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
                        "bucket": bucket_of(record),
                        "result": "match" if match else "miss",
                        "item_id": (match or {}).get("id"),
                    }
                )
            attempted = len([m for m in report["matches"] if m["result"] in {"match", "miss"}])
            rate = 100 * hits / attempted if attempted else 0.0
            report["hit_rate_percent"] = round(rate, 1)

            # Break the rate down: a category that never matches is a finding,
            # and an aggregate can hide it entirely.
            per_bucket: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
            for entry in report["matches"]:
                if entry["result"] not in {"match", "miss"}:
                    continue
                slot = per_bucket[entry["bucket"]]
                slot[0] += entry["result"] == "match"
                slot[1] += 1
            report["by_bucket"] = {k: {"match": v[0], "of": v[1]} for k, v in per_bucket.items()}
            print("\n  By category:", flush=True)
            for name, (matched, total) in sorted(per_bucket.items()):
                print(f"    {name:<28} {matched}/{total}", flush=True)

            errors = len([m for m in report["matches"] if m["result"] == "error"])
            if errors:
                print(f"\n  {errors} request(s) errored -- rate below excludes them.", flush=True)
            print(f"\n  Hash match rate: {hits}/{attempted} ({rate:.1f}%)", flush=True)
            if rate >= 90:
                print("  => Takeout bytes match the library. The hash join is sound.", flush=True)
            elif rate > 0:
                print("  => Partial. Inspect the misses before trusting hash-targeted deletion.", flush=True)
            else:
                print("  => No matches. Takeout is rewriting bytes; hash join will NOT work.", flush=True)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport written to {args.report}", flush=True)
    print("Nothing was uploaded, trashed, or modified.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
