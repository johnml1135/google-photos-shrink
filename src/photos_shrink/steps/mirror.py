"""Build a complete local record of everything in a Takeout export.

One row per library item -- not per exported file. Takeout writes a photo once
per album it belongs to and again under its date bucket, so a naive file count
overstates the library badly; rows are grouped by the media key carried in each
sidecar's Google Photos URL.

Entirely offline. Contacts nothing, changes nothing. The output is the input to
selection, encoding and, much later, deletion.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from photos_shrink import takeout
from photos_shrink.config import load_config

FIELDS = [
    "media_key",
    "filename",
    "kind",
    "size_bytes",
    "captured",
    "albums",
    "copies",
    "edited",
    "has_sidecar",
    "uploadable",
    "latitude",
    "longitude",
    "url",
    "origin",
    "sizes",
    "path",
]


def iso_timestamp(timestamp_ms: int | None) -> str:
    if not timestamp_ms:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory a Takeout export")
    parser.add_argument("root", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="Defaults to <data_dir>/mirror.csv")
    parser.add_argument("--config", default="shrink.toml")
    args = parser.parse_args(argv)

    out = args.out or Path(load_config(args.config).run["data_dir"]) / "mirror.csv"

    print(f"Scanning {args.root} ...", flush=True)
    entries = takeout.mirror(args.root)
    if not entries:
        print("No media found.", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {
                    "media_key": entry.media_key or "",
                    "filename": entry.filename,
                    "kind": entry.kind,
                    "size_bytes": entry.size_bytes,
                    "captured": iso_timestamp(entry.taken_timestamp_ms),
                    "albums": " | ".join(entry.albums),
                    "copies": entry.copies,
                    "edited": entry.edited,
                    "has_sidecar": entry.has_sidecar,
                    "uploadable": entry.uploadable,
                    "latitude": entry.latitude if entry.latitude is not None else "",
                    "longitude": entry.longitude if entry.longitude is not None else "",
                    "url": entry.url or "",
                    "origin": entry.origin or "",
                    "sizes": " ".join(str(size) for size in entry.sizes),
                    "path": str(entry.path),
                }
            )

    by_kind = collections.Counter(e.kind for e in entries)
    bytes_by_kind: dict[str, int] = collections.defaultdict(int)
    for entry in entries:
        bytes_by_kind[entry.kind] += entry.size_bytes
    uploadable = [e for e in entries if e.uploadable]
    duplicated = sum(1 for e in entries if e.copies > 1)
    extra_copies = sum(e.copies - 1 for e in entries)
    albums = sorted({a for e in entries for a in e.albums})

    print("\n--- Library mirror ---", flush=True)
    print(f"  library items      : {len(entries):,}", flush=True)
    print(f"  exported files     : {len(entries) + extra_copies:,}", flush=True)
    print(f"  items in albums    : {duplicated:,} (counted once each)", flush=True)
    print(f"  albums             : {len(albums)}", flush=True)
    for kind, count in by_kind.most_common():
        print(f"  {kind:<18} : {count:,} items, {bytes_by_kind[kind] / 1e9:.2f} GB", flush=True)
    print(f"  total              : {sum(bytes_by_kind.values()) / 1e9:.2f} GB", flush=True)
    print(f"  replaceable        : {len(uploadable):,}", flush=True)

    blocked = len(entries) - len(uploadable)
    if blocked:
        no_key = sum(1 for e in entries if e.media_key is None)
        no_time = sum(1 for e in entries if e.taken_timestamp_ms is None)
        print(f"\n  {blocked:,} item(s) cannot be replaced safely:", flush=True)
        print(f"    no media key (cannot be identified in the library): {no_key:,}", flush=True)
        print(f"    no capture time (would be dated 'today'):           {no_time:,}", flush=True)

    summary = out.with_suffix(".summary.json")
    summary.write_text(
        json.dumps(
            {
                "library_items": len(entries),
                "exported_files": len(entries) + extra_copies,
                "albums": albums,
                "by_kind": dict(by_kind),
                "bytes_by_kind": dict(bytes_by_kind),
                "replaceable": len(uploadable),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  mirror : {out}", flush=True)
    print(f"  summary: {summary}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
