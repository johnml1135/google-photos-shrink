from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from photos_shrink.auth import (
    load_netscape_cookies,
)
from photos_shrink.remote import (
    GooglePhotosRemote,
    RemoteProtocolError,
    SessionRefreshError,
)


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


def test_login_recovers_from_existing_browser_profile_without_overwriting_seed(tmp_path):
    configured = settings(tmp_path)
    cookie_file = Path(configured["google"]["cookies_file"])
    cookie_file.write_text("manual seed\n", encoding="utf-8")
    profile = Path(configured["google"]["browser_profile"])
    profile.mkdir()
    created_from = []

    class Context:
        def cookies(self):
            return [{"name": "SID", "value": "fresh", "domain": ".google.com", "path": "/"}]

    browser_profile = profile

    class Browser:
        profile = browser_profile
        context = Context()

        def open(self, *, interactive=False, seed_cookies=True):
            assert interactive is False
            assert seed_cookies is False
            return "stable-account"

    def factory(path, **kwargs):
        temp_path = Path(path)
        if temp_path == cookie_file:
            raise ValueError("expired seed")
        assert temp_path != cookie_file
        assert temp_path.is_file()
        created_from.append((temp_path, load_netscape_cookies(temp_path)))
        client = Client()
        client.cookies_txt_path = temp_path
        return client

    remote = GooglePhotosRemote(configured, client_factory=factory, payloads=Payloads, browser=Browser())
    assert remote.login() == "stable-account"
    assert cookie_file.read_text(encoding="utf-8") == "manual seed\n"
    assert len(created_from) == 1
    assert not created_from[0][0].exists()
    assert remote._client.cookies_txt_path == cookie_file
    assert remote._browser is not None


def test_login_profile_recovery_rejects_account_mismatch_and_cleans_temp(tmp_path):
    configured = settings(tmp_path)
    cookie_file = Path(configured["google"]["cookies_file"])
    cookie_file.write_text("manual seed\n", encoding="utf-8")
    profile = Path(configured["google"]["browser_profile"])
    profile.mkdir()
    temp_paths = []

    class Context:
        def cookies(self):
            return [{"name": "SID", "value": "fresh", "domain": ".google.com", "path": "/"}]

    browser_profile = profile

    class Browser:
        profile = browser_profile
        context = Context()

        def open(self, *, interactive=False, seed_cookies=True):
            return "browser-account"

    def factory(path, **kwargs):
        if Path(path) == cookie_file:
            raise ValueError("expired seed")
        temp_paths.append(Path(path))
        client = Client()
        client.global_data["oPEP7c"] = "other-account"
        return client

    remote = GooglePhotosRemote(configured, client_factory=factory, payloads=Payloads, browser=Browser())
    with pytest.raises(RemoteProtocolError, match="different accounts"):
        remote.login()
    assert cookie_file.read_text(encoding="utf-8") == "manual seed\n"
    assert len(temp_paths) == 1
    assert not temp_paths[0].exists()


def test_login_does_not_use_profile_recovery_when_refresh_is_disabled(tmp_path):
    configured = settings(tmp_path)
    configured["google"]["session_refresh_seconds"] = 0
    profile = Path(configured["google"]["browser_profile"])
    profile.mkdir()
    browser_calls = []

    browser_profile = profile

    class Browser:
        profile = browser_profile

        def open(self, **kwargs):
            browser_calls.append(kwargs)
            return "stable-account"

    remote = GooglePhotosRemote(configured, client_factory=lambda *args, **kwargs: Client(), payloads=Payloads, browser=Browser())
    with pytest.raises(RemoteProtocolError, match="cookies.txt"):
        remote.login()
    assert browser_calls == []


