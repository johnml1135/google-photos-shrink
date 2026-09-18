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
from pathlib import Path
from typing import Any

from .config import Settings
from .integrity import sha256_file
from .ledger import UploadRecord
from .policy import Candidate, verdict
from .remote import RemoteProtocolError
from .takeout import EDIT_SUFFIX

# What a dead session looks like from inside a batch: enough reads to judge by,
# and most of them failing. A batch is 100 by default, so this trips on the
# first rotten batch without firing on a handful of missing originals.
DEAD_SESSION_READS = 10
DEAD_SESSION_SHARE = 0.8


class ReplaceError(RuntimeError):
    """Raised when a replacement cannot be completed safely."""


@dataclass(frozen=True)
class Outcome:
    """What happened, or would happen, to one job."""

    status: str  # "replaced" | "would_replace" | "verified_original_kept" | "refused" | "failed"
    detail: str
    original_media_key: str | None = None


def _same_name(library_name: Any, exported: Path) -> bool:
    """Whether two filenames name the same photo, ignoring Takeout's edit suffix.

    Takeout exports an edited photo as ``NAME-edited.jpg`` beside the untouched
    ``NAME.jpg``; Google keeps calling the item ``NAME.jpg``.
    """

    if not isinstance(library_name, str) or not library_name:
        return False
    stem, suffix = exported.stem, exported.suffix
    if stem.endswith(EDIT_SUFFIX):
        stem = stem[: -len(EDIT_SUFFIX)]
    return library_name.casefold() == f"{stem}{suffix}".casefold()


def check_original(item: dict[str, Any], job: UploadRecord, sizes: set[int] | None = None) -> None:
    """Accept the sidecar's media key only when the item it names matches the export.

    The exported file's own size is the plain case. `sizes` holds every
    exported copy's size for this media key, which matters for an edited photo:
    Takeout exports the edit and the untouched original under one media key,
    and Google reports the size of the untouched one. Accepting a copy other
    than the file we encoded costs nothing extra -- the sizes come from the
    mirror, not another request -- but it must also be the same filename, so a
    sidecar paired with an unrelated file still cannot pass.

    Google's search by content hash is deliberately not used for originals. On
    the live library it missed items whose bytes it held, and for an exact
    duplicate it returned the other copy.
    """

    if str(item.get("id")) != str(job.media_key):
        raise ReplaceError(f"library returned {item.get('id')} for the sidecar's {job.media_key}")
    exported = job.source.stat().st_size
    reported = item.get("size_bytes")
    if reported == exported:
        return
    if reported in (sizes or set()) and _same_name(item.get("filename"), job.source):
        return
    raise ReplaceError(
        f"library item is {reported} bytes but the exported original is "
        f"{exported}; the sidecar may describe a different file"
    )


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
    """Describe metadata fixes briefly."""

    parts = []
    if "timestamp" in fixes:
        parts.append("capture time")
    if fixes.get("albums"):
        parts.append(f"{len(fixes['albums'])} album(s)")
    parts += [key for key in ("favorite", "archived", "description") if key in fixes]
    return ", ".join(parts)


