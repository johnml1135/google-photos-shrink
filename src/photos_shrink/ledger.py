"""Schema and I/O for the Takeout upload journal.

`photos-shrink upload` records one entry per uploaded file, keyed by its
output path, so a later pass can verify and then replace the corresponding
original. Until now that schema existed only as string literals at the write
site (`takeout_upload.py`) and was read back by hand in at least one other
tool (`takeout_replace.py`). This module is the one place the shape of a
journal entry is defined.

The on-disk journal is a real, already-populated file that a live run is
appending to and that a separate pass is reading to decide what to trash.
Its shape must not change: every field this module does not model by name
is preserved verbatim in `UploadRecord.extra` and written back untouched, and
every field it does model by name is written back only when the tool that
owns it would have written it -- an entry the replace pass has not yet
touched keeps the exact shape the uploader gave it, with no new keys.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# What `replaced` may hold, and which of those values settle a record. A
# settled record has nothing left to trash: the original is gone, by this
# tool's hand or someone else's. Every other value leaves the original in
# the library -- `refused: <token>` because a gate said to keep it,
# `duplicate: ...` because another record replaced the same library item --
# so the copy this tool uploaded is an extra copy until `copy_removed_at`
# says it was cleared.
REPLACED = "replaced"
GONE = "gone"
REFUSED = "refused"
SETTLED = (REPLACED, GONE)

# Outcomes that describe a pass which changed nothing: a dry run, or a check
# that deliberately kept the original.
UNCHANGED = frozenset({"would_replace", "would_remove_copy", "verified_original_kept", "kept"})


def settled(replaced: str | None) -> bool:
    """Whether a `replaced` value means there is nothing left to trash."""

    if not replaced:
        return False
    return replaced.split(":", 1)[0] in SETTLED

# Written together, unconditionally, every time the uploader records a new
# entry (see photos_shrink/steps/upload.py). Always present in a fresh record,
# even when the value is None (e.g. mime_type), matching the journal's
# existing shape.
_CORE_FIELDS = (
    "source",
    "media_key",
    "output",
    "output_sha256",
    "media_item_id",
    "filename",
    "mime_type",
    "verified",
    "verified_detail",
    "capture_time",
    "uploaded_at",
)

# Written later, by the replacement pass. Included in the saved journal only
# once actually set, so an entry the replace pass has not reached yet keeps
# the exact shape the uploader wrote -- no "replaced": null appears early.
_OPTIONAL_FIELDS = ("replaced", "replaced_at", "original_media_key", "replace_error", "copy_removed_at")

_PATH_FIELDS = ("source", "output")

# `from_dict` and `to_dict` both drive off the tuples above, so adding a
# journal field means adding it to the dataclass and to one tuple -- never
# to a third hand-written constructor call that can silently fall behind.


def _as_path(value: object) -> Path | None:
    return Path(value) if value else None


def _as_str(value: object) -> str | None:
    return None if value is None else str(value)


@dataclass
class UploadRecord:
    """One entry in the upload journal.

    Every field either tool is known to write is modeled by name. Anything
    else -- a key from an older or newer version of either tool -- lands in
    `extra` and is written back exactly as read, so this module never has to
    know the journal's whole history to be safe to round-trip.
    """

    source: Path | None = None
    output: Path | None = None
    media_key: str | None = None
    output_sha256: str | None = None
    media_item_id: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    verified: str | None = None  # "ok" | "mismatch" | "unverified"
    verified_detail: str | None = None
    capture_time: str | None = None
    uploaded_at: str | None = None
    replaced: str | None = None
    replaced_at: str | None = None
    original_media_key: str | None = None
    replace_error: str | None = None
    # When this upload was trashed as an extra copy of an original that is
    # refused, and so stays in the library.
    copy_removed_at: str | None = None
    extra: dict = field(default_factory=dict)
    # The key order this entry had on disk. Older versions of the uploader
    # wrote the same keys in a different order, and re-emitting them
    # canonically would rewrite a file whose contents did not change.
    key_order: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, key: str, data: dict) -> UploadRecord:
        """Build a record from one raw journal entry.

        `key` is the entry's top-level key (its output path, as written by
        the uploader) and is used as a fallback when the entry itself has no
        "output" value, so a hand-edited or older entry still loads.
        """

        known = {f.name for f in fields(cls) if f.name != "extra"}
        values: dict[str, object] = {}
        for name in _CORE_FIELDS + _OPTIONAL_FIELDS:
            raw = data.get(name, key) if name == "output" else data.get(name)
            values[name] = _as_path(raw) if name in _PATH_FIELDS else raw
        values["extra"] = {k: v for k, v in data.items() if k not in known}
        values["key_order"] = tuple(data)
        return cls(**values)

    def to_dict(self) -> dict:
        """Render this record back to the raw shape the journal stores."""

        rendered: dict[str, object] = {}
        for name in _CORE_FIELDS:
            value = getattr(self, name)
            rendered[name] = _as_str(value) if name in _PATH_FIELDS else value
        for name in _OPTIONAL_FIELDS:
            value = getattr(self, name)
            if value is not None:
                rendered[name] = value
        rendered.update(self.extra)

        # Emit in the order this entry arrived in, so loading and saving a
        # journal an older version wrote leaves the bytes untouched. Keys set
        # since it was read follow, in the canonical order above.
        result: dict[str, object] = {}
        for name in self.key_order:
            if name in rendered:
                result[name] = rendered[name]
        for name, value in rendered.items():
            if name not in result:
                result[name] = value
        return result

    @property
    def key(self) -> str:
        """The journal's top-level key for this record."""

        return _as_str(self.output) or ""