def test_login_profile_recovery_fails_if_temporary_cookie_export_cannot_be_removed(tmp_path, monkeypatch):
    configured = settings(tmp_path)
    cookie_file = Path(configured["google"]["cookies_file"])
    cookie_file.write_text("manual seed\n", encoding="utf-8")
    profile = Path(configured["google"]["browser_profile"])
    profile.mkdir()
    browser_profile = profile
    temp_paths = []

    class Context:
        def cookies(self):
            return [{"name": "SID", "value": "fresh", "domain": ".google.com", "path": "/"}]

    class Browser:
        profile = browser_profile
        context = Context()

        def open(self, **kwargs):
            return "stable-account"

    def factory(path, **kwargs):
        if Path(path) == cookie_file:
            raise ValueError("expired seed")
        temp_paths.append(Path(path))
        return Client()

    original_unlink = Path.unlink

    def fail_temp_unlink(path, *args, **kwargs):
        if path.name.startswith("photos-shrink-browser-"):
            raise OSError("busy")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temp_unlink)
    remote = GooglePhotosRemote(configured, client_factory=factory, payloads=Payloads, browser=Browser())
    with pytest.raises(RemoteProtocolError, match="temporary browser cookie export"):
        remote.login()
    assert len(temp_paths) == 1
    original_unlink(temp_paths[0])


class GetItemInfo:
    rpcid = "refresh-read"


class MutatingPayload:
    rpcid = "refresh-write"


class RefreshSession:
    def __init__(self):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set("SID", "stale", domain=".google.com", path="/")
        self.cookies.set("OLD", "stale", domain="accounts.google.com", path="/")
        self.cookies.set("keep", "value", domain="example.test", path="/")


class RefreshClient(Client):
    def __init__(self):
        super().__init__()
        self.session = RefreshSession()
        self.global_data = {
            "oPEP7c": "stable-account",
            "FdrFJe": "old-fsid",
            "cfb2h": "old-bl",
            "SNlM0e": "old-at",
            "Im6cmf": "/_ /old".replace(" ", ""),
        }
        self.global_calls = 0

    def get_global_data(self):
        self.global_calls += 1
        return {
            "oPEP7c": "stable-account",
            "FdrFJe": "new-fsid",
            "cfb2h": "new-bl",
            "SNlM0e": "new-at",
            "Im6cmf": "/_ /new".replace(" ", ""),
        }


def test_refresh_propagates_browser_google_cookies_and_request_tokens(tmp_path):
    class Browser:
        def refresh_session(self, expected_account):
            assert expected_account == "stable-account"
            return [{"name": "SID", "value": "fresh", "domain": ".google.com", "path": "/"}]

    client = RefreshClient()
    remote = GooglePhotosRemote(settings(tmp_path), client=client, payloads=Payloads, browser=Browser())
    remote.refresh_session()

    assert client.session.cookies.get("SID", domain=".google.com", path="/") == "fresh"
    assert client.session.cookies.get("OLD", domain="accounts.google.com", path="/") is None
    assert client.session.cookies.get("keep", domain="example.test", path="/") == "value"
    assert client.global_data["FdrFJe"] == "new-fsid"
    assert client.global_data["SNlM0e"] == "new-at"


def test_refresh_rolls_back_cookiejar_and_global_data_on_identity_mismatch(tmp_path):
    class Browser:
        def refresh_session(self, expected_account):
            return [{"name": "SID", "value": "fresh", "domain": ".google.com", "path": "/"}]

    client = RefreshClient()
    client.get_global_data = lambda: {
        "oPEP7c": "other-account",
        "FdrFJe": "new-fsid",
        "cfb2h": "new-bl",
        "SNlM0e": "new-at",
        "Im6cmf": "/new",
    }
    old_global = dict(client.global_data)
    remote = GooglePhotosRemote(settings(tmp_path), client=client, payloads=Payloads, browser=Browser())
    with pytest.raises(RemoteProtocolError, match="account"):
        remote.refresh_session()

    assert dict(client.global_data) == old_global
    assert client.session.cookies.get("SID", domain=".google.com", path="/") == "stale"


