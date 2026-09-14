import csv
import os
from pathlib import Path

import pytest

from photos_shrink.config import load_config
from photos_shrink.pipeline import Pipeline, PipelineError
from photos_shrink.state import StateStore


class FakeMedia:
    def estimate(self, source, settings, work_dir):
        return {"estimated_bytes": 10, "method": "fake"}

    def encode(self, source, destination, settings):
        destination.write_bytes(b"compressed")
        import hashlib
        return {"size_bytes": 10, "output_sha256": hashlib.sha256(b"compressed").hexdigest(), "width": 100, "height": 100, "format": "avif", "codec": "av1"}

    def verify(self, source, output, settings):
        return {"ok": True, "size_bytes": output.stat().st_size}


class FakeRemote:
    def __init__(self, root):
        self.root = root
        self.items = [{"id": "orig", "dedup_key": "d", "filename": "A.JPG", "size_bytes": 100,
                       "width": 100, "height": 100, "kind": "photo", "timestamp_ms": 1704067200000,
                       "timezone_offset": 0, "duration_seconds": None, "mime_type": "image/jpeg",
                       "metadata": {"albums": [], "description": "hello", "favorite": True,
                                    "archived": False}, "skip_reason": None}]
        self.uploads = 0
        self.trashed = 0
        self.upload_id = "replacement"
        self.found_upload = None
        self.restore_calls = 0
        self.verify_calls = 0

    def account_id(self): return "account"
    def list_items(self): return self.items
    def download(self, item, destination): destination.write_bytes(b"original" * 12 + b"1234")
    def find_uploaded(self, path): return self.found_upload
    def upload(self, path):
        self.uploads += 1
        return {"id": self.upload_id, "size_bytes": 10}
    def restore_metadata(self, original, replacement): self.restore_calls += 1
    def verify_replacement(self, original, replacement, output_info):
        self.verify_calls += 1
        assert {"kind", "width", "height", "duration_seconds", "size_bytes", "output_sha256", "output_path"} <= set(output_info)
    def trash(self, item): self.trashed += 1
    def is_trashed(self, item): return self.trashed > 0


def test_plan_only_writes_csv_without_remote_mutation(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\nminimum_savings_percent = 20\n", encoding="utf-8")
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    remote.items[0]["space_taken_bytes"] = 75
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)
    report = tmp_path / "plan.csv"
    result = Pipeline(cfg, remote, state, media=FakeMedia()).run(plan_only=True, report_path=report)
    assert result["planned"] == 1
    assert remote.uploads == 0 and remote.trashed == 0
    with report.open(newline="", encoding="utf-8-sig") as f:
        row = next(csv.DictReader(f))
    assert row["old_path"].endswith("A.JPG")
    assert row["estimated_savings_bytes"] == "90"
    assert row["original_quota_bytes"] == "75"
    assert row["estimated_quota_savings_bytes"] == ""
    assert row["savings_basis"] == "file_bytes"
    state.close()


def test_ambiguous_upload_resume_never_blindly_retries(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\npause_seconds = 0\n", encoding="utf-8")
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)
    item = remote.items[0]
    backup = tmp_path / "original.jpg"
    backup.write_bytes(b"original" * 12 + b"1234")
    output = tmp_path / "output.avif"
    output.write_bytes(b"compressed")
    import hashlib
    state.capture_snapshot("orig", item, hashlib.sha256(backup.read_bytes()).hexdigest(), backup)
    state.record_upload_intent("orig", hashlib.sha256(output.read_bytes()).hexdigest(), output)
    with pytest.raises(PipelineError, match="no blind retry"):
        Pipeline(cfg, remote, state, media=FakeMedia()).run(yes=True, report_path=tmp_path / "plan.csv")
    assert remote.uploads == 0 and remote.trashed == 0
    state.close()


def test_original_identity_response_cannot_be_trashed(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\npause_seconds = 0\n", encoding="utf-8")
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    remote.upload_id = "orig"
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)
    with pytest.raises(PipelineError, match="identity"):
        Pipeline(cfg, remote, state, media=FakeMedia()).run(yes=True, report_path=tmp_path / "plan.csv")
    assert remote.trashed == 0
    state.close()


