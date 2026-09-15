"""Encode a Takeout export to smaller files, resumably.

Reads the deduplicated mirror, encodes each eligible item, verifies it, and
writes a CSV that the uploader consumes. Entirely offline: it contacts nothing,
uploads nothing, and deletes nothing.

Built for a whole library rather than a sample. A full run takes hours, so it
resumes: an item whose output already exists is reused rather than re-encoded,
and the CSV is flushed periodically so a run that dies loses at most the work
since the last flush. Re-running after an interruption is always safe.

Photos are encoded before videos, because photos are fast and videos are not --
a library's worth of HEVC can run many times longer than all its photos.
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

FLUSH_EVERY = 25


def eligible(entry: takeout.MirrorEntry) -> str | None:
    """Return a skip reason, or None when the entry may be encoded."""

    if entry.taken_timestamp_ms is None:
        return "no timestamp: would be dated 'today' on upload"
    if entry.media_key is None:
        return "no media key: the original could not be identified later"
    return None


def output_path(entry: takeout.MirrorEntry, target_dir: Path) -> Path:
    """Deterministic, collision-free output name.

    Stems collide across a large library, so the media key disambiguates. Being
    deterministic is what makes resuming possible.
    """

    suffix = ".avif" if entry.kind == "photo" else ".mp4"
    return target_dir / f"{entry.path.stem}-{(entry.media_key or '')[3:15]}{suffix}"


def write_report(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in FIELDS})
    tmp.replace(path)


def human(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def main() -> int:
    parser = argparse.ArgumentParser(description="Encode a Takeout export, resumably")
    parser.add_argument("root", type=Path, help="Extracted Takeout root")
    parser.add_argument("--limit", type=int, default=0, help="0 encodes everything")
    parser.add_argument("--work", type=Path, default=Path("G:/takeout-work"))
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument(
        "--kinds",
        choices=("photo", "video", "all"),
        default="all",
        help="Which media to encode (default: all, photos first)",
    )
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--no-resume", action="store_true", help="Re-encode even if output exists")
    args = parser.parse_args()

    settings = load_config(args.config).as_dict()
    work = args.work
    target_dir = work / "out"
    target_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report or work / "encoded.csv"
    ffprobe = settings.get("tools", {}).get("ffprobe", "ffprobe")

    print(f"Scanning {args.root} ...", flush=True)
    entries = takeout.mirror(args.root)
    kinds = {"photo", "video"} if args.kinds == "all" else {args.kinds}
    entries = [e for e in entries if e.kind in kinds]
    # Photos first: they are quick, so the bulk of the savings lands early even
    # if a long video pass is interrupted later.
    entries.sort(key=lambda e: (e.kind != "photo", -e.size_bytes))
    print(f"  {len(entries):,} candidate item(s)", flush=True)

    rows: list[dict] = []
    encoded = reused = skipped = 0
    total_old = total_new = 0
    started = time.time()

    for entry in entries:
        if args.limit and (encoded + reused) >= args.limit:
            break

        reason = eligible(entry)
        row = {
            "source": str(entry.path),
            "media_key": entry.media_key or "",
            "album": " | ".join(entry.albums),
            "taken_timestamp_ms": entry.taken_timestamp_ms or "",
            "old_bytes": entry.size_bytes,
        }
        if reason:
            row.update(status="skipped", reason=reason, output="")
            rows.append(row)
            skipped += 1
            continue

        output = output_path(entry, target_dir)
        row["output"] = str(output)

        reuse = output.exists() and output.stat().st_size > 0 and not args.no_resume
        try:
            if reuse:
                info = media.probe(output, ffprobe)
            else:
                encode_settings = dict(settings)
                if entry.latitude is not None:
                    encode_settings["source_metadata"] = {
                        "latitude": entry.latitude,
                        "longitude": entry.longitude,
                    }
                info = media.encode(entry.path, output, encode_settings)
                media.verify(entry.path, output, encode_settings)
            source_info = media.probe(entry.path, ffprobe)
        except Exception as exc:  # noqa: BLE001 - every outcome is recorded
            row.update(status="skipped", reason=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            skipped += 1
            print(f"  SKIP {entry.path.name}: {type(exc).__name__}: {exc}", flush=True)
            continue

        new_bytes = output.stat().st_size
        saved = entry.size_bytes - new_bytes
        percent = 100 * saved / entry.size_bytes if entry.size_bytes else 0.0
        row.update(
            new_bytes=new_bytes,
            saved_bytes=saved,
            saved_percent=round(percent, 2),
            old_dimensions=f"{source_info['width']}x{source_info['height']}",
            new_dimensions=f"{info['width']}x{info['height']}",
            status="encoded",
            reason="reused existing output" if reuse else "",
        )
        rows.append(row)
        total_old += entry.size_bytes
        total_new += new_bytes
        if reuse:
            reused += 1
        else:
            encoded += 1

        done = encoded + reused
        if not reuse and done % 10 == 0:
            elapsed = time.time() - started
            remaining = (len(entries) - done) * (elapsed / max(1, encoded))
            print(
                f"  [{done:,}/{len(entries):,}] {entry.kind} "
                f"{total_old / 1e9:.2f} GB -> {total_new / 1e9:.2f} GB "
                f"({100 * (total_old - total_new) / max(1, total_old):.1f}% saved) "
                f"~{human(remaining)} left",
                flush=True,
            )
        if len(rows) % FLUSH_EVERY == 0:
            write_report(report_path, rows)

    write_report(report_path, rows)
    print(f"\n--- encoded {encoded:,}, reused {reused:,}, skipped {skipped:,} ---", flush=True)
    if total_old:
        saved = total_old - total_new
        print(f"  {total_old / 1e9:.2f} GB -> {total_new / 1e9:.2f} GB", flush=True)
        print(f"  saved {saved / 1e9:.2f} GB ({100 * saved / total_old:.1f}%)", flush=True)
    print(f"  elapsed {human(time.time() - started)}", flush=True)
    print(f"  report: {report_path}", flush=True)
    print("  Nothing was uploaded or deleted.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
