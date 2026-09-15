"""Google Photos remote adapter: the browser-cookie half of the library.

Uploads go through the official API (`photos_api`), which cannot write album
membership or delete. Everything that needs those -- identifying an item by
content hash, restoring its metadata onto a replacement, and trashing the
original -- has to come through here, on an exported browser session.

gpwc is the read/metadata/trash client; Playwright refreshes the session when
the exported cookies go stale, which they do within about fifteen minutes.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import math
import mimetypes
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .auth import (
    BrowserAuthenticator,
    NetscapeCookie,
    write_netscape_cookies,
)
from .integrity import sha256_file


class RemoteProtocolError(RuntimeError):
    """Raised when an upstream response is missing information we need."""


class SessionRefreshError(RemoteProtocolError):
    """Raised when an authenticated browser/session refresh cannot be installed."""


def _asdict(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_asdict(item) for item in value]
    if isinstance(value, dict):
        return {key: _asdict(item) for key, item in value.items()}
    return value


class GooglePhotosRemote:
    def __init__(
        self,
        settings: dict[str, Any],
        *,
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
        payloads: Any | None = None,
        browser: BrowserAuthenticator | None = None,
    ) -> None:
        self.settings = settings
        self.google = settings.get("google", settings)
        self.skip_shared = bool(settings.get("run", {}).get("skip_shared", True))
        self.cookies_file = Path(self.google.get("cookies_file", ".photos-shrink/cookies.txt"))
        self.account_index = int(self.google.get("account_index", 0))
        self._client = client
        self._client_factory = client_factory
        self._payloads = payloads
        self._browser = browser
        self._library_context: dict[str, Any] = {}
        self._trusted_media: set[str] = set()
        self._last_refresh_monotonic = time.monotonic()
        self._session_refresh_seconds = self._refresh_interval()

    def _load_dependencies(self) -> None:
        if self._payloads is None:
            try:
                from gpwc import payloads
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RemoteProtocolError("gpwc is required for Google Photos access") from exc
            self._payloads = payloads
        if self._client_factory is None:
            try:
                from gpwc import Client
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RemoteProtocolError("gpwc is required for Google Photos access") from exc
            self._client_factory = Client

    def login(self, *, force: bool = False) -> str:
        """Load the gpwc session from Netscape cookies and return account ID."""

        self._load_dependencies()
        if force:
            raise RemoteProtocolError(
                "Google blocks automated sign-in. Export cookies.txt from your normal "
                "Chrome Google Photos session to the configured [google].cookies_file."
            )
        if self._client is None:
            load_error: Exception | None = None
            if self.cookies_file.is_file():
                try:
                    self._client = self._client_factory(self.cookies_file, account_index=self.account_index)
                except Exception as exc:  # noqa: BLE001 - upstream session errors vary
                    load_error = exc
            else:
                load_error = FileNotFoundError(self.cookies_file)
            if self._client is None:
                recovered = self._recover_client_from_browser() if self._session_refresh_seconds > 0 else None
                if recovered is None:
                    if isinstance(load_error, FileNotFoundError):
                        raise RemoteProtocolError(
                            "cookies.txt is missing. Export cookies.txt from your normal Chrome "
                            "Google Photos session to the configured [google].cookies_file."
                        ) from load_error
                    raise RemoteProtocolError(
                        "Google session could not be loaded. Export a fresh cookies.txt from "
                        "your normal Chrome Google Photos session."
                    ) from load_error
        identity = self.account_id()
        if force and self._browser is not None and self._browser.account_id() != identity:
            raise RemoteProtocolError("browser and gpwc sessions are authenticated to different accounts")
        return identity

    def _recover_client_from_browser(self) -> str | None:
        if self._browser is None:
            try:
                self._browser = BrowserAuthenticator(self.settings)
            except Exception:
                return None
        profile = getattr(self._browser, "profile", None)
        if profile is None or not Path(profile).is_dir():
            return None
        try:
            browser_identity = self._browser.open(interactive=False, seed_cookies=False)
            context = getattr(self._browser, "context", None)
            browser_cookies = context.cookies() if context is not None else None
            if not isinstance(browser_cookies, list):
                raise RemoteProtocolError("browser returned malformed cookies")
            cookies: list[NetscapeCookie] = []
            for cookie in browser_cookies:
                if not isinstance(cookie, dict):
                    raise RemoteProtocolError("browser returned malformed cookies")
                if not self._is_google_cookie(cookie):
                    continue
                domain = cookie.get("domain")
                name = cookie.get("name")
                value = cookie.get("value")
                if not all(isinstance(part, str) and part for part in (domain, name)) or not isinstance(value, str):
                    raise RemoteProtocolError("browser returned an incomplete cookie")
                cookies.append(
                    NetscapeCookie(
                        domain=domain,
                        include_subdomains=domain.startswith("."),
                        path=str(cookie.get("path", "/")),
                        secure=bool(cookie.get("secure", False)),
                        expires=int(cookie.get("expires", 0) or 0),
                        name=name,
                        value=value,
                    )
                )
            descriptor, temp_name = tempfile.mkstemp(prefix="photos-shrink-browser-", suffix=".txt")
            os.close(descriptor)
            temp_path = Path(temp_name)
            try:
                write_netscape_cookies(temp_path, cookies)
                self._client = self._client_factory(temp_path, account_index=self.account_index)
                self._bound_session_timeout()
                client_identity = self.account_id()
                if client_identity != browser_identity:
                    self._client = None
                    raise RemoteProtocolError(
                        "browser and gpwc sessions are authenticated to different accounts"
                    )
                if hasattr(self._client, "cookies_txt_path"):
                    self._client.cookies_txt_path = self.cookies_file
                return client_identity
            finally:
                try:
                    temp_path.unlink()
                except OSError as exc:
                    self._client = None
                    raise RemoteProtocolError("temporary browser cookie export could not be removed") from exc
        except RemoteProtocolError:
            self._client = None
            raise
        except Exception as exc:  # noqa: BLE001 - browser/session implementations vary
            self._client = None
            raise RemoteProtocolError("Google session could not be recovered from browser profile") from exc

    def account_id(self) -> str:
        if self._client is None:
            self.login()
        global_data = getattr(self._client, "global_data", None)
        value = global_data.get("oPEP7c") if isinstance(global_data, dict) else None
        if not isinstance(value, (str, int)) or not str(value):
            raise RemoteProtocolError("Google Photos returned no stable account identity")
        return str(value)

    def _refresh_interval(self) -> float:
        value = self.google.get("session_refresh_seconds", 300)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 300.0
        return max(0.0, float(value))

    @staticmethod
    def _is_google_cookie(cookie: Any) -> bool:
        domain = str(cookie.get("domain", "")).lower().lstrip(".") if isinstance(cookie, dict) else ""
        return domain == "google.com" or domain.endswith(".google.com") or domain == "googleusercontent.com" or domain.endswith(".googleusercontent.com")

    @staticmethod
    def _cookie_jar_snapshot(jar: Any) -> Any:
        try:
            return copy.deepcopy(jar)
        except Exception:
            return jar.copy() if callable(getattr(jar, "copy", None)) else None

    @staticmethod
    def _restore_cookie_jar(jar: Any, snapshot: Any) -> None:
        if snapshot is None:
            return
        try:
            jar.clear()
            jar.update(snapshot)
        except (AttributeError, TypeError):
            return

    def _merge_browser_cookies(self, jar: Any, cookies: list[dict[str, Any]]) -> None:
        for cookie in list(jar):
            if self._is_google_cookie({"domain": getattr(cookie, "domain", "")}):
                try:
                    jar.clear(cookie.domain, cookie.path, cookie.name)
                except (AttributeError, KeyError):
                    pass
        for cookie in cookies:
            if not self._is_google_cookie(cookie):
                continue
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain")
            if not all(isinstance(value, str) and value for value in (name, domain)) or not isinstance(value, str):
                raise SessionRefreshError("browser returned an incomplete Google cookie")
            kwargs: dict[str, Any] = {
                "domain": domain,
                "path": str(cookie.get("path", "/")),
                "secure": bool(cookie.get("secure", False)),
            }
            if cookie.get("expires") not in (None, 0, -1):
                kwargs["expires"] = cookie["expires"]
            try:
                jar.set(name, value, **kwargs)
            except (AttributeError, TypeError) as exc:
                raise SessionRefreshError("Google client cookie jar cannot accept browser cookies") from exc

    def refresh_session(self) -> None:
        """Refresh browser authentication and atomically install it in gpwc."""

        try:
            self._load_dependencies()
            if self._client is None:
                self.login()
        except SessionRefreshError:
            raise
        except Exception as exc:  # noqa: BLE001 - dependency/session failures vary
            raise SessionRefreshError("Google Photos session refresh could not initialize") from exc
        if self._client is None:
            raise SessionRefreshError("Google client is not initialized")
        self._bound_session_timeout()
        session = getattr(self._client, "session", None)
        jar = getattr(session, "cookies", None)
        if jar is None:
            raise SessionRefreshError("Google client has no cookie jar")
        old_cookies = self._cookie_jar_snapshot(jar)
        old_global_data = copy.deepcopy(getattr(self._client, "global_data", None))
        try:
            if self._browser is None:
                self._browser = BrowserAuthenticator(self.settings)
            expected_account = self.account_id()
            refresh = getattr(self._browser, "refresh_session", None)
            if callable(refresh):
                browser_cookies = refresh(expected_account)
            else:
                browser_identity = self._browser.open(interactive=False)
                if browser_identity != expected_account:
                    raise SessionRefreshError("browser and gpwc sessions are authenticated to different accounts")
                context = getattr(self._browser, "context", None)
                browser_cookies = context.cookies() if context is not None else []
            if not isinstance(browser_cookies, list) or any(not isinstance(cookie, dict) for cookie in browser_cookies):
                raise SessionRefreshError("browser returned malformed cookies")
            self._merge_browser_cookies(jar, browser_cookies)
            get_global_data = getattr(self._client, "get_global_data", None)
            if not callable(get_global_data):
                raise SessionRefreshError("Google client cannot refresh request tokens")
            fresh_global_data = get_global_data()
            required = ("oPEP7c", "FdrFJe", "cfb2h", "SNlM0e", "Im6cmf")
            if not isinstance(fresh_global_data, dict) or any(not fresh_global_data.get(key) for key in required):
                raise SessionRefreshError("Google Photos returned incomplete session request tokens")
            if str(fresh_global_data["oPEP7c"]) != expected_account:
                raise SessionRefreshError("Google Photos refresh returned a different account")
            self._client.global_data = fresh_global_data
            self._last_refresh_monotonic = time.monotonic()
        except SessionRefreshError:
            self._restore_cookie_jar(jar, old_cookies)
            self._client.global_data = old_global_data
            raise
        except Exception as exc:  # noqa: BLE001 - browser/upstream implementations vary
            self._restore_cookie_jar(jar, old_cookies)
            self._client.global_data = old_global_data
            raise SessionRefreshError("Google Photos session refresh failed") from exc

    def _maybe_refresh(self) -> None:
        if self._session_refresh_seconds <= 0:
            return
        if not self._refresh_due():
            return
        try:
            self.refresh_session()
        except SessionRefreshError:
            # A timed refresh is opportunistic. The browser profile can be
            # signed out while the HTTP session is still perfectly good, and
            # discarding that session would abandon a working run. The jar is
            # already rolled back, so carry on: if the session really is dead
            # the next request fails on its own merits, naming the real
            # operation. Back off so one stale profile does not re-attempt a
            # browser round trip before every subsequent request.
            self._last_refresh_monotonic = time.monotonic()

    def _refresh_due(self) -> bool:
        return (
            self._session_refresh_seconds > 0
            and time.monotonic() - self._last_refresh_monotonic >= self._session_refresh_seconds
        )

    @staticmethod
    def _is_read_only(payload: Any) -> bool:
        return type(payload).__name__ in {
            "GetLibraryPageByTakenDate",
            "GetItemInfo",
            "GetItemInfoExt",
            "GetRemoteMatchesByHash",
        }

    @staticmethod
    def _request_name(payload: Any) -> str:
        return str(getattr(payload, "rpcid", None) or type(payload).__name__)

    def _request(self, payload: Any) -> Any:
        rpc_name = self._request_name(payload)
        try:
            response = self._client.send_api_request(payload)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status is None:
                status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" status={status}" if status is not None else ""
            raise RemoteProtocolError(f"Google Photos request failed rpc={rpc_name}{suffix}") from exc
        if not getattr(response, "success", False):
            status = getattr(response, "status_code", None)
            suffix = f" status={status}" if status is not None else ""
            raise RemoteProtocolError(f"Google Photos returned an unsuccessful response rpc={rpc_name}{suffix}")
        data = getattr(response, "data", None)
        if data is None:
            raise RemoteProtocolError(f"Google Photos returned an empty response rpc={rpc_name}")
        return data

    def _execute(self, payload: Any) -> Any:
        if self._client is None:
            self.login()
        self._bound_session_timeout()
        self._maybe_refresh()
        try:
            return self._request(payload)
        except RemoteProtocolError as original:
            if not self._is_read_only(payload) or self._session_refresh_seconds <= 0:
                raise
            try:
                self.refresh_session()
            except SessionRefreshError as exc:
                # Report what actually failed. The refresh was an attempted
                # recovery, not the operation the caller asked for, and naming
                # it instead hides which request went wrong.
                raise original from exc
            return self._request(payload)

    def _bound_session_timeout(self) -> None:
        """Give gpwc's requests session a finite API timeout."""
        session = getattr(self._client, "session", None)
        request = getattr(session, "request", None)
        if session is None or not callable(request) or getattr(session, "_photos_shrink_timeout", False):
            return
        def bounded_request(method: str, url: str, **kwargs: Any) -> Any:
            kwargs.setdefault("timeout", (10, 60))
            return request(method, url, **kwargs)
        try:
            session.request = bounded_request
            session._photos_shrink_timeout = True
        except (AttributeError, TypeError):
            pass

    def _item_for_media(self, media_key: str, library_item: Any | None = None) -> dict[str, Any]:
        info = self._execute(self._payloads.GetItemInfo(media_key))
        ext = self._execute(self._payloads.GetItemInfoExt(media_key))
        item = self._convert_item(info, ext, library_item or self._library_context.get(media_key))
        item["raw"] = {"info": _asdict(info), "info_ext": _asdict(ext)}
        return item

    @staticmethod
    def _normalize_flag(value: Any) -> bool | None:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        return None

    @staticmethod
    def _first_not_none(*values: Any) -> Any:
        for value in values:
            if value is not None:
                return value
        return None

    @staticmethod
    def _validate_location(latitude: Any, longitude: Any) -> tuple[float, float] | None:
        if latitude is None and longitude is None:
            return None
        if isinstance(latitude, bool) or isinstance(longitude, bool):
            raise RemoteProtocolError("location metadata is malformed")
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
            raise RemoteProtocolError("location metadata is malformed")
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            raise RemoteProtocolError("location metadata is malformed")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise RemoteProtocolError("location metadata is out of bounds")
        return float(latitude), float(longitude)

    @staticmethod
    def _locations_match(left: tuple[float, float] | None, right: tuple[float, float] | None) -> bool:
        if left is None or right is None:
            return left is right
        return all(math.isclose(a, b, rel_tol=0.0, abs_tol=1e-7) for a, b in zip(left, right))


    def get_item(self, id: str) -> dict[str, Any]:
        self._load_dependencies()
        if not isinstance(id, str) or not id:
            raise RemoteProtocolError("item ID is required")
        return self._item_for_media(id)

    def _convert_item(self, info: Any, ext: Any, library_item: Any | None) -> dict[str, Any]:
        media_key = getattr(ext, "media_key", None) or getattr(info, "media_key", None)
        dedup_key = getattr(ext, "dedup_key", None) or getattr(info, "dedup_key", None)
        filename = getattr(ext, "file_name", None)
        size = getattr(ext, "size", None)
        width = getattr(ext, "res_width", None)
        height = getattr(ext, "res_height", None)
        timestamp = getattr(ext, "timestamp", None)
        timezone_offset = getattr(ext, "timezone_offset", None)
        duration_ms = getattr(info, "video_duration", None)
        if not isinstance(media_key, str) or not media_key or not isinstance(dedup_key, str) or not dedup_key:
            raise RemoteProtocolError("item identity is incomplete")
        if not isinstance(filename, str) or not filename:
            raise RemoteProtocolError("item filename is unknown")
        if not isinstance(size, int) or size < 0 or not isinstance(width, int) or width <= 0 or not isinstance(height, int) or height <= 0:
            raise RemoteProtocolError("item dimensions or size are unknown")
        if not isinstance(timestamp, int) or not isinstance(timezone_offset, int):
            raise RemoteProtocolError("item capture timestamp is unknown")
        duration = None
        if duration_ms is not None:
            if not isinstance(duration_ms, int) or duration_ms < 0:
                raise RemoteProtocolError("item duration is malformed")
            duration = duration_ms / 1000
        mime_type = mimetypes.guess_type(filename)[0]
        kind = "video" if (duration_ms is not None or (mime_type or "").startswith("video/")) else "photo"
        missing = object()
        favorite_raw = getattr(info, "is_favorite", missing)
        if favorite_raw is missing:
            favorite_raw = getattr(library_item, "is_favorite", missing)
        favorite = self._normalize_flag(favorite_raw)
        archived_raw = getattr(info, "is_archived", missing)
        if archived_raw is missing:
            archived_raw = getattr(library_item, "is_archived", missing)
        archived = self._normalize_flag(archived_raw)
        metadata = {
            "albums": [],
            "description": getattr(ext, "description_full", None),
            "favorite": favorite,
            "archived": archived,
            "latitude": None,
            "longitude": None,
        }
        geo = getattr(ext, "geo_location", None)
        coordinates = getattr(geo, "coordinates", None) if geo is not None else None
        if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in coordinates[:2]):
                raise RemoteProtocolError("item location is malformed")
            latitude, longitude = self._validate_location(
                coordinates[0] / 10_000_000, coordinates[1] / 10_000_000
            ) or (None, None)
            metadata["latitude"], metadata["longitude"] = latitude, longitude
        elif coordinates is not None:
            raise RemoteProtocolError("item location is malformed")
        albums = getattr(ext, "albums", None)
        if not isinstance(albums, list):
            raise RemoteProtocolError("item albums are malformed")
        for album in albums:
            album_id = getattr(album, "media_key", None)
            title = getattr(album, "title", None)
            shared = getattr(album, "is_shared", None)
            if not isinstance(album_id, str) or not album_id or not isinstance(title, str) or not isinstance(shared, bool):
                raise RemoteProtocolError("item album metadata is incomplete")
            metadata["albums"].append({"id": album_id, "title": title, "shared": shared})
        skip_reason: str | None = None
        source = getattr(ext, "source", None)
        source_values = source if isinstance(source, list) else []
        source_kind = source_values[0] if source_values and isinstance(source_values[0], str) else None
        trusted = media_key in self._trusted_media
        is_owned = getattr(library_item, "is_owned", None)
        if source_kind in {"shared", "partnerShared"} or is_owned is False:
            skip_reason = "shared item"
        elif not trusted and is_owned is not True:
            skip_reason = "ownership is unknown"
        elif favorite is None or archived is None:
            skip_reason = "favorite/archive metadata unknown"
        elif source_kind is None and not trusted:
            skip_reason = "ownership/source is unknown"
        elif any(album["shared"] for album in metadata["albums"]) and self.skip_shared:
            skip_reason = "shared album association"
        if getattr(library_item, "is_partial_upload", False) or getattr(info, "is_partial_upload", False):
            skip_reason = "partial upload"
        if getattr(library_item, "live_photo_duration", None) or getattr(info, "live_photo_duration", None):
            skip_reason = "motion photo association is unsupported"
        if mime_type is None:
            skip_reason = skip_reason or "media type is unknown"
        original_url = getattr(info, "download_original_url", None)
        if not isinstance(original_url, str) or not urlparse(original_url).scheme:
            skip_reason = skip_reason or "original download URL is unavailable"
        # Quota, from whichever of the two payloads reports it. The gate reads
        # both the byte count and the flag, so they must agree by construction.
        space_taken = self._first_not_none(
            getattr(ext, "space_taken", None), getattr(info, "space_taken", None)
        )
        return {
            "id": media_key,
            "dedup_key": dedup_key,
            "filename": filename,
            "size_bytes": size,
            "space_taken_bytes": space_taken,
            "space_consuming": None if space_taken is None else space_taken > 0,
            "width": width,
            "height": height,
            "kind": kind,
            "timestamp_ms": timestamp,
            "timezone_offset": timezone_offset,
            "duration_seconds": duration,
            "mime_type": mime_type,
            "metadata": metadata,
            "skip_reason": skip_reason,
            "original_url": original_url,
            "is_original_quality": getattr(info, "is_original_quality", None),
            "trashed": getattr(info, "trash_timestamp", None) is not None,
        }

    def _session_get(self, url: str, **kwargs: Any) -> Any:
        if self._client is None:
            self.login()
        session = getattr(self._client, "session", None)
        if session is None or not callable(getattr(session, "get", None)):
            raise RemoteProtocolError("Google Photos client has no HTTP session")
        kwargs.setdefault("timeout", (10, 120))
        try:
            response = session.get(url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as exc:
            status = getattr(locals().get("response"), "status_code", None)
            close = getattr(locals().get("response"), "close", None)
            if callable(close):
                close()
            if status is not None:
                raise RemoteProtocolError(f"original download request failed (status={status})") from exc
            raise RemoteProtocolError("original download request failed") from exc

    def download(self, item: dict[str, Any], destination: str | os.PathLike[str]) -> None:
        url = item.get("original_url")
        if not isinstance(url, str) or urlparse(url).scheme not in {"https", "http"}:
            raise RemoteProtocolError("item has no valid original download URL")
        expected_size = item.get("size_bytes")
        try:
            response = self._session_get(url, stream=True)
        except RemoteProtocolError as exc:
            # Signed original URLs can expire during a long plan/apply run.
            # Refresh only on an explicit authorization failure and preserve
            # the snapshot identity/size before using the new URL.
            if "status=403" not in str(exc) or not isinstance(item.get("id"), str):
                raise
            refreshed = self.get_item(item["id"])
            for key in ("id", "dedup_key", "size_bytes", "width", "height"):
                if item.get(key) is not None and refreshed.get(key) != item.get(key):
                    raise RemoteProtocolError(f"item {key} changed while refreshing download URL")
            url = refreshed.get("original_url")
            if not isinstance(url, str):
                raise RemoteProtocolError("refreshed item has no original download URL")
            response = self._session_get(url, stream=True)
        try:
            headers = getattr(response, "headers", {}) or {}
            if not isinstance(headers, dict) and not callable(getattr(headers, "get", None)):
                raise RemoteProtocolError("original download returned malformed headers")
            content_type = str(headers.get("content-type", "")).lower()
            if "text/html" in content_type or "application/json" in content_type:
                raise RemoteProtocolError("original download returned a non-media response")
            content_length = headers.get("content-length")
            if content_length and isinstance(expected_size, int):
                try:
                    if int(content_length) != expected_size:
                        raise RemoteProtocolError("original download size does not match metadata")
                except ValueError as exc:
                    raise RemoteProtocolError("original download size is malformed") from exc
        except Exception:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            raise
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        total = 0
        first_chunk = b""
        try:
            with os.fdopen(fd, "wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        if not first_chunk:
                            first_chunk = bytes(chunk[:256])
                        output.write(chunk)
                        total += len(chunk)
            if first_chunk.lstrip().lower().startswith((b"<html", b"<!doctype", b"{")):
                raise RemoteProtocolError("original download returned a non-media response")
            if total == 0 or (isinstance(expected_size, int) and total != expected_size):
                raise RemoteProtocolError("original download did not match metadata")
            os.replace(temp_name, target)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def find_uploaded(self, path: str | os.PathLike[str]) -> dict[str, Any] | None:
        self._load_dependencies()
        file_path = Path(path)
        if not file_path.is_file():
            raise RemoteProtocolError("upload source does not exist")
        digest = hashlib.sha1()
        with file_path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        content_hash = base64.b64encode(digest.digest()).decode("ascii")
        data = self._execute(self._payloads.GetRemoteMatchesByHash([content_hash]))
        if not isinstance(data, list):
            raise RemoteProtocolError("Google Photos hash response is malformed")
        matches = [match for match in data if getattr(match, "hash", None) == content_hash]
        if len(matches) > 1:
            raise RemoteProtocolError("Google Photos returned multiple exact hash matches")
        for match in matches:
            if getattr(match, "hash", None) != content_hash:
                continue
            media_key = getattr(match, "media_key", None)
            dedup_key = getattr(match, "dedup_key", None)
            if not isinstance(media_key, str) or not media_key or not isinstance(dedup_key, str) or not dedup_key:
                raise RemoteProtocolError("Google Photos hash match has incomplete identity")
            return {
                "id": media_key,
                "dedup_key": dedup_key,
                "filename": None,
                "size_bytes": file_path.stat().st_size,
                "space_taken_bytes": file_path.stat().st_size,
                "width": getattr(match, "res_width", None),
                "height": getattr(match, "res_height", None),
                "kind": "video" if getattr(match, "video_duration", None) else "photo",
                "timestamp_ms": getattr(match, "timestamp", None),
                "timezone_offset": getattr(match, "timezone_offset", None),
                "duration_seconds": ((getattr(match, "video_duration", None) or 0) / 1000) or None,
                "mime_type": None,
                "metadata": {},
                "skip_reason": None,
                "original_url": None,
                "is_original_quality": None,
                "raw": {"match": _asdict(match)},
                "content_hash": content_hash,
            }
        return None

    def _verify_remote_bytes(self, item: dict[str, Any], expected_sha256: str) -> None:
        if not isinstance(item.get("original_url"), str):
            raise RemoteProtocolError("uploaded item has no original URL for byte verification")
        with tempfile.TemporaryDirectory(prefix="photos-shrink-verify-") as work:
            probe = Path(work) / "remote-original"
            self.download(item, probe)
            if sha256_file(probe) != expected_sha256:
                raise RemoteProtocolError(
                    "Google Photos changed uploaded bytes; select Original quality and retry"
                )

    def restore_metadata(self, original: dict[str, Any], replacement: dict[str, Any]) -> None:
        dedup = replacement.get("dedup_key")
        if not isinstance(dedup, str) or not dedup:
            raise RemoteProtocolError("replacement identity is incomplete")
        metadata = original.get("metadata")
        if not isinstance(metadata, dict):
            raise RemoteProtocolError("original metadata is malformed")
        timestamp = original.get("timestamp_ms")
        timezone_offset = original.get("timezone_offset")
        if not isinstance(timestamp, int) or not isinstance(timezone_offset, int):
            raise RemoteProtocolError("original timestamp metadata is incomplete")
        latitude, longitude = metadata.get("latitude"), metadata.get("longitude")
        coordinates = self._validate_location(latitude, longitude)
        replacement_metadata = replacement.get("metadata")
        if isinstance(replacement_metadata, dict):
            replacement_coordinates = self._validate_location(
                replacement_metadata.get("latitude"), replacement_metadata.get("longitude")
            )
            if coordinates is not None and replacement_coordinates is not None and not self._locations_match(
                coordinates, replacement_coordinates
            ):
                raise RemoteProtocolError("replacement location does not match original metadata")
            if coordinates is None and replacement_coordinates is not None:
                delete_geo = getattr(self._payloads, "DeleteItemGeoData", None)
                if delete_geo is None:
                    raise RemoteProtocolError("location deletion is unsupported by gpwc")
                self._execute(delete_geo([dedup]))
        albums = metadata.get("albums")
        if not isinstance(albums, list):
            raise RemoteProtocolError("original album metadata is malformed")
        for album in albums:
            if not isinstance(album, dict) or not isinstance(album.get("id"), str) or not isinstance(album.get("shared"), bool):
                raise RemoteProtocolError("original album metadata is incomplete")
            if album["shared"] and self.skip_shared:
                raise RemoteProtocolError("shared album association cannot be restored safely")
        replacement_timestamp = replacement.get("timestamp_ms")
        replacement_timezone = replacement.get("timezone_offset")
        timestamp_matches = (
            isinstance(replacement_timestamp, int)
            and isinstance(replacement_timezone, int)
            and replacement_timestamp == timestamp
            and replacement_timezone == timezone_offset
        )
        if not timestamp_matches:
            if timezone_offset % 1000:
                raise RemoteProtocolError("original timezone offset is not whole seconds")
            self._execute(self._payloads.SetItemTimestamp(dedup, timestamp, timezone_offset // 1000))
        description = metadata.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise RemoteProtocolError("original description metadata is malformed")
            if not isinstance(replacement_metadata, dict) or replacement_metadata.get("description") != description:
                self._execute(self._payloads.SetItemDescription(dedup, description))
        favorite = metadata.get("favorite")
        if not isinstance(favorite, bool):
            raise RemoteProtocolError("original favorite metadata is unknown")
        if not isinstance(replacement_metadata, dict) or replacement_metadata.get("favorite") != favorite:
            self._execute(self._payloads.SetFavorite([dedup]) if favorite else self._payloads.UnFavorite([dedup]))
        archived = metadata.get("archived")
        if not isinstance(archived, bool):
            raise RemoteProtocolError("original archive metadata is unknown")
        if not isinstance(replacement_metadata, dict) or replacement_metadata.get("archived") != archived:
            self._execute(self._payloads.SetArchive([dedup]) if archived else self._payloads.UnArchive([dedup]))
        replacement_albums = (
            replacement_metadata.get("albums", []) if isinstance(replacement_metadata, dict) else None
        )
        if not isinstance(replacement_albums, list):
            replacement_albums = []
        existing_album_ids = {
            album.get("id") for album in replacement_albums if isinstance(album, dict)
        }
        for album in albums:
            if album["id"] in existing_album_ids:
                continue
            payload_type = (
                self._payloads.AddItemsToExistingSharedAlbum
                if album["shared"]
                else self._payloads.AddItemsToExistingAlbum
            )
            self._execute(payload_type([replacement["id"]], album["id"]))

    def verify_replacement(self, original: dict[str, Any], replacement: dict[str, Any], output_info: dict[str, Any]) -> None:
        required = ("id", "dedup_key", "size_bytes", "width", "height", "kind")
        if any(key not in replacement or replacement[key] in (None, "") for key in required):
            raise RemoteProtocolError("replacement identity or dimensions are incomplete")
        if replacement.get("id") == original.get("id") or replacement.get("dedup_key") == original.get("dedup_key"):
            raise RemoteProtocolError("replacement identity is the original item")
        fresh = self.get_item(str(replacement["id"]))
        for key in required:
            if fresh.get(key) != replacement.get(key):
                raise RemoteProtocolError(f"replacement {key} changed unexpectedly")
        for key in ("size_bytes", "width", "height", "kind"):
            if output_info.get(key) != fresh.get(key):
                raise RemoteProtocolError(f"replacement {key} does not match encoded output")
        if output_info.get("kind") == "video":
            expected_duration = output_info.get("duration_seconds")
            actual_duration = fresh.get("duration_seconds")
            if expected_duration is None or actual_duration is None or abs(expected_duration - actual_duration) > 0.25:
                raise RemoteProtocolError("replacement duration does not match encoded output")
        for key in ("timestamp_ms", "timezone_offset"):
            if original.get(key) is None or fresh.get(key) != original.get(key):
                raise RemoteProtocolError(f"replacement {key} does not match original capture metadata")
        if fresh.get("skip_reason"):
            raise RemoteProtocolError("replacement has an unsafe metadata state")
        original_metadata = original.get("metadata")
        fresh_metadata = fresh.get("metadata")
        if not isinstance(original_metadata, dict) or not isinstance(fresh_metadata, dict):
            raise RemoteProtocolError("replacement metadata is incomplete")
        for key in ("description", "favorite", "archived"):
            if fresh_metadata.get(key) != original_metadata.get(key):
                raise RemoteProtocolError(f"replacement {key} does not match original metadata")
        original_location = self._validate_location(
            original_metadata.get("latitude"), original_metadata.get("longitude")
        )
        fresh_location = self._validate_location(
            fresh_metadata.get("latitude"), fresh_metadata.get("longitude")
        )
        if not self._locations_match(fresh_location, original_location):
            raise RemoteProtocolError("replacement location does not match original metadata")
        original_albums = {
            (album.get("id"), album.get("title"), album.get("shared"))
            for album in original_metadata.get("albums", [])
            if isinstance(album, dict)
        }
        fresh_albums = {
            (album.get("id"), album.get("title"), album.get("shared"))
            for album in fresh_metadata.get("albums", [])
            if isinstance(album, dict)
        }
        if fresh_albums != original_albums:
            raise RemoteProtocolError("replacement albums do not match original metadata")
        expected_sha = output_info.get("sha256") or output_info.get("output_sha256")
        output_path = output_info.get("path") or output_info.get("output_path")
        if not expected_sha or output_path is None:
            raise RemoteProtocolError("replacement verification requires output hash and local path")
        if replacement.get("sha256") and replacement["sha256"] != expected_sha:
            raise RemoteProtocolError("replacement content hash does not match encoded output")
        local_path = Path(output_path)
        if not local_path.is_file() or sha256_file(local_path) != expected_sha:
            raise RemoteProtocolError("local encoded output hash changed")
        self._verify_remote_bytes(fresh, expected_sha)

    def trash(self, item: dict[str, Any]) -> None:
        media_key, dedup_key = item.get("id"), item.get("dedup_key")
        if not isinstance(media_key, str) or not media_key or not isinstance(dedup_key, str) or not dedup_key:
            raise RemoteProtocolError("cannot trash item with incomplete identity")
        if item.get("skip_reason"):
            raise RemoteProtocolError("cannot trash item with unsafe metadata")
        fresh = self.get_item(media_key)
        if fresh.get("id") != media_key or fresh.get("dedup_key") != dedup_key:
            raise RemoteProtocolError("item identity changed before trash")
        if fresh.get("trashed"):
            return
        for key in ("size_bytes", "width", "height", "kind", "timestamp_ms", "timezone_offset"):
            if item.get(key) is None or fresh.get(key) != item.get(key):
                raise RemoteProtocolError(f"item {key} changed before trash")
        if fresh.get("skip_reason"):
            raise RemoteProtocolError("item metadata became unsafe before trash")
        original_metadata = item.get("metadata")
        fresh_metadata = fresh.get("metadata")
        if not isinstance(original_metadata, dict) or not isinstance(fresh_metadata, dict):
            raise RemoteProtocolError("item metadata is incomplete before trash")
        for key in ("description", "favorite", "archived", "albums", "latitude", "longitude"):
            if fresh_metadata.get(key) != original_metadata.get(key):
                raise RemoteProtocolError(f"item {key} changed before trash")
        self._execute(self._payloads.MoveToTrash([dedup_key]))

    def is_trashed(self, item: dict[str, Any]) -> bool:
        fresh = self.get_item(str(item.get("id", "")))
        return bool(fresh.get("trashed"))

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        self._browser = None
        session = getattr(self._client, "session", None)
        close = getattr(session, "close", None)
        if callable(close):
            close()