class _Batch:
    """One batch's jobs and outcomes: the steps replacing and removing share.

    `live` holds the jobs still standing; `fail` records a failure against its
    own job and drops it, so no later step in the batch touches it.
    """

    def __init__(self, library: Any, jobs: list[UploadRecord], settings: Settings,
                 progress: Callable[[str], None], sizes: dict[str, set[int]] | None = None):
        self.library, self.settings, self.progress = library, settings, progress
        self.sizes = sizes or {}
        self.jobs = {job.key: job for job in jobs}
        self.outcomes: dict[str, Outcome] = {}
        self.live: dict[str, UploadRecord] = {}
        self.originals: dict[str, dict[str, Any]] = {}
        self.refused: dict[str, str] = {}
        self.matches: dict[str, dict[str, str]] = {}
        self._check_disk(jobs)

    def fail(self, job: UploadRecord, detail: str) -> None:
        self.outcomes[job.key] = Outcome("failed", detail, job.media_key)
        self.live.pop(job.key, None)

    def _check_disk(self, jobs: list[UploadRecord]) -> None:
        """Everything that can be settled on disk, before any request."""

        for job in jobs:
            if not job.media_key:
                self.fail(job, "no media key; the original cannot be identified")
            elif job.source is None or not job.source.is_file():
                self.fail(job, f"source original is missing from the export: {job.source}")
            elif job.output is None or not job.output.is_file():
                self.fail(job, "the encoded output is missing")
            elif job.output_sha256 and sha256_file(job.output) != job.output_sha256:
                self.fail(job, "the encoded file on disk no longer matches what was uploaded")
            else:
                self.live[job.key] = job

    def read_originals(self) -> None:
        """Read originals by the sidecar's media key, and set the refused ones aside.

        A refused job leaves `live` for `refused`, with its original kept in
        `originals`: replacing never touches it, and removing acts only on it.
        """

        asked = len(self.live)
        self.progress(f"reading {asked} original(s)")
        found = self.library.get_items([job.media_key for job in self.live.values()])
        for job in list(self.live.values()):
            item = found.get(job.media_key)
            if item is None or isinstance(item, Exception):
                self.fail(job, f"original could not be read: {item or 'no response'}")
                continue
            try:
                check_original(item, job, self.sizes.get(job.media_key or ""))
            except ReplaceError as exc:
                self.fail(job, str(exc))
                continue
            self.originals[job.key] = item
            blocked = verdict(self.settings, Candidate.from_library_item(item))
            if blocked:
                self.refused[job.key] = blocked
                self.live.pop(job.key)

        # Reads failing wholesale is the session, not the photos. Live, an
        # expired cookie answered 2,803 reads in a row with the same error
        # while the run charged on through them; a later run rotted more
        # slowly, losing 70-80% of each batch, which a test for "every read
        # failed" never catches. Each lost read spends an item that has to be
        # tried again, so the run stops and asks for a fresh cookie instead.
        read = len(self.live) + len(self.refused)
        if asked >= DEAD_SESSION_READS and read <= asked * (1 - DEAD_SESSION_SHARE):
            raise RemoteProtocolError(
                f"only {read} of {asked} originals in this batch could be read; the session is gone"
            )

    def find_replacements(self) -> None:
        """Resolve each live job's replacement by the content hash of what was uploaded."""

        self.progress(f"finding {len(self.live)} replacement(s)")
        found = self.library.find_uploaded_many([job.output for job in self.live.values()])
        for key, job in list(self.live.items()):
            match = found.get(job.output)
            if match is None:
                self.fail(job, "replacement not found by content hash; not guessing")
            elif isinstance(match, Exception):
                self.fail(job, f"replacement lookup failed: {match}")
            else:
                try:
                    check_identity(self.originals[key], match)
                except ReplaceError as exc:
                    self.fail(job, str(exc))
                else:
                    self.matches[key] = match

    def trash_and_confirm(self, dedup_of: Callable[[str], str], status: str, detail: Callable[[str], str]) -> None:
        """Trash one item per live job in one call, then confirm each in the bin.

        Two jobs can name one library item under different media keys; it
        shares one dedup key, so it is trashed once and confirms both.
        """

        if not self.live:
            return
        self.progress(f"trashing {len(self.live)} item(s)")
        try:
            self.library.trash_many(list(dict.fromkeys(dedup_of(key) for key in self.live)))
        except Exception as exc:  # noqa: BLE001 - recorded against every job in the call
            for job in list(self.live.values()):
                self.fail(job, f"trash failed: {type(exc).__name__}: {exc}")
            return
        binned = self.library.in_bin([dedup_of(key) for key in self.live])
        for key, job in list(self.live.items()):
            if dedup_of(key) in binned:
                self.outcomes[key] = Outcome(status, detail(key), job.media_key)
            else:
                self.fail(job, "trash was not confirmed by the server")