@pytest.mark.parametrize("stage", ["upload_intent", "uploaded", "trash_ready"])
def test_each_resume_stage_reconciles_with_complete_verification(tmp_path: Path, stage: str):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\npause_seconds = 0\n", encoding="utf-8")
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)
    item = remote.items[0]
    backup = tmp_path / "original.jpg"
    backup.write_bytes(b"original" * 12 + b"1234")
    output = tmp_path / "output.avif"
    output.write_bytes(b"compressed")
    import hashlib
    original_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    state.capture_snapshot("orig", item, original_hash, backup)
    state.record_upload_intent("orig", output_hash, output)
    replacement = {"id": "replacement", "dedup_key": "replacement-dedup", "size_bytes": 10}
    remote.found_upload = replacement
    if stage == "uploaded":
        state.mark_uploaded("orig", "replacement", output_hash)
    elif stage == "trash_ready":
        state.mark_uploaded("orig", "replacement", output_hash)
        state.mark_trash_ready("orig")
    result = Pipeline(cfg, remote, state, media=FakeMedia()).run(yes=True, report_path=tmp_path / "plan.csv")
    assert result["replaced"] == 1
    assert remote.uploads == 0 and remote.trashed == 1 and remote.verify_calls >= 1
    state.close()


@pytest.mark.parametrize("tamper", ["backup", "output"])
def test_tampered_resume_artifact_stops_before_remote_mutation(tmp_path: Path, tamper: str):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\npause_seconds = 0\n", encoding="utf-8")
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)
    item = remote.items[0]
    backup = tmp_path / "original.jpg"
    backup.write_bytes(b"original" * 12 + b"1234")
    output = tmp_path / "output.avif"
    output.write_bytes(b"compressed")
    import hashlib
    state.capture_snapshot("orig", item, hashlib.sha256(backup.read_bytes()).hexdigest(), backup)
    output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    state.record_upload_intent("orig", output_hash, output)
    state.mark_uploaded("orig", "replacement", output_hash)
    if tamper == "backup":
        backup.write_bytes(b"tampered")
    else:
        output.write_bytes(b"tampered")
    with pytest.raises(PipelineError):
        Pipeline(cfg, remote, state, media=FakeMedia()).run(yes=True, report_path=tmp_path / "plan.csv")
    assert remote.restore_calls == 0 and remote.trashed == 0
    state.close()


def test_pending_resume_is_blocked_when_encoding_settings_change(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\npause_seconds = 0\n", encoding="utf-8")
    old_cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    state = StateStore(tmp_path / "state.sqlite", "account", old_cfg.fingerprint)
    item = remote.items[0]
    backup = tmp_path / "original.jpg"
    backup.write_bytes(b"original" * 12 + b"1234")
    output = tmp_path / "output.avif"
    output.write_bytes(b"compressed")
    import hashlib
    state.capture_snapshot("orig", item, hashlib.sha256(backup.read_bytes()).hexdigest(), backup)
    state.record_upload_intent("orig", hashlib.sha256(output.read_bytes()).hexdigest(), output)
    state.close()
    config.write_text("[photos]\nquality = 55\n[run]\nwork_dir = 'work'\npause_seconds = 0\n", encoding="utf-8")
    new_cfg = load_config(config)
    state = StateStore(tmp_path / "state.sqlite", "account", new_cfg.fingerprint)
    result = Pipeline(new_cfg, remote, state, media=FakeMedia()).run(yes=True, report_path=tmp_path / "plan.csv")
    assert result["replaced"] == 0 and remote.uploads == 0 and remote.trashed == 0
    state.close()


def test_apply_persists_stage_before_trash_and_resume_is_safe(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\nminimum_savings_percent = 20\n", encoding="utf-8")
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)
    result = Pipeline(cfg, remote, state, media=FakeMedia()).run(yes=True, report_path=tmp_path / "plan.csv")
    assert result["replaced"] == 1
    assert remote.uploads == 1 and remote.trashed == 1
    state.close()


@pytest.mark.parametrize("stage", ["fresh", "upload_intent", "uploaded", "trash_ready"])
def test_keep_originals_verifies_replacement_without_trashing(
    tmp_path: Path, stage: str
):
    config = tmp_path / "shrink.toml"
    config.write_text(
        "[run]\nwork_dir = 'work'\nminimum_savings_percent = 20\npause_seconds = 0\n",
        encoding="utf-8",
    )
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)
    item = remote.items[0]

    if stage != "fresh":
        backup = tmp_path / "original.jpg"
        backup.write_bytes(b"original" * 12 + b"1234")
        output = tmp_path / "output.avif"
        output.write_bytes(b"compressed")
        import hashlib

        original_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
        output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        state.capture_snapshot("orig", item, original_hash, backup)
        state.record_upload_intent("orig", output_hash, output)
        remote.found_upload = {"id": "replacement", "dedup_key": "replacement-dedup", "size_bytes": 10}
        if stage in {"uploaded", "trash_ready"}:
            state.mark_uploaded("orig", "replacement", output_hash)
        if stage == "trash_ready":
            state.mark_trash_ready("orig")

    result = Pipeline(cfg, remote, state, media=FakeMedia()).run(
        yes=True, keep_originals=True, report_path=tmp_path / "plan.csv"
    )

    assert result["replaced"] == 0
    assert result["verified"] == 1
    assert remote.trashed == 0
    assert (state.get_item("orig") or {}).get("stage") == "trash_ready"
    with (tmp_path / "plan.csv").open(newline="", encoding="utf-8-sig") as stream:
        row = next(csv.DictReader(stream))
    assert row["status"] == "verified_original_kept"
    assert row["reason"] == "pilot mode: original kept after replacement verification"
    assert row["actual_size_bytes"] == "10"
    assert row["new_width"] == "100"
    assert row["actual_savings_percent"] == "90.0"
    state.close()


