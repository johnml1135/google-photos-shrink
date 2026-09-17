"""Local, conservative media probing and transcoding helpers."""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .integrity import sha256_file as _sha256


class MediaError(RuntimeError):
    """Base class for media processing failures."""


class UnsupportedMediaError(MediaError):
    """The input format is known but deliberately not processed."""


class VerificationError(MediaError):
    """A transcoded file did not satisfy the preservation contract."""


_RAW_SUFFIXES = {
    ".arw",
    ".cr2",
    ".cr3",
    ".dng",
    ".nef",
    ".orf",
    ".pef",
    ".raf",
    ".rw2",
    ".srw",
}
_UNSUPPORTED_STILL_SUFFIXES = {".heic", ".heif", ".jxl"}


def _tool(settings: dict[str, Any], name: str, default: str) -> str:
    return str(settings.get("tools", {}).get(name, default))


def _rotation(stream: dict[str, Any]) -> int:
    tags = stream.get("tags") or {}
    value = tags.get("rotate", 0)
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            value = side_data["rotation"]
            break
    try:
        return int(round(float(value))) % 360
    except (TypeError, ValueError):
        return 0


def _duration(stream: dict[str, Any], fmt: dict[str, Any]) -> float | None:
    value = stream.get("duration", fmt.get("duration"))
    try:
        duration = float(value) if value is not None else None
        return (
            duration
            if duration is not None and math.isfinite(duration) and duration >= 0
            else None
        )
    except (TypeError, ValueError):
        return None


def _frame_rate(stream: dict[str, Any]) -> float | None:
    value = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    try:
        numerator, denominator = str(value).split("/", 1)
        denominator = float(denominator)
        return float(numerator) / denominator if denominator else None
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None


def _image_probe(path: Path) -> dict[str, Any] | None:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        return None
    if path.suffix.lower() in {".heic", ".heif", ".hif"}:
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener(thumbnails=False)
        except ImportError:
            return None
    try:
        image = Image.open(path)
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    with image:
        width, height = image.size
        skip_reason = None
        try:
            exif = image.getexif()
            orientation = exif.get(274, 1)
            # 0 is outside the 1-8 the spec allows, but cameras write it to mean
            # "not specified" and every viewer, Google Photos included, renders
            # it unrotated. Treating it as malformed excluded real photos.
            if orientation == 0:
                orientation = 1
            if orientation not in range(1, 9):
                skip_reason = "malformed EXIF orientation metadata"
            elif orientation in (5, 6, 7, 8):
                width, height = height, width
        except Exception:
            skip_reason = "malformed EXIF orientation metadata"
        if path.suffix.lower() in _RAW_SUFFIXES:
            skip_reason = "RAW image is unsupported"
        elif getattr(image, "n_frames", 1) > 1:
            skip_reason = "animated image is unsupported"
        metadata = image.info.get("xmp", b"")
        if isinstance(metadata, str):
            metadata = metadata.encode("utf-8", "ignore")
        bit_depth = image.info.get("bit_depth")
        nclx_profile = image.info.get("nclx_profile")
        transfer = (
            nclx_profile.get("transfer_characteristics")
            if isinstance(nclx_profile, dict)
            else None
        )
        gainmap = any(
            "gainmap" in str(key).lower() or "hdrgm" in str(key).lower()
            for key in image.info
        )
        try:
            high_bit_depth = bit_depth is not None and int(bit_depth) > 8
        except (TypeError, ValueError):
            high_bit_depth = False
        if b"MotionPhoto" in metadata or b"MicroVideo" in metadata:
            skip_reason = "motion photo is unsupported"
        elif image.mode == "CMYK" and image.info.get("icc_profile"):
            skip_reason = "CMYK image with ICC profile is unsupported"
        elif (
            image.mode in {"I;16", "I;16B", "I;16L", "F"}
            or high_bit_depth
            or transfer in {16, 18}
            or image.info.get("hdr")
            or gainmap
            or b"GainMap" in metadata
            or b"hdrgm" in metadata
        ):
            skip_reason = "HDR image is unsupported"

        return {
            "kind": "photo",
            "width": int(width),
            "height": int(height),
            "size_bytes": path.stat().st_size,
            "format": str(image.format or path.suffix.lstrip(".")).lower(),
            "codec": str(image.format or path.suffix.lstrip(".")).lower(),
            "duration_seconds": None,
            "frame_rate": None,
            "has_audio": False,
            "skip_reason": skip_reason,
        }


