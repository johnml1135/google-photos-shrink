"""Serial, resumable planning and replacement orchestration."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .auth import UploadNotStartedError
from .config import Settings
from .integrity import sha256_file as _hash


class PipelineError(RuntimeError):
    """Raised when a safety precondition fails."""


def _safe_filename(filename: str, suffix: str | None = None) -> str:
    name = Path(str(filename or "item")).name.replace("\x00", "_")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .") or "item"
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name, re.IGNORECASE):
        name = "_" + name
    name = name[:120].rstrip(" .") or "item"
    if suffix:
        name = Path(name).stem + suffix
    return name


class Pipeline:
    def __init__(self, settings: Settings, remote: Any, state: Any, media: Any | None = None,
                 progress: Callable[[str], None] | None = None):
        self.settings = settings
        self.remote = remote
        self.state = state
        if media is None:
            from . import media as media_module
            media = media_module
        self.media = media
        self.progress = progress or (lambda message: None)
        self.work_dir = Path(settings.run["work_dir"]).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._output_ids: set[str] = {str(row["replacement_id"]) for row in self.state.rows() if row.get("replacement_id")}
        register_replacements = getattr(self.remote, "register_replacements", None)
        if callable(register_replacements):
            register_replacements(self._output_ids)

    def _item_dir(self, item_id: str) -> Path:
        # IDs are untrusted: a digest gives a stable path below work_dir.
        directory = self.work_dir / hashlib.sha256(str(item_id).encode()).hexdigest()[:24]
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _skip_reason(self, item: dict[str, Any]) -> str | None:
        if item.get("skip_reason"):
            return str(item["skip_reason"])
        if self.settings.run.get("photos_only", False) and item.get("kind") != "photo":
            return "non_photo"
        if self.settings.run["skip_shared"]:
            albums = (item.get("metadata") or {}).get("albums") or []
            if any(bool(album.get("shared")) for album in albums if isinstance(album, dict)):
                return "shared_album"
        if self.settings.run.get("skip_non_space_consuming", True) and (
            item.get("space_consuming") is False or
            (item.get("space_taken_bytes") is not None and int(item["space_taken_bytes"]) <= 0)
        ):
            return "non_space_consuming"
        return self.settings.exclusion_reason(item)

    def _is_output(self, item_id: str) -> bool:
        return str(item_id) in self._output_ids

    def _protect_pending_upload(self, plan: dict[str, Any], row: dict[str, Any]) -> None:
        """Resolve a pending upload's exact remote object before inventory."""
        find_uploaded = getattr(self.remote, "find_uploaded", None)
        if not callable(find_uploaded):
            return
        try:
            self._local_output_info(plan, row)
        except PipelineError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed on any local verification failure
            raise PipelineError(
                f"pending upload output cannot be verified for {plan['item'].get('id')}: {exc}"
            ) from exc
        found = find_uploaded(plan["output"])
        if found is None:
            return
        replacement_id = found.get("id")
        if not replacement_id:
            raise PipelineError("reconciled upload did not contain an item id")
        self._check_identity(plan["item"], found)
        self._output_ids.add(str(replacement_id))

    def _download_backup(self, item: dict[str, Any], directory: Path) -> tuple[Path, str]:
        filename = _safe_filename(item.get("filename", "item"))
        path = directory / ("original-" + filename)
        self.remote.download(item, path)
        digest = _hash(path)
        expected = item.get("sha256") or item.get("content_hash")
        if expected and digest != expected:
            raise PipelineError(f"download hash mismatch for {item.get('id')}")
        expected_size = item.get("size_bytes")
        if expected_size is not None and path.stat().st_size != int(expected_size):
            raise PipelineError(f"download size mismatch for {item.get('id')}")
        return path, digest

    def _build_plan(self, item: dict[str, Any]) -> dict[str, Any]:
        item_id = str(item["id"])
        directory = self._item_dir(item_id)
        backup, original_hash = self._download_backup(item, directory)
        kind = item.get("kind") or "photo"
        suffix = ".avif" if kind == "photo" else ".mp4"
        output = directory / ("replacement" + suffix)
        self.state.capture_snapshot(item_id, item, original_hash, backup)
        estimate_settings = {**self.settings.as_dict(), "source_metadata": item.get("metadata") or {}}
        estimate = self.media.estimate(backup, estimate_settings, directory)
        estimated = int(estimate.get("estimated_bytes", 0))
        old_size = int(item.get("size_bytes") or backup.stat().st_size)
        savings = old_size - estimated
        self.state.set_plan(item_id, estimated, savings, output)
        source_info: dict[str, Any] = {}
        planned_info: dict[str, Any] = {}
        if hasattr(self.media, "probe"):
            source_info = self.media.probe(backup, self.settings.tools["ffprobe"])
            if hasattr(self.media, "target_dimensions") and source_info.get("width") and source_info.get("height"):
                width, height = self.media.target_dimensions(source_info["width"], source_info["height"], kind,
                                                            self.settings.as_dict())
                planned_info.update({"width": width, "height": height})
        planned_info.update({"format": "avif" if kind == "photo" else "mp4",
                             "codec": "av1" if kind == "photo" else "hevc"})
        return {"item": item, "backup": backup, "output": output, "original_hash": original_hash,
                "old_size": old_size, "estimated_size": estimated, "estimated_savings": savings,
                "estimate_method": estimate.get("method", "unknown"), "old_path": str(backup),
                "new_path": str(output), "source_info": source_info, "planned_info": planned_info,
                "status": ("planned" if savings > 0 and old_size and
                    100 * savings / old_size >= float(self.settings.run["minimum_savings_percent"])
                    else "insufficient_savings")}

    def _resume_plan(self, row: dict[str, Any]) -> dict[str, Any]:
        item = json.loads(row["original_json"])
        backup = Path(row.get("backup_path") or "")
        output = Path(row.get("output_path") or "")
        original_hash = row.get("original_hash")
        output_hash = row.get("output_hash")
        if not original_hash or not output_hash:
            raise PipelineError("resume journal is missing original or output hash")
        old_size = int(row.get("original_bytes") or item.get("size_bytes") or 0)
        estimate = int(row.get("estimated_bytes") or 0)
        changed = row.get("encoding_fingerprint") not in {None, self.settings.fingerprint}
        exclusion = self._skip_reason(item)
        actual_size = row.get("actual_bytes")
        comparison_size = int(actual_size) if actual_size is not None else estimate
        savings = old_size - comparison_size
        estimated_savings = old_size - estimate
        qualifies = bool(old_size and comparison_size < old_size and
                         100 * savings / old_size >= float(self.settings.run["minimum_savings_percent"]))
        if exclusion:
            status = "pending_excluded"
            reason = exclusion
        elif changed:
            status = "pending_config_change"
            reason = "encoding settings changed; pending operation requires explicit new plan"
        elif not qualifies:
            status = "insufficient_savings"
            reason = "current minimum savings threshold is not met"
        else:
            status = "planned"
            reason = None
        return {"item": item, "backup": backup, "output": output, "original_hash": original_hash,
                "old_size": old_size, "estimated_size": estimate,
                "estimated_savings": estimated_savings, "estimate_method": "resumed",
                "old_path": str(backup), "new_path": str(output), "planned_info": {"format": "avif" if item.get("kind") == "photo" else "mp4",
                                                                                       "codec": "av1" if item.get("kind") == "photo" else "hevc"},
                "status": status, "reason": reason,
                "resumed": True}

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        if os.path.normcase(str(left)) == os.path.normcase(str(right)):
            return True
        try:
            return left.exists() and right.exists() and os.path.samefile(left, right)
        except OSError:
            return False

    def _validate_report_path(self, report_path: Path) -> None:
        if report_path.suffix.casefold() != ".csv":
            raise PipelineError("report path must end in .csv")
        work_dir = self.work_dir.resolve()
        if report_path.is_relative_to(work_dir) and report_path.parent != work_dir:
            raise PipelineError("report path must be in the work directory root")
        browser_profile = Path(self.settings.google["browser_profile"]).expanduser().resolve()
        if report_path.is_relative_to(browser_profile):
            raise PipelineError("report path cannot be inside the browser profile")
        protected: list[Path] = [self.settings.path.resolve()]
        cookies_file = self.settings.google.get("cookies_file")
        if cookies_file:
            protected.append(Path(cookies_file).expanduser().resolve())
        state_path = getattr(self.state, "path", None)
        if state_path:
            state_path = Path(state_path).expanduser().resolve()
            protected.extend(
                state_path.with_name(state_path.name + suffix)
                for suffix in ("", "-wal", "-shm", ".lock")
            )
            rows = self.state.rows() if callable(getattr(self.state, "rows", None)) else []
            for row in rows:
                for key in ("backup_path", "source_path", "output_path"):
                    journal_path = row.get(key)
                    if journal_path:
                        protected.append(Path(journal_path).expanduser().resolve())
        if any(self._same_path(report_path, candidate) for candidate in protected):
            raise PipelineError("report path is protected")

    def _write_report(self, plans: list[dict[str, Any]], report_path: Path) -> None:
        report_path = report_path.resolve()
        self._validate_report_path(report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        fields = ["id", "filename", "kind", "capture_timestamp_ms", "old_path", "new_path", "old_size_bytes", "estimated_size_bytes",
                  "estimated_savings_bytes", "estimated_savings_percent", "estimate_method", "original_quota_bytes", "estimated_quota_savings_bytes", "savings_basis", "actual_size_bytes", "actual_savings_bytes", "actual_savings_percent", "old_format", "new_format",
                  "old_codec", "new_codec", "old_width", "old_height", "new_width", "new_height", "status", "reason"]
        fd, temporary_name = tempfile.mkstemp(prefix=f".{report_path.name}.", suffix=".tmp", dir=report_path.parent)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for plan in plans:
                    item = plan["item"]
                    output_info = plan.get("output_info") or plan.get("planned_info") or {}
                    values = {"id": item.get("id"), "filename": item.get("filename"), "kind": item.get("kind"),
                              "capture_timestamp_ms": item.get("timestamp_ms"), "old_path": plan.get("old_path"), "new_path": plan.get("new_path"),
                              "old_size_bytes": plan.get("old_size"), "estimated_size_bytes": plan.get("estimated_size"),
                              "estimated_savings_bytes": plan.get("estimated_savings"),
                              "estimated_savings_percent": (100 * plan["estimated_savings"] / plan["old_size"] if plan.get("old_size") and plan.get("estimated_savings") is not None else None),
                              "estimate_method": plan.get("estimate_method"),
                              "original_quota_bytes": item.get("space_taken_bytes"),
                              "estimated_quota_savings_bytes": None,
                              "savings_basis": "file_bytes",
                              "actual_size_bytes": output_info.get("size_bytes"),
                              "actual_savings_bytes": (plan.get("old_size") - output_info["size_bytes"] if output_info.get("size_bytes") is not None else None),
                              "actual_savings_percent": (100 * (plan["old_size"] - output_info["size_bytes"]) / plan["old_size"] if plan.get("old_size") and output_info.get("size_bytes") is not None else None),
                              "old_format": item.get("mime_type") or plan.get("source_info", {}).get("format"), "new_format": output_info.get("format"),
                              "old_codec": item.get("codec") or plan.get("source_info", {}).get("codec"), "new_codec": output_info.get("codec"),
                              "old_width": item.get("width") or plan.get("source_info", {}).get("width"), "old_height": item.get("height") or plan.get("source_info", {}).get("height"),
                              "new_width": output_info.get("width"), "new_height": output_info.get("height"),
                              "status": plan.get("status"), "reason": plan.get("reason")}
                    writer.writerow({key: self._csv_value(value) for key, value in values.items()})
            os.replace(temporary, report_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _csv_value(value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.lstrip(" \t\r\n")
            if value[:1] in ("\t", "\r", "\n") or (stripped and stripped[:1] in "=+-@"):
                return "'" + value
        return value

    def _verify_backup(self, plan: dict[str, Any], row: dict[str, Any]) -> None:
        backup = plan["backup"]
        if not backup.is_file():
            raise PipelineError("local original backup is missing")
        expected = row.get("original_hash")
        if not expected:
            raise PipelineError("journal original hash is missing")
        if _hash(backup) != expected:
            raise PipelineError("local original backup no longer verifies")

    def _local_output_info(self, plan: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        output = plan["output"]
        if not output.is_file():
            raise PipelineError("local replacement is missing")
        output_hash = _hash(output)
        expected_hash = row.get("output_hash")
        if not expected_hash:
            raise PipelineError("journal output hash is missing")
        if output_hash != expected_hash:
            raise PipelineError("local replacement no longer matches the upload intent")
        info: dict[str, Any] = {"output_sha256": output_hash, "output_path": str(output.resolve()),
                                "size_bytes": output.stat().st_size, "kind": plan["item"].get("kind")}
        if hasattr(self.media, "probe"):
            info.update(self.media.probe(output, self.settings.tools["ffprobe"]))
        if hasattr(self.media, "verify"):
            checked = self.media.verify(plan["backup"], output, self.settings.as_dict())
            if checked.get("ok") is False:
                raise PipelineError("replacement verification failed")
        info.setdefault("format", "avif" if plan["item"].get("kind") == "photo" else "mp4")
        info.setdefault("codec", "av1" if plan["item"].get("kind") == "photo" else "hevc")
        for key in ("width", "height", "duration_seconds", "kind"):
            info.setdefault(key, plan["item"].get(key))
        return info

    def _finalize_one(self, plan: dict[str, Any], replacement: dict[str, Any],
                      output_info: dict[str, Any], *, keep_originals: bool,
                      restore_metadata: bool) -> bool:
        item = plan["item"]
        item_id = str(item["id"])
        self._check_identity(item, replacement)
        if restore_metadata:
            self.remote.restore_metadata(item, replacement)
        self.remote.verify_replacement(item, replacement, output_info)
        self.state.mark_trash_ready(item_id)
        if keep_originals:
            plan["status"] = "verified_original_kept"
            plan["reason"] = "pilot mode: original kept after replacement verification"
            return False
        self.remote.trash(item)
        if not self.remote.is_trashed(item):
            raise PipelineError(f"remote did not confirm trash for {item_id}")
        self.state.mark_trashed(item_id)
        plan["status"] = "replaced"
        return True

    def _apply_one(self, plan: dict[str, Any], *, keep_originals: bool = False) -> bool:
        item = plan["item"]
        item_id = str(item["id"])
        row = self.state.get_item(item_id) or {}
        self._verify_backup(plan, row)
        if row.get("stage") in {"trash_ready", "uploaded"}:
            replacement = self.remote.get_item(row["replacement_id"]) if hasattr(self.remote, "get_item") else {"id": row["replacement_id"]}
            self._check_identity(item, replacement)
            output_info = self._local_output_info(plan, row)
            plan["output_info"] = output_info
            return self._finalize_one(
                plan, replacement, output_info, keep_originals=keep_originals,
                restore_metadata=row.get("stage") == "uploaded",
            )
        if row.get("stage") == "upload_intent":
            output_info = self._local_output_info(plan, row)
            found = self.remote.find_uploaded(plan["output"])
            if found is None:
                plan["status"] = "pending_reconcile"
                plan["reason"] = "upload outcome is ambiguous; exact hash not found"
                raise PipelineError(f"cannot reconcile upload for {item_id}; no blind retry performed")
            replacement = found
            replacement_id = replacement.get("id")
            if not replacement_id:
                raise PipelineError("upload response did not contain an item id")
            self._check_identity(item, replacement)
            self.state.mark_uploaded(item_id, replacement_id, row["output_hash"])
            register_replacements = getattr(self.remote, "register_replacements", None)
            if callable(register_replacements):
                register_replacements([str(replacement_id)])
            plan["output_info"] = output_info
        else:
            encode_settings = {**self.settings.as_dict(), "source_metadata": item.get("metadata") or {}}
            output_info = self.media.encode(plan["backup"], plan["output"], encode_settings)
            verified = self.media.verify(plan["backup"], plan["output"], self.settings.as_dict())
            if verified.get("ok") is False:
                raise PipelineError(f"encoded output failed verification for {item_id}")
            output_hash = output_info.get("output_sha256") or _hash(plan["output"])
            actual_size = int(output_info.get("size_bytes") or plan["output"].stat().st_size)
            plan["output_info"] = {**output_info, "size_bytes": actual_size}
            actual_percent = 100 * (plan["old_size"] - actual_size) / plan["old_size"] if plan["old_size"] else 0
            plan["status"] = "insufficient_actual_savings" if not (actual_size < plan["old_size"] and actual_percent >= float(self.settings.run["minimum_savings_percent"])) else "encoded"
            if plan["status"] == "insufficient_actual_savings":
                return False
            # A matching remote object found before this operation has no provenance
            # relationship to this original. Never adopt it and mutate its metadata.
            found = self.remote.find_uploaded(plan["output"])
            if found is not None:
                plan["status"] = "skipped"
                plan["reason"] = "existing_remote_content"
                self.state.mark_skipped(item_id, item, plan["reason"])
                return False
            self.state.mark_encoded(item_id, output_hash, actual_size)
            # This durable intent is the crash boundary before the network request.
            self.state.record_upload_intent(item_id, output_hash, plan["output"])
            plan["output_info"] = self._local_output_info(plan, self.state.get_item(item_id) or {})
            try:
                replacement = self.remote.upload(plan["output"])
            except UploadNotStartedError:
                # The browser failed before receiving any file bytes. Only this
                # explicit outcome permits a fresh attempt without reconciliation.
                self.state.mark_encoded(item_id, output_hash, actual_size)
                raise
            replacement_id = replacement.get("id")
            if not replacement_id:
                raise PipelineError("upload response did not contain an item id")
            self._check_identity(item, replacement)
            self.state.mark_uploaded(item_id, replacement_id, output_hash)
        replacement = self.remote.get_item(replacement_id) if hasattr(self.remote, "get_item") else replacement
        self._check_identity(item, replacement)
        output_info = self._local_output_info(plan, self.state.get_item(item_id) or {})
        output_info.update({key: value for key, value in (plan.get("output_info") or {}).items()
                            if key not in {"output_sha256", "output_path", "size_bytes"}})
        plan["output_info"] = output_info
        return self._finalize_one(
            plan, replacement, output_info, keep_originals=keep_originals,
            restore_metadata=True,
        )

    @staticmethod
    def _check_identity(original: dict[str, Any], replacement: dict[str, Any]) -> None:
        if not replacement or str(replacement.get("id")) == str(original.get("id")):
            raise PipelineError("replacement identity is not distinct from original")
        if original.get("dedup_key") and replacement.get("dedup_key") == original.get("dedup_key"):
            raise PipelineError("replacement has the original deduplication identity")

    def run(self, *, plan_only: bool = False, yes: bool = False, confirm: Callable[[Path], bool] | None = None,
            report_path: str | Path | None = None, keep_originals: bool = False) -> dict[str, Any]:
        report = Path(report_path or self.work_dir / "photos-shrink.csv").expanduser().resolve()
        self._validate_report_path(report)
        plans: list[dict[str, Any]] = []
        pending_ids: set[str] = set()
        terminal_ids: set[str] = set()
        # Load resumable work before inventory so it participates in the limit and
        # can avoid an unnecessary remote listing altogether.
        for row in self.state.rows():
            if row.get("stage") in {"upload_intent", "uploaded", "trash_ready"}:
                plan = self._resume_plan(row)
                if row.get("stage") == "upload_intent":
                    self._protect_pending_upload(plan, row)
                plans.append(plan)
                pending_ids.add(str(row["original_id"]))
            elif row.get("stage") == "trashed":
                terminal_ids.add(str(row["original_id"]))
        limit = int(self.settings.run.get("limit", 0))
        count_toward_limit = {"planned", "pending_reconcile"}
        planned_count = sum(plan["status"] in count_toward_limit for plan in plans)

        def record_skip(item: dict[str, Any], reason: str) -> None:
            item_id = str(item["id"])
            self.state.mark_skipped(item_id, item, reason)
            plans.append({"item": item, "old_size": item.get("size_bytes"), "estimated_size": None,
                          "estimated_savings": None, "old_path": "", "new_path": "", "status": "skipped",
                          "reason": reason, "estimate_method": ""})
            self._write_report(plans, report)

        def plan_item(item: dict[str, Any]) -> None:
            nonlocal planned_count
            item_id = str(item.get("id", ""))
            if not item_id or item_id in pending_ids or item_id in terminal_ids or self._is_output(item_id):
                return
            reason = self._skip_reason(item)
            if reason:
                record_skip(item, reason)
                return
            if limit and planned_count >= limit:
                return
            self.progress(f"Planning {item.get('filename', item_id)}")
            try:
                plan = self._build_plan(item)
            except Exception as exc:  # noqa: BLE001 - item errors are captured in the report
                error_reason = f"local_error:{type(exc).__name__}:{str(exc)[:500]}"
                self.state.mark_skipped(item_id, item, error_reason)
                plans.append({"item": item, "old_size": item.get("size_bytes"), "estimated_size": None,
                              "estimated_savings": None, "old_path": "", "new_path": "", "status": "skipped",
                              "reason": error_reason, "estimate_method": ""})
                self._write_report(plans, report)
                return
            plans.append(plan)
            if plan["status"] in count_toward_limit:
                planned_count += 1
            self._write_report(plans, report)

        if not (limit and planned_count >= limit):
            selection_order = self.settings.run.get("selection_order", "largest")
            self.progress("Scanning Google Photos...")
            listing = self.remote.list_items()
            try:
                if selection_order == "newest":
                    scanned = 0
                    for item in listing:
                        scanned += 1
                        self.progress(f"Inventory: scanned {scanned} items")
                        plan_item(item)
                        if limit and planned_count >= limit:
                            break
                    self.progress(f"Inventory: {scanned} items")
                else:
                    items: list[dict[str, Any]] = []
                    for item in listing:
                        items.append(item)
                        self.progress(f"Inventory: scanned {len(items)} items")
                    self.progress(f"Inventory: {len(items)} items")
                    for item in sorted(items, key=lambda value: int(value.get("size_bytes") or 0), reverse=True):
                        plan_item(item)
            finally:
                close_listing = getattr(listing, "close", None)
                if callable(close_listing):
                    close_listing()
        self._write_report(plans, report)
        result = {"planned": sum(plan["status"] == "planned" for plan in plans), "report": str(report),
                  "replaced": 0, "verified": 0, "awaiting_confirmation": False}
        if plan_only:
            return result
        if not yes and (confirm is None or not confirm(report)):
            result["awaiting_confirmation"] = True
            return result
        try:
            applied = 0
            for plan in plans:
                if plan["status"] not in {"planned", "pending_reconcile"}:
                    continue
                if limit and applied >= limit:
                    break
                applied += 1
                self.progress(f"Applying {plan['item'].get('filename', plan['item']['id'])}")
                try:
                    if self._apply_one(plan, keep_originals=keep_originals):
                        result["replaced"] += 1
                    elif plan.get("status") == "verified_original_kept":
                        result["verified"] += 1
                except Exception as exc:
                    plan["status"] = "failed"
                    plan["reason"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    self._write_report(plans, report)
                pause = int(self.settings.run.get("pause_seconds", 0))
                if pause:
                    time.sleep(pause)
        finally:
            self._write_report(plans, report)
        return result
