"""Tests for the two decisions the uploader makes about other people's data.

`verify_item` is the sole author of `UploadRecord.verified`, and
`pending_replacement()` gates on that field: an item it calls "ok" becomes an
original this tool is willing to trash. It had no test at all. Live, its
no-dimensions branch is what kept 121 freshly uploaded videos out of the
replace queue -- correctly, as Google had not finished processing them -- and
nothing anywhere pinned that behaviour.

The second decision is the gate the uploader applies before spending quota.
Its absence once put 194 partner-shared photos on this account's storage,
each a copy of a photo that cost it nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photos_shrink.policy import Candidate, verdict
from photos_shrink.steps.upload import verify_item


def item(**overrides) -> dict:
    metadata = {"width": "1500", "height": "1000", "creationTime": "2024-06-10T13:12:51.135Z"}
    metadata.update(overrides.pop("mediaMetadata", {}))
    return {"filename": "photo.avif", "mediaMetadata": metadata, **overrides}


@pytest.fixture
def probe_says(monkeypatch):
    """Stand in for ffprobe: the dimensions of the file that was uploaded."""

    def set_dimensions(width: int, height: int):
        monkeypatch.setattr(
            "photos_shrink.steps.upload.media.probe",
            lambda source, ffprobe: {"width": width, "height": height},
        )

    return set_dimensions


class TestVerifyItem:
    def test_a_matching_item_verifies_and_reports_its_capture_time(self, probe_says):
        probe_says(1500, 1000)
        assert verify_item(item(), Path("photo.avif"), "ffprobe") == (
            "ok",
            "2024-06-10T13:12:51.135Z",
        )

    def test_an_item_still_being_processed_is_unverified_not_wrong(self, probe_says):
        """A video Google has not finished processing reports no dimensions.

        Unverified keeps it out of the replace queue, so its original is never
        trashed on the strength of an upload nobody has checked.
        """

        probe_says(1920, 1080)
        verdict_, detail = verify_item(item(mediaMetadata={"width": None, "height": None}), Path("v.mp4"), "ffprobe")
        assert (verdict_, detail) == ("unverified", "the API returned no dimensions")

    def test_missing_metadata_entirely_is_unverified(self, probe_says):
        probe_says(1500, 1000)
        assert verify_item({"filename": "photo.avif"}, Path("photo.avif"), "ffprobe")[0] == "unverified"

    def test_different_dimensions_are_a_mismatch(self, probe_says):
        probe_says(1200, 800)
        status, detail = verify_item(item(), Path("photo.avif"), "ffprobe")
        assert status == "mismatch"
        assert "stored 1500x1000 but uploaded 1200x800" in detail

    def test_google_storing_another_file_under_this_name_is_a_mismatch(self, probe_says):
        """Two uploads of one photo: Google keeps the first and reports its name."""

        probe_says(1500, 1000)
        status, detail = verify_item(item(filename="first.avif"), Path("photo.avif"), "ffprobe")
        assert status == "mismatch"
        assert "first.avif" in detail

    def test_no_capture_time_is_unverified(self, probe_says):
        probe_says(1500, 1000)
        stored = item(mediaMetadata={"creationTime": ""})
        assert verify_item(stored, Path("photo.avif"), "ffprobe") == (
            "unverified",
            "no capture time was recorded",
        )

    def test_dimensions_are_checked_before_the_name(self, probe_says):
        """Both wrong reports the dimensions, which is the substantive fault."""

        probe_says(1200, 800)
        assert "1500x1000" in verify_item(item(filename="other.avif"), Path("photo.avif"), "ffprobe")[1]

    @pytest.mark.parametrize("value", ["", "wide", None, {}])
    def test_unreadable_dimensions_never_raise(self, probe_says, value):
        probe_says(1500, 1000)
        stored = item(mediaMetadata={"width": value, "height": value})
        assert verify_item(stored, Path("photo.avif"), "ffprobe")[0] == "unverified"


class TestTheGateBeforeQuotaIsSpent:
    """The uploader asks the gate again, with the origin the encoder never had."""

    ROW = {"source": "a.jpg", "media_key": "K", "saved_percent": "60", "taken_timestamp_ms": "1"}

    def _settings(self, tmp_path, **run):
        from photos_shrink.config import load_config

        body = "[run]\nwork_dir = 'work'\npause_seconds = 0\n"
        for key, value in run.items():
            body += f"{key} = {str(value).lower() if isinstance(value, bool) else value}\n"
        path = tmp_path / "shrink.toml"
        path.write_text(body, encoding="utf-8")
        return load_config(path)

    def test_a_photo_shared_in_by_someone_else_is_refused(self, tmp_path):
        candidate = Candidate.from_encode_row(self.ROW, kind="photo", origin="fromPartnerSharing")
        assert verdict(self._settings(tmp_path), candidate) == "shared_to_you"

    def test_an_ordinary_photo_passes(self, tmp_path):
        candidate = Candidate.from_encode_row(self.ROW, kind="photo", origin="mobileUpload")
        assert verdict(self._settings(tmp_path), candidate) is None

    def test_only_a_known_shared_origin_is_refused(self, tmp_path):
        """An origin the mirror did not record passes the gate.

        Refusal turns on recognising a shared origin, not on proving an
        ordinary one, so an unrecorded origin is treated as this account's.
        The uploader covers that gap earlier: a mirror with no `origin`
        column at all stops the run instead (steps/upload.py:109-115).
        """

        candidate = Candidate.from_encode_row(self.ROW, kind="photo", origin=None)
        assert verdict(self._settings(tmp_path), candidate) is None
