"""Finish a Takeout-sourced replacement: restore metadata, then trash the original.

The API can upload but cannot write album membership or delete, so this last
step runs on the browser session. It is short: the Takeout sidecar already gave
us each item's media key, so there is no library scan.

This is the only code here that destroys anything, so every deletion has to
earn it. `replace_one` is the whole safety property: nine steps, in order, each
a refusal point. An original is trashed only when all nine hold:

  1. the job names a media key
  2. `confirm_original` -- fetch the item by that key, and require it to have
     the same id and exactly the exported original's size in bytes
  3. the configured gate (`policy.verdict`) allows it -- shared albums, date
     and name exclusions, and items that consume no quota are all refused
  4. the replacement exists and resolves by its own content hash
  5. `check_identity` -- the replacement is a distinct item from the original
  6. `output_info_for` -- hash and path the verification requires
  7. `restore_metadata`
  8. `verify_replacement`
  9. `trash`, confirmed by `is_trashed`

Dry run is the default. Every original also remains in the Takeout export on
disk, so even a mistake is recoverable by re-upload.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import media
from .config import Settings
from .integrity import sha256_file
from .ledger import UploadRecord
from .policy import Candidate, verdict


class ReplaceError(RuntimeError):
    """Raised when a replacement cannot be completed safely."""


def check_identity(original: dict[str, Any], replacement: dict[str, Any]) -> None:
    """Refuse to mutate or trash anything that is not a distinct new item.

    """

    if not replacement or str(replacement.get("id")) == str(original.get("id")):
        raise ReplaceError("replacement identity is not distinct from original")
    if original.get("dedup_key") and replacement.get("dedup_key") == original.get("dedup_key"):
        raise ReplaceError("replacement carries the original deduplication identity")


def confirm_original(library: Any, source: Path, media_key: str) -> dict[str, Any]:
    """Fetch the library item the sidecar names, and check it is this export.

    The media key is read from the sidecar, and sidecars are paired to media by
    filename -- which `takeout` has to de-truncate, so a pairing can be wrong.
    The key is accepted when the item it names agrees with the exported file:
    the same id, and exactly the same size in bytes.

    Google's search by content hash is deliberately not used. On the live
    library it missed items whose bytes it held, and for an exact duplicate it
    returned the other copy -- refusing correct pairings while costing a lookup
    for every item.
    """

    if not source.is_file():
        raise ReplaceError(f"source original is missing from the export: {source}")
    item = library.get_item(media_key)
    if str(item.get("id")) != str(media_key):
        raise ReplaceError(f"get_item returned {item.get('id')} for the sidecar's {media_key}")
    exported = source.stat().st_size
    if item.get("size_bytes") != exported:
        raise ReplaceError(
            f"library item is {item.get('size_bytes')} bytes but the exported original is "
            f"{exported}; the sidecar may describe a different file"
        )
    return item


def output_info_for(record: UploadRecord, output: Path, ffprobe: str) -> dict[str, Any]:
    """Build what verify_replacement requires: the encoded hash and its path.

    probe() reports dimensions and codec but neither the hash nor the path, and
    verify_replacement refuses without both -- so building this from probe alone
    made every replacement fail.
    """

    info = dict(media.probe(output, ffprobe))
    recorded = record.output_sha256
    actual = sha256_file(output)
    if recorded and recorded != actual:
        raise ReplaceError("the encoded file on disk no longer matches what was uploaded")
    info["sha256"] = actual
    info["path"] = str(output)
    info["size_bytes"] = output.stat().st_size
    return info


@dataclass(frozen=True)
class Outcome:
    """What happened, or would happen, to one job."""

    status: str  # "replaced" | "verified_original_kept" | "refused" | "would_replace"
    detail: str
    original_media_key: str | None = None


def replace_one(
    library: Any,
    job: UploadRecord,
    *,
    settings: Settings,
    ffprobe: str,
    apply: bool,
    keep_originals: bool,
) -> Outcome:
    """Restore metadata onto the replacement and trash the original.

    `library` is any adapter offering find_uploaded, get_item,
    restore_metadata, verify_replacement, trash and is_trashed -- the cookie
    session in production, a fake in tests.

    Raises `ReplaceError` for anything that cannot be completed safely.
    A policy refusal is not an error: it is reported as `Outcome(status="refused", ...)`
    so the caller can tell "this item must never be touched" apart from "something
    went wrong that needs investigation".
    """

    media_key = job.media_key
    if not media_key:
        raise ReplaceError("no media key; the original cannot be identified")
    if job.source is None:
        raise ReplaceError("no source original recorded for this upload")

    # Identity before anything else touches this item.
    original = confirm_original(library, job.source, media_key)

    blocked = verdict(settings, Candidate.from_library_item(original))
    if blocked:
        return Outcome(status="refused", detail=blocked, original_media_key=media_key)

    if job.output is None:
        raise ReplaceError("no encoded output recorded for this upload")
    replacement = library.find_uploaded(job.output)
    if replacement is None:
        raise ReplaceError("replacement not found by content hash; not guessing")
    check_identity(original, replacement)

    if not apply:
        albums = len((original.get("metadata") or {}).get("albums") or [])
        return Outcome(
            status="would_replace",
            detail=f"would restore {albums} album(s) and trash {media_key}",
            original_media_key=media_key,
        )

    info = output_info_for(job, job.output, ffprobe)
    # The receipt is now complete: the file on disk is the one whose hash was
    # recorded at upload, and the library holds an item with exactly those
    # bytes. That is proof this replacement is ours, which the web client cannot
    # read for an API upload -- so vouch for it before anything is restored or
    # verified. Never earlier: without a full receipt nothing is trusted.
    library.trust_replacement(str(replacement["id"]))
    library.restore_metadata(original, replacement)
    library.verify_replacement(original, replacement, info)

    if keep_originals:
        return Outcome(
            status="verified_original_kept",
            detail="verified, original kept",
            original_media_key=media_key,
        )

    library.trash(original)
    if not library.is_trashed(original):
        raise ReplaceError("trash was not confirmed by the server")
    return Outcome(
        status="replaced", detail="replaced, original trashed", original_media_key=media_key
    )
