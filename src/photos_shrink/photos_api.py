"""Official Google Photos Library API client for uploading and verifying.

This is the supported path, and it replaces scraped cookies for everything the
API is allowed to do: uploading bytes, creating media items and albums, and
reading back the items this app itself created. It cannot delete, and it cannot
see items it did not create -- those remain on the browser adapter.

Authentication is OAuth with a stored refresh token rather than an exported
browser session, so it survives across runs instead of expiring in minutes.

Note on refresh-token lifetime: while the Cloud Console OAuth consent screen is
in "Testing", Google expires refresh tokens after seven days and this client
will report `invalid_grant`. Publishing the consent screen to "In production"
removes that limit. See README.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import requests

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
API_ROOT = "https://photoslibrary.googleapis.com/v1"
UPLOAD_ENDPOINT = f"{API_ROOT}/uploads"

SCOPES = (
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
)

# The API accepts these but mimetypes does not always know them.
EXTRA_TYPES = {
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mov": "video/quicktime",
    ".mts": "video/mp2t",
}

# Documented API ceilings; exceeding them wastes a full upload.
MAX_PHOTO_BYTES = 200 * 1024 * 1024
MAX_VIDEO_BYTES = 20 * 1024 * 1024 * 1024


class PhotosApiError(RuntimeError):
    """Raised when the Photos API cannot be used safely."""


def load_client_credentials(work_dir: str | os.PathLike[str]) -> tuple[str, str]:
    """Find the OAuth client id and secret.

    The environment wins so a shell can override, but the setup wizard writes
    them beside the other private state so ordinary runs need no exported
    variables at all.
    """

    client_id = os.environ.get("PHOTOS_API_CLIENT_ID", "").strip()
    client_secret = os.environ.get("PHOTOS_API_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return client_id, client_secret

    path = Path(work_dir) / "api-client.env"
    if path.exists():
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
        client_id = client_id or values.get("PHOTOS_API_CLIENT_ID", "")
        client_secret = client_secret or values.get("PHOTOS_API_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        raise PhotosApiError(
            "no OAuth client credentials found. Run tools/setup_google_api.sh, "
            "or set PHOTOS_API_CLIENT_ID and PHOTOS_API_CLIENT_SECRET."
        )
    return client_id, client_secret


class ReauthorizationRequired(PhotosApiError):
    """Raised when the stored refresh token is no longer accepted."""


def content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in EXTRA_TYPES:
        return EXTRA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    if not guessed:
        raise PhotosApiError(f"cannot determine a media type for {path.name}")
    return guessed


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches the single loopback redirect carrying the authorization code."""

    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).code = (query.get("code") or [None])[0]
        type(self).state = (query.get("state") or [None])[0]
        type(self).error = (query.get("error") or [None])[0]
        body = b"<html><body><h2>Authorization received. You can close this tab.</h2></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Silence the default stderr access log."""


