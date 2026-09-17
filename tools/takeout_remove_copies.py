"""Trash uploaded replacements whose originals will never be replaced.

A replacement whose original is refused -- it costs no quota, was shared in by
someone else, or is otherwise left alone -- is only an extra copy on this
account's storage. This asks the same gate as `takeout_replace.py` of every
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

from photos_shrink.config import load_config
from photos_shrink.ledger import UploadJournal
from photos_shrink.remote import COOKIE_HINT, RemoteProtocolError, open_session
from photos_shrink.replacement import remove_extra_copies


def main() -> int:
    parser = argparse.ArgumentParser(description="Trash replacements whose originals are refused")
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch", type=int, default=100, help="Items per batch (default 100)")
    parser.add_argument("--apply", action="store_true", help="Actually trash the extra copies")
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

    todo = journal.copy_removal_candidates()
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("No uploads to check.", flush=True)
        return 0
    print(f"{len(todo)} upload(s) not yet replaced, in batches of {args.batch}", flush=True)
    if not args.apply:
        print("\nDRY RUN -- nothing will be changed. Re-run with --apply.\n", flush=True)

    counts: dict[str, int] = {}
    started = time.monotonic()
    try:
        with open_session(settings) as remote:
            print(f"Account: {remote.account_id()}", flush=True)
            for start in range(0, len(todo), args.batch):
                batch = todo[start : start + args.batch]
                label = f"[{start + 1}-{start + len(batch)}/{len(todo)}]"
                try:
                    outcomes = remove_extra_copies(
                        remote, batch, settings=settings, apply=args.apply,
                        progress=lambda message, label=label: print(f"{label} {message}", flush=True),
                    )
                except RemoteProtocolError as exc:
                    print(f"{label} batch stopped: {exc}", flush=True)
                    counts["failed"] = counts.get("failed", 0) + len(batch)
                    break
                for record in batch:
                    outcome = outcomes[record.key]
                    counts[outcome.status] = counts.get(outcome.status, 0) + 1
                    if outcome.status != "kept":
                        name = record.source.name if record.source else "?"
                        print(f"  {name}: {outcome.status.upper()} {outcome.detail}", flush=True)
                    if args.apply and outcome.status == "copy_removed":
                        record.replaced = f"refused: {outcome.detail}"
                        record.copy_removed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                if args.apply:
                    journal.save()
                if args.pause and start + args.batch < len(todo):
                    time.sleep(args.pause)
    except RemoteProtocolError as exc:
        print(f"\n{exc}", file=sys.stderr)
        print(COOKIE_HINT, file=sys.stderr)
        return 2

    summary = ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
    print(f"\n--- {summary} in {time.monotonic() - started:.1f}s ---", flush=True)
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
