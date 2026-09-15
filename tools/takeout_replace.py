"""Finish a Takeout-sourced replacement: restore metadata, then trash the original.

All the safety logic -- the ten refusal points that decide whether an original
may be trashed -- lives in `photos_shrink.replacement.replace_one`. This tool
is just argparse, a loop over the journal's pending replacements, and
reporting. See that module's docstring for the full ordered list of checks.

Dry run is the default. Every original also remains in the Takeout export on
disk, so even a mistake is recoverable by re-upload.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import replace as _replace
from pathlib import Path

from photos_shrink.config import load_config
from photos_shrink.ledger import UploadJournal
from photos_shrink.remote import GooglePhotosRemote
from photos_shrink.replacement import replace_one


def _load_mirror_keys(mirror_path: Path) -> dict[str, str]:
    keys: dict[str, str] = {}
    if mirror_path.exists():
        with open(mirror_path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("media_key"):
                    keys[row["path"]] = row["media_key"]
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore metadata and trash replaced originals")
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--mirror", type=Path, default=None)
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true", help="Actually restore and trash")
    parser.add_argument("--keep-originals", action="store_true", help="Restore and verify, never trash")
    parser.add_argument("--pause", type=float, default=0.5)
    args = parser.parse_args()

    if not args.journal.exists():
        print(f"No upload journal at {args.journal}.", file=sys.stderr)
        return 2
    journal = UploadJournal.load(args.journal)

    settings = load_config(args.config)
    work_dir = Path(settings.run["work_dir"])
    mirror_path = args.mirror or work_dir / "mirror.csv"
    mirror_keys = _load_mirror_keys(mirror_path)

    todo = journal.pending_replacement()
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("Nothing pending. Uploads must be verified before they can replace.", flush=True)
        return 0

    print(f"{len(todo)} verified upload(s) pending replacement", flush=True)
    if not args.apply:
        print("\nDRY RUN -- nothing will be changed. Re-run with --apply.\n", flush=True)

    ffprobe = settings.as_dict().get("tools", {}).get("ffprobe", "ffprobe")
    remote = GooglePhotosRemote(settings.as_dict())
    replaced = failed = refused = 0
    try:
        try:
            print(f"Account: {remote.login()}", flush=True)
        except Exception as exc:  # noqa: BLE001 - an expired cookie is routine, not a crash
            print(f"\nCould not open a Google Photos session: {exc}", file=sys.stderr)
            print(
                "Export a fresh cookies.txt into .photos-shrink/ and re-run. "
                "Nothing was changed.",
                file=sys.stderr,
            )
            return 2
        for index, record in enumerate(todo, 1):
            name = record.source.name if record.source else "?"
            prefix = f"  [{index}/{len(todo)}] {name}:"

            # A media key missing from the journal entry may still be
            # recoverable from the mirror; the fallback is applied only to
            # the job handed to replace_one, never written back onto the
            # journal record itself.
            job = record
            if not record.media_key:
                fallback = mirror_keys.get(str(record.source))
                if fallback:
                    job = _replace(record, media_key=fallback)

            try:
                outcome = replace_one(
                    remote,
                    job,
                    settings=settings,
                    ffprobe=ffprobe,
                    apply=args.apply,
                    keep_originals=args.keep_originals,
                )
            except Exception as exc:  # noqa: BLE001 - recorded per item, never fatal
                record.replace_error = f"{type(exc).__name__}: {exc}"
                print(f"{prefix} FAILED {type(exc).__name__}: {exc}", flush=True)
                failed += 1
            else:
                if outcome.status == "refused":
                    record.replaced = f"refused: {outcome.detail}"
                    refused += 1
                    print(f"{prefix} REFUSED ({outcome.detail})", flush=True)
                elif outcome.status == "would_replace":
                    print(f"{prefix} {outcome.detail}", flush=True)
                else:
                    # The status is already the word the journal records.
                    record.replaced = outcome.status
                    record.original_media_key = outcome.original_media_key
                    record.replaced_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                    print(f"{prefix} {outcome.detail}", flush=True)
                    replaced += 1
            finally:
                if args.apply:
                    journal.save()
            if args.pause:
                time.sleep(args.pause)
    finally:
        remote.close()

    if args.apply:
        print(f"\n--- replaced {replaced}, refused {refused}, failed {failed} ---", flush=True)
        print(f"  journal: {args.journal}", flush=True)
        print("  Originals remain in the Takeout export on disk.", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
