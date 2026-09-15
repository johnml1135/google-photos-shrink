"""Tests for the Takeout upload journal's schema and I/O."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from photos_shrink.ledger import UploadJournal, UploadRecord


def raw_entry(**overrides) -> dict:
    """A journal entry exactly as tools/takeout_upload.py writes one."""

    entry = {
        "source": "G:\\takeout-extracted\\Takeout\\Google Photos\\Photos from 2019\\IMG_0001.jpg",
        "media_key": "AF1QipExampleKey",
        "output": "G:\\takeout-work\\out\\IMG_0001.avif",
        "output_sha256": "deadbeef" * 8,
        "media_item_id": "AIPEgExampleItemId",
        "filename": "IMG_0001.avif",
        "mime_type": None,
        "verified": "ok",
        "verified_detail": "2019-01-01T00:00:00Z",
        "capture_time": "2019-01-01T00:00:00Z",
        "uploaded_at": "2026-09-15T12:00:00",
    }
    entry.update(overrides)
    return entry


def write_journal(path: Path, entries: dict) -> None:
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


class TestRoundTrip:
    def test_load_then_save_is_byte_compatible(self, tmp_path):
        """Same top-level shape: object keyed by output path, same keys/values."""

        path = tmp_path / "journal.json"
        original = {raw_entry()["output"]: raw_entry()}
        write_journal(path, original)

        journal = UploadJournal.load(path)
        journal.save()

        round_tripped = json.loads(path.read_text(encoding="utf-8"))
        assert round_tripped == original

    def test_unknown_key_survives_round_trip_untouched(self, tmp_path):
        """A key this module does not model must not be silently dropped."""

        path = tmp_path / "journal.json"
        entry = raw_entry(bytes_verified="differs", some_future_field={"nested": 1})
        original = {entry["output"]: entry}
        write_journal(path, original)

        journal = UploadJournal.load(path)
        journal.save()

        round_tripped = json.loads(path.read_text(encoding="utf-8"))
        assert round_tripped == original
        record = journal.all_records()[0]
        assert record.extra["bytes_verified"] == "differs"
        assert record.extra["some_future_field"] == {"nested": 1}

    def test_replaced_field_only_appears_once_set(self, tmp_path):
        """An entry the replace pass has not reached keeps the uploader's shape."""

        path = tmp_path / "journal.json"
        entry = raw_entry()
        assert "replaced" not in entry
        write_journal(path, {entry["output"]: entry})

        journal = UploadJournal.load(path)
        journal.save()

        round_tripped = json.loads(path.read_text(encoding="utf-8"))
        assert "replaced" not in round_tripped[entry["output"]]

    def test_replaced_field_round_trips_once_present(self, tmp_path):
        path = tmp_path / "journal.json"
        entry = raw_entry(replaced="replaced", replaced_at="2026-09-15T13:00:00", original_media_key="AF1QipOriginal")
        write_journal(path, {entry["output"]: entry})

        journal = UploadJournal.load(path)
        journal.save()

        round_tripped = json.loads(path.read_text(encoding="utf-8"))
        assert round_tripped[entry["output"]]["replaced"] == "replaced"
        assert round_tripped[entry["output"]]["replaced_at"] == "2026-09-15T13:00:00"
        assert round_tripped[entry["output"]]["original_media_key"] == "AF1QipOriginal"

    def test_load_missing_file_is_an_empty_journal(self, tmp_path):
        journal = UploadJournal.load(tmp_path / "does-not-exist.json")
        assert len(journal) == 0
        assert journal.all_records() == []