def _resume_state(tmp_path: Path, cfg, remote: FakeRemote, *, output_bytes: bytes = b"compressed"):
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)
    item = remote.items[0]
    backup = tmp_path / "original.jpg"
    backup.write_bytes(b"original" * 12 + b"1234")
    output = tmp_path / "output.avif"
    output.write_bytes(output_bytes)
    import hashlib

    original_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    state.capture_snapshot("orig", item, original_hash, backup)
    state.record_upload_intent("orig", output_hash, output)
    state.mark_uploaded("orig", "replacement", output_hash)
    return state, backup, output, output_hash


class RecoveryRemote(FakeRemote):
    def __init__(self, root):
        super().__init__(root)
        self.registered: list[set[str]] = []

    def register_replacements(self, ids):
        self.registered.append(set(ids))


def test_upload_intent_recovery_registers_exact_hash_match_before_verification(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text(
        "[run]\nwork_dir = 'work'\npause_seconds = 0\n",
        encoding="utf-8",
    )
    cfg = load_config(config)
    remote = RecoveryRemote(tmp_path)
    remote.found_upload = {"id": "replacement", "dedup_key": "new-dedup", "size_bytes": 10}
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)
    item = remote.items[0]
    backup = tmp_path / "original.jpg"
    backup.write_bytes(b"original" * 12 + b"1234")
    output = tmp_path / "output.avif"
    output.write_bytes(b"compressed")
    import hashlib

    original_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    state.capture_snapshot("orig", item, original_hash, backup)
    state.set_plan("orig", 10, 90, output)
    state.record_upload_intent("orig", output_hash, output)
    report = tmp_path / "report.csv"
    result = Pipeline(cfg, remote, state, media=FakeMedia()).run(
        yes=True, keep_originals=True, report_path=report
    )
    assert result["verified"] == 1
    assert remote.registered == [set(), {"replacement"}]
    assert state.get_item("orig")["replacement_id"] == "replacement"
    with report.open(newline="", encoding="utf-8-sig") as stream:
        row = next(csv.DictReader(stream))
    assert row["actual_size_bytes"] == "10"
    state.close()


