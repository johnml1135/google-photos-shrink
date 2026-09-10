from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from photos_shrink.auth import UploadNotStartedError
from photos_shrink.remote import GooglePhotosRemote, RemoteProtocolError


@dataclass
class Response:
    content: bytes = b""
    status_code: int = 200
    headers: dict[str, str] | None = None
    url: str = "https://photos.google.com/"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def iter_content(self, chunk_size=65536):
        yield self.content


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class Payloads:
    class GetLibraryPageByTakenDate:
        def __init__(self, *, page_id, source, page_size):
            self.page_id = page_id
            self.source = source
            self.page_size = page_size

    class GetRemoteMatchesByHash:
        def __init__(self, hashes):
            self.hashes = hashes

    class MoveToTrash:
        def __init__(self, keys):
            self.keys = keys

    class SetFavorite:
        def __init__(self, keys):
            self.keys = keys

    class UnFavorite(SetFavorite):
        pass

    class SetArchive:
        def __init__(self, keys):
            self.keys = keys

    class UnArchive(SetArchive):
        pass

    class SetItemTimestamp:
        def __init__(self, key, timestamp, timezone_offset):
            self.args = (key, timestamp, timezone_offset)

    class SetItemDescription:
        def __init__(self, key, description):
            self.args = (key, description)

    class SetItemGeoData:
        def __init__(self, keys, *args):
            self.keys = keys

    class AddItemsToExistingAlbum:
        def __init__(self, keys, album_id):
            self.keys = keys
            self.album_id = album_id

    class AddItemsToExistingSharedAlbum:
        def __init__(self, keys, album_id):
            self.keys = keys
            self.album_id = album_id


class Client:
    def __init__(self, response=None):
        self.session = Session(response or Response())
        self.global_data = {"oPEP7c": "stable-account"}
        self.calls = []
        self.responses = {}

    def send_api_request(self, payload):
        self.calls.append(payload)
        return self.responses.get(type(payload), type("R", (), {"success": True, "data": None})())


def settings(tmp_path: Path) -> dict:
    return {
        "google": {
            "cookies_file": str(tmp_path / "cookies.txt"),
            "browser_profile": str(tmp_path / "browser"),
            "browser_channel": "chrome",
            "account_index": 0,
        }
    }


def test_login_without_cookies_explains_normal_chrome_export(tmp_path):
    calls: list[Path] = []

    def client_factory(cookies_file, **kwargs):
        calls.append(Path(cookies_file))
        return Client()

    remote = GooglePhotosRemote(
        settings(tmp_path), client_factory=client_factory, payloads=Payloads
    )

    with pytest.raises(RemoteProtocolError, match="Export.*cookies.txt"):
        remote.login()

    assert calls == []


def test_small_run_limit_requests_a_small_initial_library_page(tmp_path):
    configured = settings(tmp_path)
    configured["run"] = {"limit": 3}
    remote = GooglePhotosRemote(configured, client=Client(), payloads=Payloads)
    requests = []

    def execute(payload):
        requests.append(payload)
        return type("Page", (), {"items": [], "next_page_id": None})()

    remote._execute = execute

    assert list(remote.list_items()) == []
    assert requests[0].page_size == 15


def test_small_run_limit_paginates_full_inventory_with_small_pages(tmp_path):
    configured = settings(tmp_path)
    configured["run"] = {"limit": 3}
    remote = GooglePhotosRemote(configured, client=Client(), payloads=Payloads)
    first_page = [type("Item", (), {"media_key": str(index)})() for index in range(15)]
    second_page = [type("Item", (), {"media_key": "15"})()]
    requests = []

    def execute(payload):
        requests.append(payload)
        if payload.page_id is None:
            return type("Page", (), {"items": first_page, "next_page_id": "another-page"})()
        return type("Page", (), {"items": second_page, "next_page_id": None})()

    remote._execute = execute
    remote._item_for_media = lambda media_key, library_item: {"id": media_key}

    assert len(list(remote.list_items())) == 16
    assert len(requests) == 2


