"""Configuration loading and validation for photos-shrink."""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Raised when shrink.toml is invalid."""


DEFAULTS: dict[str, dict[str, Any]] = {
    "photos": {"short_edge": 1500, "format": "avif", "quality": 60},
    "videos": {"long_edge": 1920, "short_edge": 1080, "codec": "hevc", "crf": 28,
               "preset": "slow", "max_fps": 0, "audio_bitrate_kbps": 96},
    "tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
    "google": {"cookies_file": ".photos-shrink/cookies.txt", "browser_profile": ".photos-shrink/browser",
               "browser_channel": "chrome", "browser_headless": True, "account_index": 0,
               "session_refresh_seconds": 300, "upload_timeout_seconds": 300,
               "upload_poll_seconds": 5, "auto_original_quality": True},
    "run": {"work_dir": ".photos-shrink", "pause_seconds": 10, "minimum_savings_percent": 20,
            "threads": 2, "skip_shared": True, "limit": 0, "skip_non_space_consuming": True,
            "photos_only": False, "selection_order": "largest"},
    "exclude": {"timezone": "America/New_York", "date_ranges": [], "name_globs": []},
}
ALLOWED = {name: set(values) for name, values in DEFAULTS.items()}


def _merge(raw: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for section, values in raw.items():
        if section not in ALLOWED or not isinstance(values, dict):
            raise ConfigError(f"unknown configuration section: {section}")
        unknown = set(values) - ALLOWED[section]
        if unknown:
            raise ConfigError(f"unknown {section} key: {min(unknown)}")
        result[section].update(values)
    return result


def _positive_int(value: Any, key: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{key} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    path: Path
    photos: dict[str, Any]
    videos: dict[str, Any]
    tools: dict[str, Any]
    google: dict[str, Any]
    run: dict[str, Any]
    exclude: dict[str, Any]
    fingerprint: str

    @property
    def config_dir(self) -> Path:
        return self.path.parent

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {key: copy.deepcopy(getattr(self, key)) for key in DEFAULTS}

    def __getitem__(self, key: str) -> dict[str, Any]:
        return getattr(self, key)

    def matches_exclusion(self, filename: str, timestamp_ms: int | None) -> bool:
        return self.exclusion_reason({"filename": filename, "timestamp_ms": timestamp_ms}) is not None

    def exclusion_reason(self, item: dict[str, Any]) -> str | None:
        filename = str(item.get("filename") or "")
        globs = self.exclude["name_globs"]
        if any(fnmatch.fnmatchcase(filename.casefold(), str(pattern).casefold()) for pattern in globs):
            return "excluded_name"
        timestamp_ms = item.get("timestamp_ms")
        if timestamp_ms is None:
            return "missing_capture_date"
        try:
            local_date = datetime.fromtimestamp(float(timestamp_ms) / 1000, UTC).astimezone(
                ZoneInfo(self.exclude["timezone"])
            ).date()
        except (TypeError, ValueError, OSError):
            return "invalid_capture_date"
        for date_range in self.exclude["date_ranges"]:
            if date_range["start"] <= local_date <= date_range["end"]:
                return "excluded_date"
        return None


def load_config(path: str | os.PathLike[str] = "shrink.toml") -> Settings:
    path = Path(path).expanduser().resolve()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    if not path.exists():
        raise ConfigError(f"configuration file not found: {path}")
    values = _merge(raw, DEFAULTS)
    if not isinstance(values["photos"]["format"], str) or not isinstance(values["videos"]["codec"], str):
        raise ConfigError("photos.format and videos.codec must be strings")
    if not isinstance(values["exclude"]["name_globs"], list) or not all(isinstance(v, str) for v in values["exclude"]["name_globs"]):
        raise ConfigError("exclude.name_globs must be a list of strings")
    if not isinstance(values["exclude"]["date_ranges"], list):
        raise ConfigError("exclude.date_ranges must be a list")
    if not isinstance(values["run"]["skip_shared"], bool) or not isinstance(values["run"]["skip_non_space_consuming"], bool):
        raise ConfigError("run.skip_shared and run.skip_non_space_consuming must be booleans")
    if not isinstance(values["run"]["photos_only"], bool):
        raise ConfigError("run.photos_only must be a boolean")
    if not isinstance(values["run"]["selection_order"], str) or values["run"]["selection_order"] not in {"largest", "newest"}:
        raise ConfigError("run.selection_order must be largest or newest")
    if isinstance(values["google"]["account_index"], bool) or not isinstance(values["google"]["account_index"], int) or values["google"]["account_index"] < 0:
        raise ConfigError("google.account_index must be an integer >= 0")
    if not isinstance(values["google"]["browser_headless"], bool):
        raise ConfigError("google.browser_headless must be a boolean")
    if not isinstance(values["google"]["auto_original_quality"], bool):
        raise ConfigError("google.auto_original_quality must be a boolean")
    _positive_int(values["google"]["session_refresh_seconds"], "google.session_refresh_seconds")
    _positive_int(values["google"]["upload_timeout_seconds"], "google.upload_timeout_seconds", 1)
    _positive_int(values["google"]["upload_poll_seconds"], "google.upload_poll_seconds", 1)
    _positive_int(values["photos"]["short_edge"], "photos.short_edge", 1)
    _positive_int(values["photos"]["quality"], "photos.quality", 1)
    if values["photos"]["quality"] > 100:
        raise ConfigError("photos.quality must be between 1 and 100")
    for key in ("long_edge", "short_edge", "max_fps", "audio_bitrate_kbps"):
        _positive_int(values["videos"][key], f"videos.{key}")
    _positive_int(values["videos"]["crf"], "videos.crf")
    if values["videos"]["crf"] > 51:
        raise ConfigError("videos.crf must be between 0 and 51")
    if values["videos"]["long_edge"] == 0 or values["videos"]["short_edge"] == 0:
        raise ConfigError("video dimensions must be positive")
    if values["videos"]["audio_bitrate_kbps"] == 0:
        raise ConfigError("videos.audio_bitrate_kbps must be positive")
    if values["videos"]["preset"] not in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"}:
        raise ConfigError("videos.preset is not a supported ffmpeg preset")
    for key in ("pause_seconds", "threads", "limit"):
        _positive_int(values["run"][key], f"run.{key}")
    if values["run"]["threads"] == 0:
        raise ConfigError("run.threads must be at least 1")
    minimum = values["run"]["minimum_savings_percent"]
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not 0 <= minimum <= 100:
        raise ConfigError("run.minimum_savings_percent must be between 0 and 100")
    if values["photos"]["format"].lower() != "avif":
        raise ConfigError("photos.format currently supports only avif")
    if values["videos"]["codec"].lower() != "hevc":
        raise ConfigError("videos.codec currently supports only hevc")
    try:
        ZoneInfo(values["exclude"]["timezone"])
    except (ZoneInfoNotFoundError, TypeError):
        raise ConfigError(f"unknown exclude.timezone: {values['exclude']['timezone']}")
    parsed_ranges: list[dict[str, date]] = []
    for index, date_range in enumerate(values["exclude"]["date_ranges"]):
        if not isinstance(date_range, dict) or set(date_range) != {"start", "end"}:
            raise ConfigError(f"exclude.date_ranges[{index}] must contain start and end")
        try:
            start = date.fromisoformat(str(date_range["start"]))
            end = date.fromisoformat(str(date_range["end"]))
        except ValueError as exc:
            raise ConfigError(f"invalid date range at index {index}") from exc
        if start > end:
            raise ConfigError("date range start must not be after end")
        parsed_ranges.append({"start": start, "end": end})
    values["exclude"]["date_ranges"] = parsed_ranges
    for section, keys in (("google", ("cookies_file", "browser_profile")), ("run", ("work_dir",))):
        for key in keys:
            if not isinstance(values[section][key], str) or not values[section][key].strip():
                raise ConfigError(f"{section}.{key} must be a non-empty path")
            candidate = Path(str(values[section][key])).expanduser()
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            values[section][key] = str(candidate.resolve())
    for key in ("ffmpeg", "ffprobe"):
        tool = Path(str(values["tools"][key])).expanduser()
        if tool.is_absolute() or len(tool.parts) > 1:
            if not tool.is_absolute():
                tool = path.parent / tool
            values["tools"][key] = str(tool.resolve())
    canonical = _jsonable({"photos": values["photos"], "videos": values["videos"],
                           "tools": values["tools"], "threads": values["run"]["threads"]})
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return Settings(path, *(values[name] for name in DEFAULTS), fingerprint)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def default_config_text() -> str:
    return """[photos]
short_edge = 1500
format = "avif"
quality = 60

[videos]
long_edge = 1920
short_edge = 1080
codec = "hevc"
crf = 28
preset = "slow"
max_fps = 0
audio_bitrate_kbps = 96

[tools]
ffmpeg = "ffmpeg"
ffprobe = "ffprobe"

[google]
cookies_file = ".photos-shrink/cookies.txt"
browser_profile = ".photos-shrink/browser"
browser_channel = "chrome"
browser_headless = true
account_index = 0
session_refresh_seconds = 300
upload_timeout_seconds = 300
upload_poll_seconds = 5
auto_original_quality = true

[run]
work_dir = ".photos-shrink"
pause_seconds = 10
minimum_savings_percent = 20
threads = 2
skip_shared = true
limit = 0
skip_non_space_consuming = true
photos_only = false
selection_order = "largest"

[exclude]
timezone = "America/New_York"
date_ranges = []
name_globs = []
"""
