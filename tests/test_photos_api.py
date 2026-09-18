"""Tests for the official Google Photos Library API client."""

from __future__ import annotations

import json

import pytest

from photos_shrink.photos_api import (
    PhotosApiClient,
    PhotosApiError,
    ReauthorizationRequired,
    content_type,
    load_client_credentials,
)


class Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class Session:
    """Records calls and replays queued responses."""

    def __init__(self):
        self.posts = []
        self.gets = []
        self.post_responses = []
        self.get_responses = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.post_responses.pop(0)

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.get_responses.pop(0)


def client(tmp_path, session, *, refresh_token="stored-refresh"):
    token_path = tmp_path / "api-token.json"
    if refresh_token is not None:
        token_path.write_text(json.dumps({"refresh_token": refresh_token}), encoding="utf-8")
    return PhotosApiClient("id", "secret", token_path, session=session)


def access_ok():
    return Response(200, {"access_token": "fresh-access", "expires_in": 3600})


class TestContentType:
    @pytest.mark.parametrize(
        "name,expected",
        [("a.avif", "image/avif"), ("a.heic", "image/heic"), ("a.MOV", "video/quicktime")],
    )
    def test_formats_the_api_accepts_but_mimetypes_may_not_know(self, tmp_path, name, expected):
        assert content_type(tmp_path / name) == expected

    def test_common_types_still_resolve(self, tmp_path):
        assert content_type(tmp_path / "a.jpg") == "image/jpeg"

    def test_unknown_type_is_refused(self, tmp_path):
        with pytest.raises(PhotosApiError):
            content_type(tmp_path / "a.unknownext")


class TestCredentials:
    def test_missing_client_credentials_are_refused(self, tmp_path):
        with pytest.raises(PhotosApiError):
            PhotosApiClient("", "", tmp_path / "t.json")

    def test_missing_token_file_asks_for_setup(self, tmp_path):
        api = client(tmp_path, Session(), refresh_token=None)
        with pytest.raises(ReauthorizationRequired, match="setup"):
            api.access_token()

    def test_refresh_exchanges_the_stored_token(self, tmp_path):
        session = Session()
        session.post_responses = [access_ok()]
        api = client(tmp_path, session)
        assert api.access_token() == "fresh-access"
        _, kwargs = session.posts[0]
        assert kwargs["data"]["grant_type"] == "refresh_token"
        assert kwargs["data"]["refresh_token"] == "stored-refresh"

    def test_access_token_is_cached_until_expiry(self, tmp_path):
        session = Session()
        session.post_responses = [access_ok()]
        api = client(tmp_path, session)
        api.access_token()
        api.access_token()
        assert len(session.posts) == 1

    def test_invalid_grant_explains_the_seven_day_testing_limit(self, tmp_path):
        session = Session()
        session.post_responses = [Response(400, {"error": "invalid_grant"})]
        api = client(tmp_path, session)
        with pytest.raises(ReauthorizationRequired, match="seven days"):
            api.access_token()

    def test_unreadable_token_file_is_reported(self, tmp_path):
        path = tmp_path / "api-token.json"
        path.write_text("not json", encoding="utf-8")
        api = PhotosApiClient("id", "secret", path, session=Session())
        with pytest.raises(PhotosApiError, match="unreadable"):
            api.access_token()