def test_find_uploaded_uses_sha1_base64_and_rejects_malformed_success(tmp_path):
    path = tmp_path / "encoded.jpg"
    path.write_bytes(b"encoded bytes")
    client = Client()
    remote = GooglePhotosRemote(settings(tmp_path), client=client, payloads=Payloads)
    match = type("M", (), {"hash": base64.b64encode(hashlib.sha1(path.read_bytes()).digest()).decode(), "media_key": "id", "dedup_key": "dedup"})()
    client.responses[Payloads.GetRemoteMatchesByHash] = type("R", (), {"success": True, "data": [match]})()

    result = remote.find_uploaded(path)

    assert result["id"] == "id"
    assert client.calls[0].hashes == [match.hash]

    client.responses[Payloads.GetRemoteMatchesByHash] = type("R", (), {"success": True, "data": object()})()
    with pytest.raises(RemoteProtocolError):
        remote.find_uploaded(path)


def test_download_requires_original_url_and_writes_stream(tmp_path):
    destination = tmp_path / "original.jpg"
    client = Client(Response(content=b"original", headers={"content-type": "image/jpeg"}))
    remote = GooglePhotosRemote(settings(tmp_path), client=client, payloads=Payloads)

    remote.download({"id": "x", "metadata": {}, "original_url": "https://example.test/original"}, destination)

    assert destination.read_bytes() == b"original"
    assert client.session.calls[0][0].endswith("original")


def test_download_refuses_success_response_that_is_not_original(tmp_path):
    destination = tmp_path / "original.jpg"
    response = Response(content=b"login html", headers={"content-type": "text/html"})
    remote = GooglePhotosRemote(settings(tmp_path), client=Client(response), payloads=Payloads)

    with pytest.raises(RemoteProtocolError):
        remote.download({"id": "x", "metadata": {}, "original_url": "https://example.test/original"}, destination)
    assert not destination.exists()


def test_trash_fails_closed_when_item_identity_or_metadata_is_uncertain(tmp_path):
    client = Client()
    remote = GooglePhotosRemote(settings(tmp_path), client=client, payloads=Payloads)

    with pytest.raises(RemoteProtocolError):
        remote.trash({"id": "x", "dedup_key": None, "metadata": {}})
    assert client.calls == []


def test_upload_refuses_browser_session_for_a_different_account(tmp_path):
    path = tmp_path / "encoded.jpg"
    path.write_bytes(b"encoded bytes")

    class Browser:
        def open(self, *, interactive=False):
            return "other-account"

        def upload(self, path):
            raise AssertionError("upload must not start")

        def close(self):
            pass

    with pytest.raises(UploadNotStartedError, match="different accounts"):
        GooglePhotosRemote(settings(tmp_path), client=Client(), payloads=Payloads, browser=Browser()).upload(path)


@pytest.mark.parametrize("failure", ["construct", "account", "open"])
def test_upload_pre_submission_failures_are_retryable(tmp_path, monkeypatch, failure):
    path = tmp_path / "encoded.jpg"
    path.write_bytes(b"encoded bytes")

    class Browser:
        def __init__(self, settings):
            if failure == "construct":
                raise ValueError("browser setup failed")

        def open(self, *, interactive=False):
            if failure == "open":
                raise ValueError("browser open failed")
            return "stable-account"

        def upload(self, path):
            raise AssertionError("upload must not start")

        def close(self):
            pass

    client = Client()
    if failure == "account":
        client.global_data = {}
    monkeypatch.setattr("photos_shrink.remote.BrowserAuthenticator", Browser)
    with pytest.raises(UploadNotStartedError):
        GooglePhotosRemote(settings(tmp_path), client=client, payloads=Payloads).upload(path)


def test_trusted_upload_with_missing_source_is_safe(tmp_path):
    remote = GooglePhotosRemote({"run": {"skip_shared": False}}, payloads=Payloads, client=Client())
    info, ext, library_item = _shared_album_item()
    ext.source = []
    remote._trusted_media.add("media")

    item = remote._convert_item(info, ext, library_item)

    assert item["skip_reason"] is None


def test_saved_flag_and_owner_actor_are_not_ownership_proof():
    remote = GooglePhotosRemote({"run": {"skip_shared": False}}, payloads=Payloads, client=Client())
    info, ext, library_item = _shared_album_item()
    library_item.is_owned = None
    ext.owner = SimpleNamespace(actor_id="some-actor")

    item = remote._convert_item(info, ext, library_item)

    assert item["skip_reason"] == "ownership is unknown"


