"""Finish Takeout-sourced replacements in batches: fix each replacement, then trash originals.

The API can upload but cannot write album membership or delete, so this last
step runs on the browser session, which lasts about fifteen minutes. So it
works a batch at a time: every read, fix and trash below is one batched request
for the whole batch, not a round trip per item.

This is the only code here that destroys anything. An original is trashed only
when all of these hold, and any item that fails one is left alone while the
rest of its batch carries on:

  1. the job names a media key, and the exported original is on disk
  2. the encoded file on disk is the one whose hash was recorded at upload
  3. the item the sidecar's media key names has that id and exactly the
     exported original's size in bytes -- a sidecar paired with the wrong
     file names an item of another size
  4. the configured gate (`policy.verdict`) allows it, and the original is not
     shared, a partial upload, or a motion photo
  5. the replacement resolves by its content hash to a distinct item
  6. the replacement has the original's capture time, is in every album the
     original is in, and carries its favorite, archive and description --
     fixed where it does not, then re-read to prove the fix took
  7. `trash_many`, confirmed by finding the original's dedup key in the bin

Dry run is the default. Every original also remains in the Takeout export on
disk, so even a mistake is recoverable by re-upload.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .integrity import sha256_file
from .ledger import UploadRecord
from .policy import Candidate, verdict


class ReplaceError(RuntimeError):
    """Raised when a replacement cannot be completed safely."""


# `GooglePhotosRemote` flags an item it cannot safely handle. For an original
# about to be trashed, these flags refuse it. The others -- ownership the web
# client cannot read, an unknown media type, no download URL -- say nothing
# about whether deleting it loses anything: the original is in this account's
# own Takeout export and costs its quota, which the gate requires.
REFUSED_ORIGINAL_STATES = {
    "shared item": "shared_item",
    "shared album association": "shared_album",
    "partial upload": "partial_upload",
    "motion photo association is unsupported": "motion_photo",
    "favorite/archive metadata unknown": "unknown_favorite_or_archive",
}


@dataclass(frozen=True)
class Outcome:
    """What happened, or would happen, to one job."""

    status: str  # "replaced" | "would_replace" | "verified_original_kept" | "refused" | "failed"
    detail: str
    original_media_key: str | None = None


def check_original(item: dict[str, Any], job: UploadRecord) -> None:
    """Accept the sidecar's media key only when the item it names matches the export.

    Google's search by content hash is deliberately not used for originals. On
    the live library it missed items whose bytes it held, and for an exact
    duplicate it returned the other copy.
    """

    if str(item.get("id")) != str(job.media_key):
        raise ReplaceError(f"library returned {item.get('id')} for the sidecar's {job.media_key}")
    exported = job.source.stat().st_size
    if item.get("size_bytes") != exported:
        raise ReplaceError(
            f"library item is {item.get('size_bytes')} bytes but the exported original is "
            f"{exported}; the sidecar may describe a different file"
        )


def refusal(settings: Settings, original: dict[str, Any]) -> str | None:
    """Why this original must not be replaced, or None when it may be."""

    blocked = verdict(settings, Candidate.from_library_item(original))
    if blocked:
        return blocked
    metadata = original.get("metadata") or {}
    # Checked here as well as through the flags: the adapter reports only its
    # first concern, so an unreadable owner can hide an unknown favorite.
    if metadata.get("favorite") is None or metadata.get("archived") is None:
        return "unknown_favorite_or_archive"
    return REFUSED_ORIGINAL_STATES.get(original.get("skip_reason") or "")


def check_identity(original: dict[str, Any], replacement: dict[str, Any]) -> None:
    """Refuse a replacement that is not a distinct new item."""

    if str(replacement.get("id")) == str(original.get("id")):
        raise ReplaceError("replacement identity is not distinct from original")
    if original.get("dedup_key") and replacement.get("dedup_key") == original.get("dedup_key"):
        raise ReplaceError("replacement carries the original deduplication identity")


def _albums(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    albums = (item.get("metadata") or {}).get("albums") or []
    return {album["id"]: album for album in albums if isinstance(album, dict) and album.get("id")}


def _to_the_second(item: dict[str, Any]) -> tuple[Any, Any]:
    timestamp = item.get("timestamp_ms")
    return (timestamp // 1000 if isinstance(timestamp, int) else timestamp, item.get("timezone_offset"))


def needed_fixes(original: dict[str, Any], replacement: dict[str, Any]) -> dict[str, Any]:
    """What must change on the replacement to carry the original's metadata. Empty when nothing."""

    fixes: dict[str, Any] = {}
    # Compared to the second: Google sets a capture time in whole seconds, so a
    # fixed replacement can never carry the original's milliseconds.
    if _to_the_second(replacement) != _to_the_second(original):
        fixes["timestamp"] = (original.get("timestamp_ms"), original.get("timezone_offset"))
    have = _albums(replacement)
    missing = [album for key, album in _albums(original).items() if key not in have]
    if missing:
        fixes["albums"] = missing
    wanted, current = original.get("metadata") or {}, replacement.get("metadata") or {}
    for key in ("favorite", "archived"):
        if wanted.get(key) != current.get(key):
            fixes[key] = bool(wanted.get(key))
    # An original with no description has nothing to carry; one the upload
    # happened to add is not a loss.
    if wanted.get("description") and wanted.get("description") != current.get("description"):
        fixes["description"] = wanted["description"]
    return fixes