class TestUpload:
    def test_upload_sends_the_documented_headers(self, tmp_path):
        media = tmp_path / "photo.avif"
        media.write_bytes(b"bytes")
        session = Session()
        session.post_responses = [access_ok(), Response(200, text="upload-token-123")]
        api = client(tmp_path, session)

        assert api.upload_bytes(media) == "upload-token-123"
        url, kwargs = session.posts[1]
        assert url.endswith("/v1/uploads")
        headers = kwargs["headers"]
        assert headers["X-Goog-Upload-Protocol"] == "raw"
        assert headers["X-Goog-Upload-Content-Type"] == "image/avif"
        assert headers["Content-type"] == "application/octet-stream"
        assert headers["Authorization"] == "Bearer fresh-access"

    def test_empty_upload_token_is_refused(self, tmp_path):
        media = tmp_path / "photo.jpg"
        media.write_bytes(b"bytes")
        session = Session()
        session.post_responses = [access_ok(), Response(200, text="   ")]
        api = client(tmp_path, session)
        with pytest.raises(PhotosApiError, match="no upload token"):
            api.upload_bytes(media)

    def test_oversized_photo_is_refused_before_uploading(self, tmp_path):
        media = tmp_path / "huge.jpg"
        media.write_bytes(b"x")
        session = Session()
        api = client(tmp_path, session)
        import photos_shrink.photos_api as module

        original = module.MAX_PHOTO_BYTES
        module.MAX_PHOTO_BYTES = 0
        try:
            with pytest.raises(PhotosApiError, match="over the API limit"):
                api.upload_bytes(media)
        finally:
            module.MAX_PHOTO_BYTES = original
        assert session.posts == [], "no bytes should be sent for an oversized file"


class TestCreateMediaItem:
    def test_creates_and_returns_the_item(self, tmp_path):
        session = Session()
        session.post_responses = [
            access_ok(),
            Response(200, {"newMediaItemResults": [{"status": {}, "mediaItem": {"id": "new-1"}}]}),
        ]
        api = client(tmp_path, session)
        created = api.create_media_item("tok", "photo.avif")
        assert created["id"] == "new-1"
        _, kwargs = session.posts[1]
        item = kwargs["json"]["newMediaItems"][0]["simpleMediaItem"]
        assert item == {"uploadToken": "tok", "fileName": "photo.avif"}
        assert "albumId" not in kwargs["json"]

    def test_album_is_passed_when_given(self, tmp_path):
        session = Session()
        session.post_responses = [
            access_ok(),
            Response(200, {"newMediaItemResults": [{"status": {}, "mediaItem": {"id": "x"}}]}),
        ]
        api = client(tmp_path, session)
        api.create_media_item("tok", "p.avif", album_id="album-9")
        assert session.posts[1][1]["json"]["albumId"] == "album-9"

    def test_per_item_failure_status_is_raised(self, tmp_path):
        session = Session()
        session.post_responses = [
            access_ok(),
            Response(200, {"newMediaItemResults": [{"status": {"code": 3, "message": "bad token"}}]}),
        ]
        api = client(tmp_path, session)
        with pytest.raises(PhotosApiError, match="bad token"):
            api.create_media_item("tok", "p.avif")

    def test_empty_results_are_refused(self, tmp_path):
        session = Session()
        session.post_responses = [access_ok(), Response(200, {"newMediaItemResults": []})]
        api = client(tmp_path, session)
        with pytest.raises(PhotosApiError, match="no media item"):
            api.create_media_item("tok", "p.avif")

    def test_api_error_payload_is_surfaced(self, tmp_path):
        session = Session()
        session.post_responses = [
            access_ok(),
            Response(403, {"error": {"message": "insufficient scope"}}),
        ]
        api = client(tmp_path, session)
        with pytest.raises(PhotosApiError, match="insufficient scope"):
            api.create_media_item("tok", "p.avif")


class TestReadBack:
    def test_get_media_item_reads_app_created_data(self, tmp_path):
        session = Session()
        session.post_responses = [access_ok()]
        session.get_responses = [Response(200, {"id": "new-1", "filename": "photo.avif"})]
        api = client(tmp_path, session)
        assert api.get_media_item("new-1")["filename"] == "photo.avif"
        url, kwargs = session.gets[0]
        assert url.endswith("/v1/mediaItems/new-1")
        assert kwargs["headers"]["Authorization"] == "Bearer fresh-access"

    def test_a_quota_failure_carries_its_status(self, tmp_path):
        """A caller re-checking many items has to tell a per-item failure from
        an account-wide one: past a 429 every later lookup fails too."""

        session = Session()
        session.post_responses = [access_ok()]
        session.get_responses = [Response(429, {"error": {"message": "Quota exceeded"}})]
        api = client(tmp_path, session)
        with pytest.raises(PhotosApiError, match="Quota exceeded") as caught:
            api.get_media_item("new-1")
        assert caught.value.status == 429


