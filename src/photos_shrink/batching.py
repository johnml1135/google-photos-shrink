"""Drive a journal's jobs through a browser session, a batch at a time.

Replacing originals and removing extra copies are different acts on different
items, but the run around them is one thing: slice the jobs, label the batch,
do the work, report each outcome, write them to the journal, save, pause, and
stop the moment the session is gone. That run was written out twice, and every
fix to it had to be made twice -- the guard that stops a dead session went into
one copy an hour before the other.

Nothing here talks to Google. The work a batch does arrives as a callable, so
a test drives a whole run with no session at all, which is what the two copies
could never offer.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .ledger import UploadJournal, UploadRecord
from .remote import RemoteProtocolError

# A batch that raises has done nothing this run can record: the work either
# returns an outcome per job or reports that the session is gone.
Work = Callable[[list[UploadRecord], Callable[[str], None]], dict[str, Any]]
Report = Callable[[str], None]


def run_batches(
    jobs: list[UploadRecord],
    *,
    records: dict[str, UploadRecord],
    journal: UploadJournal,
    work: Work,
    apply: bool,
    size: int = 100,
    pause: float = 0.0,
    report: Report = print,
    announce: Callable[[str], bool] = lambda status: True,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Run every job in batches of `size`; return the outcome counts.

    `records` maps a job's key to the journal record it belongs to -- a job
    can carry a media key recovered from the mirror that the record itself
    must not gain. Outcomes reach the journal through `journal.apply`, which
    is the only thing that decides what a status means, and a batch is saved
    before the next one starts so an interrupted run keeps what it did.

    A batch that raises `RemoteProtocolError` ends the run: the session is
    gone, so every later batch would fail the same way. Its jobs are counted
    as failed and left untouched in the journal, ready for the next run.

    Every outcome is counted; `announce` says which are worth a line, so a
    run whose ordinary answer is "kept" does not print thousands of them.
    """

    counts: dict[str, int] = {}
    for start in range(0, len(jobs), size):
        batch = jobs[start : start + size]
        label = f"[{start + 1}-{start + len(batch)}/{len(jobs)}]"
        batch_started = clock()
        try:
            outcomes = work(batch, lambda message: report(f"{label} {message}"))
        except RemoteProtocolError as exc:
            # The request carrying the whole batch failed, most often an
            # expired session, so nothing in it ran.
            report(f"{label} batch stopped: {exc}")
            counts["failed"] = counts.get("failed", 0) + len(batch)
            break
        for job in batch:
            outcome = outcomes[job.key]
            record = records[job.key]
            name = record.source.name if record.source else "?"
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            if announce(outcome.status):
                report(f"  {name}: {outcome.status.upper()} {outcome.detail}")
            if apply:
                journal.apply(record, outcome)
        if apply:
            journal.save()
        report(f"{label} done in {clock() - batch_started:.1f}s")
        if pause and start + size < len(jobs):
            sleep(pause)
    return counts


def summarise(counts: dict[str, int], seconds: float) -> str:
    """The one line a run ends on."""

    body = ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
    return f"\n--- {body} in {seconds:.1f}s ---"
