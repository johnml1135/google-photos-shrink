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

    path.write_text("[google]\nsession_refresh_seconds = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="session_refresh_seconds"):
        load_config(path)
    path.write_text("[run]\nphotos_only = 'yes'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="photos_only"):
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
        "[run]\nphotos_only = true\n",
        encoding="utf-8",
    )
    assert load_config(path).fingerprint == original


def test_the_shipped_shrink_toml_actually_loads():
    """The suite builds its own configs, so nothing else reads the real one.

    A key removed from DEFAULTS but left in the shipped shrink.toml passes
    every other test here and then fails at startup for every tool.
    """

    from photos_shrink.config import load_config as _load

    shipped = Path(__file__).resolve().parents[1] / "shrink.toml"
    assert _load(shipped).run["work_dir"]


def test_data_dir_falls_back_to_work_dir_when_unset(tmp_path: Path):
    """A single-drive setup keeps working with no data_dir at all."""

    path = tmp_path / "shrink.toml"
    path.write_text("[run]\nwork_dir = 'state'\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.run["data_dir"] == cfg.run["work_dir"] == str((tmp_path / "state").resolve())


def test_data_dir_is_separate_from_work_dir_when_set(tmp_path: Path):
    """Credentials stay in work_dir; the bulk data goes wherever data_dir says."""

    data = tmp_path / "elsewhere" / "takeout-work"
    path = tmp_path / "shrink.toml"
    path.write_text(f"[run]\nwork_dir = 'state'\ndata_dir = '{data.as_posix()}'\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.run["work_dir"] == str((tmp_path / "state").resolve())
    assert cfg.run["data_dir"] == str(data.resolve())


def test_a_relative_data_dir_resolves_against_the_config_file(tmp_path: Path):
    path = tmp_path / "shrink.toml"
    path.write_text("[run]\ndata_dir = 'bulk'\n", encoding="utf-8")
    assert load_config(path).run["data_dir"] == str((tmp_path / "bulk").resolve())


def test_data_dir_does_not_change_the_encoding_fingerprint(tmp_path: Path):
    """Moving where outputs live must not mark every existing output stale."""

    path = tmp_path / "shrink.toml"
    path.write_text("", encoding="utf-8")
    original = load_config(path).fingerprint
    path.write_text("[run]\ndata_dir = 'G:/somewhere-else'\n", encoding="utf-8")
    assert load_config(path).fingerprint == original


class TestVideoEncoderSettings:
    """Each encoder's quality scale and presets are its own."""

    def _config(self, tmp_path, body):
        path = tmp_path / "shrink.toml"
        path.write_text(f"[videos]\n{body}\n", encoding="utf-8")
        return path

    def test_av1_is_the_default_encoder(self, tmp_path):
        settings = load_config(self._config(tmp_path, "crf = 36"))
        assert settings.videos["encoder"] == "libsvtav1"
        assert settings.videos["preset"] == "8"

    def test_av1_accepts_a_crf_above_x265s_limit(self, tmp_path):
        """AV1's scale runs to 63; 51 is x265's ceiling, not AV1's."""

        assert load_config(self._config(tmp_path, "crf = 60")).videos["crf"] == 60

    def test_x265_still_refuses_a_crf_above_51(self, tmp_path):
        with pytest.raises(ConfigError, match="between 0 and 51"):
            load_config(self._config(tmp_path, 'encoder = "libx265"\ncrf = 60\npreset = "slow"'))

    def test_a_word_preset_is_refused_for_av1(self, tmp_path):
        with pytest.raises(ConfigError, match="not a supported preset"):
            load_config(self._config(tmp_path, 'preset = "slow"'))

    def test_a_numeric_preset_is_refused_for_x265(self, tmp_path):
        with pytest.raises(ConfigError, match="not a supported preset"):
            load_config(self._config(tmp_path, 'encoder = "libx265"\ncrf = 30\npreset = "8"'))

    def test_nvenc_takes_its_own_presets(self, tmp_path):
        settings = load_config(self._config(tmp_path, 'encoder = "hevc_nvenc"\ncrf = 32\npreset = "p7"'))
        assert settings.videos["preset"] == "p7"

    def test_an_unknown_encoder_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="videos.encoder must be one of"):
            load_config(self._config(tmp_path, 'encoder = "libmagic"'))