def test_missing_favorite_and_archive_flags_are_unsafe():
    remote = GooglePhotosRemote({"run": {"skip_shared": False}}, payloads=Payloads, client=Client())
    info, ext, library_item = _shared_album_item()
    del info.is_favorite
    del info.is_archived

    item = remote._convert_item(info, ext, library_item)

    assert item["metadata"]["favorite"] is None
    assert item["metadata"]["archived"] is None
    assert item["skip_reason"] == "favorite/archive metadata unknown"


def test_upstream_parser_shaped_item_normalizes_timestamp_albums_and_video_units():
    from gpwc.parser import ItemInfoExt, LibraryItem

    library_raw = [
        "media",
        ["thumb", 1920, 1080],
        1700000000123,
        "dedup",
        -18000,
        1700000000123,
        None,
        [],
        None,
        None,
        None,
        None,
        [],
        False,
        {"163238866": [1], "76647426": [2500]},
    ]
    album_raw = [
        "album",
        ["thumb", 1, 1],
        None,
        None,
        None,
        None,
        ["album-owner"],
        None,
        None,
        None,
        {"72930366": [None, "Vacation", [None, None, None, None, 1, None, None, None, None, 2], 1, False]},
    ]
    ext_raw = [
        [
            "media",
            "description",
            "IMG_1.MP4",
            1700000000123,
            -18000,
            123456,
            1920,
            1080,
            None,
            [[400000000, -730000000]],
            None,
            "dedup",
            [],
            None,
            None,
            None,
            None,
            None,
            None,
            [album_raw],
            None,
            None,
            None,
            "camera",
            None,
            None,
            None,
            [2, None, None, [["owner"]]],
            ["owner"],
            None,
            [None, 123456, 2],
            "other",
        ],
        [400000000, -730000000],
    ]
    library_item = LibraryItem.from_data(library_raw)
    ext = ItemInfoExt.from_data(ext_raw)
    remote = GooglePhotosRemote({"run": {"skip_shared": True}}, payloads=Payloads, client=Client())
    info = type("I", (), {"media_key": "media", "dedup_key": "dedup", "video_duration": 2500, "download_original_url": "https://example.test/original", "is_favorite": True, "is_archived": False, "is_partial_upload": False, "live_photo_duration": None, "space_taken": 123456})()

    item = remote._convert_item(info, ext, library_item)

    assert item["timestamp_ms"] == 1700000000123
    assert item["timezone_offset"] == -18000
    assert item["duration_seconds"] == 2.5
    assert item["kind"] == "video"
    assert item["metadata"]["albums"] == [{"id": "album", "title": "Vacation", "shared": False}]
    assert item["metadata"]["latitude"] == 40.0
    ext.geo_location.coordinates = [1, -2]
    near_zero = remote._convert_item(info, ext, library_item)
    assert near_zero["metadata"]["latitude"] == 0.0000001
    assert near_zero["metadata"]["longitude"] == -0.0000002
    assert item["skip_reason"] is None
    library_item.is_favorite = False
    library_item.is_archived = True
    fresh_flags = remote._convert_item(info, ext, library_item)
    assert fresh_flags["metadata"]["favorite"] is True
    assert fresh_flags["metadata"]["archived"] is False
    assert item["space_consuming"] is True
    ext.space_taken = 0
    info.space_taken = 0
    zero = remote._convert_item(info, ext, library_item)
    assert zero["space_consuming"] is False


