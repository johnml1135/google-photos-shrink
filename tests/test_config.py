from datetime import date
from pathlib import Path

import pytest

from photos_shrink.config import ConfigError, load_config


def test_load_config_resolves_paths_and_fingerprint_is_stable(tmp_path: Path):
    config_path = tmp_path / "shrink.toml"
    config_path.write_text(
        """
[photos]
short_edge = 1200
[run]
work_dir = "state"
[exclude]
timezone = "America/New_York"
date_ranges = [{start = "2024-01-01", end = "2024-01-03"}]
name_globs = ["*.JPG"]
""",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.photos["short_edge"] == 1200
    assert cfg.run["work_dir"] == str(tmp_path / "state")
    assert cfg.tools["ffmpeg"] == "ffmpeg"
    assert cfg.exclude["date_ranges"][0]["start"] == date(2024, 1, 1)
    assert cfg.exclusion_reason({"filename": "IMG_1.jpg", "timestamp_ms": 1704153600000})
    assert cfg.fingerprint == load_config(config_path).fingerprint


def test_load_config_rejects_invalid_ranges_and_unknown_keys(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text("[photos]\nquality = 101\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="quality"):
        load_config(path)

    path.write_text(
        '[exclude]\ndate_ranges = [{start = "2024-01-03", end = "2024-01-01"}]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="date range"):
        load_config(path)


def test_missing_capture_date_is_excluded_with_reason(tmp_path: Path):
    path = tmp_path / "shrink.toml"
    path.write_text("", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.exclusion_reason({"filename": "x.jpg", "timestamp_ms": None}) == "missing_capture_date"


def test_browser_headless_defaults_true_and_must_be_boolean(tmp_path: Path):
    path = tmp_path / "shrink.toml"
    path.write_text("", encoding="utf-8")
    assert load_config(path).google["browser_headless"] is True

    path.write_text("[google]\nbrowser_headless = 'yes'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="browser_headless"):
        load_config(path)


def test_pilot_selection_defaults_and_validation(tmp_path: Path):
    path = tmp_path / "shrink.toml"
    path.write_text("", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.google["session_refresh_seconds"] == 300
    assert cfg.run["photos_only"] is False
    assert cfg.run["selection_order"] == "largest"

    path.write_text("[google]\nsession_refresh_seconds = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="session_refresh_seconds"):
        load_config(path)
    path.write_text("[run]\nphotos_only = 'yes'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="photos_only"):
        load_config(path)
    path.write_text("[run]\nselection_order = 'oldest'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="selection_order"):
        load_config(path)
    path.write_text("[run]\nselection_order = ['newest']\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="selection_order"):
        load_config(path)
    path.write_text("[google]\nsession_refresh_seconds = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="session_refresh_seconds"):
        load_config(path)


def test_control_settings_do_not_change_encoding_fingerprint(tmp_path: Path):
    path = tmp_path / "shrink.toml"
    path.write_text("", encoding="utf-8")
    original = load_config(path).fingerprint
    path.write_text(
        "[google]\nsession_refresh_seconds = 0\n"
        "[run]\nphotos_only = true\nselection_order = 'newest'\nlimit = 1\n",
        encoding="utf-8",
    )
    assert load_config(path).fingerprint == original