class TestAtomicWrite:
    def test_save_writes_via_temp_file_and_replace(self, tmp_path):
        path = tmp_path / "journal.json"
        journal = UploadJournal(path)
        journal.record(UploadRecord(output=Path("out.avif"), verified="ok"))

        assert path.exists()
        assert not path.with_suffix(path.suffix + ".tmp").exists()

    def test_interrupted_write_never_touches_the_real_path(self, tmp_path, monkeypatch):
        """A crash mid-write must not leave a truncated journal on disk.

        The write goes to a temp file first; only a successful write is
        swapped into place with `replace()`. Simulate the crash happening
        while the temp file is being written and confirm the real path --
        and its previously-good content -- is untouched.
        """

        path = tmp_path / "journal.json"
        journal = UploadJournal(path)
        journal.record(UploadRecord(output=Path("first.avif"), verified="ok"))
        good_contents = path.read_text(encoding="utf-8")

        real_write_text = Path.write_text

        def exploding_write_text(self, *args, **kwargs):
            if self.name.endswith(".tmp"):
                raise OSError("simulated crash mid-write")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", exploding_write_text)
        with pytest.raises(OSError, match="simulated crash"):
            journal.record(UploadRecord(output=Path("second.avif"), verified="ok"))

        assert path.read_text(encoding="utf-8") == good_contents
        assert not path.with_suffix(path.suffix + ".tmp").exists()


class TestAlreadyUploadedKeys:
    def test_collects_media_keys_of_uploaded_items(self, tmp_path):
        path = tmp_path / "journal.json"
        entries = {
            "out1.avif": raw_entry(output="out1.avif", media_key="key-1"),
            "out2.avif": raw_entry(output="out2.avif", media_key="key-2"),
        }
        write_journal(path, entries)

        journal = UploadJournal.load(path)

        assert journal.already_uploaded_keys() == {"key-1", "key-2"}

    def test_ignores_entries_with_no_media_key(self, tmp_path):
        path = tmp_path / "journal.json"
        entries = {
            "out1.avif": raw_entry(output="out1.avif", media_key=""),
            "out2.avif": raw_entry(output="out2.avif", media_key="key-2"),
        }
        write_journal(path, entries)

        journal = UploadJournal.load(path)

        assert journal.already_uploaded_keys() == {"key-2"}


class TestPendingReplacement:
    def test_verified_and_not_replaced_is_pending(self, tmp_path):
        path = tmp_path / "journal.json"
        entries = {
            "ok-not-replaced.avif": raw_entry(output="ok-not-replaced.avif", verified="ok"),
        }
        write_journal(path, entries)

        journal = UploadJournal.load(path)
        pending = journal.pending_replacement()

        assert len(pending) == 1
        assert pending[0].output == Path("ok-not-replaced.avif")

    def test_already_replaced_is_not_pending(self, tmp_path):
        path = tmp_path / "journal.json"
        entries = {
            "ok-replaced.avif": raw_entry(output="ok-replaced.avif", verified="ok", replaced="replaced"),
        }
        write_journal(path, entries)

        journal = UploadJournal.load(path)

        assert journal.pending_replacement() == []

    def test_refused_replacement_is_not_pending_again(self, tmp_path):
        """A prior refusal (e.g. "refused: shared_album") still counts as handled."""

        path = tmp_path / "journal.json"
        entries = {
            "refused.avif": raw_entry(output="refused.avif", verified="ok", replaced="refused: shared_album"),
        }
        write_journal(path, entries)

        journal = UploadJournal.load(path)

        assert journal.pending_replacement() == []

    def test_unverified_is_not_pending(self, tmp_path):
        path = tmp_path / "journal.json"
        entries = {
            "mismatch.avif": raw_entry(output="mismatch.avif", verified="mismatch"),
            "unverified.avif": raw_entry(output="unverified.avif", verified="unverified"),
        }
        write_journal(path, entries)

        journal = UploadJournal.load(path)

        assert journal.pending_replacement() == []


