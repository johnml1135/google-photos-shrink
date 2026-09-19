"""Tests for the run around a batch.

This loop was written out twice, once per session step, and neither copy had a
test: both needed a live Google session to reach. The guard that stops a dead
run went into one copy an hour before the other, and the journal-save that
keeps an interrupted run's work sat after the `break` in both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photos_shrink.batching import run_batches, summarise
from photos_shrink.ledger import UploadJournal, UploadRecord
from photos_shrink.remote import RemoteProtocolError


class Outcome:
    def __init__(self, status, detail="", original_media_key=None):
        self.status, self.detail, self.original_media_key = status, detail, original_media_key


def jobs_for(*names: str) -> list[UploadRecord]:
    return [UploadRecord(output=Path(f"{n}.avif"), source=Path(f"{n}.jpg"), verified="ok") for n in names]


def journal_of(tmp_path, jobs) -> UploadJournal:
    journal = UploadJournal(tmp_path / "j.json")
    for job in jobs:
        journal.record(job)
    return journal


def run(tmp_path, jobs, work, *, apply=True, **kwargs):
    lines: list[str] = []
    counts = run_batches(
        jobs,
        records={job.key: job for job in jobs},
        journal=journal_of(tmp_path, jobs),
        work=work,
        apply=apply,
        report=lines.append,
        clock=lambda: 0.0,
        sleep=lambda seconds: lines.append(f"slept {seconds}"),
        **kwargs,
    )
    return counts, lines


def replaces_everything(batch, progress):
    progress(f"working {len(batch)}")
    return {job.key: Outcome("replaced", "done", "ORIG") for job in batch}


class TestRunBatches:
    def test_every_job_is_worked_and_counted(self, tmp_path):
        jobs = jobs_for(*[f"n{i}" for i in range(5)])
        counts, _ = run(tmp_path, jobs, replaces_everything, size=2)
        assert counts == {"replaced": 5}

    def test_the_batch_size_is_respected(self, tmp_path):
        seen: list[int] = []

        def work(batch, progress):
            seen.append(len(batch))
            return {job.key: Outcome("replaced") for job in batch}

        run(tmp_path, jobs_for(*[f"n{i}" for i in range(5)]), work, size=2)
        assert seen == [2, 2, 1]

    def test_outcomes_reach_the_journal(self, tmp_path):
        jobs = jobs_for("a", "b")
        journal = journal_of(tmp_path, jobs)
        run_batches(
            jobs, records={job.key: job for job in jobs}, journal=journal,
            work=replaces_everything, apply=True, report=lambda message: None,
        )
        assert [r.replaced for r in journal.all_records()] == ["replaced", "replaced"]
        assert UploadJournal.load(journal.path).all_records()[0].replaced == "replaced"

    def test_a_dry_run_writes_nothing(self, tmp_path):
        jobs = jobs_for("a", "b")
        journal = journal_of(tmp_path, jobs)
        run_batches(
            jobs, records={job.key: job for job in jobs}, journal=journal,
            work=replaces_everything, apply=False, report=lambda message: None,
        )
        assert [r.replaced for r in journal.all_records()] == [None, None]

    def test_a_batch_is_saved_before_the_next_one_starts(self, tmp_path):
        """An interrupted run keeps what it already did."""

        jobs = jobs_for("a", "b", "c", "d")
        journal = journal_of(tmp_path, jobs)
        saved: list[int] = []

        def work(batch, progress):
            saved.append(sum(1 for r in UploadJournal.load(journal.path).all_records() if r.replaced))
            return {job.key: Outcome("replaced") for job in batch}

        run_batches(
            jobs, records={job.key: job for job in jobs}, journal=journal,
            work=work, apply=True, size=2, report=lambda message: None,
        )
        assert saved == [0, 2]  # the second batch starts with the first already on disk

    def test_a_dead_session_stops_the_run_and_counts_what_it_skipped(self, tmp_path):
        worked: list[int] = []

        def work(batch, progress):
            worked.append(len(batch))
            raise RemoteProtocolError("the session is gone")

        counts, lines = run(tmp_path, jobs_for(*[f"n{i}" for i in range(6)]), work, size=2)
        assert worked == [2]  # it stopped rather than trying the rest
        assert counts == {"failed": 2}
        assert any("batch stopped" in line for line in lines)

    def test_a_stopped_batch_leaves_its_records_untouched(self, tmp_path):
        """Nothing is marked, so the next run picks the same jobs up again."""

        jobs = jobs_for("a", "b")
        journal = journal_of(tmp_path, jobs)

        def work(batch, progress):
            raise RemoteProtocolError("the session is gone")

        run_batches(
            jobs, records={job.key: job for job in jobs}, journal=journal,
            work=work, apply=True, report=lambda message: None,
        )
        assert [r.replaced for r in journal.all_records()] == [None, None]

    def test_each_outcome_is_reported_with_its_file(self, tmp_path):
        _, lines = run(tmp_path, jobs_for("a"), replaces_everything)
        assert any("a.jpg: REPLACED done" in line for line in lines)

    def test_the_progress_a_batch_reports_carries_its_label(self, tmp_path):
        _, lines = run(tmp_path, jobs_for("a", "b"), replaces_everything, size=1)
        assert "[1-1/2] working 1" in lines and "[2-2/2] working 1" in lines

    def test_an_ordinary_answer_can_be_left_unannounced(self, tmp_path):
        jobs = jobs_for("a", "b")
        counts, lines = run(
            tmp_path, jobs, lambda batch, progress: {job.key: Outcome("kept", "not refused") for job in batch},
            announce=lambda status: status != "kept",
        )
        assert counts == {"kept": 2}
        assert not any("KEPT" in line for line in lines)

    def test_it_pauses_between_batches_but_not_after_the_last(self, tmp_path):
        _, lines = run(tmp_path, jobs_for("a", "b", "c", "d"), replaces_everything, size=2, pause=7)
        assert [line for line in lines if line.startswith("slept")] == ["slept 7"]

    @pytest.mark.parametrize("size", [1, 3, 100])
    def test_no_job_is_worked_twice_whatever_the_batch_size(self, tmp_path, size):
        jobs = jobs_for(*[f"n{i}" for i in range(7)])
        seen: list[str] = []

        def work(batch, progress):
            seen.extend(job.key for job in batch)
            return {job.key: Outcome("replaced") for job in batch}

        run(tmp_path, jobs, work, size=size)
        assert sorted(seen) == sorted(job.key for job in jobs)


class TestSummarise:
    def test_it_reads_as_one_line_of_statuses(self):
        assert summarise({"replaced": 2, "failed": 1}, 3.25) == "\n--- failed 1, replaced 2 in 3.2s ---"

    def test_a_run_that_did_nothing_still_says_so(self):
        assert summarise({}, 0.0) == "\n---  in 0.0s ---"
