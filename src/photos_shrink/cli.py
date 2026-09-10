"""Command line interface for photos-shrink."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from .config import ConfigError, load_config
from .pipeline import Pipeline, PipelineError
from .state import StateError, StateStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photos-shrink",
        description="Safely replace oversized Google Photos media.",
    )
    parser.add_argument(
        "--config", default="shrink.toml", help="TOML configuration path"
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="build local plan without upload or trash",
    )
    parser.add_argument(
        "--yes", action="store_true", help="apply without an interactive confirmation"
    )
    parser.add_argument(
        "--keep-originals",
        action="store_true",
        help="pilot mode: upload and verify replacements while keeping originals",
    )
    parser.add_argument("--report", help="CSV report path")
    parser.add_argument("--limit", type=int, help="process at most N eligible items")
    parser.add_argument(
        "--login",
        action="store_true",
        help="explain the required normal-Chrome cookie export",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="check local encoder and authentication setup",
    )
    return parser


def doctor(settings) -> int:
    failures: list[str] = []
    for key in ("ffmpeg", "ffprobe"):
        configured = settings.tools[key]
        if Path(configured).parent != Path(".") or Path(configured).is_absolute():
            found = Path(configured).exists()
        else:
            found = shutil.which(configured) is not None
        if not found:
            failures.append(
                f"{key} not found: {configured} (set [tools].{key} to an executable path)"
            )
    try:
        from PIL import features

        if not features.check("avif"):
            failures.append("Pillow has no AVIF support")
    except (ImportError, AttributeError):
        failures.append("Pillow is unavailable")
    ffmpeg = settings.tools["ffmpeg"]
    if not failures:
        try:
            command = [ffmpeg, "-hide_banner", "-encoders"]
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=15, check=False
            )
            if "libx265" not in result.stdout:
                failures.append("ffmpeg lacks libx265 encoder")
            if "aac" not in result.stdout.lower():
                failures.append("ffmpeg lacks AAC encoder")
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"could not inspect ffmpeg encoders: {exc}")
    if failures:
        for failure in failures:
            print(f"doctor: FAIL: {failure}", file=sys.stderr)
        return 1
    print("doctor: local encoders and Pillow AVIF support look ready")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_config(args.config)
        if args.limit is not None:
            if args.limit < 0:
                raise ConfigError("--limit must be >= 0")
            run = dict(settings.run)
            run["limit"] = args.limit
            settings = replace(settings, run=run)
        if args.doctor:
            return doctor(settings)
        from .remote import GooglePhotosRemote

        if args.login:
            remote = GooglePhotosRemote(settings.as_dict())
            try:
                remote.login(force=True)
                print("login complete")
            finally:
                remote.close()
            return 0

        preflight = doctor(settings)
        if preflight:
            return preflight
        remote = GooglePhotosRemote(settings.as_dict())
        try:
            remote.login()
            account = remote.account_id()
            state_path = Path(settings.run["work_dir"]) / "state.sqlite"
            with StateStore(state_path, account, settings.fingerprint) as state:
                pipeline = Pipeline(settings, remote, state, progress=print)
                result = pipeline.run(
                    plan_only=args.plan_only,
                    yes=args.yes,
                    keep_originals=args.keep_originals,
                    confirm=lambda path: (
                        input(
                            f"Review {path}; upload and verify replacements while "
                            f"keeping originals? [y/N] " if args.keep_originals else
                            f"Review {path}; upload and trash planned items? [y/N] "
                        )
                        .strip()
                        .lower()
                        in {"y", "yes"}
                    ),
                    report_path=args.report,
                )
                print(
                    f"planned={result['planned']} replaced={result['replaced']} "
                    f"verified={result.get('verified', 0)} report={result['report']}"
                )
            return 0
        finally:
            remote.close()
    except KeyboardInterrupt:
        print("photos-shrink: interrupted", file=sys.stderr)
        return 130
    except (ConfigError, PipelineError, StateError, OSError, RuntimeError) as exc:
        print(f"photos-shrink: {exc}", file=sys.stderr)
        return 2
