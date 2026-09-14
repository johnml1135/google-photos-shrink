"""Read a Google Takeout Photos export and pair media with its sidecar metadata.

Takeout separates each media file from its metadata, and the sidecar naming is
lossy: Google appends ``.supplemental-metadata.json`` but truncates the whole
name to a fixed budget, migrates duplicate counters across the extension, and
clips ``-edited`` suffixes. Every de-truncation here is self-validating -- a
candidate is only accepted when it resolves to a sidecar that actually exists,
so a wrong guess yields "no sidecar" rather than another item's timestamps.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPLEMENTAL = "supplemental-metadata"
EDITED = "-edited"

PHOTO_SUFFIXES = frozenset(
    {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
        ".avif", ".tif", ".tiff", ".bmp", ".ico",
    }
)
VIDEO_SUFFIXES = frozenset(
    {
        ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".mpg", ".mpeg",
        ".3gp", ".3g2", ".mts", ".m2ts", ".mod", ".divx",
    }
)

_COUNTER = re.compile(r"^(?P<base>.*)\((?P<counter>\d+)\)$")


class TakeoutError(RuntimeError):
    """Raised when a Takeout export cannot be read safely."""


@dataclass(frozen=True)
class TakeoutRecord:
    """One media file from the export, joined to its sidecar when one resolves."""

    path: Path
    sidecar: Path | None
    kind: str
    size_bytes: int
    edited: bool
    album: str | None
    title: str | None = None
    taken_timestamp_ms: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    people: tuple[str, ...] = ()
    sha256: str | None = None

    @property
    def has_metadata(self) -> bool:
        return self.sidecar is not None and self.taken_timestamp_ms is not None


def _split_counter(text: str) -> tuple[str, str | None]:
    match = _COUNTER.match(text)
    if match is None:
        return text, None
    return match.group("base"), match.group("counter")


def _strip_supplemental(stem: str) -> str:
    """Drop a trailing ``.supplemental-metadata`` however far it was truncated."""

    base, dot, last = stem.rpartition(".")
    if dot and last and SUPPLEMENTAL.startswith(last):
        return base
    return stem


def sidecar_target(json_path: str | Path) -> str:
    """Return the media filename a sidecar claims to describe."""

    name = Path(json_path).name
    if not name.endswith(".json"):
        raise TakeoutError(f"not a sidecar: {name}")
    stem = name[: -len(".json")]
    stem, trailing_counter = _split_counter(stem)
    stem = _strip_supplemental(stem)
    stem, inner_counter = _split_counter(stem)
    counter = trailing_counter or inner_counter
    if counter is None:
        return stem
    base, dot, suffix = stem.rpartition(".")
    if dot:
        return f"{base}({counter}).{suffix}"
    return f"{stem}({counter})"


def index_sidecars(directory: str | Path) -> dict[str, Path]:
    """Map each claimed media filename to its sidecar within one directory."""

    index: dict[str, Path] = {}
    for path in sorted(Path(directory).glob("*.json")):
        if path.name == "metadata.json":
            continue
        try:
            target = sidecar_target(path)
        except TakeoutError:
            continue
        index.setdefault(target, path)
    return index


def _split_suffix(name: str) -> tuple[str, str]:
    """Split a filename into its base name and extension."""

    stem, dot, suffix = name.rpartition(".")
    if not dot:
        return name, ""
    return stem, suffix


def _edited_candidates(name: str) -> Iterator[str]:
    """Yield the un-edited filenames a ``-edited`` variant may have come from."""

    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    for length in range(len(EDITED), 1, -1):
        marker = EDITED[:length]
        if stem.endswith(marker):
            trimmed = stem[: -len(marker)]
            yield f"{trimmed}.{suffix}" if suffix else trimmed


def resolve_sidecar(media_name: str, index: dict[str, Path]) -> tuple[Path | None, bool]:
    """Find the sidecar for a media file, reporting whether it is an edited variant.

    Candidates are tried most-specific first and only accepted on a real hit, so
    truncation guessing can never silently bind a file to the wrong metadata.
    """

    exact = index.get(media_name)
    if exact is not None:
        return exact, False

    for candidate in _edited_candidates(media_name):
        hit = index.get(candidate)
        if hit is not None:
            return hit, True

    # Google truncates the base name and re-appends the extension, so a sidecar
    # target is a prefix of the real *stem* rather than of the whole filename.
    # Accept only when exactly one sidecar could be the source.
    stem, suffix = _split_suffix(media_name)
    prefixes = []
    for key in index:
        key_stem, key_suffix = _split_suffix(key)
        if key_suffix.lower() == suffix.lower() and len(key_stem) < len(stem) and stem.startswith(key_stem):
            prefixes.append(key)
    if len(prefixes) == 1:
        return index[prefixes[0]], False
    return None, False


def parse_sidecar(path: str | Path) -> dict[str, Any]:
    """Extract the fields worth carrying forward from a sidecar."""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TakeoutError(f"unreadable sidecar: {path}") from exc
    if not isinstance(raw, dict):
        raise TakeoutError(f"malformed sidecar: {path}")

    taken = raw.get("photoTakenTime") or raw.get("creationTime") or {}
    timestamp_ms: int | None = None
    if isinstance(taken, dict) and taken.get("timestamp") is not None:
        try:
            timestamp_ms = int(taken["timestamp"]) * 1000
        except (TypeError, ValueError):
            timestamp_ms = None

    geo = raw.get("geoDataExif") or raw.get("geoData") or {}
    latitude = longitude = None
    if isinstance(geo, dict):
        lat, lon = geo.get("latitude"), geo.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
            latitude, longitude = float(lat), float(lon)

    people: list[str] = []
    for entry in raw.get("people") or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            people.append(entry["name"])

    title = raw.get("title")
    return {
        "title": title if isinstance(title, str) and title else None,
        "taken_timestamp_ms": timestamp_ms,
        "latitude": latitude,
        "longitude": longitude,
        "people": tuple(people),
    }


def sha256(path: str | Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _album_name(directory: Path, root: Path) -> str | None:
    """Return the album a directory represents, or None for a date bucket."""

    if directory == root:
        return None
    metadata = directory / "metadata.json"
    if not metadata.exists():
        return None
    try:
        raw = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return directory.name
    title = raw.get("title") if isinstance(raw, dict) else None
    return title if isinstance(title, str) and title else directory.name


def classify(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in PHOTO_SUFFIXES:
        return "photo"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return None


def scan(root: str | Path, *, compute_hash: bool = False) -> Iterator[TakeoutRecord]:
    """Walk a Takeout export, yielding one record per media file."""

    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise TakeoutError(f"takeout root is not a directory: {base}")

    directories = sorted({path.parent for path in base.rglob("*") if path.is_file()})
    for directory in directories:
        index = index_sidecars(directory)
        album = _album_name(directory, base)
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            kind = classify(path)
            if kind is None:
                continue
            sidecar, edited = resolve_sidecar(path.name, index)
            fields: dict[str, Any] = {
                "title": None,
                "taken_timestamp_ms": None,
                "latitude": None,
                "longitude": None,
                "people": (),
            }
            if sidecar is not None:
                try:
                    fields = parse_sidecar(sidecar)
                except TakeoutError:
                    sidecar = None
            yield TakeoutRecord(
                path=path,
                sidecar=sidecar,
                kind=kind,
                size_bytes=path.stat().st_size,
                edited=edited,
                album=album,
                sha256=sha256(path) if compute_hash else None,
                **fields,
            )