def test_pending_upload_match_protects_recovered_replacement_from_inventory(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text(
        "[run]\nwork_dir = 'work'\npause_seconds = 0\n",
        encoding="utf-8",
    )
    cfg = load_config(config)
    remote = RecoveryRemote(tmp_path)
    remote.found_upload = {"id": "replacement", "dedup_key": "new-dedup", "size_bytes": 10}
    remote.items.append(_selection_item("replacement", size=100, timestamp=1704067200000))
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)
    item = remote.items[0]
    backup = tmp_path / "original.jpg"
    backup.write_bytes(b"original" * 12 + b"1234")
    output = tmp_path / "output.avif"
    output.write_bytes(b"compressed")
    import hashlib

    original_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    state.capture_snapshot("orig", item, original_hash, backup)
    state.set_plan("orig", 10, 90, output)
    state.record_upload_intent("orig", output_hash, output)
    result = Pipeline(cfg, remote, state, media=FakeMedia()).run(
        yes=True, keep_originals=True, report_path=tmp_path / "report.csv"
    )
    assert result["planned"] == 1
    assert result["verified"] == 1
    assert remote.uploads == 0
    state.close()


@pytest.mark.parametrize("skip_setup", [
    lambda item: item.update(space_taken_bytes=0),
    lambda item: item.update(metadata={"albums": [{"shared": True}]}),
])
def test_resume_skip_reason_blocks_remote_mutation(tmp_path: Path, skip_setup):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\npause_seconds = 0\n", encoding="utf-8")
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    skip_setup(remote.items[0])
    state, *_ = _resume_state(tmp_path, cfg, remote)
    result = Pipeline(cfg, remote, state, media=FakeMedia()).run(
        yes=True, report_path=tmp_path / "plan.csv"
    )
    assert result["replaced"] == 0
    assert remote.restore_calls == 0 and remote.trashed == 0
    state.close()


