"""Finish a Takeout-sourced replacement: restore metadata, then trash the original.

The API can upload but cannot write album membership or delete, so this last
step runs on the browser session. It is short: the Takeout sidecar already gave
us each item's media key, so there is no library scan.

This is the only code here that destroys anything, so every deletion has to earn
it. An original is trashed only when all of this holds:

  1. Its bytes, hashed locally from the Takeout export, resolve in the library
     to exactly the media key the sidecar claimed. A media key alone is a
     *claim* about identity; the content hash is proof. Without this a mistyped
     or mismatched sidecar would trash an unrelated photo whose replacement was
     never uploaded.
  2. The configured gate allows it -- shared albums, date and name exclusions,
     and items that consume no quota are all refused, using the same
     pipeline.skip_reason every other entry point uses.
  3. The replacement exists, resolves by its own content hash, and is a
     distinct item from the original.
  4. Metadata and album membership have been restored onto it and verified
     against the hash and path of the file that was actually encoded.

Dry run is the default. Every original also remains in the Takeout export on
disk, so even a mistake is recoverable by re-upload.
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
from photos_shrink.integrity import sha256_file
from photos_shrink.pipeline import skip_reason
from photos_shrink.remote import GooglePhotosRemote


class ReplaceError(RuntimeError):
    """Raised when a replacement cannot be completed safely."""


def check_identity(original: dict, replacement: dict) -> None:
    """Refuse to mutate or trash anything that is not a distinct new item."""

    if not replacement or str(replacement.get("id")) == str(original.get("id")):
        raise ReplaceError("replacement identity is not distinct from original")
    if original.get("dedup_key") and replacement.get("dedup_key") == original.get("dedup_key"):
        raise ReplaceError("replacement carries the original deduplication identity")


def confirm_original(remote, source: Path, media_key: str) -> dict:
    """Prove the library item we are about to trash is the photo we encoded.

    The sidecar's media key is a claim; hashing the exported bytes and asking
    the library which item owns them is proof. Both must name the same item.
    """

    if not source.is_file():
        raise ReplaceError(f"source original is missing from the export: {source}")
    found = remote.find_uploaded(source)
    if found is None:
        raise ReplaceError(
            "the exported original's bytes do not resolve to any library item; "
            "cannot prove which item to trash"
        )
    if str(found.get("id")) != str(media_key):
        raise ReplaceError(
            f"content hash resolves to {found.get('id')} but the sidecar claims {media_key}"
        )
    return found


def output_info_for(entry: dict, output: Path, ffprobe: str) -> dict:
    """Build what verify_replacement requires: the encoded hash and its path.

    probe() reports dimensions and codec but neither the hash nor the path, and
    verify_replacement refuses without both -- so building this from probe alone
    made every replacement fail.
    """

    info = dict(media.probe(output, ffprobe))
    recorded = entry.get("output_sha256")
    actual = sha256_file(output)
    if recorded and recorded != actual:
        raise ReplaceError("the encoded file on disk no longer matches what was uploaded")
    info["sha256"] = actual
    info["path"] = str(output)
    info["size_bytes"] = output.stat().st_size
    return info


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
    journal = json.loads(args.journal.read_text(encoding="utf-8"))

    settings = load_config(args.config)
    work_dir = Path(settings.run["work_dir"])
    mirror_path = args.mirror or work_dir / "mirror.csv"

    keys: dict[str, str] = {}
    if mirror_path.exists():
        with open(mirror_path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("media_key"):
                    keys[row["path"]] = row["media_key"]

    todo = [e for e in journal.values() if e.get("verified") == "ok" and not e.get("replaced")]
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
        print(f"Account: {remote.login()}", flush=True)
        for index, entry in enumerate(todo, 1):
            source = Path(entry["source"])
            output = Path(entry["output"])
            name = source.name
            media_key = entry.get("media_key") or keys.get(str(source))
            prefix = f"  [{index}/{len(todo)}] {name}:"

            try:
                if not media_key:
                    raise ReplaceError("no media key; the original cannot be identified")

                # Proof of identity before anything else touches this item.
                original = confirm_original(remote, source, media_key)
                original = remote.get_item(media_key)

                blocked = skip_reason(settings, original)
                if blocked:
                    entry["replaced"] = f"refused: {blocked}"
                    refused += 1
                    print(f"{prefix} REFUSED ({blocked})", flush=True)
                    continue

                replacement = remote.find_uploaded(output)
                if replacement is None:
                    raise ReplaceError("replacement not found by content hash; not guessing")
                check_identity(original, replacement)

                if not args.apply:
                    albums = len((original.get("metadata") or {}).get("albums") or [])
                    print(
                        f"{prefix} would restore {albums} album(s) and trash {media_key[:18]}...",
                        flush=True,
                    )
                    continue

                info = output_info_for(entry, output, ffprobe)
                remote.restore_metadata(original, replacement)
                remote.verify_replacement(original, replacement, info)

                if args.keep_originals:
                    entry["replaced"] = "verified_original_kept"
                    print(f"{prefix} verified, original kept", flush=True)
                else:
                    remote.trash(original)
                    if not remote.is_trashed(original):
                        raise ReplaceError("trash was not confirmed by the server")
                    entry["replaced"] = "replaced"
                    print(f"{prefix} replaced, original trashed", flush=True)
                entry["original_media_key"] = media_key
                entry["replaced_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                replaced += 1
            except Exception as exc:  # noqa: BLE001 - recorded per item, never fatal
                entry["replace_error"] = f"{type(exc).__name__}: {exc}"
                print(f"{prefix} FAILED {type(exc).__name__}: {exc}", flush=True)
                failed += 1
            finally:
                if args.apply:
                    args.journal.write_text(json.dumps(journal, indent=2), encoding="utf-8")
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
