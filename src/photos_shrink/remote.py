"""Google Photos remote adapter: the browser-cookie half of the library.

Uploads go through the official API (`photos_api`), which cannot write album
membership or delete. Everything that needs those -- identifying items by
content hash, restoring metadata onto replacements, and trashing originals --
comes through the batched operations here, on an exported browser session.

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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .auth import (
    BrowserAuthenticator,
    NetscapeCookie,
    write_netscape_cookies,
)

if TYPE_CHECKING:
    from .config import Settings


class RemoteProtocolError(RuntimeError):
    """Raised when an upstream response is missing information we need."""


class SessionRefreshError(RemoteProtocolError):
    """Raised when an authenticated browser/session refresh cannot be installed."""


class MissingResponseError(RemoteProtocolError):
    """A call in a batched request that Google sent no response for."""


def _content_hash(path: Path) -> str:
    """The hash Google Photos matches uploads by: SHA-1, standard base64."""

    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return base64.b64encode(digest.digest()).decode("ascii")


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
            "GetTrashPage",
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

    def _convert_item(self, info: Any, ext: Any, library_item: Any | None) -> dict[str, Any]:
        media_key = getattr(ext, "media_key", None) or getattr(info, "media_key", None)
        dedup_key = getattr(ext, "dedup_key", None) or getattr(info, "dedup_key", None)
        filename = getattr(ext, "file_name", None)
        size = getattr(ext, "size", None)
        width = getattr(ext, "res_width", None)
        height = getattr(ext, "res_height", None)
        timestamp = getattr(ext, "timestamp", None)
        timezone_offset = getattr(ext, "timezone_offset", None)
        if timezone_offset is None:
            # An API upload with no offset in its EXIF has none in the extended
            # info, while the basic info reports 0. Taking that 0 lets the
            # replace step see the mismatch and fix it, instead of being unable
            # to read the replacement at all.
            timezone_offset = getattr(info, "timezone_offset", None)
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
        is_owned = getattr(library_item, "is_owned", None)
        if source_kind in {"shared", "partnerShared"} or is_owned is False:
            skip_reason = "shared item"
        elif is_owned is not True:
            skip_reason = "ownership is unknown"
        elif favorite is None or archived is None:
            skip_reason = "favorite/archive metadata unknown"
        elif source_kind is None:
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

    # --- Batched calls for the replace step ------------------------------------
    #
    # The web client's batchexecute endpoint carries many calls in one HTTP
    # request. Replacing one item at a time cost about ten requests and a
    # download per item; these carry a whole batch in a handful.

    def _execute_many(self, payloads: list[Any]) -> list[Any]:
        """Send several calls in one request; return each one's data or its error, in order.

        A call Google answers unsuccessfully fails alone, as a
        `RemoteProtocolError` in its slot. A request that fails outright raises.
        """

        if not payloads:
            return []
        if self._client is None:
            self.login()
        self._bound_session_timeout()
        self._maybe_refresh()
        try:
            responses = self._send_many(payloads)
        except RemoteProtocolError as original:
            if not all(self._is_read_only(p) for p in payloads) or self._session_refresh_seconds <= 0:
                raise
            try:
                self.refresh_session()
            except SessionRefreshError as exc:
                raise original from exc
            responses = self._send_many(payloads)
        by_id = {getattr(response, "response_id", None): response for response in responses}
        results: list[Any] = []
        for payload in payloads:
            name = self._request_name(payload)
            response = by_id.get(getattr(payload, "payload_id", None))
            if response is None:
                results.append(MissingResponseError(f"Google Photos sent no response rpc={name}"))
            elif not getattr(response, "success", False):
                results.append(RemoteProtocolError(f"Google Photos returned an unsuccessful response rpc={name}"))
            elif getattr(response, "data", None) is None:
                results.append(RemoteProtocolError(f"Google Photos returned an empty response rpc={name}"))
            else:
                results.append(response.data)
        return results

    def _send_many(self, payloads: list[Any]) -> list[Any]:
        try:
            responses = self._client.send_api_request(list(payloads))
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status is None:
                status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" status={status}" if status is not None else ""
            names = ",".join(sorted({self._request_name(p) for p in payloads}))
            raise RemoteProtocolError(f"Google Photos request failed rpc={names}{suffix}") from exc
        if not isinstance(responses, list):
            raise RemoteProtocolError("Google Photos batch response is malformed")
        return responses

    def get_items(self, media_keys: list[str], *, per_request: int = 25) -> dict[str, dict[str, Any] | Exception]:
        """Read many keys: each key maps to its item, or to why it could not be read.

        Live, Google sometimes leaves a call in a large request unanswered; those
        keys are asked again, one per request.
        """

        results = self._get_items(media_keys, per_request)
        unanswered = [key for key, value in results.items() if isinstance(value, MissingResponseError)]
        if unanswered:
            results.update(self._get_items(unanswered, 1))
        return results

    def _get_items(self, media_keys: list[str], per_request: int) -> dict[str, dict[str, Any] | Exception]:
        self._load_dependencies()
        results: dict[str, dict[str, Any] | Exception] = {}
        keys = list(dict.fromkeys(media_keys))
        for start in range(0, len(keys), per_request):
            chunk = keys[start : start + per_request]
            payloads: list[Any] = []
            for key in chunk:
                payloads += [self._payloads.GetItemInfo(key), self._payloads.GetItemInfoExt(key)]
            answers = self._execute_many(payloads)
            for index, key in enumerate(chunk):
                info, ext = answers[2 * index], answers[2 * index + 1]
                failure = next((a for a in (info, ext) if isinstance(a, Exception)), None)
                if failure is not None:
                    results[key] = failure
                    continue
                try:
                    results[key] = self._convert_item(info, ext, None)
                except RemoteProtocolError as exc:
                    results[key] = exc
        return results

    def find_uploaded_many(
        self, paths: list[Path], *, per_request: int = 50
    ) -> dict[Path, dict[str, Any] | None | Exception]:
        """Resolve many files by content hash: a match, None when absent, or the error.

        Only the identity and capture time are returned; `get_items` supplies
        the rest.
        """

        self._load_dependencies()
        hashes = {Path(path): _content_hash(Path(path)) for path in paths}
        results: dict[Path, dict[str, Any] | None | Exception] = {}
        entries = list(hashes.items())
        for start in range(0, len(entries), per_request):
            chunk = entries[start : start + per_request]
            (data,) = self._execute_many([self._payloads.GetRemoteMatchesByHash([h for _, h in chunk])])
            if isinstance(data, Exception) or not isinstance(data, list):
                error = data if isinstance(data, Exception) else RemoteProtocolError(
                    "Google Photos hash response is malformed"
                )
                results.update({path: error for path, _ in chunk})
                continue
            for path, content_hash in chunk:
                matches = [m for m in data if getattr(m, "hash", None) == content_hash]
                if not matches:
                    results[path] = None
                elif len(matches) > 1:
                    results[path] = RemoteProtocolError("Google Photos returned multiple exact hash matches")
                else:
                    media_key = getattr(matches[0], "media_key", None)
                    dedup_key = getattr(matches[0], "dedup_key", None)
                    if not isinstance(media_key, str) or not media_key or not isinstance(dedup_key, str) or not dedup_key:
                        results[path] = RemoteProtocolError("Google Photos hash match has incomplete identity")
                    else:
                        results[path] = {"id": media_key, "dedup_key": dedup_key}
        return results

    def restore_many(self, fixes: list[dict[str, Any]], *, per_call: int = 100) -> dict[str, Exception]:
        """Apply metadata fixes to replacements; return the failures by replacement id.

        Each fix names a `replacement` item and only what must change:
        `timestamp` as (epoch ms, offset ms), `albums` to add it to, and
        `favorite`, `archived` or `description`. A successful call proves
        nothing on its own -- the caller re-reads every fixed replacement.

        Each kind of change goes out as one call carrying a list of items, in
        a request of its own. Live, Google answered HTTP 400 to a request
        holding eleven capture-time calls, yet accepted one call listing
        several items.
        """

        self._load_dependencies()
        failures: dict[str, Exception] = {}
        timestamps: list[tuple[str, list[Any]]] = []
        albums: dict[tuple[str, bool], list[str]] = {}
        flags: dict[Any, list[tuple[str, str]]] = {}
        descriptions: list[tuple[str, str, str]] = []
        for fix in fixes:
            replacement = fix["replacement"]
            item_id, dedup = replacement["id"], replacement["dedup_key"]
            if "timestamp" in fix:
                timestamp, offset = fix["timestamp"]
                if offset % 1000:
                    failures[item_id] = RemoteProtocolError("original timezone offset is not whole seconds")
                else:
                    # Seconds, both of them. gpwc documents the timestamp in
                    # milliseconds, but Google refuses that; live, only seconds took.
                    timestamps.append((item_id, [dedup, timestamp // 1000, offset // 1000]))
            for album in fix.get("albums", []):
                if album["shared"] and self.skip_shared:
                    failures[item_id] = RemoteProtocolError("shared album association cannot be restored safely")
                else:
                    albums.setdefault((album["id"], album["shared"]), []).append(item_id)
            if "favorite" in fix:
                payload = self._payloads.SetFavorite if fix["favorite"] else self._payloads.UnFavorite
                flags.setdefault(payload, []).append((item_id, dedup))
            if "archived" in fix:
                payload = self._payloads.SetArchive if fix["archived"] else self._payloads.UnArchive
                flags.setdefault(payload, []).append((item_id, dedup))
            if "description" in fix:
                descriptions.append((item_id, dedup, fix["description"]))

        calls: list[tuple[list[str], Any]] = []
        for start in range(0, len(timestamps), per_call):
            chunk = timestamps[start : start + per_call]
            payload = self._payloads.SetItemTimestamp(*chunk[0][1])
            # gpwc builds the call for one item; the call itself takes a list.
            payload.data = [[entry for _, entry in chunk]]
            calls.append(([item_id for item_id, _ in chunk], payload))
        for (album_id, shared), item_ids in albums.items():
            payload_type = (
                self._payloads.AddItemsToExistingSharedAlbum if shared else self._payloads.AddItemsToExistingAlbum
            )
            for start in range(0, len(item_ids), per_call):
                chunk = item_ids[start : start + per_call]
                calls.append((chunk, payload_type(chunk, album_id)))
        for payload_type, entries in flags.items():
            for start in range(0, len(entries), per_call):
                chunk = entries[start : start + per_call]
                calls.append(([item_id for item_id, _ in chunk], payload_type([dedup for _, dedup in chunk])))
        for item_id, dedup, description in descriptions:
            calls.append(([item_id], self._payloads.SetItemDescription(dedup, description)))

        for item_ids, payload in calls:
            try:
                self._execute(payload)
            except RemoteProtocolError as exc:
                for item_id in item_ids:
                    failures.setdefault(item_id, exc)
        return failures

    def trash_many(self, dedup_keys: list[str], *, per_request: int = 100) -> None:
        """Move items to the bin, up to `per_request` in one call. Confirm with `get_items`."""

        self._load_dependencies()
        keys = [key for key in dedup_keys if isinstance(key, str) and key]
        if len(keys) != len(dedup_keys):
            raise RemoteProtocolError("cannot trash items with incomplete identity")
        for start in range(0, len(keys), per_request):
            self._execute(self._payloads.MoveToTrash(keys[start : start + per_request]))

    def in_bin(self, dedup_keys: list[str], *, max_pages: int = 200) -> set[str]:
        """Which of these dedup keys are in the bin.

        The bin is how a trash is confirmed. Looking the item up does not work:
        Google refuses item info for anything in the bin, and one library item
        can answer to more than one media key, so the dedup key is the identity.
        Pages are read until every key is found or the bin ends.
        """

        self._load_dependencies()
        wanted = set(dedup_keys)
        found: set[str] = set()
        page_id: str | None = None
        for _ in range(max_pages):
            page = self._execute(self._payloads.GetTrashPage(page_id))
            found |= {getattr(item, "dedup_key", None) for item in getattr(page, "items", []) or []} & wanted
            page_id = getattr(page, "next_page_id", None)
            if found == wanted or not page_id:
                break
        return found

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        self._browser = None
        session = getattr(self._client, "session", None)
        close = getattr(session, "close", None)
        if callable(close):
            close()


@contextmanager
def open_session(settings: Settings) -> Iterator[GooglePhotosRemote]:
    """Open a logged-in Google Photos session and always close it again.

    Both tools that need cookies were opening, logging in and closing by hand,
    and only one of them turned an expired export into a readable message
    rather than a stack trace. Cookies here last about fifteen minutes, so that
    is the routine outcome, not the exceptional one.

    Raises `RemoteProtocolError` when no session can be opened; the caller
    reports it and exits.
    """

    remote = GooglePhotosRemote(settings.as_dict())
    try:
        try:
            remote.login()
        except RemoteProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001 - browser/auth failures vary
            raise RemoteProtocolError(f"Google session could not be opened: {exc}") from exc
        yield remote
    finally:
        remote.close()


COOKIE_HINT = "Export a fresh cookies.txt into .photos-shrink/ and re-run. Nothing was changed."