class TestJournalMembershipAndRecording:
    def test_contains_checks_by_output_path_key(self, tmp_path):
        path = tmp_path / "journal.json"
        write_journal(path, {"out1.avif": raw_entry(output="out1.avif")})

        journal = UploadJournal.load(path)

        assert "out1.avif" in journal
        assert "out2.avif" not in journal

    def test_record_adds_a_new_entry_and_persists_it(self, tmp_path):
        path = tmp_path / "journal.json"
        journal = UploadJournal(path)

        journal.record(
            UploadRecord(
                source=Path("source.jpg"),
                output=Path("out.avif"),
                media_key="key-1",
                output_sha256="abc123",
                media_item_id="item-1",
                filename="out.avif",
                mime_type=None,
                verified="ok",
                verified_detail="detail",
                capture_time="2026-01-01T00:00:00Z",
                uploaded_at="2026-09-15T12:00:00",
            )
        )

        reloaded = UploadJournal.load(path)
        assert len(reloaded) == 1
        assert reloaded.already_uploaded_keys() == {"key-1"}
        assert len(reloaded.pending_replacement()) == 1

    def test_record_replaces_existing_entry_for_same_output(self, tmp_path):
        path = tmp_path / "journal.json"
        journal = UploadJournal(path)
        journal.record(UploadRecord(output=Path("out.avif"), verified="unverified"))
        journal.record(UploadRecord(output=Path("out.avif"), verified="ok"))

        assert len(journal) == 1
        assert journal.all_records()[0].verified == "ok"


class TestByteCompatibility:
    """Loading and saving a journal nothing has changed must not rewrite it.

    Structural equality is not enough: an older version of the uploader wrote
    the same keys in a different order, so re-emitting them canonically would
    silently churn a file whose contents did not change.
    """

    # Native separators, because that is what the uploader writes and what
    # the real journals on disk contain; see the separator test below.
    SOURCE = str(Path("G:/in/a.jpg"))
    OUTPUT = str(Path("G:/out/a.avif"))

    LEGACY_ORDER = {
        OUTPUT: {
            "source": SOURCE,
            "output": OUTPUT,
            "output_sha256": "a" * 64,
            "media_item_id": "ITEM1",
            "filename": "a.avif",
            "mime_type": "image/avif",
            "bytes_verified": True,
            "uploaded_at": "2026-09-15T10:00:00",
            "verified": "ok",
            "verified_detail": "2026-09-15T10:00:00Z",
            "capture_time": "2020-01-01T00:00:00Z",
            "media_key": "KEY1",
        }
    }

    def _write(self, path: Path, payload: dict) -> bytes:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path.read_bytes()

    def test_a_legacy_key_order_survives_a_load_and_save(self, tmp_path):
        path = tmp_path / "journal.json"
        original = self._write(path, self.LEGACY_ORDER)
        UploadJournal.load(path).save()
        assert path.read_bytes() == original

    def test_the_unknown_key_keeps_its_original_position(self, tmp_path):
        path = tmp_path / "journal.json"
        self._write(path, self.LEGACY_ORDER)
        record = UploadJournal.load(path).all_records()[0]
        assert list(record.to_dict()).index("bytes_verified") == 6

    def test_a_newly_set_field_is_appended_not_interleaved(self, tmp_path):
        path = tmp_path / "journal.json"
        self._write(path, self.LEGACY_ORDER)
        journal = UploadJournal.load(path)
        record = journal.all_records()[0]
        record.replaced = "replaced"
        keys = list(record.to_dict())
        assert keys[:12] == list(self.LEGACY_ORDER[self.OUTPUT])
        assert keys[12] == "replaced"

    def test_a_record_this_module_wrote_round_trips_too(self, tmp_path):
        path = tmp_path / "journal.json"
        journal = UploadJournal(path)
        journal.record(
            UploadRecord(
                source=Path("G:/in/b.jpg"),
                output=Path("G:/out/b.avif"),
                media_key="KEY2",
                output_sha256="b" * 64,
                media_item_id="ITEM2",
                verified="ok",
            )
        )
        original = path.read_bytes()
        UploadJournal.load(path).save()
        assert path.read_bytes() == original

    def test_path_separators_are_normalised_to_this_platform(self, tmp_path):
        """A known, deliberate exception to byte-compatibility.

        A journal hand-edited to use forward slashes is rewritten with the
        platform's separator. The result is the same path, and it matches what
        the encoder writes into encoded.csv, so the two still join.
        """

        path = tmp_path / "journal.json"
        path.write_text(
            json.dumps({"G:/out/a.avif": {"output": "G:/out/a.avif"}}, indent=2),
            encoding="utf-8",
        )
        record = UploadJournal.load(path).all_records()[0]
        assert record.output == Path("G:/out/a.avif")
        assert record.to_dict()["output"] == str(Path("G:/out/a.avif"))
