"""Upload encoded Takeout replacements through the official Photos API.

Reads the CSV written by tools/takeout_pilot.py, uploads each encoded file,
verifies the created item by reading it back, and records the result so a later
pass can delete the corresponding originals.

Uploading and verification use OAuth only -- no exported cookies. This never
deletes anything: the originals stay untouched in Google Photos and in the
Takeout archive, and removing them is a separate, deliberate step.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import requests

from photos_shrink import takeout
from photos_shrink.config import load_config
from photos_shrink.photos_api import (
    PhotosApiClient,
    PhotosApiError,
    load_client_credentials,
)


def verify_bytes(item: dict, expected_sha256: str, timeout: float = 60.0) -> str:
    """Compare the stored bytes with what was uploaded.

    Returns "match", "differs", or a reason the check could not be made. A
    failure to check is reported honestly rather than counted as success.
    """

    base_url = item.get("baseUrl")
    if not base_url:
        return "unavailable: no baseUrl returned"
    try:
        # "=d" asks for the original bytes rather than a display rendition.
        response = requests.get(f"{base_url}=d", timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        return f"unavailable: {type(exc).__name__}"

    import hashlib

    return "match" if hashlib.sha256(response.content).hexdigest() == expected_sha256 else "differs"


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload encoded replacements via the Photos API")
    parser.add_argument("--report", type=Path, default=Path("G:/takeout-work/takeout-pilot.csv"))
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument("--limit", type=int, default=0, help="0 uploads every encoded row")
    parser.add_argument("--album", default=None, help="Create/use an album for the replacements")
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--pause", type=float, default=1.0, help="Seconds between uploads")
    parser.add_argument("--dry-run", action="store_true", help="List what would upload, then stop")
    args = parser.parse_args()

    if not args.report.exists():
        print(f"No encode report at {args.report}. Run tools/takeout_pilot.py first.", file=sys.stderr)
        return 2

    rows = [r for r in csv.DictReader(open(args.report, encoding="utf-8")) if r["status"] == "encoded"]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("No encoded rows to upload.", file=sys.stderr)
        return 1

    journal_path = args.journal or args.report.with_name("takeout-upload-journal.json")
    journal: dict[str, dict] = {}
    if journal_path.exists():
        journal = json.loads(journal_path.read_text(encoding="utf-8"))

    print(f"{len(rows)} encoded file(s) in {args.report}", flush=True)
    pending = [r for r in rows if r["output"] not in journal]
    print(f"{len(journal)} already uploaded, {len(pending)} to go", flush=True)
    if args.dry_run:
        for row in pending:
            print(f"  would upload {Path(row['output']).name}", flush=True)
        return 0
    if not pending:
        print("Nothing to do.", flush=True)
        return 0

    settings = load_config(args.config)
    work_dir = Path(settings.run["work_dir"])
    try:
        client_id, client_secret = load_client_credentials(work_dir)
    except PhotosApiError as exc:
        print(f"{exc}\n\nRun: bash tools/setup_google_api.sh", file=sys.stderr)
        return 2

    api = PhotosApiClient(client_id, client_secret, work_dir / "api-token.json")
    try:
        api.access_token()
    except PhotosApiError as exc:
        print(f"Credentials are not usable: {exc}", file=sys.stderr)
        return 2
    print("API credentials verified.", flush=True)

    album_id = None
    if args.album:
        album = api.create_album(args.album)
        album_id = album.get("id")
        print(f"Album '{args.album}' -> {album_id}", flush=True)

    uploaded = failed = 0
    for index, row in enumerate(pending, 1):
        output = Path(row["output"])
        if not output.exists():
            print(f"  [{index}/{len(pending)}] {output.name}: MISSING, skipped", flush=True)
            failed += 1
            continue

        local_sha = takeout.sha256(output)
        try:
            item = api.upload(output, album_id=album_id)
        except PhotosApiError as exc:
            print(f"  [{index}/{len(pending)}] {output.name}: FAILED {exc}", flush=True)
            failed += 1
            continue

        # Read it back rather than trusting the create response alone.
        try:
            stored = api.get_media_item(item["id"])
        except PhotosApiError as exc:
            stored = {}
            print(f"      read-back failed: {exc}", flush=True)

        verdict = verify_bytes(stored or item, local_sha)
        journal[str(output)] = {
            "source": row["source"],
            "output": str(output),
            "output_sha256": local_sha,
            "media_item_id": item["id"],
            "filename": (stored or item).get("filename"),
            "mime_type": (stored or item).get("mimeType"),
            "bytes_verified": verdict,
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        journal_path.write_text(json.dumps(journal, indent=2), encoding="utf-8")

        uploaded += 1
        print(
            f"  [{index}/{len(pending)}] {output.name}: uploaded id={item['id'][:18]}... "
            f"bytes={verdict}",
            flush=True,
        )
        if args.pause:
            time.sleep(args.pause)

    print(f"\n--- uploaded {uploaded}, failed {failed} ---", flush=True)
    matched = sum(1 for e in journal.values() if e["bytes_verified"] == "match")
    differs = [e for e in journal.values() if e["bytes_verified"] == "differs"]
    print(f"  byte-verified: {matched}/{len(journal)}", flush=True)
    if differs:
        print(f"  WARNING: {len(differs)} item(s) differ from what was uploaded:", flush=True)
        for entry in differs:
            print(f"    {entry['filename']}", flush=True)
        print("  Do NOT delete the originals for those.", flush=True)
    print(f"  journal: {journal_path}", flush=True)
    print("  No originals were deleted.", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
