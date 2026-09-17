"""Tests for the only Takeout code that destroys anything.

`replace_batch` works a whole batch per request, so a failure must stay with
its own item: these tests use a fake library that records every call, and
assert both what each item's outcome was and that nothing destructive reached
an item that failed a check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from photos_shrink.config import load_config
from photos_shrink.integrity import sha256_file
from photos_shrink.ledger import UploadRecord
from photos_shrink.remote import RemoteProtocolError
from photos_shrink.replacement import needed_fixes, replace_batch

ORIGINAL_BYTES = b"original bytes"


@dataclass
class FakeLibrary:
    """A library adapter that records calls.

    `items` maps a media key to what `get_items` returns for it. `by_output`
    maps an encoded file's name to the media key its hash resolves to. Fixes
    are applied to `items` unless `fix_takes` is False; trashing puts dedup
    keys in `bin` unless `trash_takes` is False.
    """

    items: dict[str, Any] = field(default_factory=dict)
    by_output: dict[str, Any] = field(default_factory=dict)
    fix_takes: bool = True
    fix_errors: dict[str, Exception] = field(default_factory=dict)
    trash_takes: bool = True
    trash_error: Exception | None = None
    bin: set[str] = field(default_factory=set)
    calls: list[tuple] = field(default_factory=list)

    def get_items(self, keys):
        self.calls.append(("get_items", list(keys)))
        return {key: self.items[key] for key in keys if key in self.items}

    def find_uploaded_many(self, paths):
        self.calls.append(("find_uploaded_many", [Path(p).name for p in paths]))
        result = {}
        for path in paths:
            found = self.by_output.get(Path(path).name)
            if isinstance(found, str):
                found = {"id": found, "dedup_key": self.items[found]["dedup_key"]}
            result[path] = found
        return result

    def restore_many(self, fixes):
        self.calls.append(("restore_many", [(f["replacement"]["id"], sorted(k for k in f if k != "replacement")) for f in fixes]))
        if self.fix_takes:
            for fix in fixes:
                item = self.items[fix["replacement"]["id"]]
                if fix["replacement"]["id"] in self.fix_errors:
                    continue
                if "timestamp" in fix:
                    item["timestamp_ms"], item["timezone_offset"] = fix["timestamp"]
                item["metadata"]["albums"] = item["metadata"]["albums"] + list(fix.get("albums", []))
                for key in ("favorite", "archived", "description"):
                    if key in fix:
                        item["metadata"][key] = fix[key]
        return dict(self.fix_errors)

    def trash_many(self, dedup_keys):
        self.calls.append(("trash_many", list(dedup_keys)))
        if self.trash_error:
            raise self.trash_error
        if self.trash_takes:
            self.bin.update(dedup_keys)

    def in_bin(self, dedup_keys):
        self.calls.append(("in_bin", list(dedup_keys)))
        return set(dedup_keys) & self.bin

    def names(self):
        return [call[0] for call in self.calls]

    def trashed(self):
        return [key for call in self.calls if call[0] == "trash_many" for key in call[1]]


def settings_for(tmp_path: Path, **run):
    config = tmp_path / "shrink.toml"
    body = "[run]\nwork_dir = 'work'\npause_seconds = 0\n"
    for key, value in run.items():
        body += f"{key} = {str(value).lower() if isinstance(value, bool) else value}\n"
    config.write_text(body, encoding="utf-8")
    return load_config(config)


def make_item(item_id: str, **overrides) -> dict[str, Any]:
    metadata = {"albums": [], "favorite": False, "archived": False, "description": None}
    metadata.update(overrides.pop("metadata", {}))
    item = {
        "id": item_id,
        "dedup_key": f"dedup-{item_id}",
        "filename": "IMG_1.jpg",
        "kind": "photo",
        "timestamp_ms": 1_700_000_000_000,
        "timezone_offset": -14_400_000,
        "size_bytes": len(ORIGINAL_BYTES),
        "space_taken_bytes": 12345,
        "metadata": metadata,
        "skip_reason": None,
        "trashed": False,
    }
    item.update(overrides)
    return item


def make_job(tmp_path: Path, name: str = "one", *, media_key: str | None = "ORIG-one", record_hash: bool = True) -> UploadRecord:
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    source = folder / "IMG_1.jpg"
    source.write_bytes(ORIGINAL_BYTES)
    output = folder / f"{name}.avif"
    output.write_bytes(f"encoded {name}".encode())
    return UploadRecord(
        source=source, output=output, media_key=media_key,
        output_sha256=sha256_file(output) if record_hash else None,
    )


def library_for(*names: str, **kwargs) -> FakeLibrary:
    """A library where each named job's original and replacement already agree."""

    library = FakeLibrary(**kwargs)
    for name in names:
        library.items[f"ORIG-{name}"] = make_item(f"ORIG-{name}")
        library.items[f"REPL-{name}"] = make_item(f"REPL-{name}", size_bytes=99)
        library.by_output[f"{name}.avif"] = f"REPL-{name}"
    return library


