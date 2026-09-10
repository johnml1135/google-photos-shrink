"""Crash-safe SQLite journal for replacement operations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Self

from filelock import FileLock, Timeout


class StateError(RuntimeError):
    """Raised when the state journal cannot safely be used."""


class StateStore:
    def __init__(self, path: str | Path, account_id: str, settings_fingerprint: str, lock_timeout: float = 0):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(self.path) + ".lock")
        try:
            self._lock.acquire(timeout=lock_timeout)
        except Timeout as exc:
            raise StateError("another photos-shrink process is already running") from exc
        try:
            self.db = sqlite3.connect(self.path, timeout=30)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self._init(account_id, settings_fingerprint)
        except Exception:
            if getattr(self, "db", None) is not None:
                self.db.close()
            self._lock.release()
            raise

    def _init(self, account_id: str, fingerprint: str) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS journal_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
                original_id TEXT PRIMARY KEY,
                dedup_key TEXT,
                original_hash TEXT,
                original_bytes INTEGER,
                original_json TEXT NOT NULL,
                backup_path TEXT,
                source_path TEXT,
                output_path TEXT,
                output_hash TEXT,
                encoding_fingerprint TEXT,
                replacement_id TEXT,
                stage TEXT NOT NULL DEFAULT 'seen',
                estimated_bytes INTEGER,
                actual_bytes INTEGER,
                estimated_savings INTEGER,
                actual_savings INTEGER,
                skip_reason TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(items)")}
        if "original_bytes" not in columns:
            self.db.execute("ALTER TABLE items ADD COLUMN original_bytes INTEGER")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(items)")}
        if "encoding_fingerprint" not in columns:
            self.db.execute("ALTER TABLE items ADD COLUMN encoding_fingerprint TEXT")
        self.account_id = account_id
        self.settings_fingerprint = fingerprint
        current = dict(self.db.execute("SELECT key,value FROM journal_meta").fetchall())
        if current and current.get("account_id") != account_id:
            raise StateError("state journal belongs to a different account")
        if not current:
            self.db.executemany("INSERT INTO journal_meta(key,value) VALUES (?,?)", [
                ("account_id", account_id), ("settings_fingerprint", fingerprint)])
        elif current.get("settings_fingerprint") != fingerprint:
            # Encoding changes must not erase identity or completed stage history.
            self.db.execute("UPDATE journal_meta SET value=? WHERE key='settings_fingerprint'", (fingerprint,))
        self.db.commit()

    def capture_snapshot(self, original_id: str, item: dict[str, Any], original_hash: str,
                         backup_path: str | Path | None = None, source_path: str | Path | None = None) -> None:
        self.db.execute("""INSERT INTO items(original_id,dedup_key,original_hash,original_json,backup_path,source_path)
            VALUES(?,?,?,?,?,?) ON CONFLICT(original_id) DO UPDATE SET
            dedup_key=excluded.dedup_key, original_hash=CASE WHEN items.stage IN ('seen','skipped') THEN excluded.original_hash ELSE items.original_hash END,
            original_json=CASE WHEN items.stage IN ('seen','skipped') THEN excluded.original_json ELSE items.original_json END,
            backup_path=CASE WHEN items.stage IN ('seen','skipped') THEN excluded.backup_path ELSE items.backup_path END,
            source_path=COALESCE(items.source_path,excluded.source_path), updated_at=CURRENT_TIMESTAMP""",
            (original_id, item.get("dedup_key"), original_hash, json.dumps(item, sort_keys=True),
             str(backup_path) if backup_path else None, str(source_path) if source_path else None))
        self.db.execute("UPDATE items SET original_bytes=COALESCE(original_bytes, ?) WHERE original_id=?",
                        (item.get("size_bytes"), original_id))
        self.db.execute("UPDATE items SET encoding_fingerprint=COALESCE(encoding_fingerprint, ?) WHERE original_id=?",
                        (self.settings_fingerprint, original_id))
        self.db.commit()

    def set_plan(self, original_id: str, estimated_bytes: int, estimated_savings: int,
                 output_path: str | Path | None = None) -> None:
        self.db.execute("UPDATE items SET estimated_bytes=?, estimated_savings=?, encoding_fingerprint=?, output_path=COALESCE(?,output_path), updated_at=CURRENT_TIMESTAMP WHERE original_id=?",
                        (estimated_bytes, estimated_savings, self.settings_fingerprint, str(output_path) if output_path else None, original_id))
        self.db.commit()

    def record_upload_intent(self, original_id: str, output_hash: str, output_path: str | Path) -> None:
        self.db.execute("UPDATE items SET output_hash=?, output_path=?, stage='upload_intent', updated_at=CURRENT_TIMESTAMP WHERE original_id=?",
                        (output_hash, str(output_path), original_id))
        self.db.commit()

    def mark_encoded(self, original_id: str, output_hash: str, actual_bytes: int) -> None:
        self.db.execute("UPDATE items SET output_hash=?, actual_bytes=?, actual_savings=CASE WHEN original_bytes IS NULL THEN NULL ELSE original_bytes-? END, stage='encoded', updated_at=CURRENT_TIMESTAMP WHERE original_id=?",
                        (output_hash, actual_bytes, actual_bytes, original_id))
        self.db.commit()

    def mark_uploaded(self, original_id: str, replacement_id: str, output_hash: str) -> None:
        self.db.execute("UPDATE items SET replacement_id=?, output_hash=?, stage='uploaded', updated_at=CURRENT_TIMESTAMP WHERE original_id=?",
                        (replacement_id, output_hash, original_id))
        self.db.commit()

    def mark_trash_ready(self, original_id: str) -> None:
        self.db.execute("UPDATE items SET stage='trash_ready', updated_at=CURRENT_TIMESTAMP WHERE original_id=?", (original_id,))
        self.db.commit()

    def mark_trashed(self, original_id: str) -> None:
        self.db.execute("UPDATE items SET stage='trashed', updated_at=CURRENT_TIMESTAMP WHERE original_id=?", (original_id,))
        self.db.commit()

    def mark_skipped(self, original_id: str, item: dict[str, Any], reason: str) -> None:
        self.db.execute("""INSERT INTO items(original_id,dedup_key,original_json,stage,skip_reason)
            VALUES(?,?,?,'skipped',?) ON CONFLICT(original_id) DO UPDATE SET skip_reason=CASE WHEN items.stage IN ('upload_intent','uploaded','trash_ready','trashed') THEN items.skip_reason ELSE excluded.skip_reason END, stage=CASE WHEN items.stage IN ('upload_intent','uploaded','trash_ready','trashed') THEN items.stage ELSE 'skipped' END, updated_at=CURRENT_TIMESTAMP""",
            (original_id, item.get("dedup_key"), json.dumps(item, sort_keys=True), reason))
        self.db.commit()

    def get_item(self, original_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM items WHERE original_id=?", (original_id,)).fetchone()
        return dict(row) if row else None

    def is_replaced(self, original_id: str, output_hash: str | None = None) -> bool:
        row = self.get_item(original_id)
        if not row or row["stage"] not in {"uploaded", "trash_ready", "trashed"}:
            return False
        return output_hash is None or row["output_hash"] == output_hash

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM items ORDER BY original_id")]

    def close(self) -> None:
        if getattr(self, "db", None) is not None:
            self.db.close()
            self.db = None
        if getattr(self, "_lock", None) is not None:
            self._lock.release()
            self._lock = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
