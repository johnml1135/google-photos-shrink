"""Tests for the encoder's report, which every later step reads.

The report is the only record that an encode happened. A run that narrows
itself with --kinds, stops at --limit, or is killed partway must not take the
rest of it down.

The first version of this protection decided what to keep from the items a run
*considered*. Its first real use was a `--limit 50` run, which considered 9,988
photos, reached 50, and dropped 9,934 rows. Its tests passed a considered set
that happened to equal what was rewritten, so none of them exercised the gap.
The tests below are written against that gap first.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "tools" / "takeout_encode.py"
spec = importlib.util.spec_from_file_location("takeout_encode", MODULE)
takeout_encode = importlib.util.module_from_spec(spec)
sys.modules["takeout_encode"] = takeout_encode
spec.loader.exec_module(takeout_encode)

merge_report = takeout_encode.merge_report


def row(name: str, **fields) -> dict:
    return {"source": f"G:/in/{name}", "status": "encoded", "new_bytes": "10", **fields}


def sources(rows: list[dict]) -> list[str]:
    return [r["source"] for r in rows]


class TestMergeReport:
    def test_a_limited_run_keeps_every_row_it_never_reached(self):
        """The 9,934-row loss: considered everything, reached one."""

        original = [row("A.jpg"), row("B.jpg"), row("C.jpg")]
        produced = [row("A.jpg", new_bytes="7")]

        merged = merge_report(original, produced)

        assert sources(merged) == sources(original)
        assert merged[0]["new_bytes"] == "7"
        assert merged[1]["new_bytes"] == merged[2]["new_bytes"] == "10"

    def test_a_video_only_run_keeps_the_photo_rows(self):
        original = [row("a.jpg"), row("b.mp4")]
        produced = [row("b.mp4", new_bytes="3")]

        merged = merge_report(original, produced)

        assert sources(merged) == ["G:/in/a.jpg", "G:/in/b.mp4"]
        assert merged[1]["new_bytes"] == "3"

    def test_a_run_that_produced_nothing_leaves_the_report_unchanged(self):
        """A run killed before its first row must not empty the report."""

        original = [row("A.jpg"), row("B.jpg")]
        assert merge_report(original, []) == original

    def test_a_rewritten_row_replaces_its_predecessor_in_place(self):
        """No duplicates, and the row does not move to the end."""

        original = [row("A.jpg"), row("B.jpg"), row("C.jpg")]
        produced = [row("B.jpg", status="skipped")]

        merged = merge_report(original, produced)

        assert len(merged) == 3
        assert sources(merged) == sources(original)
        assert merged[1]["status"] == "skipped"

    def test_a_re_gate_can_turn_an_encoded_row_into_a_skip(self):
        """The point of re-running: the newest verdict on a source wins."""

        original = [row("big.jpg", saved_percent="-54.5")]
        produced = [row("big.jpg", status="skipped", reason="insufficient savings")]

        assert merge_report(original, produced)[0]["status"] == "skipped"

    def test_a_source_the_report_never_had_is_appended(self):
        original = [row("A.jpg")]
        produced = [row("NEW.mp4")]

        assert sources(merge_report(original, produced)) == ["G:/in/A.jpg", "G:/in/NEW.mp4"]

    def test_no_existing_report_is_just_the_new_rows(self):
        produced = [row("A.jpg"), row("B.jpg")]
        assert merge_report([], produced) == produced


class TestReportRoundTrip:
    def test_a_limited_run_leaves_the_file_whole(self, tmp_path):
        """Through the real files, the way the encoder actually uses them."""

        report = tmp_path / "encoded.csv"
        takeout_encode.write_report(report, [row(f"{i}.jpg") for i in range(100)])

        original = takeout_encode.load_report(report)
        takeout_encode.write_report(report, merge_report(original, [row("0.jpg", new_bytes="1")]))

        after = takeout_encode.load_report(report)
        assert len(after) == 100
        assert after[0]["new_bytes"] == "1"
        assert all(r["new_bytes"] == "10" for r in after[1:])

    def test_a_report_that_does_not_exist_loads_empty(self, tmp_path):
        assert takeout_encode.load_report(tmp_path / "absent.csv") == []