def unexpected_shared_album(original: dict[str, Any], replacement: dict[str, Any]) -> bool:
    """True when the replacement sits in a shared album the original is not in.

    An extra album is expected -- the uploader files replacements into its own
    batch album -- but a shared one would expose the photo further.
    """

    originals = _albums(original)
    return any(album.get("shared") for key, album in _albums(replacement).items() if key not in originals)


def describe(fixes: dict[str, Any]) -> str:
    parts = []
    if "timestamp" in fixes:
        parts.append("capture time")
    if fixes.get("albums"):
        parts.append(f"{len(fixes['albums'])} album(s)")
    parts += [key for key in ("favorite", "archived", "description") if key in fixes]
    return ", ".join(parts)


def replace_batch(
    library: Any,
    jobs: list[UploadRecord],
    *,
    settings: Settings,
    apply: bool,
    keep_originals: bool,
    progress: Callable[[str], None] = lambda message: None,
) -> dict[str, Outcome]:
    """Replace a batch of jobs; return each job's outcome, keyed by `UploadRecord.key`.

    `library` is any adapter offering get_items, find_uploaded_many,
    restore_many, trash_many and in_bin -- the cookie session in production, a
    fake in tests. A failure is recorded against its own job and never stops the rest.
    """

    outcomes: dict[str, Outcome] = {}
    live: dict[str, UploadRecord] = {}

    def fail(job: UploadRecord, detail: str) -> None:
        outcomes[job.key] = Outcome("failed", detail, job.media_key)
        live.pop(job.key, None)

    # 1-2: everything that can be settled on disk, before any request.
    for job in jobs:
        if not job.media_key:
            outcomes[job.key] = Outcome("failed", "no media key; the original cannot be identified")
        elif job.source is None or not job.source.is_file():
            outcomes[job.key] = Outcome("failed", f"source original is missing from the export: {job.source}", job.media_key)
        elif job.output is None or not job.output.is_file():
            outcomes[job.key] = Outcome("failed", "the encoded output is missing", job.media_key)
        elif job.output_sha256 and sha256_file(job.output) != job.output_sha256:
            outcomes[job.key] = Outcome("failed", "the encoded file on disk no longer matches what was uploaded", job.media_key)
        else:
            live[job.key] = job

    # 3-4: the originals, by the sidecar's media key.
    progress(f"reading {len(live)} original(s)")
    originals_by_key = library.get_items([job.media_key for job in live.values()])
    originals: dict[str, dict[str, Any]] = {}
    for job in list(live.values()):
        item = originals_by_key.get(job.media_key)
        if item is None or isinstance(item, Exception):
            fail(job, f"original could not be read: {item or 'no response'}")
            continue
        try:
            check_original(item, job)
        except ReplaceError as exc:
            fail(job, str(exc))
            continue
        blocked = refusal(settings, item)
        if blocked:
            outcomes[job.key] = Outcome("refused", blocked, job.media_key)
            live.pop(job.key)
            continue
        originals[job.key] = item

    # 5: the replacements, by the content hash of what was uploaded.
    progress(f"finding {len(live)} replacement(s)")
    matches = library.find_uploaded_many([job.output for job in live.values()])
    replacement_keys: dict[str, str] = {}
    for job in list(live.values()):
        match = matches.get(job.output)
        if match is None:
            fail(job, "replacement not found by content hash; not guessing")
        elif isinstance(match, Exception):
            fail(job, f"replacement lookup failed: {match}")
        else:
            replacement_keys[job.key] = match["id"]

    def read_replacements(keys: list[str]) -> dict[str, dict[str, Any]]:
        found = library.get_items([replacement_keys[key] for key in keys])
        result = {}
        for key in keys:
            item = found.get(replacement_keys[key])
            if item is None or isinstance(item, Exception):
                fail(live[key], f"replacement could not be read: {item or 'no response'}")
                continue
            result[key] = item
        return result

    replacements = read_replacements(list(live))
    fixes: dict[str, dict[str, Any]] = {}
    for key, replacement in replacements.items():
        job, original = live[key], originals[key]
        try:
            check_identity(original, replacement)
        except ReplaceError as exc:
            fail(job, str(exc))
            continue
        if unexpected_shared_album(original, replacement):
            fail(job, "replacement is in a shared album the original is not in")
            continue
        needed = needed_fixes(original, replacement)
        if needed:
            fixes[key] = needed

    if not apply:
        for key, job in live.items():
            detail = f"would fix {describe(fixes[key])}, then trash" if key in fixes else "ready to trash"
            outcomes[key] = Outcome("would_replace", detail, job.media_key)
        return outcomes

    # 6: fix what differs, then re-read every fixed replacement -- the re-read,
    # not the fix call's response, is what decides.
    if fixes:
        progress(f"fixing {len(fixes)} replacement(s)")
        try:
            failures = library.restore_many(
                [{"replacement": replacements[key], **needed} for key, needed in fixes.items() if key in live]
            )
        except Exception as exc:  # noqa: BLE001 - fails only the items that needed fixing
            # A rejected fix request must not stop replacements that already
            # match: fail the ones it was fixing and carry on with the rest.
            for key in fixes:
                if key in live:
                    fail(live[key], f"fixing {describe(fixes[key])} failed: {type(exc).__name__}: {exc}")
            failures = {}
        by_replacement = {replacement_keys[key]: key for key in fixes}
        for replacement_id, error in failures.items():
            key = by_replacement.get(replacement_id)
            if key in live:
                fail(live[key], f"fixing {describe(fixes[key])} failed: {error}")
        for key, replacement in read_replacements([key for key in fixes if key in live]).items():
            still = needed_fixes(originals[key], replacement)
            if still:
                fail(live[key], f"replacement still differs after fixing: {describe(still)}")
            elif unexpected_shared_album(originals[key], replacement):
                fail(live[key], "replacement is in a shared album the original is not in")

    if keep_originals:
        for key, job in live.items():
            outcomes[key] = Outcome("verified_original_kept", "verified, original kept", job.media_key)
        return outcomes

    # 7: trash every original still standing in one call, then confirm each in
    # the bin. Two jobs can name one library item under different media keys;
    # it shares one dedup key, so it is trashed once and confirms both.
    if live:
        progress(f"trashing {len(live)} original(s)")
        try:
            library.trash_many(list(dict.fromkeys(originals[key]["dedup_key"] for key in live)))
        except Exception as exc:  # noqa: BLE001 - recorded against every job in the call
            for job in list(live.values()):
                fail(job, f"trash failed: {type(exc).__name__}: {exc}")
    if live:
        binned = library.in_bin([originals[key]["dedup_key"] for key in live])
        for key, job in list(live.items()):
            if originals[key]["dedup_key"] in binned:
                outcomes[key] = Outcome("replaced", "replaced, original trashed", job.media_key)
            else:
                fail(job, "trash was not confirmed by the server")
    return outcomes
