from pathlib import Path

import pytest

from photos_shrink.state import StateError, StateStore


def test_state_persists_upload_intent_and_resume_boundaries(tmp_path: Path):
    state = StateStore(tmp_path / "state.sqlite", "account-a", "settings-a")
    state.capture_snapshot("orig", {"id": "orig", "filename": "A.JPG"}, "abc", tmp_path / "A.JPG")
    state.record_upload_intent("orig", "out-hash", tmp_path / "out.avif")
    state.mark_uploaded("orig", "replacement", "out-hash")
    state.mark_trash_ready("orig")
    state.close()

    resumed = StateStore(tmp_path / "state.sqlite", "account-a", "settings-b")
    row = resumed.get_item("orig")
    assert row["stage"] == "trash_ready"
    assert row["replacement_id"] == "replacement"
    assert row["output_hash"] == "out-hash"
    resumed.mark_trashed("orig")
    assert resumed.get_item("orig")["stage"] == "trashed"
    resumed.close()


def test_state_is_bound_to_account_and_process_lock(tmp_path: Path):
    db = tmp_path / "state.sqlite"
    first = StateStore(db, "account-a", "settings-a")
    first.close()
    with pytest.raises(StateError, match="account"):
        StateStore(db, "account-b", "settings-a")


def test_mark_skipped_persists_the_skip_reason(tmp_path: Path):
    state = StateStore(tmp_path / "state.sqlite", "account-a", "settings-a")

    state.mark_skipped("orig", {"id": "orig", "dedup_key": "dedup"}, "shared item")

    row = state.get_item("orig")
    assert row["stage"] == "skipped"
    assert row["skip_reason"] == "shared item"
    state.close()