def _ffprobe_json(path: Path, ffprobe: str) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        os.fspath(path),
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        return json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise MediaError(f"ffprobe failed for {path}: {exc}") from exc


def probe(path: str | os.PathLike[str], ffprobe: str = "ffprobe") -> dict[str, Any]:
    """Return normalized media facts, including an explicit safe skip reason."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    image_info = _image_probe(source)
    if image_info is not None:
        return image_info
    if source.suffix.lower() in _UNSUPPORTED_STILL_SUFFIXES and image_info is None:
        return {
            "kind": "unknown",
            "width": None,
            "height": None,
            "size_bytes": source.stat().st_size,
            "format": source.suffix.lstrip(".").lower(),
            "codec": None,
            "duration_seconds": None,
            "has_audio": False,
            "frame_rate": None,
            "skip_reason": "unsupported still image format",
        }

    try:
        data = _ffprobe_json(source, ffprobe)
    except MediaError:
        return {
            "kind": "unknown",
            "width": None,
            "height": None,
            "size_bytes": source.stat().st_size,
            "format": source.suffix.lstrip(".").lower() or None,
            "codec": None,
            "duration_seconds": None,
            "has_audio": False,
            "frame_rate": None,
            "skip_reason": "unsupported or unreadable media",
        }
    streams = data.get("streams") or []
    # A cover thumbnail is reported as a video stream with attached_pic set.
    # Counting it as a second video track rejected whole camera clips over an
    # embedded still that `-map 0:v:0` was never going to copy anyway.
    video_streams = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video"
        and not (stream.get("disposition") or {}).get("attached_pic")
    ]
    video = next(
        iter(video_streams), None
    )
    if video is None:
        return {
            "kind": "unknown",
            "width": None,
            "height": None,
            "size_bytes": source.stat().st_size,
            "format": (data.get("format") or {}).get("format_name"),
            "codec": None,
            "duration_seconds": None,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "frame_rate": None,
            "skip_reason": "no video stream",
        }

    raw_width, raw_height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = _rotation(video)
    width, height = (
        (raw_height, raw_width) if rotation in (90, 270) else (raw_width, raw_height)
    )
    transfer = str(video.get("color_transfer") or "").lower()
    skip_reason = (
        "multiple video tracks are unsupported" if len(video_streams) > 1 else None
    )
    if skip_reason is None and transfer in {"smpte2084", "arib-std-b67"}:
        skip_reason = "HDR video is unsupported"
    duration = _duration(video, data.get("format") or {})
    audio_streams = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]
    # `data` is deliberately not here. Phones attach timecode and motion
    # metadata tracks to ordinary clips -- an iPhone .MOV carries three -- and
    # treating those as content rejected the video outright. They are not
    # something a viewer sees, they do not survive a re-encode anywhere, and
    # the ffmpeg command maps only the first video and audio stream, so they
    # were already being dropped rather than silently mangled. Subtitles and
    # attachments stay: those a viewer would miss.
    extra_streams = [
        stream
        for stream in streams
        if stream.get("codec_type") in {"subtitle", "attachment"}
    ]
    if skip_reason is None:
        if duration is None:
            skip_reason = "video duration is missing or non-finite"
        elif len(audio_streams) > 1 or extra_streams:
            skip_reason = "multiple audio tracks or subtitles are unsupported"
    return {
        "kind": "video",
        "width": width,
        "height": height,
        "size_bytes": source.stat().st_size,
        "format": (data.get("format") or {}).get("format_name"),
        "codec": video.get("codec_name"),
        "duration_seconds": duration,
        "has_audio": bool(audio_streams),
        "frame_rate": _frame_rate(video),
        "skip_reason": skip_reason,
    }


def _bound(value: int, maximum: int) -> float:
    return maximum / value if value > maximum else 1.0


def target_dimensions(
    width: int, height: int, kind: str, settings: dict[str, Any]
) -> tuple[int, int]:
    """Calculate bounded dimensions while preserving the source aspect ratio."""
    if width <= 0 or height <= 0:
        raise ValueError("media dimensions must be positive")
    if kind == "photo":
        maximum = int(settings.get("photos", {}).get("short_edge", 1500))
        scale = min(1.0, _bound(min(width, height), maximum))
    elif kind == "video":
        options = settings.get("videos", {})
        scale = min(
            1.0,
            _bound(max(width, height), int(options.get("long_edge", 1920))),
            _bound(min(width, height), int(options.get("short_edge", 1080))),
        )
    else:
        raise ValueError(f"unknown media kind: {kind}")
    if scale == 1:
        if kind == "video":
            return max(1, width - width % 2), max(1, height - height % 2)
        return width, height
    new_width, new_height = int(round(width * scale)), int(round(height * scale))
    if kind == "video":
        # yuv420p, used by the HEVC target, requires even dimensions.
        new_width = max(1, new_width - new_width % 2)
        new_height = max(1, new_height - new_height % 2)
    return max(new_width, 1), max(new_height, 1)


def _ensure_distinct(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        raise ValueError("refusing to overwrite source")
    if (
        destination.exists()
        and source.exists()
        and os.path.samefile(source, destination)
    ):
        raise ValueError("refusing to overwrite source")
    destination.parent.mkdir(parents=True, exist_ok=True)


def _encode_photo(source: Path, destination: Path, settings: dict[str, Any]) -> None:
    from PIL import Image, ImageOps
    from PIL.TiffImagePlugin import IFDRational

    try:
        with Image.open(source) as opened:
            if getattr(opened, "n_frames", 1) > 1:
                raise UnsupportedMediaError("animated image is unsupported")
            image = ImageOps.exif_transpose(opened)
            output_format = str(
                settings.get("photos", {}).get("format", "avif")
            ).upper()
            if output_format != "AVIF":
                raise UnsupportedMediaError(
                    f"unsupported photo output format: {output_format}"
                )
            source_mode = image.mode
            threads = max(1, int(settings.get("run", {}).get("threads", 2) or 2))
            kwargs: dict[str, Any] = {
                "format": "AVIF",
                "quality": int(settings.get("photos", {}).get("quality", 60)),
                "max_threads": threads,
            }
            if image.info.get("icc_profile"):
                kwargs["icc_profile"] = image.info["icc_profile"]
            exif = image.getexif()
            metadata = settings.get("source_metadata", {})
            latitude, longitude = metadata.get("latitude"), metadata.get("longitude")
            if latitude is not None or longitude is not None:
                import math

                if any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in (latitude, longitude)):
                    raise UnsupportedMediaError("invalid source location")
                if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                    raise UnsupportedMediaError("invalid source location")
                def dms(value):
                    minutes, seconds = divmod(round(abs(value) * 3600 * 10_000_000), 60 * 10_000_000)
                    degrees, minutes = divmod(minutes, 60)
                    return (IFDRational(degrees), IFDRational(minutes), IFDRational(seconds, 10_000_000))

                gps = dict(exif.get_ifd(34853))
                gps.update({0: b"\x02\x03\x00\x00", 1: "S" if latitude < 0 else "N",
                            2: dms(latitude), 3: "W" if longitude < 0 else "E", 4: dms(longitude)})
                exif[34853] = gps
            if exif:
                kwargs["exif"] = exif.tobytes()
            if image.mode == "P" and "transparency" in image.info:
                image = image.convert("RGBA")
            elif image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            if source_mode == "CMYK":
                # The source CMYK profile describes a color space discarded by
                # the RGB conversion; carrying it into AVIF would mislabel colors.
                kwargs.pop("icc_profile", None)
            dimensions = target_dimensions(*image.size, "photo", settings)
            if dimensions != image.size:
                image = image.resize(dimensions, Image.Resampling.LANCZOS)
            image.save(destination, **kwargs)
    except UnsupportedMediaError:
        raise
    except Exception as exc:
        raise MediaError(f"photo encoding failed for {source}: {exc}") from exc


def run_ffmpeg(
    command: list[str],
    *,
    timeout: float,
    output: Path | None = None,
    stall_seconds: float = 300,
    poll_seconds: float = 5,
) -> None:
    """Run ffmpeg, and abandon it when it stops making progress.

    One 11-minute MOV in this library wedges ffmpeg: no CPU, nothing written,
    and it survives a kill. Without a bound that single file stalled a
    196-video run indefinitely, which is what it did.

    Progress is the output file growing, so a long encode is never cut off for
    being long, while a wedged one is dropped in minutes rather than hours.
    `timeout` remains an outer bound for a run that grows its file but never
    finishes. A process that will not die is left to the operating system: the
    run must lose the file, not the queue.
    """

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = last_progress = time.monotonic()
    seen = -1

    def abandon(why: str) -> MediaError:
        process.kill()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return MediaError(f"video encoding {why}: {command[-1]}")

    while True:
        try:
            _, stderr = process.communicate(timeout=poll_seconds)
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            size = output.stat().st_size if output is not None and output.exists() else -1
            if size != seen:
                seen, last_progress = size, now
            if now - last_progress >= stall_seconds:
                raise abandon(f"wrote nothing for {stall_seconds:.0f}s") from None
            if now - started >= timeout:
                raise abandon(f"did not finish within {timeout:.0f}s") from None
            continue
        break
    if process.returncode != 0:
        raise MediaError(f"video encoding failed ({process.returncode}): {stderr.strip()[:400]}")


def encode_timeout(duration_seconds: float | None, settings: dict[str, Any]) -> float:
    """The outer bound for one video, scaled by the source's own length.

    `videos.timeout_factor` and `videos.timeout_floor_seconds` tune it; the
    stall watchdog in `run_ffmpeg` is what catches a wedged encode quickly.
    """

    options = settings.get("videos", {})
    factor = float(options.get("timeout_factor", 12) or 12)
    floor = float(options.get("timeout_floor_seconds", 900) or 900)
    duration = float(duration_seconds or 0)
    return max(floor, duration * factor)


def _encode_video(
    source: Path,
    destination: Path,
    settings: dict[str, Any],
    limit_seconds: float | None = None,
    start_seconds: float | None = None,
) -> None:
    info = probe(source, _tool(settings, "ffprobe", "ffprobe"))
    if info["skip_reason"]:
        raise UnsupportedMediaError(info["skip_reason"])
    width, height = target_dimensions(info["width"], info["height"], "video", settings)
    options = settings.get("videos", {})
    filters = [f"scale={width}:{height}:flags=lanczos"]
    max_fps = float(options.get("max_fps", 0) or 0)
    if max_fps > 0 and info.get("frame_rate") and info["frame_rate"] > max_fps:
        filters.insert(0, f"fps={max_fps:g}")
    threads = int(settings.get("run", {}).get("threads", 0) or 0)
    command = [
        _tool(settings, "ffmpeg", "ffmpeg"),
        "-v",
        "error",
        "-y",
    ]
    if start_seconds is not None:
        command += ["-ss", f"{start_seconds:g}"]
    command += [
        "-i",
        os.fspath(source),
        "-map",
        "0:v:0",
        "-map_metadata",
        "0",
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx265",
        "-crf",
        str(int(options.get("crf", 28))),
        "-preset",
        str(options.get("preset", "slow")),
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
    ]
    if threads > 0:
        command += [
            "-threads",
            str(threads),
            "-x265-params",
            f"pools={threads}:frame-threads={threads}",
        ]
    if info["has_audio"]:
        command += [
            "-map",
            "0:a:0?",
            "-c:a",
            "aac",
            "-b:a",
            f"{int(options.get('audio_bitrate_kbps', 96))}k",
        ]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart"]
    if limit_seconds is not None:
        command += ["-t", f"{limit_seconds:g}"]
    command.append(os.fspath(destination))
    try:
        run_ffmpeg(
            command,
            timeout=encode_timeout(info.get("duration_seconds"), settings),
            output=destination,
            stall_seconds=float(options.get("stall_seconds", 300) or 300),
        )
    except MediaError as exc:
        raise MediaError(f"video encoding failed for {source}: {exc}") from exc
    except OSError as exc:
        raise MediaError(f"video encoding failed for {source}: {exc}") from exc


def encode(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    settings: dict[str, Any],
) -> dict[str, Any]:
    source_path, destination_path = Path(source), Path(destination)
    _ensure_distinct(source_path, destination_path)
    input_info = probe(source_path, _tool(settings, "ffprobe", "ffprobe"))
    if input_info["skip_reason"]:
        raise UnsupportedMediaError(input_info["skip_reason"])
    if input_info["kind"] == "photo":
        _encode_photo(source_path, destination_path, settings)
    elif input_info["kind"] == "video":
        _encode_video(source_path, destination_path, settings)
    else:
        raise UnsupportedMediaError(input_info["skip_reason"] or "unsupported media")
    output_info = probe(destination_path, _tool(settings, "ffprobe", "ffprobe"))
    digest = _sha256(destination_path)
    # Keep the probe fields flat at the media seam.  The aliases make
    # this usable by callers written against the original draft contract.
    return {**output_info, "sha256": digest, "output_sha256": digest}


def _decode_fully(path: Path, info: dict[str, Any], settings: dict[str, Any]) -> None:
    if info["kind"] == "photo":
        from PIL import Image

        with Image.open(path) as image:
            for frame in range(getattr(image, "n_frames", 1)):
                image.seek(frame)
                image.load()
        return
    command = [
        _tool(settings, "ffmpeg", "ffmpeg"),
        "-v",
        "error",
        "-xerror",
        "-err_detect",
        "explode",
        "-i",
        os.fspath(path),
        "-map",
        "0",
        "-f",
        "null",
        "-",
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise VerificationError(
            f"full decode failed for {path}: {getattr(exc, 'stderr', str(exc))}"
        ) from exc


def verify(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Fully decode output and validate bounds plus duration/audio preservation."""
    source_path, output_path = Path(source), Path(output)
    source_info = probe(source_path, _tool(settings, "ffprobe", "ffprobe"))
    output_info = probe(output_path, _tool(settings, "ffprobe", "ffprobe"))
    _decode_fully(output_path, output_info, settings)
    if output_info["kind"] != source_info["kind"]:
        raise VerificationError("output media kind differs from source")
    expected = target_dimensions(
        source_info["width"], source_info["height"], source_info["kind"], settings
    )
    if (
        output_info["width"] > source_info["width"]
        or output_info["height"] > source_info["height"]
    ):
        raise VerificationError("output enlarges source")
    if output_info["width"] != expected[0] or output_info["height"] != expected[1]:
        raise VerificationError(
            f"unexpected output dimensions: {output_info['width']}x{output_info['height']}, expected {expected[0]}x{expected[1]}"
        )
    if source_info["kind"] == "video":
        source_duration, output_duration = (
            source_info["duration_seconds"],
            output_info["duration_seconds"],
        )
        if (
            source_duration is None
            or output_duration is None
            or not math.isfinite(source_duration)
            or not math.isfinite(output_duration)
        ):
            raise VerificationError("video duration is unavailable")
        frame_rate = source_info.get("frame_rate") or 0
        duration_tolerance = max(0.25, 2 / frame_rate) if frame_rate > 0 else 0.25
        if abs(source_duration - output_duration) > duration_tolerance:
            raise VerificationError("video duration was not preserved")
        if output_info["has_audio"] != source_info["has_audio"]:
            raise VerificationError("video audio presence was not preserved")
        source_fps, output_fps = (
            source_info.get("frame_rate"),
            output_info.get("frame_rate"),
        )
        if source_fps and output_fps and output_fps > source_fps * 1.02:
            raise VerificationError("video frame rate was increased")
    return {"ok": True, "source": source_info, "output": output_info}