def test_execute_periodically_refreshes_before_read(tmp_path, monkeypatch):
    client = Client()
    remote = GooglePhotosRemote(
        {**settings(tmp_path), "google": {**settings(tmp_path)["google"], "session_refresh_seconds": 10}},
        client=client,
        payloads=Payloads,
    )
    remote._last_refresh_monotonic = 0
    now = [100.0]
    monkeypatch.setattr("photos_shrink.remote.time.monotonic", lambda: now[0])
    calls = []

    def refresh():
        calls.append("refresh")
        remote._last_refresh_monotonic = now[0]

    monkeypatch.setattr(remote, "refresh_session", refresh)
    client.responses[GetItemInfo] = type("R", (), {"success": True, "data": object()})()
    remote._execute(GetItemInfo())
    assert calls == ["refresh"]


def test_execute_retries_a_failed_read_once_after_refresh(tmp_path, monkeypatch):
    client = Client()
    remote = GooglePhotosRemote(settings(tmp_path), client=client, payloads=Payloads)
    calls = []

    def send(payload):
        calls.append(type(payload))
        if len(calls) == 1:
            raise RuntimeError("temporary")
        return type("R", (), {"success": True, "data": object()})()

    client.send_api_request = send
    refreshes = []
    monkeypatch.setattr(remote, "refresh_session", lambda: refreshes.append(True))
    assert remote._execute(GetItemInfo()) is not None
    assert len(calls) == 2
    assert refreshes == [True]


def test_execute_does_not_retry_mutating_request(tmp_path, monkeypatch):
    client = Client()
    remote = GooglePhotosRemote(settings(tmp_path), client=client, payloads=Payloads)
    calls = []

    def send(payload):
        calls.append(payload)
        raise RuntimeError("temporary")

    client.send_api_request = send
    refreshes = []
    monkeypatch.setattr(remote, "refresh_session", lambda: refreshes.append(True))
    with pytest.raises(RemoteProtocolError, match="refresh-write"):
        remote._execute(MutatingPayload())
    assert len(calls) == 1
    assert refreshes == []


def test_zero_refresh_interval_disables_failure_retry(tmp_path, monkeypatch):
    configured = settings(tmp_path)
    configured["google"]["session_refresh_seconds"] = 0
    client = Client()
    remote = GooglePhotosRemote(configured, client=client, payloads=Payloads)
    calls = []

    def send(payload):
        calls.append(payload)
        raise RuntimeError("temporary")

    client.send_api_request = send
    refreshes = []
    monkeypatch.setattr(remote, "refresh_session", lambda: refreshes.append(True))
    with pytest.raises(RemoteProtocolError):
        remote._execute(GetItemInfo())
    assert len(calls) == 1
    assert refreshes == []


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


def test_periodic_refresh_failure_does_not_abort_a_working_session(tmp_path, monkeypatch):
    """A timed refresh is opportunistic: losing it must not kill a healthy run."""

    client = Client()
    remote = GooglePhotosRemote(
        {**settings(tmp_path), "google": {**settings(tmp_path)["google"], "session_refresh_seconds": 10}},
        client=client,
        payloads=Payloads,
    )
    remote._last_refresh_monotonic = 0
    monkeypatch.setattr("photos_shrink.remote.time.monotonic", lambda: 100.0)

    def failing_refresh():
        raise SessionRefreshError("browser profile is not authenticated")

    monkeypatch.setattr(remote, "refresh_session", failing_refresh)
    client.responses[GetItemInfo] = type("R", (), {"success": True, "data": object()})()

    assert remote._execute(GetItemInfo()) is not None


