"""Tests for the only Takeout code that destroys anything.

Every test here asks the same question from a different angle: can an original
be trashed when something about the replacement is not proven?
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from photos_shrink.config import load_config

MODULE = Path(__file__).resolve().parents[1] / "tools" / "takeout_replace.py"
spec = importlib.util.spec_from_file_location("takeout_replace", MODULE)
takeout_replace = importlib.util.module_from_spec(spec)
sys.modules["takeout_replace"] = takeout_replace
spec.loader.exec_module(takeout_replace)

ReplaceError = takeout_replace.ReplaceError


class FakeRemote:
    def __init__(self, resolved=None):
        self.resolved = resolved or {}

    def find_uploaded(self, path):
        return self.resolved.get(Path(path).name)


def settings_for(tmp_path, **run):
    config = tmp_path / "shrink.toml"
    body = "[run]\nwork_dir = 'work'\npause_seconds = 0\n"
    for key, value in run.items():
        body += f"{key} = {str(value).lower() if isinstance(value, bool) else value}\n"
    config.write_text(body, encoding="utf-8")
    return load_config(config)


class TestConfirmOriginal:
    def test_accepts_when_the_hash_resolves_to_the_claimed_key(self, tmp_path):
        source = tmp_path / "IMG_1.jpg"
        source.write_bytes(b"original bytes")
        remote = FakeRemote({"IMG_1.jpg": {"id": "KEY1"}})
        assert takeout_replace.confirm_original(remote, source, "KEY1")["id"] == "KEY1"

    def test_refuses_when_the_hash_resolves_to_a_different_item(self, tmp_path):
        """A sidecar media key is a claim; the content hash is the proof."""

        source = tmp_path / "IMG_1.jpg"
        source.write_bytes(b"original bytes")
        remote = FakeRemote({"IMG_1.jpg": {"id": "SOMEONE_ELSE"}})
        with pytest.raises(ReplaceError, match="sidecar claims"):
            takeout_replace.confirm_original(remote, source, "KEY1")

    def test_refuses_when_the_bytes_resolve_to_nothing(self, tmp_path):
        source = tmp_path / "IMG_1.jpg"
        source.write_bytes(b"original bytes")
        with pytest.raises(ReplaceError, match="do not resolve"):
            takeout_replace.confirm_original(FakeRemote(), source, "KEY1")

    def test_refuses_when_the_exported_original_is_missing(self, tmp_path):
        with pytest.raises(ReplaceError, match="missing from the export"):
            takeout_replace.confirm_original(FakeRemote(), tmp_path / "gone.jpg", "KEY1")


class TestCheckIdentity:
    def test_refuses_an_identical_id(self):
        with pytest.raises(ReplaceError, match="not distinct"):
            takeout_replace.check_identity({"id": "A"}, {"id": "A"})

    def test_refuses_a_shared_dedup_key(self):
        with pytest.raises(ReplaceError, match="deduplication identity"):
            takeout_replace.check_identity(
                {"id": "A", "dedup_key": "D"}, {"id": "B", "dedup_key": "D"}
            )

    def test_refuses_an_empty_replacement(self):
        with pytest.raises(ReplaceError):
            takeout_replace.check_identity({"id": "A"}, {})

    def test_accepts_a_distinct_replacement(self):
        takeout_replace.check_identity({"id": "A", "dedup_key": "D1"}, {"id": "B", "dedup_key": "D2"})


class TestOutputInfo:
    def test_supplies_the_hash_and_path_verification_requires(self, tmp_path, monkeypatch):
        output = tmp_path / "out.avif"
        output.write_bytes(b"encoded bytes")
        monkeypatch.setattr(takeout_replace.media, "probe", lambda *a, **k: {"width": 10, "height": 8})
        info = takeout_replace.output_info_for({}, output, "ffprobe")
        assert info["path"] == str(output)
        assert info["sha256"] == takeout_replace.sha256_file(output)
        assert info["size_bytes"] == output.stat().st_size

    def test_refuses_when_the_encoded_file_changed_since_upload(self, tmp_path, monkeypatch):
        output = tmp_path / "out.avif"
        output.write_bytes(b"different bytes now")
        monkeypatch.setattr(takeout_replace.media, "probe", lambda *a, **k: {})
        with pytest.raises(ReplaceError, match="no longer matches"):
            takeout_replace.output_info_for({"output_sha256": "a" * 64}, output, "ffprobe")


class TestConfiguredGate:
    """The replace step must honour the same refusals as the main pipeline."""

    def test_non_quota_items_are_refused(self, tmp_path):
        settings = settings_for(tmp_path)
        item = {"id": "A", "kind": "photo", "timestamp_ms": 1, "space_taken_bytes": 0}
        assert takeout_replace.skip_reason(settings, item) == "non_space_consuming"

    def test_shared_album_items_are_refused(self, tmp_path):
        settings = settings_for(tmp_path, skip_shared="true")
        item = {
            "id": "A",
            "kind": "photo",
            "timestamp_ms": 1,
            "space_taken_bytes": 10,
            "metadata": {"albums": [{"id": "x", "shared": True}]},
        }
        assert takeout_replace.skip_reason(settings, item) == "shared_album"

    def test_an_ordinary_item_is_allowed(self, tmp_path):
        settings = settings_for(tmp_path)
        item = {"id": "A", "kind": "photo", "timestamp_ms": 1_600_000_000_000, "space_taken_bytes": 10}
        assert takeout_replace.skip_reason(settings, item) is None