def replace_batch(
    library: Any,
    jobs: list[UploadRecord],
    *,
    settings: Settings,
    apply: bool,
    keep_originals: bool,
    progress: Callable[[str], None] = lambda message: None,
    sizes: dict[str, set[int]] | None = None,
) -> dict[str, Outcome]:
    """Replace a batch of jobs; return each job's outcome, keyed by `UploadRecord.key`.

    `library` is any adapter offering get_items, find_uploaded_many,
    restore_many, trash_many and in_bin -- the cookie session in production, a
    fake in tests. A failure is recorded against its own job and never stops the rest.
    """

    batch = _Batch(library, jobs, settings, progress, sizes)
    live, fail = batch.live, batch.fail

    # 3-4: the originals.
    batch.read_originals()
    for key, token in batch.refused.items():
        batch.outcomes[key] = Outcome("refused", token, batch.jobs[key].media_key)

    # 5: the replacements.
    batch.find_replacements()

    def read_replacements(keys: list[str]) -> dict[str, dict[str, Any]]:
        found = library.get_items([batch.matches[key]["id"] for key in keys])
        result = {}
        for key in keys:
            item = found.get(batch.matches[key]["id"])
            if item is None or isinstance(item, Exception):
                fail(live[key], f"replacement could not be read: {item or 'no response'}")
                continue
            result[key] = item
        return result

    replacements = read_replacements(list(live))
    fixes: dict[str, dict[str, Any]] = {}
    for key, replacement in replacements.items():
        original = batch.originals[key]
        try:
            check_identity(original, replacement)
        except ReplaceError as exc:
            fail(live[key], str(exc))
            continue
        if unexpected_shared_album(original, replacement):
            fail(live[key], "replacement is in a shared album the original is not in")
            continue
        needed = needed_fixes(original, replacement)
        if needed:
            fixes[key] = needed

    if not apply:
        for key, job in live.items():
            detail = f"would fix {describe(fixes[key])}, then trash" if key in fixes else "ready to trash"
            batch.outcomes[key] = Outcome("would_replace", detail, job.media_key)
        return batch.outcomes

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
        by_replacement = {batch.matches[key]["id"]: key for key in fixes}
        for replacement_id, error in failures.items():
            key = by_replacement.get(replacement_id)
            if key in live:
                fail(live[key], f"fixing {describe(fixes[key])} failed: {error}")
        for key, replacement in read_replacements([key for key in fixes if key in live]).items():
            still = needed_fixes(batch.originals[key], replacement)
            if still:
                fail(live[key], f"replacement still differs after fixing: {describe(still)}")
            elif unexpected_shared_album(batch.originals[key], replacement):
                fail(live[key], "replacement is in a shared album the original is not in")

    if keep_originals:
        for key, job in live.items():
            batch.outcomes[key] = Outcome("verified_original_kept", "verified, original kept", job.media_key)
        return batch.outcomes

    # 7: trash every original still standing, confirmed in the bin.
    batch.trash_and_confirm(
        lambda key: batch.originals[key]["dedup_key"], "replaced", lambda key: "replaced, original trashed"
    )
    return batch.outcomes


def remove_extra_copies(
    library: Any,
    jobs: list[UploadRecord],
    *,
    settings: Settings,
    apply: bool,
    progress: Callable[[str], None] = lambda message: None,
    sizes: dict[str, set[int]] | None = None,
) -> dict[str, Outcome]:
    """Trash the replacements whose originals are refused; return each job's outcome.

    A refused original stays in the library, so its uploaded replacement is
    only an extra copy on this account's storage. Each job's original is read
    and put to the same gate as replacing. Only for a refused one is the
    replacement -- resolved by the content hash of the file this tool uploaded,
    and distinct from the original -- trashed and confirmed in the bin. No
    original is ever trashed here.

    Statuses: "copy_removed" and "would_remove_copy" (detail: the refusal),
    "kept" (the original is not refused), "failed".
    """

    batch = _Batch(library, jobs, settings, progress, sizes)
    batch.read_originals()
    for key, job in batch.live.items():
        batch.outcomes[key] = Outcome("kept", "the original is not refused", job.media_key)
    # From here on the jobs in play are the refused ones.
    batch.live.clear()
    batch.live.update({key: batch.jobs[key] for key in batch.refused})
    batch.find_replacements()

    if not apply:
        for key, job in batch.live.items():
            batch.outcomes[key] = Outcome("would_remove_copy", batch.refused[key], job.media_key)
        return batch.outcomes

    batch.trash_and_confirm(
        lambda key: batch.matches[key]["dedup_key"], "copy_removed", lambda key: batch.refused[key]
    )
    return batch.outcomes
