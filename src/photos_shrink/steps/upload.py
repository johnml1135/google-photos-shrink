"""Upload encoded Takeout replacements through the official Photos API.

Reads the CSV written by `photos-shrink encode`, uploads each encoded file,
verifies the created item by reading it back, and records the result so a later
pass can delete the corresponding originals.

Uploading and verification use OAuth only -- no exported cookies. This never
deletes anything: the originals stay untouched in Google Photos and in the
Takeout archive, and removing them is a separate, deliberate step.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from photos_shrink import media, policy, takeout
from photos_shrink.config import load_config
from photos_shrink.integrity import sha256_file
from photos_shrink.ledger import UploadJournal, UploadRecord
from photos_shrink.photos_api import (
    PhotosApiClient,
    PhotosApiError,
    load_client_credentials,
)


def load_origins(mirror_path: Path) -> dict[str, str] | None:
    """Media key -> sidecar origin from the mirror, or None if it predates origins."""

    if not mirror_path.exists():
        return None
    with open(mirror_path, encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if "origin" not in (reader.fieldnames or []):
            return None
        return {row["media_key"]: row["origin"] for row in reader if row.get("media_key")}


def verify_item(item: dict, source: Path, ffprobe: str) -> tuple[str, str]:
    """Check the created item against the file that was uploaded.

    Byte comparison is impossible through this API: Google re-renders AVIF (and
    other formats) to JPEG for delivery, so `baseUrl=d` returns a rendition
    roughly twice the size of the stored file rather than the stored bytes.
    Hashing that download reports a mismatch for every single item and tells you
    nothing, so it is not attempted.

    What the API does report faithfully -- filename, pixel dimensions and
    capture time -- is checked instead. Returns (verdict, detail).
    """

    metadata = item.get("mediaMetadata") or {}
    try:
        width, height = int(metadata.get("width")), int(metadata.get("height"))
    except (TypeError, ValueError):
        return "unverified", "the API returned no dimensions"

    local = media.probe(source, ffprobe)
    if (width, height) != (local["width"], local["height"]):
        return "mismatch", (
            f"stored {width}x{height} but uploaded {local['width']}x{local['height']}"
        )
    if item.get("filename") != source.name:
        return "mismatch", f"stored filename {item.get('filename')!r}"
    if not metadata.get("creationTime"):
        return "unverified", "no capture time was recorded"
    return "ok", str(metadata.get("creationTime"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload encoded replacements via the Photos API")
    parser.add_argument("--report", type=Path, default=None, help="Defaults to <data_dir>/encoded.csv")
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument("--limit", type=int, default=0, help="0 uploads every encoded row")
    parser.add_argument("--album", default=None, help="Create/use an album for the replacements")
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--mirror", type=Path, default=None, help="Defaults to <data_dir>/mirror.csv")
    parser.add_argument("--pause", type=float, default=1.0, help="Seconds between uploads")
    parser.add_argument("--dry-run", action="store_true", help="List what would upload, then stop")
    parser.add_argument(
        "--reverify",
        action="store_true",
        help="Re-check already uploaded items against the API. Uploads nothing.",
    )
    args = parser.parse_args(argv)

    settings = load_config(args.config)
    work_dir = Path(settings.run["work_dir"])
    ffprobe = settings.tools["ffprobe"]
    if args.report is None:
        args.report = Path(settings.run["data_dir"]) / "encoded.csv"
    if not args.report.exists():
        print(f"No encode report at {args.report}. Run `photos-shrink encode` first.", file=sys.stderr)
        return 2

    with open(args.report, encoding="utf-8") as handle:
        rows = [r for r in csv.DictReader(handle) if r["status"] == "encoded"]

    # Ask the gate again before spending quota. The report was gated by the
    # encoder of its day, which never checked where a photo came from: 194
    # partner-shared photos were uploaded, each a new copy on this account's
    # storage of a photo that cost it nothing.
    mirror_path = args.mirror or Path(settings.run["data_dir"]) / "mirror.csv"
    origins = load_origins(mirror_path)
    if origins is None:
        print(
            f"{mirror_path} has no origin column. Re-run `photos-shrink mirror` so uploads "
            "can refuse photos shared in by someone else.",
            file=sys.stderr,
        )
        return 2
    refused: dict[str, int] = {}
    allowed = []
    for row in rows:
        kind = takeout.classify(Path(row["source"])) or "unknown"
        # Through the module: `verdict` is a local name in this function.
        candidate = policy.Candidate.from_encode_row(row, kind=kind, origin=origins.get(row.get("media_key", "")))
        token = policy.verdict(settings, candidate)
        if token:
            refused[token] = refused.get(token, 0) + 1
        else:
            allowed.append(row)
    for token, count in sorted(refused.items()):
        print(f"  refused {count:,}: {policy.explain(token)}", flush=True)
    rows = allowed
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("No encoded rows to upload.", file=sys.stderr)
        return 1

    journal_path = args.journal or args.report.with_name("takeout-upload-journal.json")
    journal = UploadJournal.load(journal_path)

    done_keys = journal.already_uploaded_keys()
    print(f"{len(rows)} encoded file(s) in {args.report}", flush=True)
    pending = [
        r for r in rows
        if r["output"] not in journal and (not r.get("media_key") or r["media_key"] not in done_keys)
    ]
    print(f"{len(journal)} already uploaded, {len(pending)} to go", flush=True)
    if args.dry_run:
        for row in pending:
            print(f"  would upload {Path(row['output']).name}", flush=True)
        return 0
    if not pending and not args.reverify:
        print("Nothing to do.", flush=True)
        return 0

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

    if args.reverify:
        # Scoped by the report, like every other run of this tool. The journal
        # holds every upload ever made; re-checking the 121 rows of
        # encoded-videos.csv costs 121 requests, where the whole journal would
        # spend 9,244 against a quota of 10,000 a day.
        wanted = {row["output"] for row in rows}
        scoped = [r for r in journal.all_records() if str(r.output) in wanted]
        print(f"re-checking {len(scoped)} of {len(journal)} uploaded item(s)", flush=True)
        try:
            for index, record in enumerate(scoped, 1):
                try:
                    stored = api.get_media_item(record.media_item_id)
                except PhotosApiError as exc:
                    # An API failure says nothing about the item, so the verdict
                    # already recorded stands. Overwriting it with "unverified"
                    # would strand a verified upload: only verified uploads are
                    # allowed to replace their original.
                    print(f"  {record.output.name}: not checked  {exc}", flush=True)
                    if exc.status == 429:
                        print("  Quota is gone for the day; stopping.", file=sys.stderr)
                        break
                    continue
                verdict, detail = verify_item(stored, record.output, ffprobe)
                record.verified, record.verified_detail = verdict, detail
                record.capture_time = (stored.get("mediaMetadata") or {}).get("creationTime")
                print(f"  {record.output.name}: {verdict}  {detail}", flush=True)
                # Save as it goes: a pass that is interrupted keeps what it checked
                # rather than spending the quota a second time.
                if index % 50 == 0:
                    journal.save()
        finally:
            # Whatever ended the pass -- the quota, a keyboard, an error
            # nobody modelled -- what it checked is already paid for.
            journal.save()
        ok = sum(1 for r in scoped if r.verified == "ok")
        print(f"\n  verified {ok}/{len(scoped)}; nothing was uploaded.", flush=True)
        return 0

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

        local_sha = sha256_file(output)
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

        verdict, detail = verify_item(stored or item, output, ffprobe)
        journal.record(UploadRecord(
            source=Path(row["source"]),
            media_key=row.get("media_key") or "",
            output=output,
            output_sha256=local_sha,
            media_item_id=item["id"],
            filename=(stored or item).get("filename"),
            mime_type=(stored or item).get("mimeType"),
            verified=verdict,
            verified_detail=detail,
            capture_time=((stored or item).get("mediaMetadata") or {}).get("creationTime"),
            uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        ))

        uploaded += 1
        print(
            f"  [{index}/{len(pending)}] {output.name}: uploaded  {verdict}  {detail}",
            flush=True,
        )
        if args.pause:
            time.sleep(args.pause)

    print(f"\n--- uploaded {uploaded}, failed {failed} ---", flush=True)
    ok = sum(1 for r in journal.all_records() if r.verified == "ok")
    bad = [r for r in journal.all_records() if r.verified == "mismatch"]
    unverified = [r for r in journal.all_records() if r.verified == "unverified"]
    print(f"  verified (name, dimensions, capture time): {ok}/{len(journal)}", flush=True)
    note = "  Note: the API cannot confirm stored bytes; it re-renders AVIF to JPEG on download."
    print(note, flush=True)
    if unverified:
        print(f"  {len(unverified)} item(s) could not be checked.", flush=True)
    if bad:
        print(f"  WARNING: {len(bad)} item(s) do not match what was uploaded:", flush=True)
        for record in bad:
            print(f"    {record.filename}: {record.verified_detail}", flush=True)
        print("  Do NOT delete the originals for those.", flush=True)
    print(f"  journal: {journal_path}", flush=True)
    print("  No originals were deleted.", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
