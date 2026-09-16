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
from dataclasses import replace
from pathlib import Path

from photos_shrink import media, takeout
from photos_shrink.config import load_config
from photos_shrink.policy import Candidate, explain, verdict

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


def output_path(entry: takeout.MirrorEntry, target_dir: Path) -> Path:
    """Deterministic, collision-free output name.

    Stems collide across a large library, so the media key disambiguates. Being
    deterministic is what makes resuming possible.
    """

    suffix = ".avif" if entry.kind == "photo" else ".mp4"
    return target_dir / f"{entry.path.stem}-{(entry.media_key or '')[3:15]}{suffix}"


def load_report(path: Path) -> list[dict]:
    """Every row of an existing report, in order; empty when there is none."""

    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def merge_report(original: list[dict], produced: list[dict]) -> list[dict]:
    """The existing report with this run's rows swapped in, in place.

    A run narrowed with --kinds, stopped by --limit, or killed partway only
    produces rows for what it actually reached. Every other row must survive,
    or re-encoding videos deletes the photo rows the uploader reads.

    What to keep is decided from the rows the run *produced*, never from the
    items it *considered*. The first version keyed on the considered set, and
    --limit stops a run long before it reaches everything it considered: its
    first real use dropped 9,934 of 10,184 rows. A row this run did not write
    is kept, full stop.
    """

    fresh = {row["source"]: row for row in produced}
    merged = [fresh.pop(row.get("source"), row) for row in original]
    merged.extend(fresh.values())  # sources the existing report never had
    return merged


def write_report(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in FIELDS})
    tmp.replace(path)


def format_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def main() -> int:
    parser = argparse.ArgumentParser(description="Encode a Takeout export, resumably")
    parser.add_argument("root", type=Path, help="Extracted Takeout root")
    parser.add_argument("--limit", type=int, default=0, help="0 encodes everything")
    parser.add_argument("--work", type=Path, default=None, help="Defaults to run.work_dir")
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

    config = load_config(args.config)
    settings = config.as_dict()
    minimum_savings = float(config.run.get("minimum_savings_percent", 0))
    work = args.work or Path(config.run["work_dir"])
    target_dir = work / "out"
    target_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report or work / "encoded.csv"
    ffprobe = config.tools["ffprobe"]

    # Say where this run reads and writes before doing anything. work_dir
    # defaults to the repo's .photos-shrink, and a run that meant to resume a
    # library encoded elsewhere will find no outputs there and quietly start
    # over -- two hours of re-encoding, with nothing on screen to show it.
    existing = sum(1 for _ in target_dir.iterdir()) if target_dir.is_dir() else 0
    print(f"  outputs: {target_dir}  ({existing:,} already there)", flush=True)
    print(f"  report : {report_path}", flush=True)
    print(f"Scanning {args.root} ...", flush=True)
    entries = takeout.mirror(args.root)
    kinds = {"photo", "video"} if args.kinds == "all" else {args.kinds}
    entries = [e for e in entries if e.kind in kinds]
    # Photos first: they are quick, so the bulk of the savings lands early even
    # if a long video pass is interrupted later.
    entries.sort(key=lambda e: (e.kind != "photo", -e.size_bytes))
    print(f"  {len(entries):,} candidate item(s)", flush=True)

    # Snapshot once, before anything is written: every flush merges this
    # run's rows into it, so a run that stops early leaves the rest intact.
    original_rows = load_report(report_path)
    if original_rows:
        print(f"  {len(original_rows):,} existing row(s); only rows this run reaches change", flush=True)

    rows: list[dict] = []
    encoded = reused = skipped = 0
    total_old = total_new = 0
    started = time.time()

    for entry in entries:
        if args.limit and (encoded + reused) >= args.limit:
            break

        candidate = Candidate.from_mirror_entry(entry)
        token = verdict(config, candidate)
        row = {
            "source": str(entry.path),
            "media_key": entry.media_key or "",
            "album": " | ".join(entry.albums),
            "taken_timestamp_ms": entry.taken_timestamp_ms or "",
            "old_bytes": entry.size_bytes,
        }
        if token:
            row.update(status="skipped", reason=explain(token), output="")
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
        encoded_token = verdict(config, replace(candidate, saved_percent=percent))
        if encoded_token:
            # Uploading this would spend quota to save little or nothing.
            row.update(
                status="skipped",
                reason=explain(encoded_token, percent=percent, minimum=minimum_savings),
                new_bytes=new_bytes,
                saved_bytes=saved,
                saved_percent=round(percent, 2),
            )
            rows.append(row)
            skipped += 1
            continue
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
                f"~{format_duration(remaining)} left",
                flush=True,
            )
        if len(rows) % FLUSH_EVERY == 0:
            write_report(report_path, merge_report(original_rows, rows))

    write_report(report_path, merge_report(original_rows, rows))
    print(f"\n--- encoded {encoded:,}, reused {reused:,}, skipped {skipped:,} ---", flush=True)
    if total_old:
        saved = total_old - total_new
        print(f"  {total_old / 1e9:.2f} GB -> {total_new / 1e9:.2f} GB", flush=True)
        print(f"  saved {saved / 1e9:.2f} GB ({100 * saved / total_old:.1f}%)", flush=True)
    print(f"  elapsed {format_duration(time.time() - started)}", flush=True)
    print(f"  report: {report_path}", flush=True)
    print("  Nothing was uploaded or deleted.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
