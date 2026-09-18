from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from photos_shrink import media
from photos_shrink.media import (
    UnsupportedMediaError,
    encode,
    probe,
    target_dimensions,
    verify,
)

_FFMPEG_BIN = os.environ.get("PHOTOS_SHRINK_FFMPEG_BIN")
if not _FFMPEG_BIN:
    for _candidate in (
        Path(__file__).parents[1]
        / ".cache"
        / "tools"
        / "ff-full"
        / "ffmpeg-master-latest-win64-gpl"
        / "bin",
        Path(__file__).parents[2]
        / ".cache"
        / "tools"
        / "ffmpeg"
        / "ffmpeg-9.0.1-essentials_build"
        / "bin",
        Path(__file__).parents[3]
        / ".cache"
        / "tools"
        / "ffmpeg"
        / "ffmpeg-9.0.1-essentials_build"
        / "bin",
        Path(__file__).parents[2]
        / ".cache"
        / "tools"
        / "ffmpeg"
        / "ffmpeg-9.0.1-essentials_build"
        / "bin",
    ):
        if (_candidate / "ffmpeg.exe").is_file():
            _FFMPEG_BIN = str(_candidate)
            break

PHOTO_SETTINGS = {
    "photos": {"short_edge": 1500, "format": "avif", "quality": 60},
    "videos": {
        "long_edge": 1920,
        "short_edge": 1080,
        "codec": "av1",
        "encoder": "libsvtav1",
        "crf": 36,
        "preset": "10",
        "max_fps": 0,
        "audio_bitrate_kbps": 96,
    },
    "tools": {
        "ffmpeg": str(Path(_FFMPEG_BIN) / "ffmpeg.exe") if _FFMPEG_BIN else "ffmpeg",
        "ffprobe": str(Path(_FFMPEG_BIN) / "ffprobe.exe") if _FFMPEG_BIN else "ffprobe",
    },
}


@pytest.mark.parametrize("latitude,longitude", [(40.1234567, -73.7654321), (-33.8, 151.2), (0, 0)])
def test_photo_embeds_google_location_in_avif(tmp_path, latitude, longitude):
    from PIL import Image

    source = tmp_path / "original.jpg"
    output = tmp_path / "replacement.avif"
    Image.new("RGB", (80, 40)).save(source)
    encode(source, output, {**PHOTO_SETTINGS, "source_metadata": {
        "latitude": latitude, "longitude": longitude,
    }})
    with Image.open(output) as image:
        gps = image.getexif().get_ifd(34853)
    def degrees(value):
        return float(value[0]) + float(value[1]) / 60 + float(value[2]) / 3600
    assert degrees(gps[2]) * (-1 if gps[1] == "S" else 1) == pytest.approx(latitude, abs=1e-7)
    assert degrees(gps[4]) * (-1 if gps[3] == "W" else 1) == pytest.approx(longitude, abs=1e-7)


def test_photo_target_limits_short_edge_without_upscaling() -> None:
    assert target_dimensions(4000, 3000, "photo", PHOTO_SETTINGS) == (2000, 1500)
    assert target_dimensions(1200, 800, "photo", PHOTO_SETTINGS) == (1200, 800)


def test_photo_target_keeps_large_panorama_when_short_edge_is_small() -> None:
    assert target_dimensions(6000, 1000, "photo", PHOTO_SETTINGS) == (6000, 1000)


def test_video_target_handles_portrait_orientation_and_both_bounds() -> None:
    assert target_dimensions(3840, 2160, "video", PHOTO_SETTINGS) == (1920, 1080)
    assert target_dimensions(2160, 3840, "video", PHOTO_SETTINGS) == (1080, 1920)
    assert target_dimensions(720, 1280, "video", PHOTO_SETTINGS) == (720, 1280)
    assert target_dimensions(2000, 1001, "video", PHOTO_SETTINGS) == (1920, 960)