def test_resume_uses_actual_bytes_for_current_savings_threshold(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text(
        "[run]\nwork_dir = 'work'\npause_seconds = 0\nminimum_savings_percent = 20\n",
        encoding="utf-8",
    )
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    state, _, output, output_hash = _resume_state(tmp_path, cfg, remote, output_bytes=b"x" * 90)
    state.db.execute(
        "UPDATE items SET estimated_bytes=?, actual_bytes=?, output_hash=? WHERE original_id=?",
        (10, 90, output_hash, "orig"),
    )
    state.db.commit()
    result = Pipeline(cfg, remote, state, media=FakeMedia()).run(
        yes=True, report_path=tmp_path / "plan.csv"
    )
    assert result["replaced"] == 0
    assert remote.restore_calls == 0 and remote.trashed == 0
    state.close()


@pytest.mark.parametrize("missing_column", ["original_hash", "output_hash"])
def test_resume_requires_journal_hashes(tmp_path: Path, missing_column: str):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\npause_seconds = 0\n", encoding="utf-8")
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    state, *_ = _resume_state(tmp_path, cfg, remote)
    state.db.execute(f"UPDATE items SET {missing_column}=NULL WHERE original_id='orig'")
    state.db.commit()
    with pytest.raises(PipelineError, match="hash"):
        Pipeline(cfg, remote, state, media=FakeMedia()).run(
            yes=True, report_path=tmp_path / "plan.csv"
        )
    assert remote.restore_calls == 0 and remote.trashed == 0
    state.close()


def test_estimate_receives_source_metadata(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\n", encoding="utf-8")
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    remote.items[0]["metadata"].update(latitude=40.1, longitude=-73.1)

    class MetadataMedia(FakeMedia):
        def estimate(self, source, settings, work_dir):
            assert settings["source_metadata"]["latitude"] == 40.1
            assert settings["source_metadata"]["longitude"] == -73.1
            return super().estimate(source, settings, work_dir)

    with StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint) as state:
        result = Pipeline(cfg, remote, state, media=MetadataMedia()).run(plan_only=True)
    assert result["planned"] == 1


def test_limit_counts_only_savings_qualified_plans(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text(
        "[run]\nwork_dir = 'work'\nlimit = 1\nminimum_savings_percent = 20\n",
        encoding="utf-8",
    )
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    remote.items[0]["filename"] = "insufficient.jpg"
    remote.items[0]["size_bytes"] = 100
    remote.items.append({**remote.items[0], "id": "qualified", "dedup_key": "qualified-dedup",
                         "filename": "qualified.jpg", "size_bytes": 100})

    class BoundedMedia(FakeMedia):
        def estimate(self, source, settings, work_dir):
            return {"estimated_bytes": 100 if source.name.endswith("insufficient.jpg") else 10,
                    "method": "bounded-test"}

    with StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint) as state:
        result = Pipeline(cfg, remote, state, media=BoundedMedia()).run(
            plan_only=True, report_path=tmp_path / "plan.csv"
        )
    assert result["planned"] == 1
    assert "qualified.jpg" in (tmp_path / "plan.csv").read_text(encoding="utf-8-sig")


def _selection_item(item_id: str, *, size: int = 100, kind: str = "photo", timestamp: int = 0) -> dict:
    return {
        "id": item_id,
        "dedup_key": f"dedup-{item_id}",
        "filename": f"{item_id}.jpg",
        "size_bytes": size,
        "width": 100,
        "height": 100,
        "kind": kind,
        "timestamp_ms": timestamp,
        "timezone_offset": 0,
        "duration_seconds": None,
        "mime_type": "image/jpeg" if kind == "photo" else "video/mp4",
        "metadata": {"albums": []},
        "skip_reason": None,
    }


class StreamingRemote(FakeRemote):
    def __init__(self, root, items):
        super().__init__(root)
        self.items = items
        self.yielded = []
        self.closed = False

    def list_items(self):
        try:
            for item in self.items:
                self.yielded.append(item["id"])
                yield item
        finally:
            self.closed = True


def test_newest_first_stops_after_eligible_plan_and_closes_listing(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\nlimit = 1\nselection_order = 'newest'\n", encoding="utf-8")
    cfg = load_config(config)
    remote = StreamingRemote(tmp_path, [
        _selection_item("skipped", timestamp=3),
        _selection_item("too-small", size=10, timestamp=2),
        _selection_item("qualified", timestamp=1),
        _selection_item("overread", timestamp=0),
    ])
    remote.items[0]["skip_reason"] = "fixture_skip"
    class SelectionMedia(FakeMedia):
        def estimate(self, source, settings, work_dir):
            return {"estimated_bytes": 10 if source.name.endswith("qualified.jpg") else 10, "method": "selection"}
    with StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint) as state:
        result = Pipeline(cfg, remote, state, media=SelectionMedia()).run(plan_only=True)
    assert result["planned"] == 1
    assert remote.yielded == ["skipped", "too-small", "qualified"]
    assert remote.closed is True


def test_default_largest_first_scans_all_and_plans_largest(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\nlimit = 1\n", encoding="utf-8")
    cfg = load_config(config)
    remote = StreamingRemote(tmp_path, [
        _selection_item("small", size=100),
        _selection_item("large", size=200),
    ])
    remote.download = lambda item, destination: destination.write_bytes(b"x" * item["size_bytes"])
    with StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint) as state:
        result = Pipeline(cfg, remote, state, media=FakeMedia()).run(plan_only=True)
    assert result["planned"] == 1
    assert remote.yielded == ["small", "large"]
    report = (tmp_path / "work" / "photos-shrink.csv").read_text(encoding="utf-8-sig")
    assert "large.jpg" in report
    assert "small.jpg" not in report


def test_pending_plan_fills_limit_without_inventory_or_new_download(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\nlimit = 1\nselection_order = 'newest'\n", encoding="utf-8")
    cfg = load_config(config)
    remote = StreamingRemote(tmp_path, [_selection_item("new")])
    item = _selection_item("pending")
    backup = tmp_path / "pending.jpg"
    backup.write_bytes(b"original" * 12 + b"1234")
    import hashlib
    with StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint) as state:
        state.capture_snapshot("pending", item, hashlib.sha256(backup.read_bytes()).hexdigest(), backup)
        pending_output = tmp_path / "pending.avif"
        pending_output.write_bytes(b"output")
        state.set_plan("pending", 10, 90, pending_output)
        state.record_upload_intent("pending", hashlib.sha256(b"output").hexdigest(), pending_output)
        result = Pipeline(cfg, remote, state, media=FakeMedia()).run(plan_only=True)
    assert result["planned"] == 1
    assert remote.yielded == []


def test_photos_only_excludes_pending_video_and_allows_photo(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\nphotos_only = true\nlimit = 1\nselection_order = 'newest'\n", encoding="utf-8")
    cfg = load_config(config)
    remote = StreamingRemote(tmp_path, [_selection_item("photo"), _selection_item("after")])
    video = _selection_item("pending-video", kind="video")
    backup = tmp_path / "pending-video.mp4"
    backup.write_bytes(b"original" * 12 + b"1234")
    import hashlib
    with StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint) as state:
        state.capture_snapshot("pending-video", video, hashlib.sha256(backup.read_bytes()).hexdigest(), backup)
        pending_output = tmp_path / "pending-video.mp4"
        pending_output.write_bytes(b"output")
        state.set_plan("pending-video", 10, 90, pending_output)
        state.record_upload_intent("pending-video", hashlib.sha256(b"output").hexdigest(), pending_output)
        result = Pipeline(cfg, remote, state, media=FakeMedia()).run(plan_only=True)
        row = next(plan for plan in state.rows() if plan["original_id"] == "pending-video")
    assert result["planned"] == 1
    assert row["stage"] == "upload_intent"
    assert remote.yielded == ["photo"]


@pytest.mark.parametrize("report_name", [
    "shrink.toml",
    "cookies.txt",
    "state.sqlite",
    "work/item-dir/item-backup.csv",
])
def test_report_path_guard_rejects_protected_targets_before_inventory(tmp_path: Path, report_name: str):
    config = tmp_path / "shrink.toml"
    config.write_text(
        "[google]\ncookies_file = 'cookies.txt'\nbrowser_profile = 'browser'\n"
        "[run]\nwork_dir = 'work'\n",
        encoding="utf-8",
    )
    cfg = load_config(config)
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)

    class NoInventoryRemote(FakeRemote):
        def list_items(self):
            raise AssertionError("report validation must precede inventory")

    try:
        with pytest.raises(PipelineError, match="report"):
            Pipeline(cfg, NoInventoryRemote(tmp_path), state, media=FakeMedia()).run(
                plan_only=True, report_path=tmp_path / report_name
            )
    finally:
        state.close()