class PhotosApiClient:
    """Uploads to Google Photos over the official API using a stored refresh token."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: str | os.PathLike[str],
        *,
        session: Any | None = None,
        timeout: float = 180.0,
    ):
        if not client_id or not client_secret:
            raise PhotosApiError("an OAuth client id and secret are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = Path(token_path)
        self.session = session or requests.Session()
        self.timeout = timeout
        self._access_token: str | None = None
        self._expires_at = 0.0

    # -- authorization ---------------------------------------------------

    def _store(self, refresh_token: str) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(
            json.dumps({"refresh_token": refresh_token}, indent=2), encoding="utf-8"
        )
        try:  # Best effort: the token grants account access.
            os.chmod(self.token_path, 0o600)
        except OSError:
            pass

    def _stored_refresh_token(self) -> str | None:
        if not self.token_path.exists():
            return None
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PhotosApiError(f"unreadable token file: {self.token_path}") from exc
        token = data.get("refresh_token") if isinstance(data, dict) else None
        return token if isinstance(token, str) and token else None

    def authorize(self, *, open_browser: bool = True, port: int = 0) -> str:
        """Run the one-time consent flow and store the refresh token.

        Uses a loopback redirect with PKCE. Returns the refresh token.
        """

        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        state = secrets.token_urlsafe(24)

        server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
        redirect_uri = f"http://127.0.0.1:{server.server_port}"
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            # Without this an already-consented account returns no refresh token.
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        url = f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"
        print(f"Open this URL to authorize:\n\n{url}\n", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.handle_request()
        finally:
            server.server_close()

        if _CallbackHandler.error:
            raise PhotosApiError(f"authorization was refused: {_CallbackHandler.error}")
        if not _CallbackHandler.code:
            raise PhotosApiError("no authorization code was received")
        if _CallbackHandler.state != state:
            raise PhotosApiError("authorization state mismatch; the response is not trusted")

        response = self.session.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": _CallbackHandler.code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=self.timeout,
        )
        payload = self._json(response, "token exchange")
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise PhotosApiError(
                "Google returned no refresh token. Revoke the app's access and retry "
                "so consent is requested again."
            )
        self._store(refresh_token)
        self._access_token = payload.get("access_token")
        self._expires_at = time.monotonic() + float(payload.get("expires_in", 0)) - 60
        return refresh_token

    def access_token(self) -> str:
        """Return a valid access token, refreshing it when required."""

        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token
        refresh_token = self._stored_refresh_token()
        if refresh_token is None:
            raise ReauthorizationRequired(
                f"no stored credentials at {self.token_path}; run the setup tool first"
            )
        response = self.session.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=self.timeout,
        )
        if response.status_code == 400:
            detail = ""
            try:
                detail = (response.json() or {}).get("error", "")
            except ValueError:
                pass
            if detail == "invalid_grant":
                raise ReauthorizationRequired(
                    "the stored refresh token was rejected (invalid_grant). If the "
                    "OAuth consent screen is still in Testing, Google expires refresh "
                    "tokens after seven days; publish it to In production, then "
                    "authorize again."
                )
        payload = self._json(response, "token refresh")
        token = payload.get("access_token")
        if not token:
            raise PhotosApiError("token refresh returned no access token")
        self._access_token = token
        self._expires_at = time.monotonic() + float(payload.get("expires_in", 0)) - 60
        return token

    # -- api calls -------------------------------------------------------

    def _json(self, response: Any, what: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise PhotosApiError(f"{what} returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise PhotosApiError(f"{what} returned an unexpected response")
        if response.status_code >= 400:
            message = (payload.get("error") or {})
            if isinstance(message, dict):
                message = message.get("message") or message.get("status") or ""
            raise PhotosApiError(f"{what} failed ({response.status_code}): {message}")
        return payload

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token()}", **extra}

    def upload_bytes(self, path: str | os.PathLike[str]) -> str:
        """Upload raw bytes and return the resulting upload token."""

        media = Path(path)
        size = media.stat().st_size
        mime = content_type(media)
        limit = MAX_VIDEO_BYTES if mime.startswith("video/") else MAX_PHOTO_BYTES
        if size > limit:
            raise PhotosApiError(f"{media.name} is {size} bytes, over the API limit of {limit}")

        with open(media, "rb") as handle:
            response = self.session.post(
                UPLOAD_ENDPOINT,
                data=handle,
                headers=self._headers(
                    **{
                        "Content-type": "application/octet-stream",
                        "X-Goog-Upload-Content-Type": mime,
                        "X-Goog-Upload-Protocol": "raw",
                    }
                ),
                timeout=self.timeout,
            )
        if response.status_code >= 400:
            raise PhotosApiError(f"upload failed ({response.status_code}) for {media.name}")
        token = (response.text or "").strip()
        if not token:
            raise PhotosApiError(f"upload of {media.name} returned no upload token")
        return token

    def create_media_item(
        self,
        upload_token: str,
        filename: str,
        *,
        description: str | None = None,
        album_id: str | None = None,
    ) -> dict[str, Any]:
        """Turn an upload token into a library item."""

        item: dict[str, Any] = {"simpleMediaItem": {"uploadToken": upload_token, "fileName": filename}}
        if description:
            item["description"] = description
        body: dict[str, Any] = {"newMediaItems": [item]}
        if album_id:
            body["albumId"] = album_id

        response = self.session.post(
            f"{API_ROOT}/mediaItems:batchCreate",
            headers=self._headers(**{"Content-type": "application/json"}),
            json=body,
            timeout=self.timeout,
        )
        payload = self._json(response, "media item creation")
        results = payload.get("newMediaItemResults") or []
        if not results:
            raise PhotosApiError(f"no media item was created for {filename}")
        result = results[0]
        status = result.get("status") or {}
        # A zero or absent code means OK; anything else failed for this item.
        if status.get("code") not in (None, 0):
            raise PhotosApiError(
                f"creating {filename} failed: {status.get('message') or status.get('code')}"
            )
        created = result.get("mediaItem")
        if not isinstance(created, dict) or not created.get("id"):
            raise PhotosApiError(f"creating {filename} returned no media item")
        return created

    def get_media_item(self, media_item_id: str) -> dict[str, Any]:
        """Read back an item this app created."""

        response = self.session.get(
            f"{API_ROOT}/mediaItems/{media_item_id}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        return self._json(response, "media item lookup")

    def create_album(self, title: str) -> dict[str, Any]:
        response = self.session.post(
            f"{API_ROOT}/albums",
            headers=self._headers(**{"Content-type": "application/json"}),
            json={"album": {"title": title}},
            timeout=self.timeout,
        )
        return self._json(response, "album creation")

    def upload(
        self,
        path: str | os.PathLike[str],
        *,
        description: str | None = None,
        album_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload a file and create its library item in one step."""

        media = Path(path)
        token = self.upload_bytes(media)
        return self.create_media_item(
            token, media.name, description=description, album_id=album_id
        )