class TestLoadClientCredentials:
    def test_environment_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHOTOS_API_CLIENT_ID", "env-id")
        monkeypatch.setenv("PHOTOS_API_CLIENT_SECRET", "env-secret")
        (tmp_path / "api-client.env").write_text(
            "PHOTOS_API_CLIENT_ID=file-id\nPHOTOS_API_CLIENT_SECRET=file-secret\n", encoding="utf-8"
        )
        assert load_client_credentials(tmp_path) == ("env-id", "env-secret")

    def test_falls_back_to_the_wizard_written_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PHOTOS_API_CLIENT_ID", raising=False)
        monkeypatch.delenv("PHOTOS_API_CLIENT_SECRET", raising=False)
        (tmp_path / "api-client.env").write_text(
            "# written by the setup wizard\n"
            "PHOTOS_API_CLIENT_ID=file-id.apps.googleusercontent.com\n"
            "PHOTOS_API_CLIENT_SECRET='file-secret'\n",
            encoding="utf-8",
        )
        assert load_client_credentials(tmp_path) == (
            "file-id.apps.googleusercontent.com",
            "file-secret",
        )

    def test_missing_credentials_point_at_the_wizard(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PHOTOS_API_CLIENT_ID", raising=False)
        monkeypatch.delenv("PHOTOS_API_CLIENT_SECRET", raising=False)
        with pytest.raises(PhotosApiError, match="setup_google_api"):
            load_client_credentials(tmp_path)


class TestTransientRetry:
    def test_a_transient_conflict_is_retried_and_succeeds(self, tmp_path, monkeypatch):
        """A 409 "operation was aborted" appeared about once per 500 live uploads."""

        monkeypatch.setattr("photos_shrink.photos_api.time.sleep", lambda _s: None)
        session = Session()
        session.post_responses = [
            access_ok(),
            Response(409, {"error": {"message": "The operation was aborted."}}),
            Response(200, {"newMediaItemResults": [{"status": {}, "mediaItem": {"id": "new-1"}}]}),
        ]
        api = client(tmp_path, session)
        assert api.create_media_item("tok", "p.avif")["id"] == "new-1"

    def test_the_same_upload_token_is_resent_so_no_duplicate_is_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr("photos_shrink.photos_api.time.sleep", lambda _s: None)
        session = Session()
        session.post_responses = [
            access_ok(),
            Response(503, {"error": {"message": "backend unavailable"}}),
            Response(200, {"newMediaItemResults": [{"status": {}, "mediaItem": {"id": "new-1"}}]}),
        ]
        api = client(tmp_path, session)
        api.create_media_item("tok", "p.avif")
        tokens = [
            call[1]["json"]["newMediaItems"][0]["simpleMediaItem"]["uploadToken"]
            for call in session.posts[1:]
        ]
        assert tokens == ["tok", "tok"]

    def test_a_real_rejection_is_not_retried(self, tmp_path, monkeypatch):
        monkeypatch.setattr("photos_shrink.photos_api.time.sleep", lambda _s: None)
        session = Session()
        session.post_responses = [access_ok(), Response(403, {"error": {"message": "insufficient scope"}})]
        api = client(tmp_path, session)
        with pytest.raises(PhotosApiError, match="insufficient scope"):
            api.create_media_item("tok", "p.avif")
        assert len(session.posts) == 2, "a permission failure must not be retried"

    def test_retries_are_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr("photos_shrink.photos_api.time.sleep", lambda _s: None)
        session = Session()
        session.post_responses = [access_ok()] + [Response(503, {"error": {}})] * 10
        api = client(tmp_path, session)
        with pytest.raises(PhotosApiError):
            api.create_media_item("tok", "p.avif")
        assert len(session.posts) == 1 + 4