def test_report_write_is_atomic_and_preserves_existing_file_on_failure(tmp_path: Path, monkeypatch):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\n", encoding="utf-8")
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    report = tmp_path / "report.csv"
    report.write_text("keep this report", encoding="utf-8")
    pipeline = Pipeline(cfg, remote, StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint), media=FakeMedia())
    monkeypatch.setattr(pipeline, "_csv_value", lambda value: (_ for _ in ()).throw(RuntimeError("write failed")))

    with pytest.raises(RuntimeError, match="write failed"):
        pipeline._write_report([{"item": remote.items[0], "status": "planned"}], report)

    assert report.read_text(encoding="utf-8") == "keep this report"
    pipeline.state.close()


def test_report_path_guard_rejects_hardlink_to_journal(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\n", encoding="utf-8")
    cfg = load_config(config)
    state = StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint)
    hardlink = tmp_path / "journal-copy.csv"
    os.link(state.path, hardlink)

    class NoInventoryRemote(FakeRemote):
        def list_items(self):
            raise AssertionError("report validation must precede inventory")

    try:
        with pytest.raises(PipelineError, match="protected"):
            Pipeline(cfg, NoInventoryRemote(tmp_path), state, media=FakeMedia()).run(
                plan_only=True, report_path=hardlink
            )
    finally:
        state.close()


def test_csv_report_neutralizes_formula_after_leading_whitespace(tmp_path: Path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir = 'work'\n", encoding="utf-8")
    cfg = load_config(config)
    remote = FakeRemote(tmp_path)
    remote.items[0]["filename"] = "\t=HYPERLINK('https://example.test')"
    remote.items[0]["skip_reason"] = "unsafe test item"
    report = tmp_path / "report.csv"
    with StateStore(tmp_path / "state.sqlite", "account", cfg.fingerprint) as state:
        Pipeline(cfg, remote, state, media=FakeMedia()).run(plan_only=True, report_path=report)
    with report.open(newline="", encoding="utf-8-sig") as stream:
        row = next(csv.DictReader(stream))
    assert row["filename"] == "'\t=HYPERLINK('https://example.test')"
    assert row["old_path"] == ""
    assert row["new_path"] == ""
    assert row["reason"] == "unsafe test item"
    assert Pipeline._csv_value("") == ""
