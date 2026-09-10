"""Browser authentication and local Netscape cookie handling.

The browser is deliberately kept separate from gpwc's requests session.  The
two sessions are compared by Google's stable ``oPEP7c`` identity before a
browser upload is attempted.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class NetscapeCookie:
    domain: str
    include_subdomains: bool
    path: str
    secure: bool
    expires: int
    name: str
    value: str


def load_netscape_cookies(path: str | os.PathLike[str]) -> list[NetscapeCookie]:
    """Read a Netscape ``cookies.txt`` file without exposing cookie values."""

    result: list[NetscapeCookie] = []
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    for line_number, raw_line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        line = line.removeprefix("#HttpOnly_")
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError(f"invalid Netscape cookie line {line_number}")
        domain, include_subdomains, cookie_path, secure, expires, name, value = fields
        try:
            result.append(
                NetscapeCookie(
                    domain=domain,
                    include_subdomains=include_subdomains.upper() == "TRUE",
                    path=cookie_path,
                    secure=secure.upper() == "TRUE",
                    # MozillaCookieJar treats 0 as expired.  Keep a blank or
                    # -1 as session-cookie expiry while exposing 0 to callers.
                    expires=0 if not expires or expires == "-1" else int(expires),
                    name=name,
                    value=value,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid Netscape cookie line {line_number}") from exc
    return result


def write_netscape_cookies(path: str | os.PathLike[str], cookies: list[NetscapeCookie]) -> None:
    """Write browser cookies in a format accepted by gpwc."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        lines.append(
            "\t".join(
                (
                    cookie.domain,
                    "TRUE" if cookie.include_subdomains else "FALSE",
                    cookie.path,
                    "TRUE" if cookie.secure else "FALSE",
                    "" if cookie.expires <= 0 else str(cookie.expires),
                    cookie.name,
                    cookie.value,
                )
            )
        )
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass


class BrowserAuthError(RuntimeError):
    """Raised when browser authentication cannot establish a known account."""


class UploadNotStartedError(BrowserAuthError):
    """Raised when the upload controls fail before file submission begins."""


class BrowserAuthenticator:
    """Manage a visible, dedicated persistent Chrome profile."""

    def __init__(
        self,
        settings: dict[str, Any],
        *,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        google = settings.get("google", settings)
        self.cookies_file = Path(google.get("cookies_file", ".photos-shrink/cookies.txt"))
        self.profile = Path(google.get("browser_profile", ".photos-shrink/browser"))
        self.channel = google.get("browser_channel", "chrome")
        self.browser_headless = bool(google.get("browser_headless", True))
        self.account_index = int(google.get("account_index", 0))
        self._playwright_factory = playwright_factory
        self._playwright = None
        self.context = None
        self.page = None

    @property
    def photos_url(self) -> str:
        suffix = f"/u/{self.account_index}/" if self.account_index else "/"
        return f"https://photos.google.com{suffix}"

    def open(self, *, interactive: bool = False) -> str:
        """Open Photos and return the stable account identity.

        ``interactive=False`` only uses an existing cookie/profile session and
        fails if it is not already authenticated.  ``login`` opts into the
        visible first-run flow.
        """

        if self.context is not None:
            return self.account_id()
        if self._playwright_factory is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise BrowserAuthError("Playwright is required for browser authentication") from exc
            self._playwright_factory = sync_playwright
        self.profile.mkdir(parents=True, exist_ok=True)
        self._playwright = self._playwright_factory().start()
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                str(self.profile), headless=False if interactive else self.browser_headless, channel=self.channel
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self._import_cookies()
            self.page.goto(self.photos_url, wait_until="domcontentloaded")
            identity = self.account_id()
            page_url = str(getattr(self.page, "url", ""))
            page_host = (urlparse(page_url).hostname or "").lower()
            # Google sign-in pages can expose WIZ_global_data before they
            # redirect back to Photos.  Always wait for the Photos origin in
            # an interactive login, even when that early value looks like an
            # account identity.
            if interactive and (not identity or page_host != "photos.google.com"):
                self.page.wait_for_function(
                    "() => location.hostname === 'photos.google.com' && window.WIZ_global_data && window.WIZ_global_data.oPEP7c",
                    timeout=0,
                )
                identity = self.account_id()
            if not identity:
                raise BrowserAuthError("Google Photos browser session is not authenticated")
            page_url = str(getattr(self.page, "url", ""))
            parsed_url = urlparse(page_url)
            if parsed_url.scheme.lower() not in {"https", "http"}:
                raise BrowserAuthError("browser did not reach Google Photos")
            if (parsed_url.hostname or "").lower() != "photos.google.com":
                raise BrowserAuthError("browser account identity was read outside Google Photos")
            expected_path = f"/u/{self.account_index}/" if self.account_index else "/"
            if self.account_index and not parsed_url.path.rstrip("/").startswith(expected_path.rstrip("/")):
                raise BrowserAuthError("browser account route does not match configured account index")
            if interactive:
                self._export_cookies()
            return identity
        except Exception:
            self.close()
            raise

    def login(self) -> str:
        """Run the visible first-run login flow and save cookies locally."""

        return self.open(interactive=True)

    def account_id(self) -> str:
        if self.page is None:
            raise BrowserAuthError("browser session is not open")
        try:
            value = self.page.evaluate("() => window.WIZ_global_data?.oPEP7c")
        except Exception as exc:
            raise BrowserAuthError("could not read Google account identity") from exc
        if not isinstance(value, (str, int)) or not str(value):
            return ""
        return str(value)

    def upload(self, path: str | os.PathLike[str]) -> None:
        if self.page is None:
            self.open(interactive=False)
        assert self.page is not None
        file_path = str(Path(path).resolve())
        inputs = self.page.locator('input[type="file"]')
        if inputs.count() > 0:
            inputs.first.set_input_files(file_path)
            return
        try:
            self.page.get_by_role("button", name="Create and add photos", exact=True).click()
            import_menuitem = self.page.get_by_role(
                "menuitem", name="Import photos from your computer", exact=True
            )
            with self.page.expect_file_chooser(timeout=10000) as chooser_info:
                import_menuitem.click()
        except Exception as exc:  # noqa: BLE001 - Playwright errors vary by UI state
            raise UploadNotStartedError("Google Photos upload control is unavailable") from exc
        chooser_info.value.set_files(file_path)

    def _import_cookies(self) -> None:
        if not self.cookies_file.is_file() or self.context is None:
            return
        cookies = []
        for cookie in load_netscape_cookies(self.cookies_file):
            entry: dict[str, Any] = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
            }
            if cookie.expires > 0:
                entry["expires"] = cookie.expires
            cookies.append(entry)
        if cookies:
            self.context.add_cookies(cookies)

    def _export_cookies(self) -> None:
        if self.context is None:
            return
        cookies = []
        for cookie in self.context.cookies():
            domain = str(cookie.get("domain", ""))
            if not domain:
                continue
            cookies.append(
                NetscapeCookie(
                    domain=domain,
                    include_subdomains=domain.startswith("."),
                    path=str(cookie.get("path", "/")),
                    secure=bool(cookie.get("secure", False)),
                    expires=int(cookie.get("expires", 0) or 0),
                    name=str(cookie.get("name", "")),
                    value=str(cookie.get("value", "")),
                )
            )
        write_netscape_cookies(self.cookies_file, cookies)

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            self.context = None
            self.page = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None