def test_probe_reports_image_shape_and_safe_animation_skip(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    image_path = tmp_path / "still.jpg"
    pil.new("RGB", (80, 40), (30, 60, 90)).save(image_path)
    result = probe(image_path)
    assert result["kind"] == "photo"
    assert result["width"] == 80
    assert result["height"] == 40
    assert result["size_bytes"] == image_path.stat().st_size
    assert result["has_audio"] is False
    assert result["skip_reason"] is None


    animation_path = tmp_path / "animation.gif"
    frames = [pil.new("RGB", (12, 8), color) for color in ("red", "blue")]
    frames[0].save(
        animation_path, save_all=True, append_images=[frames[1]], duration=50
    )
    animated = probe(animation_path)
    assert animated["skip_reason"] == "animated image is unsupported"


def test_probe_rejects_multiple_video_tracks_before_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import photos_shrink.media as media_module

    source = tmp_path / "multiple.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(media_module, "_ffprobe_json", lambda path, ffprobe: {
        "streams": [
            {"codec_type": "video", "width": 100, "height": 80, "codec_name": "h264",
             "duration": "1", "avg_frame_rate": "10/1"},
            {"codec_type": "video", "width": 100, "height": 80, "codec_name": "h264",
             "duration": "1", "avg_frame_rate": "10/1"},
        ],
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1"},
    })
    ffmpeg_calls: list[object] = []
    monkeypatch.setattr(media_module.subprocess, "run", lambda *args, **kwargs: ffmpeg_calls.append(args))

    result = media_module.probe(source)

    assert result["skip_reason"] == "multiple video tracks are unsupported"
    with pytest.raises(UnsupportedMediaError, match="multiple video tracks"):
        media_module.encode(source, tmp_path / "output.mp4", PHOTO_SETTINGS)
    assert ffmpeg_calls == []


def test_encode_photo_applies_exif_orientation_and_verifies_full_decode(
    tmp_path: Path,
) -> None:
    pil = pytest.importorskip("PIL.Image")
    features = pytest.importorskip("PIL.features")
    if not features.check("avif"):
        pytest.skip("Pillow has no AVIF encoder")

    source = tmp_path / "oriented.jpg"
    exif = pil.Exif()
    exif[274] = 6
    pil.new("RGB", (40, 80), "green").save(source, exif=exif)
    output = tmp_path / "oriented.avif"

    encoded = encode(source, output, PHOTO_SETTINGS)
    assert encoded["sha256"]
    assert encoded["kind"] == "photo"
    checked = verify(source, output, PHOTO_SETTINGS)
    assert checked["ok"] is True
    assert checked["output"]["width"] == 80
    assert checked["output"]["height"] == 40


