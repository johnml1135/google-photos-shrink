from __future__ import annotations

from pathlib import Path

import pytest

from photos_shrink import cli


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "shrink.toml"
    path.write_text(
        "[run]\nwork_dir = 'work'\npause_seconds = 0\nthreads = 1\n", encoding="utf-8"
    )
    return path


def test_normal_run_runs_doctor_before_constructing_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    calls: list[str] = []

    def failed_doctor(settings: object) -> int:
        calls.append("doctor")
        return 1

    class UnexpectedRemote:
        def __init__(self, settings: object) -> None:
            calls.append("remote")
            raise AssertionError("remote must not initialize after doctor failure")

    monkeypatch.setattr(cli, "doctor", failed_doctor)
    monkeypatch.setattr("photos_shrink.remote.GooglePhotosRemote", UnexpectedRemote)
    assert cli.main(["--config", str(config), "--plan-only"]) == 1
    assert calls == ["doctor"]


def test_login_skips_doctor_and_uses_force_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    calls: list[bool] = []

    def unexpected_doctor(settings: object) -> int:
        raise AssertionError("standalone login should not run doctor")

    class LoginRemote:
        def __init__(self, settings: object) -> None:
            pass

        def login(self, *, force: bool = False) -> str:
            calls.append(force)
            return "account"

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "doctor", unexpected_doctor)
    monkeypatch.setattr("photos_shrink.remote.GooglePhotosRemote", LoginRemote)
    assert cli.main(["--config", str(config), "--login"]) == 0
    assert calls == [True]
    assert "login complete" in capsys.readouterr().out


def test_keyboard_interrupt_returns_130_and_closes_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    closed: list[bool] = []

    class InterruptingRemote:
        def __init__(self, settings: object) -> None:
            pass

        def login(self) -> str:
            raise KeyboardInterrupt

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(cli, "doctor", lambda settings: 0)
    monkeypatch.setattr("photos_shrink.remote.GooglePhotosRemote", InterruptingRemote)
    assert cli.main(["--config", str(config), "--plan-only"]) == 130
    assert closed == [True]
    assert "interrupted" in capsys.readouterr().err.lower()


def test_keep_originals_flag_is_available_in_help() -> None:
    parser = cli._parser()
    args = parser.parse_args(["--keep-originals"])
    assert args.keep_originals is True
    assert "keep" in parser.format_help().lower()


def test_selection_flags_are_available() -> None:
    parser = cli._parser()
    args = parser.parse_args(["--photos-only", "--newest-first"])
    assert args.photos_only is True
    assert args.newest_first is True


def test_selection_flags_override_run_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    seen: list[dict] = []

    def stop_after_doctor(settings: object) -> int:
        seen.append(settings.run)
        return 1

    monkeypatch.setattr(cli, "doctor", stop_after_doctor)
    assert cli.main([
        "--config", str(config), "--photos-only", "--newest-first", "--plan-only"
    ]) == 1
    assert seen[0]["photos_only"] is True
    assert seen[0]["selection_order"] == "newest"
