"""Tests for the encoder's report, which every later step reads.

The report is the only record that an encode happened. A run that narrows
itself with --kinds or --limit must not take the rest of it down.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "tools" / "takeout_encode.py"
spec = importlib.util.spec_from_file_location("takeout_encode", MODULE)
takeout_encode = importlib.util.module_from_spec(spec)
sys.modules["takeout_encode"] = takeout_encode
spec.loader.exec_module(takeout_encode)


def write_rows(path: Path, rows: list[dict]) -> None:
    takeout_encode.write_report(path, rows)


def read_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


PHOTO = {"source": "G:/in/a.jpg", "output": "G:/out/a.avif", "status": "encoded",
         "old_bytes": "100", "new_bytes": "10", "saved_percent": "90.0"}
VIDEO = {"source": "G:/in/b.mp4", "output": "G:/out/b.mp4", "status": "encoded",
         "old_bytes": "900", "new_bytes": "300", "saved_percent": "66.7"}


class TestCarriedRows:
    def test_a_video_only_run_keeps_the_photo_rows(self, tmp_path):
        """The bug: re-encoding videos deleted ten thousand photo rows."""

        report = tmp_path / "encoded.csv"
        write_rows(report, [PHOTO, VIDEO])

        carried = takeout_encode.carried_rows(report, {VIDEO["source"]})
        assert [r["source"] for r in carried] == [PHOTO["source"]]

    def test_rows_this_run_covers_are_not_carried(self, tmp_path):
        """Otherwise a re-encode would leave its own stale row behind beside it."""

        report = tmp_path / "encoded.csv"
        write_rows(report, [PHOTO, VIDEO])

        carried = takeout_encode.carried_rows(report, {PHOTO["source"], VIDEO["source"]})
        assert carried == []

    def test_no_existing_report_carries_nothing(self, tmp_path):
        assert takeout_encode.carried_rows(tmp_path / "absent.csv", {"x"}) == []

    def test_the_rewritten_report_holds_both_halves(self, tmp_path):
        """End to end: carried rows plus this run's rows, no loss, no duplicates."""

        report = tmp_path / "encoded.csv"
        write_rows(report, [PHOTO, VIDEO])

        carried = takeout_encode.carried_rows(report, {VIDEO["source"]})
        reencoded = dict(VIDEO, new_bytes="200", saved_percent="77.8")
        write_rows(report, carried + [reencoded])

        rows = read_rows(report)
        assert len(rows) == 2
        by_source = {r["source"]: r for r in rows}
        assert by_source[PHOTO["source"]]["new_bytes"] == "10"
        assert by_source[VIDEO["source"]]["new_bytes"] == "200"

    def test_a_carried_row_keeps_every_column(self, tmp_path):
        report = tmp_path / "encoded.csv"
        write_rows(report, [PHOTO])
        carried = takeout_encode.carried_rows(report, set())
        assert {k: carried[0][k] for k in PHOTO} == PHOTO
