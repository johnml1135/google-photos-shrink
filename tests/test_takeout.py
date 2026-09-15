"""Tests for reading a Google Takeout Photos export."""

from __future__ import annotations

import json

import pytest

from photos_shrink import takeout


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def sidecar_payload(title="IMG_0001.jpg", timestamp="1600000000", lat=0.0, lon=0.0, people=()):
    return {
        "title": title,
        "photoTakenTime": {"timestamp": timestamp},
        "geoData": {"latitude": lat, "longitude": lon},
        "people": [{"name": name} for name in people],
    }


class TestSidecarTarget:
    def test_classic_naming(self):
        assert takeout.sidecar_target("IMG_0001.jpg.json") == "IMG_0001.jpg"

    def test_supplemental_naming(self):
        name = "IMG_0001.jpg.supplemental-metadata.json"
        assert takeout.sidecar_target(name) == "IMG_0001.jpg"

    @pytest.mark.parametrize(
        "name",
        [
            "IMG_0001.jpg.supplemental-metadat.json",
            "IMG_0001.jpg.supplemental-met.json",
            "IMG_0001.jpg.supple.json",
            "IMG_0001.jpg.s.json",
        ],
    )
    def test_truncated_supplemental(self, name):
        assert takeout.sidecar_target(name) == "IMG_0001.jpg"

    def test_counter_migrates_back_across_the_extension(self):
        assert takeout.sidecar_target("IMG_0001.jpg(1).json") == "IMG_0001(1).jpg"

    def test_counter_after_supplemental(self):
        name = "IMG_0001.jpg.supplemental-metadata(2).json"
        assert takeout.sidecar_target(name) == "IMG_0001(2).jpg"

    def test_real_extension_is_not_mistaken_for_supplemental(self):
        assert takeout.sidecar_target("clip.mp4.json") == "clip.mp4"

    def test_rejects_non_sidecar(self):
        with pytest.raises(takeout.TakeoutError):
            takeout.sidecar_target("IMG_0001.jpg")


class TestResolveSidecar:
    def test_exact_match(self, tmp_path):
        write(tmp_path / "IMG_0001.jpg.json", sidecar_payload())
        index = takeout.index_sidecars(tmp_path)
        found, edited = takeout.resolve_sidecar("IMG_0001.jpg", index)
        assert found is not None and not edited

    def test_edited_variant_falls_back_to_the_original_sidecar(self, tmp_path):
        write(tmp_path / "IMG_0001.jpg.json", sidecar_payload())
        index = takeout.index_sidecars(tmp_path)
        found, edited = takeout.resolve_sidecar("IMG_0001-edited.jpg", index)
        assert found == tmp_path / "IMG_0001.jpg.json"
        assert edited is True

    @pytest.mark.parametrize("name", ["IMG_0001-edite.jpg", "IMG_0001-edi.jpg", "IMG_0001-ed.jpg"])
    def test_truncated_edited_suffixes(self, tmp_path, name):
        write(tmp_path / "IMG_0001.jpg.json", sidecar_payload())
        index = takeout.index_sidecars(tmp_path)
        found, edited = takeout.resolve_sidecar(name, index)
        assert found is not None and edited is True

    def test_truncated_filename_resolves_when_unambiguous(self, tmp_path):
        write(tmp_path / "a_very_long_original_photo_name_that_g.jpg.json", sidecar_payload())
        index = takeout.index_sidecars(tmp_path)
        found, _ = takeout.resolve_sidecar("a_very_long_original_photo_name_that_got_clipped.jpg", index)
        assert found is not None

    def test_ambiguous_truncation_is_refused(self, tmp_path):
        write(tmp_path / "IMG_00.jpg.json", sidecar_payload())
        write(tmp_path / "IMG_000.jpg.json", sidecar_payload())
        index = takeout.index_sidecars(tmp_path)
        found, _ = takeout.resolve_sidecar("IMG_0001.jpg", index)
        assert found is None, "ambiguous prefixes must not bind to arbitrary metadata"

    def test_missing_sidecar_reports_none(self, tmp_path):
        index = takeout.index_sidecars(tmp_path)
        assert takeout.resolve_sidecar("IMG_0001.jpg", index) == (None, False)

    def test_album_metadata_is_not_treated_as_a_sidecar(self, tmp_path):
        write(tmp_path / "metadata.json", {"title": "Holiday"})
        assert takeout.index_sidecars(tmp_path) == {}