def run(library, jobs, tmp_path, *, apply=True, keep_originals=False, **run_settings):
    return replace_batch(
        library, jobs, settings=settings_for(tmp_path, **run_settings),
        apply=apply, keep_originals=keep_originals,
    )


class TestWholeBatch:
    def test_a_matching_item_is_trashed_and_confirmed(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        outcomes = run(library, [job], tmp_path)
        assert outcomes[job.key].status == "replaced"
        assert library.trashed() == ["dedup-ORIG-one"]
        assert library.names() == ["get_items", "find_uploaded_many", "get_items", "trash_many", "in_bin"]

    def test_a_batch_costs_the_same_calls_as_one_item(self, tmp_path):
        """The point of batching: requests do not grow with the batch."""

        names = [f"n{i}" for i in range(20)]
        jobs = [make_job(tmp_path, name, media_key=f"ORIG-{name}") for name in names]
        library = library_for(*names)
        outcomes = run(library, jobs, tmp_path)
        assert {o.status for o in outcomes.values()} == {"replaced"}
        assert library.names() == ["get_items", "find_uploaded_many", "get_items", "trash_many", "in_bin"]
        assert len(library.trashed()) == 20

    def test_one_failure_does_not_stop_the_rest(self, tmp_path):
        good = make_job(tmp_path, "good", media_key="ORIG-good")
        bad = make_job(tmp_path, "bad", media_key="ORIG-bad")
        library = library_for("good", "bad")
        library.items["ORIG-bad"]["size_bytes"] = 999
        outcomes = run(library, [good, bad], tmp_path)
        assert outcomes[good.key].status == "replaced"
        assert outcomes[bad.key].status == "failed"
        assert library.trashed() == ["dedup-ORIG-good"]

    def test_dry_run_reads_but_never_fixes_or_trashes(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.items["REPL-one"]["timestamp_ms"] = 1
        outcomes = run(library, [job], tmp_path, apply=False)
        assert outcomes[job.key].status == "would_replace"
        assert outcomes[job.key].detail == "would fix capture time, then trash"
        assert "restore_many" not in library.names()
        assert "trash_many" not in library.names()

    def test_keep_originals_fixes_but_never_trashes(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.items["REPL-one"]["timestamp_ms"] = 1
        outcomes = run(library, [job], tmp_path, keep_originals=True)
        assert outcomes[job.key].status == "verified_original_kept"
        assert "restore_many" in library.names()
        assert "trash_many" not in library.names()


class TestOriginal:
    def test_the_sidecar_key_is_accepted_when_id_and_size_agree(self, tmp_path):
        job = make_job(tmp_path)
        outcomes = run(library_for("one"), [job], tmp_path)
        assert outcomes[job.key].status == "replaced"

    def test_a_size_mismatch_fails_before_the_replacement_is_looked_up(self, tmp_path):
        """A sidecar paired with the wrong file names an item of another size."""

        job = make_job(tmp_path)
        library = library_for("one")
        library.items["ORIG-one"]["size_bytes"] = 999
        outcomes = run(library, [job], tmp_path)
        assert outcomes[job.key].status == "failed"
        assert "may describe a different file" in outcomes[job.key].detail
        assert library.calls[1] == ("find_uploaded_many", [])
        assert library.trashed() == []

    def test_another_id_fails(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.items["ORIG-one"]["id"] = "SOMEONE_ELSE"
        assert run(library, [job], tmp_path)[job.key].status == "failed"
        assert library.trashed() == []

    def test_an_unreadable_original_fails(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.items["ORIG-one"] = RemoteProtocolError("item identity is incomplete")
        outcome = run(library, [job], tmp_path)[job.key]
        assert outcome.status == "failed"
        assert "item identity is incomplete" in outcome.detail

    def test_a_missing_export_never_touches_the_library(self, tmp_path):
        job = make_job(tmp_path)
        job.source.unlink()
        library = library_for("one")
        assert run(library, [job], tmp_path)[job.key].status == "failed"
        assert library.calls[0] == ("get_items", [])

    def test_no_media_key_fails(self, tmp_path):
        job = make_job(tmp_path, media_key=None)
        assert run(library_for("one"), [job], tmp_path)[job.key].status == "failed"

    def test_an_item_that_costs_no_quota_is_refused(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.items["ORIG-one"]["space_taken_bytes"] = 0
        outcome = run(library, [job], tmp_path)[job.key]
        assert (outcome.status, outcome.detail) == ("refused", "non_space_consuming")
        assert library.trashed() == []

    @pytest.mark.parametrize("flag, token", [
        ("motion photo association is unsupported", "motion_photo"),
        ("shared item", "shared_item"),
        ("partial upload", "partial_upload"),
    ])
    def test_originals_that_would_lose_something_are_refused(self, tmp_path, flag, token):
        job = make_job(tmp_path)
        library = library_for("one")
        library.items["ORIG-one"]["skip_reason"] = flag
        outcome = run(library, [job], tmp_path)[job.key]
        assert (outcome.status, outcome.detail) == ("refused", token)

    def test_an_unreadable_owner_is_not_a_refusal(self, tmp_path):
        """The original is in this account's Takeout and costs its quota."""

        job = make_job(tmp_path)
        library = library_for("one")
        library.items["ORIG-one"]["skip_reason"] = "ownership is unknown"
        assert run(library, [job], tmp_path)[job.key].status == "replaced"

    def test_an_unknown_favorite_is_refused_even_behind_an_ownership_flag(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.items["ORIG-one"]["skip_reason"] = "ownership is unknown"
        library.items["ORIG-one"]["metadata"]["favorite"] = None
        outcome = run(library, [job], tmp_path)[job.key]
        assert (outcome.status, outcome.detail) == ("refused", "unknown_favorite_or_archive")

    def test_a_shared_album_original_is_refused(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.items["ORIG-one"]["metadata"]["albums"] = [{"id": "s", "title": "Family", "shared": True}]
        outcome = run(library, [job], tmp_path, skip_shared=True)[job.key]
        assert (outcome.status, outcome.detail) == ("refused", "shared_album")


class TestReplacement:
    def test_an_encoded_file_changed_since_upload_fails_before_any_request(self, tmp_path):
        job = make_job(tmp_path)
        job.output.write_bytes(b"changed")
        library = library_for("one")
        outcome = run(library, [job], tmp_path)[job.key]
        assert outcome.status == "failed"
        assert "no longer matches" in outcome.detail
        assert library.calls[0] == ("get_items", [])

    def test_not_found_by_hash_fails(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        del library.by_output["one.avif"]
        outcome = run(library, [job], tmp_path)[job.key]
        assert outcome.status == "failed"
        assert "not found by content hash" in outcome.detail
        assert library.trashed() == []

    def test_a_lookup_error_fails(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.by_output["one.avif"] = RemoteProtocolError("multiple exact hash matches")
        assert run(library, [job], tmp_path)[job.key].status == "failed"

    def test_the_original_itself_is_not_a_replacement(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.by_output["one.avif"] = "ORIG-one"
        outcome = run(library, [job], tmp_path)[job.key]
        assert outcome.status == "failed"
        assert "not distinct" in outcome.detail
        assert library.trashed() == []

    def test_the_uploaders_batch_album_is_fine(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.items["REPL-one"]["metadata"]["albums"] = [{"id": "b", "title": "photos-shrink batch 1", "shared": False}]
        assert run(library, [job], tmp_path)[job.key].status == "replaced"
        assert "restore_many" not in library.names()

    def test_an_extra_shared_album_fails(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.items["REPL-one"]["metadata"]["albums"] = [{"id": "s", "title": "Family", "shared": True}]
        assert run(library, [job], tmp_path)[job.key].status == "failed"
        assert library.trashed() == []


class TestFixes:
    def test_a_wrong_date_is_fixed_then_reread_then_trashed(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        library.items["REPL-one"]["timestamp_ms"] = 1
        outcome = run(library, [job], tmp_path)[job.key]
        assert outcome.status == "replaced"
        assert library.names() == [
            "get_items", "find_uploaded_many", "get_items",
            "restore_many", "get_items", "trash_many", "in_bin",
        ]
        assert library.calls[3] == ("restore_many", [("REPL-one", ["timestamp"])])
        assert library.calls[4] == ("get_items", ["REPL-one"])

    def test_a_missing_album_is_added(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one")
        trip = {"id": "a", "title": "Trip", "shared": False}
        library.items["ORIG-one"]["metadata"]["albums"] = [trip]
        assert run(library, [job], tmp_path)[job.key].status == "replaced"
        assert library.calls[3] == ("restore_many", [("REPL-one", ["albums"])])
        assert trip in library.items["REPL-one"]["metadata"]["albums"]

    def test_a_fix_that_does_not_take_is_never_trashed(self, tmp_path):
        """The re-read decides, not the fix call's response."""

        job = make_job(tmp_path)
        library = library_for("one", fix_takes=False)
        library.items["REPL-one"]["timestamp_ms"] = 1
        outcome = run(library, [job], tmp_path)[job.key]
        assert outcome.status == "failed"
        assert "still differs after fixing: capture time" in outcome.detail
        assert "trash_many" not in library.names()

    def test_a_failed_fix_call_is_never_trashed(self, tmp_path):
        """The live failure: rpc=DaSgWe on setting the capture time."""

        job = make_job(tmp_path)
        library = library_for("one", fix_errors={"REPL-one": RemoteProtocolError("rpc=DaSgWe")})
        library.items["REPL-one"]["timestamp_ms"] = 1
        outcome = run(library, [job], tmp_path)[job.key]
        assert outcome.status == "failed"
        assert "rpc=DaSgWe" in outcome.detail
        assert "trash_many" not in library.names()

    def test_only_items_that_need_it_are_fixed_or_reread(self, tmp_path):
        clean = make_job(tmp_path, "clean", media_key="ORIG-clean")
        dated = make_job(tmp_path, "dated", media_key="ORIG-dated")
        library = library_for("clean", "dated")
        library.items["REPL-dated"]["timestamp_ms"] = 1
        outcomes = run(library, [clean, dated], tmp_path)
        assert {outcomes[clean.key].status, outcomes[dated.key].status} == {"replaced"}
        assert library.calls[3] == ("restore_many", [("REPL-dated", ["timestamp"])])
        assert library.calls[4] == ("get_items", ["REPL-dated"])


class TestFixRequestRejected:
    def test_a_rejected_fix_request_fails_only_the_items_being_fixed(self, tmp_path):
        """The live run: Google answered the whole fix request with HTTP 400."""

        clean = make_job(tmp_path, "clean", media_key="ORIG-clean")
        dated = make_job(tmp_path, "dated", media_key="ORIG-dated")
        library = library_for("clean", "dated")
        library.items["REPL-dated"]["timestamp_ms"] = 1

        def rejected(fixes):
            raise RemoteProtocolError("Google Photos request failed rpc=DaSgWe status=400")

        library.restore_many = rejected
        outcomes = run(library, [clean, dated], tmp_path)
        assert outcomes[clean.key].status == "replaced"
        assert outcomes[dated.key].status == "failed"
        assert "status=400" in outcomes[dated.key].detail
        assert library.trashed() == ["dedup-ORIG-clean"]


class TestTrash:
    def test_two_keys_for_one_library_item_trash_once_and_both_confirm(self, tmp_path):
        """The live IMG_2004 case: two sidecars, two media keys, one item."""

        first = make_job(tmp_path, "first", media_key="ORIG-first")
        second = make_job(tmp_path, "second", media_key="ORIG-second")
        library = library_for("first", "second")
        library.items["ORIG-second"]["dedup_key"] = "dedup-ORIG-first"
        outcomes = run(library, [first, second], tmp_path)
        assert {outcomes[first.key].status, outcomes[second.key].status} == {"replaced"}
        assert library.trashed() == ["dedup-ORIG-first"]

    def test_an_unconfirmed_trash_fails(self, tmp_path):
        job = make_job(tmp_path)
        library = library_for("one", trash_takes=False)
        outcome = run(library, [job], tmp_path)[job.key]
        assert (outcome.status, outcome.detail) == ("failed", "trash was not confirmed by the server")

    def test_a_trash_error_fails_the_whole_call_without_confirming(self, tmp_path):
        jobs = [make_job(tmp_path, n, media_key=f"ORIG-{n}") for n in ("a", "b")]
        library = library_for("a", "b", trash_error=RemoteProtocolError("rpc=XwAOJf"))
        outcomes = run(library, jobs, tmp_path)
        assert {o.status for o in outcomes.values()} == {"failed"}
        assert library.names()[-1] == "trash_many"


class TestNeededFixes:
    def test_nothing_when_everything_agrees(self):
        assert needed_fixes(make_item("O"), make_item("R")) == {}

    def test_milliseconds_are_not_a_difference(self):
        """Google sets capture time in whole seconds, so a fix drops them."""

        assert needed_fixes(make_item("O", timestamp_ms=1_700_000_000_521), make_item("R")) == {}

    def test_a_second_is_a_difference(self):
        assert "timestamp" in needed_fixes(make_item("O", timestamp_ms=1_700_000_001_000), make_item("R"))

    def test_timezone_alone_is_a_difference(self):
        assert "timestamp" in needed_fixes(make_item("O"), make_item("R", timezone_offset=0))

    def test_favorite_archive_and_description_are_carried(self):
        original = make_item("O", metadata={"favorite": True, "archived": True, "description": "beach"})
        assert needed_fixes(original, make_item("R")) == {"favorite": True, "archived": True, "description": "beach"}

    def test_a_description_only_the_replacement_has_is_not_a_loss(self):
        assert needed_fixes(make_item("O"), make_item("R", metadata={"description": "x"})) == {}