def test_replacement_verification_requires_hash_and_accepts_album_order_and_duration_tolerance(tmp_path):
    output = tmp_path / "encoded.jpg"
    output.write_bytes(b"encoded")
    sha256 = GooglePhotosRemote._sha256(output)
    metadata = {
        "description": "caption",
        "favorite": True,
        "archived": False,
        "albums": [
            {"id": "a", "title": "A", "shared": False},
            {"id": "b", "title": "B", "shared": False},
        ],
        "latitude": None,
        "longitude": None,
    }
    original = {"id": "original", "dedup_key": "old", "timestamp_ms": 10, "timezone_offset": 0, "metadata": metadata}
    replacement = {
        "id": "replacement",
        "dedup_key": "new",
        "size_bytes": 7,
        "width": 100,
        "height": 100,
        "kind": "video",
        "sha256": sha256,
    }

    class VerificationRemote(GooglePhotosRemote):
        def get_item(self, id):
            return {
                **replacement,
                "timestamp_ms": 10,
                "timezone_offset": 0,
                "duration_seconds": 1.1,
                "metadata": {**metadata, "albums": list(reversed(metadata["albums"]))},
                "skip_reason": None,
                "is_original_quality": False,
                "original_url": "https://example.test/original",
            }

        def _verify_remote_bytes(self, item, expected_sha256):
            assert expected_sha256 == sha256

    remote = VerificationRemote({"run": {"skip_shared": True}})
    output_info = {
        "size_bytes": 7,
        "width": 100,
        "height": 100,
        "kind": "video",
        "duration_seconds": 1.0,
        "sha256": sha256,
        "path": output,
    }
    remote.verify_replacement(original, replacement, output_info)

    with pytest.raises(RemoteProtocolError, match="requires output hash"):
        remote.verify_replacement(original, replacement, {**output_info, "sha256": None})
    with pytest.raises(RemoteProtocolError, match="local encoded output hash changed"):
        remote.verify_replacement(original, {**replacement, "sha256": None}, {**output_info, "sha256": "wrong"})


def test_photo_replacement_verification_does_not_require_video_duration(tmp_path):
    output = tmp_path / "encoded.jpg"
    output.write_bytes(b"encoded")
    sha256 = GooglePhotosRemote._sha256(output)
    metadata = {
        "description": "caption",
        "favorite": False,
        "archived": False,
        "albums": [],
        "latitude": None,
        "longitude": None,
    }
    original = {
        "id": "original",
        "dedup_key": "old",
        "timestamp_ms": 10,
        "timezone_offset": 0,
        "metadata": metadata,
    }
    replacement = {
        "id": "replacement",
        "dedup_key": "new",
        "size_bytes": 7,
        "width": 100,
        "height": 100,
        "kind": "photo",
        "sha256": sha256,
    }

    class VerificationRemote(GooglePhotosRemote):
        def get_item(self, id):
            return {
                **replacement,
                "timestamp_ms": 10,
                "timezone_offset": 0,
                "duration_seconds": None,
                "metadata": metadata,
                "skip_reason": None,
            }

        def _verify_remote_bytes(self, item, expected_sha256):
            assert expected_sha256 == sha256

    VerificationRemote({"run": {"skip_shared": True}}).verify_replacement(
        original,
        replacement,
        {"size_bytes": 7, "width": 100, "height": 100, "kind": "photo", "sha256": sha256, "path": output},
    )


def test_remote_byte_verification_rejects_changed_download(tmp_path):
    output = tmp_path / "encoded.jpg"
    output.write_bytes(b"encoded")
    expected_sha256 = GooglePhotosRemote._sha256(output)
    client = Client(Response(content=b"changed", headers={"content-type": "image/jpeg"}))
    remote = GooglePhotosRemote(settings(tmp_path), client=client, payloads=Payloads)

    with pytest.raises(RemoteProtocolError, match="changed uploaded bytes"):
        remote._verify_remote_bytes(
            {"original_url": "https://example.test/original", "size_bytes": len(b"changed")}, expected_sha256
        )


def test_restore_metadata_uses_shared_album_payload_when_shared_albums_enabled(tmp_path):
    client = Client()
    remote = GooglePhotosRemote(
        {**settings(tmp_path), "run": {"skip_shared": False}}, client=client, payloads=Payloads
    )
    remote._execute = lambda payload: client.calls.append(payload) or {}
    original = {
        "timestamp_ms": 10,
        "timezone_offset": 0,
        "metadata": {
            "description": None,
            "favorite": False,
            "archived": False,
            "latitude": None,
            "longitude": None,
            "albums": [{"id": "shared-album", "title": "Family", "shared": True}],
        },
    }

    remote.restore_metadata(original, {"id": "replacement", "dedup_key": "new-dedup"})

    shared_calls = [call for call in client.calls if isinstance(call, Payloads.AddItemsToExistingSharedAlbum)]
    regular_calls = [call for call in client.calls if isinstance(call, Payloads.AddItemsToExistingAlbum)]
    assert len(shared_calls) == 1
    assert shared_calls[0].keys == ["replacement"]
    assert shared_calls[0].album_id == "shared-album"
    assert regular_calls == []


