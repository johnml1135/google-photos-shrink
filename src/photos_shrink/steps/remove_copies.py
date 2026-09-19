"""Trash uploaded replacements whose originals will never be replaced.

A replacement whose original is refused -- it costs no quota, was shared in by
someone else, or is otherwise left alone -- is only an extra copy on this
account's storage. This asks the same gate as the replace step of every
upload not yet replaced, and trashes the replacement of each refused original,
confirmed in the bin. No original is touched. The safety logic lives in
`photos_shrink.replacement.remove_extra_copies`.

Dry run is the default.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from photos_shrink import media
from photos_shrink.batching import run_batches, summarise
from photos_shrink.config import load_config
from photos_shrink.ledger import UploadJournal
from photos_shrink.mirror_sizes import load_exported_sizes
from photos_shrink.remote import COOKIE_HINT, RemoteProtocolError, open_session
from photos_shrink.replacement import SessionWatch, remove_extra_copies


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trash replacements whose originals are refused")
    parser.add_argument("--journal", type=Path, default=None, help="Defaults to <data_dir>/takeout-upload-journal.json")
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch", type=int, default=100, help="Items per batch (default 100)")
    parser.add_argument("--apply", action="store_true", help="Actually trash the extra copies")
    parser.add_argument("--pause", type=float, default=1.0, help="Seconds between batches")
    args = parser.parse_args(argv)

    settings = load_config(args.config)
    if args.journal is None:
        args.journal = Path(settings.run["data_dir"]) / "takeout-upload-journal.json"
    if not args.journal.exists():
        print(f"No upload journal at {args.journal}.", file=sys.stderr)
        return 2
    if args.batch < 1:
        print("--batch must be at least 1.", file=sys.stderr)
        return 2
    journal = UploadJournal.load(args.journal)
    exported_sizes = load_exported_sizes(Path(settings.run["data_dir"]) / "mirror.csv")

    todo = journal.copy_removal_candidates()
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("No uploads to check.", flush=True)
        return 0
    print(f"{len(todo)} upload(s) not yet replaced, in batches of {args.batch}", flush=True)
    if not args.apply:
        print("\nDRY RUN -- nothing will be changed. Re-run with --apply.\n", flush=True)

    # One watch for the run: a dead session is many empty batches, at any size.
    watch = SessionWatch()
    started = time.monotonic()
    try:
        with open_session(settings) as remote:
            print(f"Account: {remote.account_id()}", flush=True)
            counts = run_batches(
                todo, records={record.key: record for record in todo}, journal=journal,
                apply=args.apply, size=args.batch, pause=args.pause,
                report=lambda message: print(message, flush=True),
                announce=lambda status: status != "kept",
                work=lambda batch, progress: remove_extra_copies(
                    remote, batch, settings=settings, apply=args.apply,
                    sizes=exported_sizes, watch=watch,
                    probe=lambda source: media.probe(source, settings.tools["ffprobe"]), progress=progress,
                ),
            )
    except RemoteProtocolError as exc:
        print(f"\n{exc}", file=sys.stderr)
        print(COOKIE_HINT, file=sys.stderr)
        return 2

    print(summarise(counts, time.monotonic() - started), flush=True)
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
