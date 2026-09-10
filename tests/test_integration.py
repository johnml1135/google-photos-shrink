from __future__ import annotations

import csv
import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest

import photos_shrink.media as media
from photos_shrink.config import load_config
from photos_shrink.pipeline import Pipeline
from photos_shrink.state import StateStore


class StrictGoogleBackend:
    def __init__(self, original_path: Path, item: dict[str, Any]) -> None:
        self.original_path = original_path
        self.original = item
        self.replacement: dict[str, Any] | None = None
        self.uploaded_bytes: bytes | None = None
        self.uploads = 0
        self.trash_calls = 0
        self.metadata_restored = 0
        self._trashed = False

    def account_id(self) -> str:
        return "integration-account"

    def list_items(self) -> list[dict[str, Any]]:
        return [] if self._trashed else [self.original]

    def download(self, item: dict[str, Any], destination: Path) -> None:
        shutil.copyfile(self.original_path, destination)

    def find_uploaded(self, path: Path) -> dict[str, Any] | None:
        digest = hashlib.sha256(path.read_bytes()).digest()
        if (
            self.uploaded_bytes is None
            or digest != hashlib.sha256(self.uploaded_bytes).digest()
        ):
            return None
        return self.replacement

    def upload(self, path: Path) -> dict[str, Any]:
        self.uploads += 1
        self.uploaded_bytes = path.read_bytes()
        self.replacement = {
            "id": "replacement-1",
            "dedup_key": "replacement-dedup",
            "filename": "A.avif",
            "size_bytes": len(self.uploaded_bytes),
            "width": 0,
            "height": 0,
            "kind": "photo",
            "metadata": {
                "albums": [],
                "description": None,
                "favorite": False,
                "archived": False,
            },
        }
        return self.replacement

    def get_item(self, item_id: str) -> dict[str, Any]:
        if self.replacement is None or item_id != self.replacement["id"]:
            raise AssertionError(f"unknown item requested: {item_id}")
        return self.replacement

    def restore_metadata(
        self, original: dict[str, Any], replacement: dict[str, Any]
    ) -> None:
        self.metadata_restored += 1
        replacement["metadata"] = original["metadata"]
        assert replacement["metadata"] == original["metadata"]

    def verify_replacement(
        self,
        original: dict[str, Any],
        replacement: dict[str, Any],
        output_info: dict[str, Any],
    ) -> None:
        required = {
            "kind",
            "width",
            "height",
            "size_bytes",
            "output_sha256",
            "output_path",
        }
        missing = required - output_info.keys()
        assert not missing, (
            f"replacement verification omitted fields: {sorted(missing)}"
        )
        output_path = Path(output_info["output_path"])
        assert output_path.is_file()
        output_bytes = output_path.read_bytes()
        assert output_info["kind"] == "photo"
        assert output_info["width"] <= original["width"]
        assert output_info["height"] <= original["height"]
        assert (
            output_info["size_bytes"] == len(output_bytes) == replacement["size_bytes"]
        )
        assert output_info["output_sha256"] == hashlib.sha256(output_bytes).hexdigest()
        assert self.uploaded_bytes == output_bytes

    def trash(self, item: dict[str, Any]) -> None:
        assert item["id"] == self.original["id"]
        self.trash_calls += 1
        self._trashed = True

    def is_trashed(self, item: dict[str, Any]) -> bool:
        return self._trashed and item["id"] == self.original["id"]


def _make_source(path: Path) -> None:
    image = pytest.importorskip("PIL.Image")
    pixels = [
        ((index * 37) % 256, (index * 91) % 256, (index * 173) % 256)
        for index in range(800 * 600)
    ]
    # Keep JPEG quality high so the real AVIF encode has meaningful savings.
    source = image.new("RGB", (800, 600))
    source.putdata(pixels)
    source.save(path, format="JPEG", quality=95, optimize=False)


def test_real_media_pipeline_uploads_once_and_trashes_only_after_strict_verify(
    tmp_path: Path,
) -> None:
    source = tmp_path / "original.jpg"
    _make_source(source)
    original_bytes = source.read_bytes()
    metadata = {
        "albums": [{"id": "album-1", "title": "Trip", "shared": False}],
        "description": "keep this description",
        "favorite": True,
        "archived": False,
        "latitude": None,
        "longitude": None,
    }
    item = {
        "id": "original-1",
        "dedup_key": "original-dedup",
        "filename": "A.JPG",
        "size_bytes": len(original_bytes),
        "sha256": hashlib.sha256(original_bytes).hexdigest(),
        "width": 800,
        "height": 600,
        "kind": "photo",
        "timestamp_ms": 1704067200000,
        "timezone_offset": 0,
        "duration_seconds": None,
        "mime_type": "image/jpeg",
        "metadata": metadata,
        "skip_reason": None,
    }
    config_path = tmp_path / "shrink.toml"
    config_path.write_text(
        "[run]\nwork_dir = 'work'\npause_seconds = 0\nminimum_savings_percent = 20\nthreads = 2\n",
        encoding="utf-8",
    )
    settings = load_config(config_path)
    remote = StrictGoogleBackend(source, item)
    report = tmp_path / "photos-shrink.csv"
    state_path = tmp_path / "state.sqlite"

    with StateStore(state_path, remote.account_id(), settings.fingerprint) as state:
        first = Pipeline(settings, remote, state, media=media).run(
            yes=True, report_path=report
        )
        assert first["replaced"] == 1
        assert remote.uploads == 1
        assert remote.trash_calls == 1
        assert remote.metadata_restored == 1

    with report.open(newline="", encoding="utf-8-sig") as stream:
        row = next(csv.DictReader(stream))
    assert row["status"] == "replaced"
    assert int(row["actual_size_bytes"]) == len(remote.uploaded_bytes or b"")
    assert int(row["actual_savings_bytes"]) > 0

    with StateStore(state_path, remote.account_id(), settings.fingerprint) as state:
        second = Pipeline(settings, remote, state, media=media).run(
            yes=True, report_path=tmp_path / "second.csv"
        )
        assert second["replaced"] == 0
    assert remote.uploads == 1
    assert remote.trash_calls == 1
