"""Finish a Takeout-sourced replacement: restore metadata, then trash the original.

The API can upload but cannot write album membership or delete, so this last
step runs on the browser session. It is short: the Takeout sidecar already gave
us each item's media key, so there is no library scan -- just a handful of small
requests per photo, with no byte transfer.

Safety follows the same sequence the main pipeline uses, in the same order:

    identity distinct -> restore metadata -> verify replacement -> trash

Nothing is trashed unless its replacement has been found, matched to the
original's identity, given the original's metadata, and verified. Dry run is the
default; --apply is required to change anything. The Takeout copy of every
original stays on disk regardless, so a mistake is recoverable by re-upload.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from photos_shrink import media
from photos_shrink.config import load_config
from photos_shrink.remote import GooglePhotosRemote


class ReplaceError(RuntimeError):
    """Raised when a replacement cannot be completed safely."""


def check_identity(original: dict, replacement: dict) -> None:
    """Refuse to mutate or trash anything that is not a distinct new item."""

    if not replacement or str(replacement.get("id")) == str(original.get("id")):
        raise ReplaceError("replacement identity is not distinct from original")
    if original.get("dedup_key") and replacement.get("dedup_key") == original.get("dedup_key"):
        raise ReplaceError("replacement carries the original deduplication identity")


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore metadata and trash replaced originals")
    parser.add_argument("--journal", type=Path, default=Path("G:/takeout-work/takeout-upload-journal.json"))
    parser.add_argument("--mirror", type=Path, default=Path("G:/takeout-work/mirror.csv"))
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true", help="Actually restore and trash")
    parser.add_argument("--keep-originals", action="store_true", help="Restore and verify, but never trash")
    parser.add_argument("--pause", type=float, default=0.5)
    args = parser.parse_args()

    if not args.journal.exists():
        print(f"No upload journal at {args.journal}.", file=sys.stderr)
        return 2
    journal = json.loads(args.journal.read_text(encoding="utf-8"))

    # The mirror supplies each original's media key, which is what removes the
    # need for a full library scan.
    keys: dict[str, str] = {}
    if args.mirror.exists():
        for row in csv.DictReader(open(args.mirror, encoding="utf-8")):
            if row.get("media_key"):
                keys[row["path"]] = row["media_key"]

    todo = [e for e in journal.values() if e.get("verified") == "ok" and not e.get("replaced")]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("Nothing pending. Uploads must be verified before they can replace.", flush=True)
        return 0

    settings = load_config(args.config)
    print(f"{len(todo)} verified upload(s) pending replacement", flush=True)
    if not args.apply:
        print("\nDRY RUN -- nothing will be changed. Re-run with --apply.\n", flush=True)

    remote = GooglePhotosRemote(settings.as_dict())
    replaced = failed = 0
    try:
        print(f"Account: {remote.login()}", flush=True)
        for index, entry in enumerate(todo, 1):
            source = entry["source"]
            output = Path(entry["output"])
            name = Path(source).name
            media_key = keys.get(source) or entry.get("media_key")
            if not media_key:
                print(f"  [{index}/{len(todo)}] {name}: no media key, skipped", flush=True)
                failed += 1
                continue

            try:
                original = remote.get_item(media_key)
                replacement = remote.find_uploaded(output)
                if replacement is None:
                    raise ReplaceError("replacement not found by content hash; not guessing")
                check_identity(original, replacement)

                if not args.apply:
                    print(
                        f"  [{index}/{len(todo)}] {name}: would restore "
                        f"{len((original.get('metadata') or {}).get('albums') or [])} album(s) "
                        f"then trash {media_key[:18]}...",
                        flush=True,
                    )
                    continue

                output_info = media.probe(output, settings.as_dict().get("tools", {}).get("ffprobe", "ffprobe"))
                output_info = {**output_info, "size_bytes": output.stat().st_size}
                remote.restore_metadata(original, replacement)
                remote.verify_replacement(original, replacement, output_info)

                if args.keep_originals:
                    entry["replaced"] = "verified_original_kept"
                    print(f"  [{index}/{len(todo)}] {name}: verified, original kept", flush=True)
                else:
                    remote.trash(original)
                    if not remote.is_trashed(original):
                        raise ReplaceError("trash was not confirmed by the server")
                    entry["replaced"] = "replaced"
                    print(f"  [{index}/{len(todo)}] {name}: replaced, original trashed", flush=True)
                entry["original_media_key"] = media_key
                entry["replaced_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                replaced += 1
            except Exception as exc:  # noqa: BLE001 - every failure is recorded, never fatal
                entry["replace_error"] = f"{type(exc).__name__}: {exc}"
                print(f"  [{index}/{len(todo)}] {name}: FAILED {type(exc).__name__}: {exc}", flush=True)
                failed += 1
            finally:
                if args.apply:
                    args.journal.write_text(json.dumps(journal, indent=2), encoding="utf-8")
            if args.pause:
                time.sleep(args.pause)
    finally:
        remote.close()

    if args.apply:
        print(f"\n--- replaced {replaced}, failed {failed} ---", flush=True)
        print(f"  journal: {args.journal}", flush=True)
        print("  Originals remain in the Takeout export on disk.", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
