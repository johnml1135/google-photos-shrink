"""Tests for the only Takeout code that destroys anything.

`replace_one` holds ten refusal points in a fixed order (see
`photos_shrink.replacement`'s module docstring). Testing only the pure
helper functions -- confirm_original, check_identity, output_info_for -- would
prove each check works in isolation but say nothing about the order they run
in. The tests below use a fake library adapter that records every call it
receives, so each test can assert not just "this raised" but "the mutating
calls after it were never reached".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from photos_shrink import replacement as replacement_module
from photos_shrink.config import load_config
from photos_shrink.ledger import UploadRecord
from photos_shrink.replacement import (
    ReplaceError,
    check_identity,
    confirm_original,
    output_info_for,
    replace_one,
)

MUTATING_CALLS = {"restore_metadata", "verify_replacement", "trash", "is_trashed"}


@dataclass
class FakeLibrary:
    """Records every call so tests can assert on order, not just outcome.

    `by_hash` maps a file's basename to the library item `find_uploaded`
    should resolve it to. `items` maps a media key to what `get_item` returns
    for it. `verify_error` and `trash_confirms` let a test make one specific
    step fail without touching the others.
    """

    by_hash: dict[str, dict[str, Any]] = field(default_factory=dict)
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    verify_error: Exception | None = None
    trash_confirms: bool = True
    calls: list[tuple] = field(default_factory=list)

    def find_uploaded(self, path):
        result = self.by_hash.get(Path(path).name)
        self.calls.append(("find_uploaded", Path(path).name, result))
        return result

    def get_item(self, media_key):
        result = self.items.get(media_key, {})
        self.calls.append(("get_item", media_key, result))
        return result

    def restore_metadata(self, original, replacement):
        self.calls.append(("restore_metadata", original.get("id"), replacement.get("id")))

    def verify_replacement(self, original, replacement, info):
        self.calls.append(("verify_replacement", original.get("id"), replacement.get("id")))
        if self.verify_error is not None:
            raise self.verify_error

    def trash(self, item):
        self.calls.append(("trash", item.get("id")))

    def is_trashed(self, item):
        self.calls.append(("is_trashed", item.get("id")))
        return self.trash_confirms

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def settings_for(tmp_path: Path, **run):
    config = tmp_path / "shrink.toml"
    body = "[run]\nwork_dir = 'work'\npause_seconds = 0\n"
    for key, value in run.items():
        body += f"{key} = {str(value).lower() if isinstance(value, bool) else value}\n"
    config.write_text(body, encoding="utf-8")
    return load_config(config)


def make_item(item_id: str, **overrides) -> dict[str, Any]:
    item = {
        "id": item_id,
        "dedup_key": f"dedup-{item_id}",
        "filename": "IMG_1.jpg",
        "kind": "photo",
        "timestamp_ms": 1_700_000_000_000,
        "space_taken_bytes": 12345,
        "metadata": {"albums": []},
    }
    item.update(overrides)
    return item


def make_job(tmp_path: Path, *, media_key="ORIG1", output_sha256=None) -> UploadRecord:
    source = tmp_path / "IMG_1.jpg"
    source.write_bytes(b"original bytes")
    output = tmp_path / "out.avif"
    output.write_bytes(b"encoded bytes")
    return UploadRecord(
        source=source,
        output=output,
        media_key=media_key,
        output_sha256=output_sha256,
    )


def patch_probe(monkeypatch):
    monkeypatch.setattr(replacement_module.media, "probe", lambda *a, **k: {"width": 10, "height": 8})


# --- Unit tests for the pure helpers -----------------------------------------


class TestConfirmOriginal:
    def test_accepts_when_the_hash_resolves_to_the_claimed_key(self, tmp_path):
        library = FakeLibrary(by_hash={"IMG_1.jpg": {"id": "KEY1"}})
        source = tmp_path / "IMG_1.jpg"
        source.write_bytes(b"original bytes")
        assert confirm_original(library, source, "KEY1")["id"] == "KEY1"

    def test_refuses_when_the_hash_resolves_to_a_different_item(self, tmp_path):
        """A sidecar media key is a claim; the content hash is the proof."""

        library = FakeLibrary(by_hash={"IMG_1.jpg": {"id": "SOMEONE_ELSE"}})
        source = tmp_path / "IMG_1.jpg"
        source.write_bytes(b"original bytes")
        with pytest.raises(ReplaceError, match="sidecar claims"):
            confirm_original(library, source, "KEY1")

    def test_refuses_when_the_bytes_resolve_to_nothing(self, tmp_path):
        source = tmp_path / "IMG_1.jpg"
        source.write_bytes(b"original bytes")
        with pytest.raises(ReplaceError, match="do not resolve"):
            confirm_original(FakeLibrary(), source, "KEY1")

    def test_refuses_when_the_exported_original_is_missing(self, tmp_path):
        with pytest.raises(ReplaceError, match="missing from the export"):
            confirm_original(FakeLibrary(), tmp_path / "gone.jpg", "KEY1")


class TestCheckIdentity:
    def test_refuses_an_identical_id(self):
        with pytest.raises(ReplaceError, match="not distinct"):
            check_identity({"id": "A"}, {"id": "A"})

    def test_refuses_a_shared_dedup_key(self):
        with pytest.raises(ReplaceError, match="deduplication identity"):
            check_identity({"id": "A", "dedup_key": "D"}, {"id": "B", "dedup_key": "D"})

    def test_refuses_an_empty_replacement(self):
        with pytest.raises(ReplaceError):
            check_identity({"id": "A"}, {})

    def test_accepts_a_distinct_replacement(self):
        check_identity({"id": "A", "dedup_key": "D1"}, {"id": "B", "dedup_key": "D2"})

class TestOutputInfo:
    def test_supplies_the_hash_and_path_verification_requires(self, tmp_path, monkeypatch):
        from photos_shrink.integrity import sha256_file

        patch_probe(monkeypatch)
        output = tmp_path / "out.avif"
        output.write_bytes(b"encoded bytes")
        record = UploadRecord()
        info = output_info_for(record, output, "ffprobe")
        assert info["path"] == str(output)
        assert info["sha256"] == sha256_file(output)
        assert info["size_bytes"] == output.stat().st_size

    def test_refuses_when_the_encoded_file_changed_since_upload(self, tmp_path, monkeypatch):
        monkeypatch.setattr(replacement_module.media, "probe", lambda *a, **k: {})
        output = tmp_path / "out.avif"
        output.write_bytes(b"different bytes now")
        record = UploadRecord(output_sha256="a" * 64)
        with pytest.raises(ReplaceError, match="no longer matches"):
            output_info_for(record, output, "ffprobe")


# --- Ordering tests: the point of the whole refactor -------------------------


class TestReplaceOneOrder:
    def test_no_media_key_touches_the_library_at_all(self, tmp_path):
        settings = settings_for(tmp_path)
        job = make_job(tmp_path, media_key=None)
        library = FakeLibrary()
        with pytest.raises(ReplaceError, match="no media key"):
            replace_one(library, job, settings=settings, ffprobe="ffprobe", apply=True, keep_originals=False)
        assert library.calls == []

    def test_confirm_original_mismatch_stops_before_get_item(self, tmp_path):
        settings = settings_for(tmp_path)
        job = make_job(tmp_path, media_key="CLAIMED")
        library = FakeLibrary(by_hash={"IMG_1.jpg": {"id": "SOMEONE_ELSE"}})
        with pytest.raises(ReplaceError, match="sidecar claims"):
            replace_one(library, job, settings=settings, ffprobe="ffprobe", apply=True, keep_originals=False)
        assert library.names() == ["find_uploaded"]

    def test_get_item_reconciliation_refuses_a_mismatched_full_item(self, tmp_path):
        """confirm_original's proof must not be silently overwritten by get_item."""

        settings = settings_for(tmp_path)
        job = make_job(tmp_path, media_key="KEY1")
        library = FakeLibrary(
            by_hash={"IMG_1.jpg": {"id": "KEY1"}},
            items={"KEY1": make_item("SOMETHING_ELSE")},
        )
        with pytest.raises(ReplaceError, match="content hash proved"):
            replace_one(library, job, settings=settings, ffprobe="ffprobe", apply=True, keep_originals=False)
        assert "restore_metadata" not in library.names()
        assert "trash" not in library.names()

    def test_gate_refusal_never_reaches_restore_metadata(self, tmp_path):
        """A refused item must not even have its replacement looked up."""

        settings = settings_for(tmp_path, skip_shared=True)
        job = make_job(tmp_path, media_key="KEY1")
        original = make_item("KEY1", metadata={"albums": [{"id": "a", "shared": True}]})
        library = FakeLibrary(by_hash={"IMG_1.jpg": {"id": "KEY1"}}, items={"KEY1": original})
        outcome = replace_one(library, job, settings=settings, ffprobe="ffprobe", apply=True, keep_originals=False)
        assert outcome.status == "refused"
        assert outcome.detail == "shared_album"
        # Only the source hash was ever looked up -- the gate refused before
        # the replacement's own content hash was ever resolved.
        assert library.names() == ["find_uploaded", "get_item"]
        assert "restore_metadata" not in library.names()
        assert "trash" not in library.names()

    def test_replacement_not_found_leaves_restore_metadata_uncalled(self, tmp_path):
        settings = settings_for(tmp_path)
        job = make_job(tmp_path, media_key="KEY1")
        original = make_item("KEY1")
        library = FakeLibrary(by_hash={"IMG_1.jpg": original}, items={"KEY1": original})
        # find_uploaded(output) resolves to nothing because "out.avif" is not in by_hash.
        with pytest.raises(ReplaceError, match="not found by content hash"):
            replace_one(library, job, settings=settings, ffprobe="ffprobe", apply=True, keep_originals=False)
        assert "restore_metadata" not in library.names()
        assert "trash" not in library.names()

    def test_identical_replacement_id_never_reaches_restore_metadata(self, tmp_path):
        settings = settings_for(tmp_path)
        job = make_job(tmp_path, media_key="KEY1")
        original = make_item("KEY1")
        library = FakeLibrary(
            by_hash={"IMG_1.jpg": original, "out.avif": original},
            items={"KEY1": original},
        )
        with pytest.raises(ReplaceError, match="not distinct"):
            replace_one(library, job, settings=settings, ffprobe="ffprobe", apply=True, keep_originals=False)
        assert "restore_metadata" not in library.names()
        assert "trash" not in library.names()

    def test_dry_run_calls_nothing_mutating(self, tmp_path):
        """apply=False must not restore metadata, verify, or trash anything."""

        settings = settings_for(tmp_path)
        job = make_job(tmp_path, media_key="KEY1")
        original = make_item("KEY1")
        replacement = make_item("REPL1")
        library = FakeLibrary(
            by_hash={"IMG_1.jpg": original, "out.avif": replacement},
            items={"KEY1": original},
        )
        outcome = replace_one(library, job, settings=settings, ffprobe="ffprobe", apply=False, keep_originals=False)
        assert outcome.status == "would_replace"
        assert not (set(library.names()) & MUTATING_CALLS)
        # Reads still happen -- confirm_original, get_item, and the
        # replacement's own hash lookup are all needed to report accurately.
        assert library.names() == ["find_uploaded", "get_item", "find_uploaded"]

    def test_verify_failure_prevents_trash(self, tmp_path, monkeypatch):
        """trash must never run once verification has raised."""

        patch_probe(monkeypatch)
        settings = settings_for(tmp_path)
        job = make_job(tmp_path, media_key="KEY1")
        original = make_item("KEY1")
        replacement = make_item("REPL1")
        library = FakeLibrary(
            by_hash={"IMG_1.jpg": original, "out.avif": replacement},
            items={"KEY1": original},
            verify_error=ReplaceError("replacement verification failed"),
        )
        with pytest.raises(ReplaceError, match="verification failed"):
            replace_one(library, job, settings=settings, ffprobe="ffprobe", apply=True, keep_originals=False)
        assert library.names() == [
            "find_uploaded",
            "get_item",
            "find_uploaded",
            "restore_metadata",
            "verify_replacement",
        ]
        assert "trash" not in library.names()
        assert "is_trashed" not in library.names()

    def test_unconfirmed_trash_raises_and_is_still_the_last_call(self, tmp_path, monkeypatch):
        patch_probe(monkeypatch)
        settings = settings_for(tmp_path)
        job = make_job(tmp_path, media_key="KEY1")
        original = make_item("KEY1")
        replacement = make_item("REPL1")
        library = FakeLibrary(
            by_hash={"IMG_1.jpg": original, "out.avif": replacement},
            items={"KEY1": original},
            trash_confirms=False,
        )
        with pytest.raises(ReplaceError, match="not confirmed"):
            replace_one(library, job, settings=settings, ffprobe="ffprobe", apply=True, keep_originals=False)
        assert library.names()[-2:] == ["trash", "is_trashed"]

    def test_full_success_runs_every_step_in_order(self, tmp_path, monkeypatch):
        patch_probe(monkeypatch)
        settings = settings_for(tmp_path)
        job = make_job(tmp_path, media_key="KEY1")
        original = make_item("KEY1")
        replacement = make_item("REPL1")
        library = FakeLibrary(
            by_hash={"IMG_1.jpg": original, "out.avif": replacement},
            items={"KEY1": original},
        )
        outcome = replace_one(library, job, settings=settings, ffprobe="ffprobe", apply=True, keep_originals=False)
        assert outcome.status == "replaced"
        assert outcome.original_media_key == "KEY1"
        assert library.names() == [
            "find_uploaded",
            "get_item",
            "find_uploaded",
            "restore_metadata",
            "verify_replacement",
            "trash",
            "is_trashed",
        ]

    def test_keep_originals_restores_and_verifies_but_never_trashes(self, tmp_path, monkeypatch):
        patch_probe(monkeypatch)
        settings = settings_for(tmp_path)
        job = make_job(tmp_path, media_key="KEY1")
        original = make_item("KEY1")
        replacement = make_item("REPL1")
        library = FakeLibrary(
            by_hash={"IMG_1.jpg": original, "out.avif": replacement},
            items={"KEY1": original},
        )
        outcome = replace_one(library, job, settings=settings, ffprobe="ffprobe", apply=True, keep_originals=True)
        assert outcome.status == "verified_original_kept"
        assert "restore_metadata" in library.names()
        assert "verify_replacement" in library.names()
        assert "trash" not in library.names()
        assert "is_trashed" not in library.names()
