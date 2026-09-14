from __future__ import annotations

import re
from http.cookiejar import MozillaCookieJar
from pathlib import Path

import pytest
import requests

from photos_shrink.auth import (
    BrowserAuthenticator,
    BrowserAuthError,
    NetscapeCookie,
    UploadNotStartedError,
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


def test_upload_uses_add_menu_then_import_menuitem_file_chooser(tmp_path: Path):
    class Locator:
        def __init__(self, count=0):
            self.count_value = count
            self.calls = []

        def count(self):
            return self.count_value

        def click(self):
            self.calls.append("click")

    class Chooser:
        def __init__(self):
            self.paths = []

        def set_files(self, path):
            self.paths.append(path)

    class ExpectChooser:
        def __init__(self, chooser):
            self.chooser = chooser

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        @property
        def value(self):
            return self.chooser

    class Page:
        def __init__(self):
            self.file_inputs = Locator()
            self.add_button = Locator()
            self.import_menuitem = Locator()
            self.chooser = Chooser()
            self.role_calls = []

        def locator(self, selector):
            assert selector == 'input[type="file"]'
            return self.file_inputs

        def get_by_role(self, role, *, name, exact):
            self.role_calls.append((role, name, exact))
            if role == "button":
                return self.add_button
            return self.import_menuitem

        def expect_file_chooser(self, *, timeout):
            assert timeout == 10000
            return ExpectChooser(self.chooser)

    page = Page()
    auth = BrowserAuthenticator({"google": {"browser_profile": str(tmp_path / "profile")}})
    auth.page = page
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"photo")

    auth.upload(path)

    assert page.role_calls == [
        ("button", "Create and add photos", True),
        ("menuitem", "Import photos from your computer", True),
    ]
    assert page.add_button.calls == ["click"]
    assert page.import_menuitem.calls == ["click"]
    assert page.chooser.paths == [str(path.resolve())]


def test_upload_control_failure_is_marked_before_submission(tmp_path: Path):
    class Locator:
        def count(self):
            return 0

        def click(self):
            raise AssertionError("button click should not be attempted")

    class Page:
        def locator(self, selector):
            return Locator()

        def get_by_role(self, role, *, name, exact):
            return Locator()

    auth = BrowserAuthenticator({"google": {"browser_profile": str(tmp_path / "profile")}})
    auth.page = Page()
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"photo")

    with pytest.raises(UploadNotStartedError):
        auth.upload(path)


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


def test_ensure_original_quality_selects_and_verifies_setting(tmp_path: Path):
    class Radio:
        def __init__(self):
            self.checked = False
            self.checks = 0

        def is_checked(self):
            return self.checked

        def count(self):
            return 1

        def check(self):
            self.checks += 1
            self.checked = True

    class Page:
        url = "https://photos.google.com/"

        def __init__(self):
            self.radio = Radio()
            self.goto_calls = []
            self.reloads = 0

        def evaluate(self, expression):
            return "account"

        def goto(self, url, wait_until=None):
            self.goto_calls.append((url, wait_until))
            self.url = url

        def reload(self, *, wait_until):
            self.reloads += 1

        def get_by_role(self, role, *, name):
            assert role == "radio"
            assert re.match(name, "Original quality")
            return self.radio

    page = Page()
    auth = BrowserAuthenticator({"google": {"browser_profile": str(tmp_path / "profile")}})
    auth.page = page
    auth.context = object()

    auth.ensure_original_quality("account")

    assert page.radio.checks == 1
    assert page.reloads == 1
    assert page.goto_calls == [
        ("https://photos.google.com/settings", "domcontentloaded"),
        ("https://photos.google.com/", "domcontentloaded"),
    ]


def test_ensure_original_quality_is_idempotent_when_already_selected(tmp_path: Path):
    class Radio:
        def count(self):
            return 1

        def is_checked(self):
            return True

        def check(self):
            raise AssertionError("already selected radio must not be checked")

    class Page:
        url = "https://photos.google.com/settings"

        def evaluate(self, expression):
            return "account"

        def get_by_role(self, role, *, name):
            return Radio()

        def reload(self, *, wait_until):
            pass

        def goto(self, url, wait_until=None):
            self.url = url

    auth = BrowserAuthenticator({"google": {"browser_profile": str(tmp_path / "profile")}})
    auth.page = Page()
    auth.context = object()
    auth.ensure_original_quality("account")


def test_ensure_original_quality_fails_closed_when_control_is_missing(tmp_path: Path):
    class Page:
        url = "https://photos.google.com/settings"

        def evaluate(self, expression):
            return "account"

        def get_by_role(self, role, *, name):
            raise RuntimeError("not found")

        def goto(self, url, wait_until=None):
            self.url = url

    auth = BrowserAuthenticator({"google": {"browser_profile": str(tmp_path / "profile")}})
    auth.page = Page()
    auth.context = object()
    with pytest.raises(BrowserAuthError, match="Original quality"):
        auth.ensure_original_quality("account")