def test_periodic_refresh_failure_backs_off_instead_of_retrying_every_request(tmp_path, monkeypatch):
    client = Client()
    remote = GooglePhotosRemote(
        {**settings(tmp_path), "google": {**settings(tmp_path)["google"], "session_refresh_seconds": 10}},
        client=client,
        payloads=Payloads,
    )
    remote._last_refresh_monotonic = 0
    monkeypatch.setattr("photos_shrink.remote.time.monotonic", lambda: 100.0)
    attempts = []

    def failing_refresh():
        attempts.append(True)
        raise SessionRefreshError("browser profile is not authenticated")

    monkeypatch.setattr(remote, "refresh_session", failing_refresh)
    client.responses[GetItemInfo] = type("R", (), {"success": True, "data": object()})()

    remote._execute(GetItemInfo())
    remote._execute(GetItemInfo())
    assert len(attempts) == 1, "a failed timed refresh must not be retried on every request"


def test_failed_retry_refresh_reports_the_original_read_failure(tmp_path, monkeypatch):
    """Diagnostics must name the request that failed, not the refresh."""

    client = Client()
    remote = GooglePhotosRemote(settings(tmp_path), client=client, payloads=Payloads)

    def send(payload):
        raise RuntimeError("temporary")

    client.send_api_request = send

    def failing_refresh():
        raise SessionRefreshError("browser profile is not authenticated")

    monkeypatch.setattr(remote, "refresh_session", failing_refresh)
    with pytest.raises(RemoteProtocolError, match="refresh-read"):
        remote._execute(GetItemInfo())


class _SessionSettings:
    """Minimal stand-in for Settings: open_session only needs as_dict()."""

    def __init__(self, payload):
        self._payload = payload

    def as_dict(self):
        return self._payload


def test_open_session_closes_the_remote_even_when_the_body_raises(tmp_path, monkeypatch):
    from photos_shrink import remote as remote_module

    closed = []

    class FakeRemote:
        def __init__(self, payload):
            self.payload = payload

        def login(self):
            return "stable-account"

        def close(self):
            closed.append(True)

    monkeypatch.setattr(remote_module, "GooglePhotosRemote", FakeRemote)
    with pytest.raises(ValueError, match="boom"):
        with remote_module.open_session(_SessionSettings(settings(tmp_path))):
            raise ValueError("boom")
    assert closed == [True]


def test_open_session_closes_the_remote_when_login_fails(tmp_path, monkeypatch):
    """An expired cookie must not leak the session it failed to open."""

    from photos_shrink import remote as remote_module

    closed = []

    class FakeRemote:
        def __init__(self, payload):
            pass

        def login(self):
            raise RemoteProtocolError("cookies.txt is missing")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(remote_module, "GooglePhotosRemote", FakeRemote)
    with pytest.raises(RemoteProtocolError, match="cookies.txt is missing"):
        with remote_module.open_session(_SessionSettings(settings(tmp_path))):
            pass
    assert closed == [True]


def test_open_session_reports_an_unexpected_login_failure_as_a_protocol_error(tmp_path, monkeypatch):
    """Callers catch RemoteProtocolError; a raw browser error would escape them."""

    from photos_shrink import remote as remote_module

    class FakeRemote:
        def __init__(self, payload):
            pass

        def login(self):
            raise OSError("chrome would not start")

        def close(self):
            pass

    monkeypatch.setattr(remote_module, "GooglePhotosRemote", FakeRemote)
    with pytest.raises(RemoteProtocolError, match="chrome would not start"):
        with remote_module.open_session(_SessionSettings(settings(tmp_path))):
            pass


def test_open_session_logs_in_before_yielding(tmp_path, monkeypatch):
    """The body must never run against a session that was never logged in."""

    from photos_shrink import remote as remote_module

    order = []

    class FakeRemote:
        def __init__(self, payload):
            pass

        def login(self):
            order.append("login")
            return "stable-account"

        def close(self):
            order.append("close")

    monkeypatch.setattr(remote_module, "GooglePhotosRemote", FakeRemote)
    with remote_module.open_session(_SessionSettings(settings(tmp_path))):
        order.append("body")
    assert order == ["login", "body", "close"]