def test_restore_metadata_still_refuses_shared_album_when_skipped(tmp_path):
    client = Client()
    remote = GooglePhotosRemote(settings(tmp_path), client=client, payloads=Payloads)
    original = {
        "timestamp_ms": 10,
        "timezone_offset": 0,
        "metadata": {
            "description": None,
            "favorite": False,
            "archived": False,
            "latitude": None,
            "longitude": None,
            "albums": [{"id": "shared-album", "title": "Family", "shared": True}],
        },
    }

    with pytest.raises(RemoteProtocolError, match="shared album association"):
        remote.restore_metadata(original, {"id": "replacement", "dedup_key": "new-dedup"})
    assert client.calls == []


def _shared_album_item(*, source="upload", is_owned=True):
    info = SimpleNamespace(
        media_key="media",
        dedup_key="dedup",
        video_duration=None,
        download_original_url="https://example.test/original",
        is_favorite=False,
        is_archived=False,
        is_partial_upload=False,
        live_photo_duration=None,
        space_taken=123,
    )
    ext = SimpleNamespace(
        media_key="media",
        dedup_key="dedup",
        file_name="photo.jpg",
        size=123,
        res_width=100,
        res_height=100,
        timestamp=1700000000000,
        timezone_offset=0,
        description_full=None,
        geo_location=None,
        albums=[
            SimpleNamespace(media_key="shared", title="Shared", is_shared=True),
            SimpleNamespace(media_key="regular", title="Regular", is_shared=False),
        ],
        source=[source],
        saved_to_your_photos=True,
        owner=None,
        space_taken=123,
    )
    library_item = SimpleNamespace(is_owned=is_owned, is_partial_upload=False, live_photo_duration=None)
    return info, ext, library_item


@pytest.mark.parametrize("skip_shared, expected_skip", [(False, None), (True, "shared album association")])
def test_owned_upload_in_shared_album_follows_skip_shared(skip_shared, expected_skip):
    remote = GooglePhotosRemote({"run": {"skip_shared": skip_shared}}, payloads=Payloads, client=Client())
    info, ext, library_item = _shared_album_item()

    item = remote._convert_item(info, ext, library_item)

    assert item["skip_reason"] == expected_skip


@pytest.mark.parametrize("source, is_owned", [("shared", True), ("partnerShared", True), ("upload", False)])
def test_shared_or_unowned_sources_remain_ineligible_even_when_shared_albums_enabled(source, is_owned):
    remote = GooglePhotosRemote({"run": {"skip_shared": False}}, payloads=Payloads, client=Client())
    info, ext, library_item = _shared_album_item(source=source, is_owned=is_owned)

    item = remote._convert_item(info, ext, library_item)

    assert item["skip_reason"] == "shared item"


def test_restore_metadata_selects_shared_and_regular_album_payloads(tmp_path):
    client = Client()
    remote = GooglePhotosRemote(
        {**settings(tmp_path), "run": {"skip_shared": False}}, client=client, payloads=Payloads
    )
    remote._execute = lambda payload: client.calls.append(payload) or {}
    original = {
        "timestamp_ms": 10,
        "timezone_offset": 0,
        "metadata": {
            "description": None,
            "favorite": False,
            "archived": False,
            "latitude": None,
            "longitude": None,
            "albums": [
                {"id": "shared-album", "title": "Shared", "shared": True},
                {"id": "regular-album", "title": "Regular", "shared": False},
            ],
        },
    }

    remote.restore_metadata(original, {"id": "replacement", "dedup_key": "new-dedup"})

    album_calls = [
        call
        for call in client.calls
        if isinstance(call, (Payloads.AddItemsToExistingAlbum, Payloads.AddItemsToExistingSharedAlbum))
    ]
    assert [type(call) for call in album_calls] == [
        Payloads.AddItemsToExistingSharedAlbum,
        Payloads.AddItemsToExistingAlbum,
    ]