def test_encode_photo_preserves_palette_transparency(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    features = pytest.importorskip("PIL.features")
    if not features.check("avif"):
        pytest.skip("Pillow has no AVIF encoder")
    source = tmp_path / "transparent.png"
    image = pil.new("P", (20, 12), 0)
    image.putpalette([255, 0, 0, 0, 0, 255] + [0] * 762)
    image.info["transparency"] = 0
    image.save(source)
    output = tmp_path / "transparent.avif"
    encode(source, output, PHOTO_SETTINGS)
    with pil.open(output) as decoded:
        assert decoded.mode == "RGBA"
        assert decoded.getpixel((0, 0))[3] == 0


def test_encode_photo_passes_configured_avif_thread_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pil = pytest.importorskip("PIL.Image")
    features = pytest.importorskip("PIL.features")
    if not features.check("avif"):
        pytest.skip("Pillow has no AVIF encoder")
    source = tmp_path / "source.png"
    pil.new("RGB", (20, 12), "red").save(source)
    output = tmp_path / "output.avif"
    original_save = pil.Image.save
    save_kwargs: dict[str, object] = {}

    def recording_save(self: object, *args: object, **kwargs: object) -> object:
        save_kwargs.update(kwargs)
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(pil.Image, "save", recording_save)
    encode(source, output, {**PHOTO_SETTINGS, "run": {"threads": 3}})
    assert save_kwargs["max_threads"] == 3


def test_encode_photo_skips_cmyk_with_embedded_icc_profile(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    source = tmp_path / "source.jpg"
    image = pil.new("CMYK", (20, 12), (0, 128, 128, 0))
    image.save(source, icc_profile=b"embedded-cmyk-profile")
    with pytest.raises(UnsupportedMediaError, match="CMYK"):
        encode(source, tmp_path / "output.avif", PHOTO_SETTINGS)


def test_encode_refuses_to_overwrite_source(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    source = tmp_path / "source.jpg"
    pil.new("RGB", (20, 20), "red").save(source)
    with pytest.raises(ValueError, match="overwrite"):
        encode(source, source, PHOTO_SETTINGS)


def test_sdr_heic_is_processed_when_pillow_heif_is_available(tmp_path: Path) -> None:
    pytest.importorskip("pillow_heif")
    from PIL import Image
    from pillow_heif import register_heif_opener

    register_heif_opener(thumbnails=False)
    source = tmp_path / "source.heic"
    Image.new("RGB", (32, 24), "purple").save(source, format="HEIF", quality=70)
    output = tmp_path / "output.avif"
    assert probe(source)["kind"] == "photo"
    assert probe(source)["skip_reason"] is None
    encode(source, output, PHOTO_SETTINGS)
    assert verify(source, output, PHOTO_SETTINGS)["ok"] is True


def _ffmpeg_available() -> bool:
    return (
        bool(
            (Path(_FFMPEG_BIN) / "ffmpeg.exe").is_file()
            and (Path(_FFMPEG_BIN) / "ffprobe.exe").is_file()
        )
        if _FFMPEG_BIN
        else bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    )


@pytest.mark.skipif(not _ffmpeg_available(), reason="FFmpeg is unavailable")
def test_video_encode_preserves_audio_duration_and_dimensions(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    subprocess.run(
        [
            PHOTO_SETTINGS["tools"]["ffmpeg"],
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=96x64:rate=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
    )
    settings = {
        **PHOTO_SETTINGS,
        "videos": {**PHOTO_SETTINGS["videos"], "preset": "12"},
    }
    result = encode(source, output, settings)
    assert result["kind"] == "video"
    assert result["has_audio"] is True
    checked = verify(source, output, settings)
    assert checked["ok"] is True
    assert checked["output"]["width"] <= 96
    assert checked["output"]["height"] <= 64
    assert checked["output"]["has_audio"] is True
    assert (
        abs(
            checked["output"]["duration_seconds"]
            - checked["source"]["duration_seconds"]
        )
        < 0.25
    )


@pytest.mark.skipif(not _ffmpeg_available(), reason="FFmpeg is unavailable")
def test_video_probe_and_encode_are_rotation_aware_and_fps_is_a_cap(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rotated.mp4"
    output = tmp_path / "rotated-output.mp4"
    subprocess.run(
        [
            PHOTO_SETTINGS["tools"]["ffmpeg"],
            "-v",
            "error",
            "-display_rotation:v:0",
            "90",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=96x64:rate=12",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    source_info = probe(source, PHOTO_SETTINGS["tools"]["ffprobe"])
    assert (source_info["width"], source_info["height"]) == (64, 96)
    settings = {
        **PHOTO_SETTINGS,
        "videos": {**PHOTO_SETTINGS["videos"], "max_fps": 6, "preset": "12"},
    }
    encode(source, output, settings)
    output_info = probe(output, PHOTO_SETTINGS["tools"]["ffprobe"])
    assert output_info["frame_rate"] <= 6.1
    assert output_info["width"] == 64 and output_info["height"] == 96


@pytest.mark.skipif(not _ffmpeg_available(), reason="FFmpeg is unavailable")
def test_verify_rejects_large_duration_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    subprocess.run(
        [
            PHOTO_SETTINGS["tools"]["ffmpeg"],
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=6",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            PHOTO_SETTINGS["tools"]["ffmpeg"],
            "-v",
            "error",
            "-i",
            str(source),
            "-c",
            "copy",
            "-t",
            "0.8",
            str(output),
        ],
        check=True,
    )
    import photos_shrink.media as media

    source_info = probe(source, PHOTO_SETTINGS["tools"]["ffprobe"])
    output_info = probe(output, PHOTO_SETTINGS["tools"]["ffprobe"])
    source_info["duration_seconds"] = 300.0
    output_info["duration_seconds"] = 294.0
    monkeypatch.setattr(
        media,
        "probe",
        lambda path, ffprobe: source_info if Path(path) == source else output_info,
    )
    monkeypatch.setattr(media, "_decode_fully", lambda path, info, settings: None)
    with pytest.raises(media.VerificationError, match="duration"):
        media.verify(source, output, PHOTO_SETTINGS)


def test_unsupported_raw_or_unknown_media_is_skipped_safely(tmp_path: Path) -> None:
    source = tmp_path / "file.raw"
    source.write_bytes(b"not a supported camera raw file")
    result = probe(source)
    assert result["skip_reason"]
    with pytest.raises(UnsupportedMediaError):
        encode(source, tmp_path / "out.avif", PHOTO_SETTINGS)


def test_probe_treats_unspecified_orientation_zero_as_upright(tmp_path: Path) -> None:
    """Orientation 0 is out of spec but common: cameras write it for "not set".

    Every viewer, Google Photos included, renders it unrotated. Rejecting it
    excluded real photos -- including 50MP originals, the best savings targets.
    """

    pil = pytest.importorskip("PIL.Image")
    source = tmp_path / "unspecified.jpg"
    exif = pil.Exif()
    exif[274] = 0
    pil.new("RGB", (60, 40), "blue").save(source, exif=exif)

    info = probe(source)
    assert info["skip_reason"] is None
    assert (info["width"], info["height"]) == (60, 40), "0 must not swap the axes"


def test_probe_still_rejects_an_out_of_range_orientation(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    source = tmp_path / "bogus.jpg"
    exif = pil.Exif()
    exif[274] = 99
    pil.new("RGB", (60, 40), "blue").save(source, exif=exif)
    assert probe(source)["skip_reason"] == "malformed EXIF orientation metadata"


def _camera_clip(extra_streams: list[dict]) -> dict:
    """An ordinary single-camera clip, plus whatever streams a test adds."""

    return {
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "codec_name": "h264",
             "duration": "10", "avg_frame_rate": "30/1"},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2},
            *extra_streams,
        ],
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "10"},
    }


def test_probe_accepts_a_phone_clip_carrying_metadata_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An iPhone .MOV ships timecode and motion tracks alongside the picture.

    Treating those as content rejected whole camera clips. They are not
    something a viewer sees, and the encoder maps only the first video and
    audio stream, so they were already being dropped rather than mangled.
    """

    import photos_shrink.media as media_module

    source = tmp_path / "IMG_3564.MOV"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        media_module, "_ffprobe_json",
        lambda path, ffprobe: _camera_clip([{"codec_type": "data"}] * 3),
    )

    result = media_module.probe(source)

    assert result["skip_reason"] is None
    assert result["kind"] == "video"
    assert result["has_audio"] is True


def test_probe_still_rejects_a_real_second_audio_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping the data-stream check must not drop the audio check with it."""

    import photos_shrink.media as media_module

    source = tmp_path / "dubbed.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        media_module, "_ffprobe_json",
        lambda path, ffprobe: _camera_clip([{"codec_type": "audio", "codec_name": "aac"}]),
    )

    assert media_module.probe(source)["skip_reason"] == (
        "multiple audio tracks or subtitles are unsupported"
    )


def test_probe_still_rejects_subtitles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subtitle track is content a viewer would miss, so it still refuses."""

    import photos_shrink.media as media_module

    source = tmp_path / "subtitled.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        media_module, "_ffprobe_json",
        lambda path, ffprobe: _camera_clip([{"codec_type": "subtitle", "codec_name": "mov_text"}]),
    )

    assert media_module.probe(source)["skip_reason"] == (
        "multiple audio tracks or subtitles are unsupported"
    )


def test_probe_ignores_an_embedded_cover_thumbnail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cover still is reported as a video stream with attached_pic set.

    Counting it as a second video track rejected a 460 MB clip over an
    embedded thumbnail `-map 0:v:0` would never have copied.
    """

    import photos_shrink.media as media_module

    source = tmp_path / "Hilton Head.MOV"
    source.write_bytes(b"fixture")
    cover = {"codec_type": "video", "codec_name": "mjpeg", "width": 480, "height": 270,
             "disposition": {"attached_pic": 1}}
    monkeypatch.setattr(media_module, "_ffprobe_json", lambda path, ffprobe: _camera_clip([cover]))

    result = media_module.probe(source)

    assert result["skip_reason"] is None
    # The picture, not the thumbnail, decides the output dimensions.
    assert (result["width"], result["height"]) == (1920, 1080)


DRIBBLE = (
    "import sys, time\n"
    "for _ in range(40):\n"
    "    sys.stderr.write('x'); sys.stderr.flush()\n"
    "    time.sleep(0.1)\n"
)

# What the wedged encode really printed: progress blocks whose frame counter
# never moved -- 1,368 of them in a minute, all reading frame=40.
STUCK_FRAMES = (
    "import time\n"
    "for _ in range(40):\n"
    "    print('frame=40'); print('out_time_ms=1584917'); print('progress=continue', flush=True)\n"
    "    time.sleep(0.1)\n"
)

# A healthy encode: the counter climbs.
FRAMES = (
    "import time\n"
    "for i in range(%d):\n"
    "    print('frame=%%d' %% i, flush=True)\n"
    "    time.sleep(0.05)\n"
)


class TestRunFfmpeg:
    """A wedged encode must cost its own item, never the run.

    The live failure: one MOV left ffmpeg with no CPU and no output for hours,
    and it ignored a kill, so a 196-video run never reached video two.
    """

    def _python(self, script):
        return [sys.executable, "-c", script]

    def test_a_successful_command_returns(self):
        media.run_ffmpeg(self._python("pass"), timeout=60)

    def test_a_failing_command_reports_its_error(self):
        with pytest.raises(media.MediaError, match="video encoding failed"):
            media.run_ffmpeg(self._python("import sys; sys.stderr.write('bad input'); sys.exit(1)"), timeout=60)

    def test_a_command_that_reports_no_progress_is_abandoned(self):
        """The live failure: ffmpeg stopped encoding but held the file open."""

        started = time.monotonic()
        with pytest.raises(media.MediaError, match="stopped advancing for"):
            media.run_ffmpeg(self._python("import time; time.sleep(30)"), timeout=600,
                             stall_seconds=1, poll_seconds=0.2)
        assert time.monotonic() - started < 15

    def test_a_dribble_of_output_is_not_progress(self):
        """What fooled the first watchdog: a wedged encode still flushes bytes."""

        with pytest.raises(media.MediaError, match="stopped advancing for"):
            media.run_ffmpeg(self._python(DRIBBLE), timeout=600, stall_seconds=1, poll_seconds=0.2)

    def test_a_repeated_frame_counter_is_not_progress(self):
        """What fooled the second watchdog: progress blocks that never advance."""

        with pytest.raises(media.MediaError, match="stopped advancing for"):
            media.run_ffmpeg(self._python(STUCK_FRAMES), timeout=600, stall_seconds=1, poll_seconds=0.2)

    def test_a_reported_frame_counter_keeps_it_alive(self):
        """A long encode must not be cut off while ffmpeg says it is working."""

        media.run_ffmpeg(self._python(FRAMES % 12), timeout=600, stall_seconds=1, poll_seconds=0.1)

    def test_the_outer_timeout_still_bounds_a_working_encode(self):
        with pytest.raises(media.MediaError, match="did not finish within"):
            media.run_ffmpeg(self._python(FRAMES % 400), timeout=1, stall_seconds=60, poll_seconds=0.1)
    def test_the_timeout_scales_with_the_sources_length(self):
        settings = {"videos": {"timeout_factor": 10, "timeout_floor_seconds": 900}}
        assert media.encode_timeout(600, settings) == 6000
        assert media.encode_timeout(5, settings) == 900
        assert media.encode_timeout(None, settings) == 900


class TestVideoEncoderArgs:
    """Each encoder's arguments, chosen by measurement on this library."""

    def test_av1_is_the_default(self):
        args = media.video_encoder_args({})
        assert args == ["-c:v", "libsvtav1", "-preset", "8", "-crf", "36"]

    def test_av1_takes_the_configured_quality_and_preset(self):
        args = media.video_encoder_args({"crf": 40, "preset": "6"})
        assert args == ["-c:v", "libsvtav1", "-preset", "6", "-crf", "40"]

    def test_nvenc_uses_a_quality_target_not_constant_qp(self):
        args = media.video_encoder_args({"encoder": "hevc_nvenc", "crf": 32, "preset": "p7"})
        assert args[:4] == ["-c:v", "hevc_nvenc", "-preset", "p7"]
        assert "-cq" in args and args[args.index("-cq") + 1] == "32"
        assert args[args.index("-rc") + 1] == "vbr"

    def test_x265_remains_available(self):
        args = media.video_encoder_args({"encoder": "libx265", "crf": 30, "preset": "slow"})
        assert args == ["-c:v", "libx265", "-crf", "30", "-preset", "slow"]

    def test_an_unknown_encoder_is_refused(self):
        with pytest.raises(media.UnsupportedMediaError, match="unsupported video encoder"):
            media.video_encoder_args({"encoder": "libmagic"})
