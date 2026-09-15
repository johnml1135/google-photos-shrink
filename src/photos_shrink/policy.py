"""The one question every replacement path must ask: can this be touched?

Two call sites used to answer this question independently -- the main pipeline
(working from a live Google Photos item) and the Takeout encoder (working from
a locally mirrored file) -- with different input shapes and, worse, different
refusals. The encoder's gate was missing `photos_only`, `shared_album` and
`non_space_consuming` entirely, so items could clear the encode step and only
discover at replace time, after quota was already spent, that they should
never have been touched.

`Candidate` is the one shape both routes normalize into. `verdict` is the one
function that decides. A refusal that cannot yet be evaluated -- an unknown
`space_taken_bytes` or `saved_percent` -- is not a refusal: it means "not yet
known", not "zero". Getting that backwards would refuse the entire library.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings
    from .takeout import MirrorEntry


@dataclass(frozen=True)
class Candidate:
    """Everything `verdict` needs to know about one item, from either route."""

    media_key: str | None
    filename: str
    kind: str  # "photo" | "video" | "unknown"
    timestamp_ms: int | None
    space_taken_bytes: int | None  # None when not yet known
    shared_album: bool
    saved_percent: float | None = None  # known only after encoding
    # The library reports quota two ways and they are not redundant: an item
    # can be flagged as consuming no quota while still reporting a byte size.
    # Both must be carried or the refusal silently stops firing.
    space_consuming: bool | None = None  # None when not yet known

    @classmethod
    def from_library_item(cls, item: dict[str, Any]) -> Candidate:
        """Build a candidate from a live Google Photos library item."""

        albums = (item.get("metadata") or {}).get("albums") or []
        shared_album = any(
            bool(album.get("shared")) for album in albums if isinstance(album, dict)
        )
        return cls(
            media_key=item.get("id"),
            filename=str(item.get("filename") or ""),
            kind=item.get("kind") or "unknown",
            timestamp_ms=item.get("timestamp_ms"),
            space_taken_bytes=item.get("space_taken_bytes"),
            shared_album=shared_album,
            space_consuming=item.get("space_consuming"),
        )

    @classmethod
    def from_mirror_entry(cls, entry: MirrorEntry) -> Candidate:
        """Build a candidate from a Takeout mirror entry.

        A Takeout export carries no live quota accounting and no "is this
        album shared" flag -- those are properties of the live library, not of
        the files on disk. `space_taken_bytes` is therefore unknown (`None`,
        not zero) and `shared_album` is `False`: the encoder honestly cannot
        answer either question, so it must not manufacture a refusal for them.
        The replace step, working from the live item, asks again with real
        answers.
        """

        return cls(
            media_key=entry.media_key,
            filename=entry.filename,
            kind=entry.kind,
            timestamp_ms=entry.taken_timestamp_ms,
            space_taken_bytes=None,
            shared_album=False,
        )


def _consumes_no_quota(candidate: Candidate) -> bool:
    """True only when the library has actually said this item costs nothing.

    Both signals are checked because they disagree in practice: the flag can be
    False while a byte count is still reported, and the byte count can be zero
    while the flag is absent. Either one, known, is enough to refuse; neither
    being known is not.
    """

    if candidate.space_consuming is False:
        return True
    if candidate.space_taken_bytes is None:
        return False
    try:
        return int(candidate.space_taken_bytes) <= 0
    except (TypeError, ValueError):
        # An unparseable size is unknown, not zero.
        return False


def verdict(settings: Settings, candidate: Candidate) -> str | None:
    """Return why this photo must not be replaced, or None when it may be.

    Refusal tokens: "no_media_key", "non_photo", "shared_album",
    "non_space_consuming", whatever `Settings.exclusion_reason` returns
    ("excluded_name", "missing_capture_date", "invalid_capture_date",
    "excluded_date"), and "insufficient_savings".
    """

    if candidate.media_key is None:
        # Nothing downstream -- encode, upload, replace -- can identify the
        # original later without one.
        return "no_media_key"
    if settings.run.get("photos_only", False) and candidate.kind != "photo":
        return "non_photo"
    if settings.run["skip_shared"] and candidate.shared_album:
        return "shared_album"
    if settings.run.get("skip_non_space_consuming", True) and _consumes_no_quota(candidate):
        # Replacing an item that consumes no quota spends quota to save none.
        # Unknown (None) is not the same as a known zero -- see the module
        # docstring.
        return "non_space_consuming"
    exclusion = settings.exclusion_reason(
        {"filename": candidate.filename, "timestamp_ms": candidate.timestamp_ms}
    )
    if exclusion:
        return exclusion
    minimum = float(settings.run.get("minimum_savings_percent", 0))
    if candidate.saved_percent is not None and candidate.saved_percent < minimum:
        # Not yet encoded (`saved_percent is None`) is not a refusal either --
        # it means "ask again after encoding", not "insufficient".
        return "insufficient_savings"
    return None


# Every token `verdict` can return, and how it reads in a report. The
# vocabulary lives here rather than in the tools that print it: a token added
# to `verdict` without a line here is caught by `test_policy.py`'s coverage
# test instead of silently reaching a report as a bare identifier.
REFUSAL_TEXT = {
    "no_media_key": "no media key: the original could not be identified later",
    "missing_capture_date": "no timestamp: would be dated 'today' on upload",
    "invalid_capture_date": "capture date could not be parsed",
    "excluded_date": "excluded by configured date range",
    "excluded_name": "excluded by configured name pattern",
    "non_photo": "photos_only is set and this item is not a photo",
    "shared_album": "shared album",
    "non_space_consuming": "item does not consume quota",
    "insufficient_savings": "encoding saved too little to be worth the quota",
}


def explain(token: str, *, percent: float | None = None, minimum: float | None = None) -> str:
    """Render a refusal token as a line a human can read in a report.

    This decides only how a refusal *reads*. Whether an item is refused is
    `verdict`'s decision alone, and no caller may add a reason of its own.
    """

    if token == "insufficient_savings" and percent is not None and minimum is not None:
        return f"insufficient savings: {percent:.1f}% < {minimum}%"
    # A token with no line here is a bug the coverage test catches, but a
    # half-finished 10,000-item encode is not the place to raise it.
    return REFUSAL_TEXT.get(token, f"refused: {token}")
