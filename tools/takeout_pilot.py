"""Encode a bounded batch of Takeout photos locally and report real savings.

Fully offline: reads the export, encodes, verifies, and writes a CSV. It never
contacts Google, uploads, or deletes. This proves the local half of the Takeout
pipeline -- selection, sidecar metadata, encoding, and verification -- so the
only unproven step left is the upload/delete round trip.

Sidecar coordinates are passed to the encoder so the replacement carries the
location Google held, matching what the browser pipeline already does.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from photos_shrink import media, takeout
from photos_shrink.config import load_config

FIELDS = [
    "source",
    "media_key",
    "album",
    "taken_timestamp_ms",
    "old_bytes",
    "new_bytes",
    "saved_bytes",
    "saved_percent",
    "old_dimensions",
    "new_dimensions",
    "output",
    "status",
    "reason",
]


def eligible(entry: takeout.MirrorEntry, *, kinds: set[str]) -> str | None:
    """Return a skip reason, or None when the entry may be encoded."""

    if entry.kind not in kinds:
        return f"not selected ({entry.kind})"
    if entry.taken_timestamp_ms is None:
        return "no timestamp: would be dated 'today' on upload"
    if entry.media_key is None:
        return "no media key: the original could not be identified later"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline Takeout encode pilot")
    parser.add_argument("root", type=Path, help="Extracted Takeout root")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--work", type=Path, default=Path("G:/takeout-work"))
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument("--videos", action="store_true", help="Encode videos instead of photos")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    settings = load_config(args.config).as_dict()
    kinds = {"video"} if args.videos else {"photo"}
    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    report_path = args.report or work / "takeout-pilot.csv"

    print(f"Scanning {args.root} ...", flush=True)
    # The mirror, not a raw file scan: a photo in three albums is exported three
    # times, and encoding each copy would upload the same photo three times.
    records = [e for e in takeout.mirror(args.root) if e.kind in kinds]
    print(f"  {len(records)} unique {'video' if args.videos else 'photo'} items", flush=True)

    # mirror() already orders largest first, which is both the clearest
    # demonstration of savings and the items that actually matter for storage.

    rows: list[dict] = []
    encoded = 0
    total_old = total_new = 0
    for record in records:
        if encoded >= args.limit:
            break
        reason = eligible(record, kinds=kinds)
        if reason:
            continue

        target_dir = work / "out"
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".avif" if record.kind == "photo" else ".mp4"
        # Stems collide across a library this size, and a collision would
        # silently overwrite another item's encode. The media key disambiguates.
        stamp = (record.media_key or "")[3:15]
        output = target_dir / f"{record.path.stem}-{stamp}{suffix}"

        row = {
            "source": str(record.path),
            "media_key": record.media_key or "",
            "album": " | ".join(record.albums),
            "taken_timestamp_ms": record.taken_timestamp_ms or "",
            "old_bytes": record.size_bytes,
            "output": str(output),
        }

        encode_settings = dict(settings)
        if record.latitude is not None:
            encode_settings["source_metadata"] = {
                "latitude": record.latitude,
                "longitude": record.longitude,
            }

        started = time.time()
        try:
            info = media.encode(record.path, output, encode_settings)
            media.verify(record.path, output, encode_settings)
        except Exception as exc:  # noqa: BLE001 - pilot records every outcome
            row.update(status="skipped", reason=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            print(f"  SKIP {record.path.name}: {type(exc).__name__}: {exc}", flush=True)
            continue

        source_info = media.probe(record.path, settings.get("tools", {}).get("ffprobe", "ffprobe"))
        new_bytes = output.stat().st_size
        saved = record.size_bytes - new_bytes
        percent = 100 * saved / record.size_bytes if record.size_bytes else 0.0
        row.update(
            new_bytes=new_bytes,
            saved_bytes=saved,
            saved_percent=round(percent, 2),
            old_dimensions=f"{source_info['width']}x{source_info['height']}",
            new_dimensions=f"{info['width']}x{info['height']}",
            status="encoded",
            reason="",
        )
        rows.append(row)
        encoded += 1
        total_old += record.size_bytes
        total_new += new_bytes
        print(
            f"  [{encoded}/{args.limit}] {record.path.name}: "
            f"{record.size_bytes/1e6:.2f} MB -> {new_bytes/1e6:.2f} MB "
            f"({percent:.1f}% saved, {source_info['width']}x{source_info['height']}"
            f" -> {info['width']}x{info['height']}, {time.time()-started:.1f}s)",
            flush=True,
        )

    with open(report_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in FIELDS})

    print(f"\n--- Encoded {encoded} of {args.limit} requested ---", flush=True)
    if encoded:
        saved = total_old - total_new
        print(f"  {total_old/1e6:.1f} MB -> {total_new/1e6:.1f} MB", flush=True)
        print(f"  saved {saved/1e6:.1f} MB ({100*saved/total_old:.1f}%)", flush=True)
    print(f"  report: {report_path}", flush=True)
    print("  Nothing was uploaded or deleted.", flush=True)
    return 0 if encoded else 1


if __name__ == "__main__":
    sys.exit(main())