class UploadJournal:
    """The upload journal: one `UploadRecord` per output path, on disk as JSON.

    Backed by a flat JSON object today (unchanged from `takeout_upload.py`'s
    original schema); the load/save boundary here is what lets that backing
    store be swapped for something else later without touching call sites.
    """

    def __init__(self, path: Path, records: dict[str, UploadRecord] | None = None):
        self.path = Path(path)
        self._records: dict[str, UploadRecord] = dict(records) if records else {}

    @classmethod
    def load(cls, path: Path) -> UploadJournal:
        path = Path(path)
        raw: dict[str, dict] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        records = {key: UploadRecord.from_dict(key, entry) for key, entry in raw.items()}
        return cls(path, records)

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, key: object) -> bool:
        return str(key) in self._records

    def all_records(self) -> list[UploadRecord]:
        """Every record in the journal, in no particular order."""

        return list(self._records.values())

    def record(self, record: UploadRecord) -> None:
        """Add or replace one entry, then persist the whole journal atomically."""

        self._records[record.key] = record
        self.save()

    def save(self) -> None:
        """Write the current state atomically: temp file, then replace.

        The journal used to be rewritten with a direct `write_text` after
        every upload, so a process killed mid-write could truncate it. This
        writes to a temp file next to it and only then swaps it into place.
        """

        data = {key: rec.to_dict() for key, rec in self._records.items()}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def already_uploaded_keys(self) -> set[str]:
        """Media keys that already have an uploaded replacement.

        An encode can be renamed or redone, but a second upload of the same
        photo is a duplicate forever, so this is keyed on library identity
        rather than the output path.
        """

        return {r.media_key for r in self._records.values() if r.media_key}

    def pending_replacement(self) -> list[UploadRecord]:
        """Verified uploads whose original has not been replaced yet."""

        return [
            r for r in self._records.values()
            if r.verified == "ok" and not r.replaced and not r.copy_removed_at
        ]

    def copy_removal_candidates(self) -> list[UploadRecord]:
        """Uploads whose original still stands and whose copy is still there.

        Pending and refused alike: the live gate decides which are extra
        copies. A settled record is not offered again -- it was testing
        `replaced != "replaced"`, so the 705 records closed as gone in one
        run came back as candidates on every run after it.
        """

        return [
            r for r in self._records.values()
            if not settled(r.replaced) and not r.copy_removed_at
        ]

    def apply(self, record: UploadRecord, outcome: Any, *, at: str | None = None) -> None:
        """Write one outcome from the replace or remove-copies pass onto a record.

        The only way a record changes stage. The mapping lived in the step
        modules, which is where the `refused: ` and `gone: ` conventions were
        minted -- read back here by string comparison, and silently wrong the
        first time a status was added without the step being taught about it.

        `outcome` is anything carrying `status`, `detail` and
        `original_media_key`; taking it by shape keeps this module free of a
        dependency on the pass that produces it.
        """

        stamp = at or time.strftime("%Y-%m-%dT%H:%M:%S")
        status, detail = outcome.status, outcome.detail
        if status in UNCHANGED:
            return
        if status == "failed":
            record.replace_error = detail
            return
        if status == "copy_gone":
            # Nothing was trashed now: the copy had already gone, so only the
            # fact that there is none left to remove is recorded.
            record.copy_removed_at = stamp
            return
        if status == "copy_removed":
            record.replaced = f"{REFUSED}: {detail}"
            record.copy_removed_at = stamp
            return
        if status in (REFUSED, GONE):
            record.replaced = f"{status}: {detail}"
        elif status == REPLACED:
            record.replaced = status
            record.original_media_key = outcome.original_media_key
        else:
            # Better a visible open record than a silent misfiling: a status
            # this module has not been taught is the code disagreeing with
            # itself, and `test_ledger.py` fails on it before a run can.
            record.replace_error = f"unknown outcome: {status}"
            return
        record.replaced_at = stamp
        record.replace_error = None
