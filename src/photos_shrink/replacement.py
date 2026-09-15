"""Finish a Takeout-sourced replacement: restore metadata, then trash the original.

The API can upload but cannot write album membership or delete, so this last
step runs on the browser session. It is short: the Takeout sidecar already gave
us each item's media key, so there is no library scan.

This is the only code here that destroys anything, so every deletion has to
earn it. `replace_one` is the whole safety property: ten steps, in order, each
a refusal point. An original is trashed only when all ten hold:

  1. the job names a media key
  2. `confirm_original` -- hash the exported original, require it to resolve
     to that key. A media key alone is a *claim*; the content hash is proof.
  3. `get_item` -- fetch the full library item, and reconcile it against the
     proven item from step 2 (same id) rather than silently trusting either
     one alone
  4. the configured gate (`policy.verdict`) allows it -- shared albums, date
     and name exclusions, and items that consume no quota are all refused
  5. the replacement exists and resolves by its own content hash
  6. `check_identity` -- the replacement is a distinct item from the original
  7. `output_info_for` -- hash and path the verification requires
  8. `restore_metadata`
  9. `verify_replacement`
  10. `trash`, confirmed by `is_trashed`

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
    """Prove the library item we are about to trash is the photo we encoded.

    The sidecar's media key is a claim; hashing the exported bytes and asking
    the library which item owns them is proof. Both must name the same item.
    """

    if not source.is_file():
        raise ReplaceError(f"source original is missing from the export: {source}")
    found = library.find_uploaded(source)
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


def _fetch_full_item(library: Any, media_key: str, proven: dict[str, Any]) -> dict[str, Any]:
    """Fetch the fuller item `get_item` offers, without discarding the proof.

    `confirm_original` already proved which item this is by content hash;
    `get_item` is only asked for the richer view (album membership, dedup
    key) that the hash-match response may not carry. The two must name the
    same item, or the fuller item is not trusted -- one is never allowed to
    silently replace the other.
    """

    full = library.get_item(media_key)
    if str(full.get("id")) != str(proven.get("id")):
        raise ReplaceError(
            f"get_item returned {full.get('id')} but the content hash proved {proven.get('id')}"
        )
    return full


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

    # Proof of identity before anything else touches this item.
    proven = confirm_original(library, job.source, media_key)
    original = _fetch_full_item(library, media_key, proven)

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
