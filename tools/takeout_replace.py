"""Finish Takeout-sourced replacements: fix each replacement's metadata, then trash originals.

All the safety logic -- what must hold before an original may be trashed --
lives in `photos_shrink.replacement.replace_batch`. This tool is just argparse,
a loop over the journal's pending replacements a batch at a time, and
reporting. See that module's docstring for the full list of checks.

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
from photos_shrink.mirror_sizes import load_exported_sizes
from photos_shrink.remote import COOKIE_HINT, RemoteProtocolError, open_session
from photos_shrink.replacement import replace_batch


def _load_mirror_keys(mirror_path: Path) -> dict[str, str]:
    keys: dict[str, str] = {}
    if mirror_path.exists():
        with open(mirror_path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("media_key"):
                    keys[row["path"]] = row["media_key"]
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description="Fix replacement metadata and trash replaced originals")
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--mirror", type=Path, default=None)
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch", type=int, default=100, help="Items per batch (default 100)")
    parser.add_argument("--apply", action="store_true", help="Actually fix and trash")
    parser.add_argument(
        "--keep-originals", action="store_true",
        help="Fix and verify, never trash; the journal is not updated",
    )
    parser.add_argument("--pause", type=float, default=1.0, help="Seconds between batches")
    args = parser.parse_args()

    if not args.journal.exists():
        print(f"No upload journal at {args.journal}.", file=sys.stderr)
        return 2
    if args.batch < 1:
        print("--batch must be at least 1.", file=sys.stderr)
        return 2
    journal = UploadJournal.load(args.journal)

    settings = load_config(args.config)
    data_dir = Path(settings.run["data_dir"])
    mirror_path = args.mirror or data_dir / "mirror.csv"
    mirror_keys = _load_mirror_keys(mirror_path)
    exported_sizes = load_exported_sizes(mirror_path)

    todo = journal.pending_replacement()
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("Nothing pending. Uploads must be verified before they can replace.", flush=True)
        return 0

    print(f"{len(todo)} verified upload(s) pending replacement, in batches of {args.batch}", flush=True)
    if not args.apply:
        print("\nDRY RUN -- nothing will be changed. Re-run with --apply.\n", flush=True)

    # A media key missing from the journal entry may still be recoverable from
    # the mirror; the fallback goes only into the job, never onto the record.
    records = {record.key: record for record in todo}
    jobs = [
        _replace(record, media_key=mirror_keys[str(record.source)])
        if not record.media_key and str(record.source) in mirror_keys
        else record
        for record in todo
    ]

    counts: dict[str, int] = {}
    started = time.monotonic()
    try:
        with open_session(settings) as remote:
            print(f"Account: {remote.account_id()}", flush=True)
            for start in range(0, len(jobs), args.batch):
                batch = jobs[start : start + args.batch]
                label = f"[{start + 1}-{start + len(batch)}/{len(jobs)}]"
                batch_started = time.monotonic()
                try:
                    outcomes = replace_batch(
                        remote, batch, settings=settings, apply=args.apply,
                        keep_originals=args.keep_originals, sizes=exported_sizes,
                        progress=lambda message, label=label: print(f"{label} {message}", flush=True),
                    )
                except RemoteProtocolError as exc:
                    # A request for the whole batch failed -- most often an
                    # expired session. Nothing after the failure ran.
                    print(f"{label} batch stopped: {exc}", flush=True)
                    counts["failed"] = counts.get("failed", 0) + len(batch)
                    break
                for job in batch:
                    outcome = outcomes[job.key]
                    record = records[job.key]
                    name = record.source.name if record.source else "?"
                    counts[outcome.status] = counts.get(outcome.status, 0) + 1
                    print(f"  {name}: {outcome.status.upper()} {outcome.detail}", flush=True)
                    if not args.apply or outcome.status == "verified_original_kept":
                        continue
                    if outcome.status == "failed":
                        record.replace_error = outcome.detail
                    elif outcome.status == "refused":
                        record.replaced = f"refused: {outcome.detail}"
                    else:
                        record.replaced = outcome.status
                        record.original_media_key = outcome.original_media_key
                        record.replaced_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                        record.replace_error = None
                if args.apply:
                    journal.save()
                print(f"{label} done in {time.monotonic() - batch_started:.1f}s", flush=True)
                if args.pause and start + args.batch < len(jobs):
                    time.sleep(args.pause)
    except RemoteProtocolError as exc:
        print(f"\n{exc}", file=sys.stderr)
        print(COOKIE_HINT, file=sys.stderr)
        return 2

    summary = ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
    print(f"\n--- {summary} in {time.monotonic() - started:.1f}s ---", flush=True)
    if args.apply:
        print(f"  journal: {args.journal}", flush=True)
        print("  Originals remain in the Takeout export on disk.", flush=True)
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
