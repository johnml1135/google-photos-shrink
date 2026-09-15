from __future__ import annotations

from http.cookiejar import MozillaCookieJar
from pathlib import Path

import pytest
import requests

from photos_shrink.auth import (
    BrowserAuthenticator,
    BrowserAuthError,
    NetscapeCookie,
    load_netscape_cookies,
    write_netscape_cookies,
)


def test_netscape_cookie_round_trip_preserves_cookie_attributes(tmp_path: Path):
    path = tmp_path / "cookies.txt"
    cookies = [
        NetscapeCookie(".google.com", True, "/", True, 0, "SID", "opaque-value"),
        NetscapeCookie("photos.google.com", False, "/u/0", False, 1893456000, "PREF", "light"),
    ]

    write_netscape_cookies(path, cookies)

    assert load_netscape_cookies(path) == cookies


def test_session_cookies_with_zero_expiry_are_sent_as_session_cookies(tmp_path: Path):
    path = tmp_path / "cookies.txt"
    write_netscape_cookies(path, [NetscapeCookie("example.test", False, "/", False, 0, "SID", "opaque")])
    loaded = load_netscape_cookies(path)
    jar = MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    request = requests.Request("GET", "https://example.test/").prepare()
    wrapped = requests.cookies.MockRequest(request)
    jar.add_cookie_header(wrapped)

    assert loaded[0].expires == 0
    assert wrapped.get_header("Cookie") == "SID=opaque"


def test_interactive_login_waits_for_photos_origin_before_accepting_identity(tmp_path: Path):
    class Page:
        def __init__(self):
            self.url = "https://accounts.google.com/signin"
            self.waits = []

        def goto(self, url, wait_until=None):
            # The sign-in page can remain visible after navigation until the
            # user completes the flow.
            self.url = "https://accounts.google.com/signin"

        def evaluate(self, expression):
            return "early-account" if "accounts.google.com" in self.url else "photos-account"

        def wait_for_function(self, expression, timeout=0):
            self.waits.append((expression, timeout))
            self.url = "https://photos.google.com/"

    class Context:
        def __init__(self, page):
            self.pages = [page]

        def new_page(self):
            return self.pages[0]

        def add_cookies(self, cookies):
            pass

        def cookies(self):
            return []

        def close(self):
            pass

    page = Page()
    context = Context(page)

    class Chromium:
        def launch_persistent_context(self, profile, *, headless, channel):
            assert headless is False
            assert channel == "chrome"
            return context

    class Playwright:
        chromium = Chromium()

        def start(self):
            return self

        def stop(self):
            pass

    auth = BrowserAuthenticator(
        {"google": {"cookies_file": str(tmp_path / "cookies.txt"), "browser_profile": str(tmp_path / "profile")}},
        playwright_factory=Playwright,
    )

    assert auth.login() == "photos-account"
    assert len(page.waits) == 1
    assert "location.hostname === 'photos.google.com'" in page.waits[0][0]
    auth.close()


@pytest.mark.parametrize("headless, expected", [(True, True), (False, False)])
def test_noninteractive_open_uses_configured_headless_mode(tmp_path: Path, headless: bool, expected: bool):
    class Page:
        url = "https://photos.google.com/"

        def evaluate(self, expression):
            return "account"

        def goto(self, url, wait_until=None):
            pass

    class Context:
        pages = [Page()]

        def add_cookies(self, cookies):
            pass

        def cookies(self):
            return []

        def close(self):
            pass

    class Chromium:
        def launch_persistent_context(self, profile, *, headless, channel):
            assert headless is expected
            return Context()

    class Playwright:
        chromium = Chromium()

        def start(self):
            return self

        def stop(self):
            pass

    auth = BrowserAuthenticator(
        {"google": {"browser_headless": headless, "browser_profile": str(tmp_path / "profile")}},
        playwright_factory=Playwright,
    )
    auth._export_cookies = lambda: (_ for _ in ()).throw(AssertionError("noninteractive open must not export cookies"))
    assert auth.open() == "account"
    auth.close()


def test_refresh_session_reloads_existing_photos_page_without_reimport_or_export(tmp_path: Path):
    class Page:
        url = "https://photos.google.com/"

        def __init__(self):
            self.account = "account"
            self.reloads = 0

        def evaluate(self, expression):
            return self.account

        def goto(self, url, wait_until=None):
            pass

        def reload(self, *, wait_until):
            self.reloads += 1
            self.account = "account"

    class Context:
        def __init__(self, page):
            self.pages = [page]
            self.imports = []
            self.cookie_values = [{"name": "SID", "value": "fresh", "domain": ".google.com", "path": "/"}]

        def new_page(self):
            return self.pages[0]

        def add_cookies(self, cookies):
            self.imports.append(cookies)

        def cookies(self):
            return self.cookie_values

        def close(self):
            pass

    page = Page()
    context = Context(page)

    class Chromium:
        def launch_persistent_context(self, profile, *, headless, channel):
            return context

    class Playwright:
        chromium = Chromium()

        def start(self):
            return self

        def stop(self):
            pass

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n.google.com\tTRUE\t/\tTRUE\t0\tSID\tseed\n",
        encoding="utf-8",
    )
    auth = BrowserAuthenticator(
        {"google": {"cookies_file": str(cookie_file), "browser_profile": str(tmp_path / "profile")}},
        playwright_factory=Playwright,
    )
    auth.open()
    original = cookie_file.read_bytes()
    assert auth.refresh_session("account") == context.cookie_values
    assert page.reloads == 1
    assert len(context.imports) == 1
    assert cookie_file.read_bytes() == original
    auth.close()


def test_refresh_session_rejects_account_change(tmp_path: Path):
    class Page:
        url = "https://photos.google.com/"

        def evaluate(self, expression):
            return "other-account"

        def goto(self, url, wait_until=None):
            pass

        def reload(self, *, wait_until):
            pass

    class Context:
        pages = [Page()]

        def add_cookies(self, cookies):
            pass

        def cookies(self):
            return []

        def close(self):
            pass

    class Chromium:
        def launch_persistent_context(self, profile, *, headless, channel):
            return Context()

    class Playwright:
        chromium = Chromium()

        def start(self):
            return self

        def stop(self):
            pass

    auth = BrowserAuthenticator(
        {"google": {"browser_profile": str(tmp_path / "profile")}},
        playwright_factory=Playwright,
    )
    auth.open()
    with pytest.raises(BrowserAuthError, match="account"):
        auth.refresh_session("expected-account")
    auth.close()


def test_open_can_skip_stale_seed_cookie_import(tmp_path: Path):
    class Page:
        url = "https://photos.google.com/"

        def evaluate(self, expression):
            return "account"

        def goto(self, url, wait_until=None):
            pass

    class Context:
        pages = [Page()]

        def add_cookies(self, cookies):
            raise AssertionError("stale seed cookies must not be imported")

        def cookies(self):
            return []

        def close(self):
            pass

    class Chromium:
        def launch_persistent_context(self, profile, *, headless, channel):
            return Context()

    class Playwright:
        chromium = Chromium()

        def start(self):
            return self

        def stop(self):
            pass

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n.google.com\tTRUE\t/\tTRUE\t0\tSID\tstale\n",
        encoding="utf-8",
    )
    auth = BrowserAuthenticator(
        {"google": {"cookies_file": str(cookie_file), "browser_profile": str(tmp_path / "profile")}},
        playwright_factory=Playwright,
    )

    assert auth.open(seed_cookies=False) == "account"
    auth.close()