class TestParseSidecar:
    def test_extracts_timestamp_geo_and_people(self, tmp_path):
        path = tmp_path / "IMG_0001.jpg.json"
        write(path, sidecar_payload(timestamp="1600000000", lat=47.6, lon=-122.3, people=["Ada"]))
        fields = takeout.parse_sidecar(path)
        assert fields["taken_timestamp_ms"] == 1600000000 * 1000
        assert fields["latitude"] == pytest.approx(47.6)
        assert fields["people"] == ("Ada",)

    def test_zero_coordinates_are_treated_as_absent(self, tmp_path):
        path = tmp_path / "IMG_0001.jpg.json"
        write(path, sidecar_payload(lat=0.0, lon=0.0))
        fields = takeout.parse_sidecar(path)
        assert fields["latitude"] is None and fields["longitude"] is None

    def test_exif_geo_wins_over_inferred_geo(self, tmp_path):
        path = tmp_path / "IMG_0001.jpg.json"
        payload = sidecar_payload(lat=1.0, lon=1.0)
        payload["geoDataExif"] = {"latitude": 47.6, "longitude": -122.3}
        write(path, payload)
        assert takeout.parse_sidecar(path)["latitude"] == pytest.approx(47.6)

    def test_malformed_sidecar_raises(self, tmp_path):
        path = tmp_path / "IMG_0001.jpg.json"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(takeout.TakeoutError):
            takeout.parse_sidecar(path)


class TestScan:
    def test_pairs_media_with_metadata_and_hashes(self, tmp_path):
        album = tmp_path / "Holiday"
        album.mkdir()
        (album / "IMG_0001.jpg").write_bytes(b"photo-bytes")
        write(album / "IMG_0001.jpg.supplemental-metadata.json", sidecar_payload(timestamp="1600000000"))
        write(album / "metadata.json", {"title": "Holiday 2024"})

        records = list(takeout.scan(tmp_path, compute_hash=True))
        assert len(records) == 1
        record = records[0]
        assert record.kind == "photo"
        assert record.album == "Holiday 2024"
        assert record.taken_timestamp_ms == 1600000000 * 1000
        assert record.sha256 == takeout.sha256(album / "IMG_0001.jpg")
        assert record.has_metadata

    def test_date_buckets_are_not_albums(self, tmp_path):
        bucket = tmp_path / "Photos from 2024"
        bucket.mkdir()
        (bucket / "IMG_0001.jpg").write_bytes(b"x")
        write(bucket / "IMG_0001.jpg.json", sidecar_payload())
        assert list(takeout.scan(tmp_path))[0].album is None

    def test_media_without_a_sidecar_is_still_reported(self, tmp_path):
        (tmp_path / "IMG_0001.jpg").write_bytes(b"x")
        record = list(takeout.scan(tmp_path))[0]
        assert record.sidecar is None
        assert not record.has_metadata, "callers must be able to quarantine these"

    def test_non_media_files_are_ignored(self, tmp_path):
        (tmp_path / "archive_browser.html").write_text("<html></html>", encoding="utf-8")
        (tmp_path / "print-subscriptions.json").write_text("{}", encoding="utf-8")
        assert list(takeout.scan(tmp_path)) == []

    def test_videos_are_classified(self, tmp_path):
        (tmp_path / "clip.MOV").write_bytes(b"v")
        assert list(takeout.scan(tmp_path))[0].kind == "video"

    def test_missing_root_raises(self, tmp_path):
        with pytest.raises(takeout.TakeoutError):
            list(takeout.scan(tmp_path / "nope"))


class TestGooglePhotosLink:
    def test_url_and_media_key_are_extracted(self, tmp_path):
        path = tmp_path / "IMG_0001.jpg.json"
        payload = sidecar_payload()
        payload["url"] = "https://photos.google.com/photo/AF1QipOeLuraoBkmjO1DBzD0oU8s0U0QcHl8dawf3MEE"
        write(path, payload)
        fields = takeout.parse_sidecar(path)
        assert fields["url"] == payload["url"]
        assert fields["media_key"] == "AF1QipOeLuraoBkmjO1DBzD0oU8s0U0QcHl8dawf3MEE"

    def test_missing_url_is_none(self, tmp_path):
        path = tmp_path / "IMG_0001.jpg.json"
        write(path, sidecar_payload())
        fields = takeout.parse_sidecar(path)
        assert fields["url"] is None and fields["media_key"] is None

    def test_non_https_url_is_rejected(self, tmp_path):
        path = tmp_path / "IMG_0001.jpg.json"
        payload = sidecar_payload()
        payload["url"] = "javascript:alert(1)"
        write(path, payload)
        assert takeout.parse_sidecar(path)["media_key"] is None

    def test_scan_surfaces_the_link(self, tmp_path):
        (tmp_path / "IMG_0001.jpg").write_bytes(b"x")
        payload = sidecar_payload()
        payload["url"] = "https://photos.google.com/photo/AF1QipABC"
        write(tmp_path / "IMG_0001.jpg.json", payload)
        record = list(takeout.scan(tmp_path))[0]
        assert record.media_key == "AF1QipABC"
