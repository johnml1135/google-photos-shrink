"""Tests for the one module that decides whether a photo may be replaced.

`verdict` is the single gate every replacement path -- the offline Takeout
encoder and the live replace step -- must pass through. The properties that
matter most: an unknown value is never treated as a refusal, and each refusal
token keeps its exact spelling, because reports and journals record them.
"""

from __future__ import annotations

from pathlib import Path

from photos_shrink.config import load_config
from photos_shrink.policy import Candidate, verdict
from photos_shrink.takeout import MirrorEntry


def settings_for(tmp_path: Path, **run):
    config = tmp_path / "shrink.toml"
    body = "[run]\nwork_dir = 'work'\n"
    for key, value in run.items():
        body += f"{key} = {str(value).lower() if isinstance(value, bool) else value}\n"
    config.write_text(body, encoding="utf-8")
    return load_config(config)


def ordinary_candidate(**overrides) -> Candidate:
    values = dict(
        media_key="KEY1",
        filename="IMG_1.jpg",
        kind="photo",
        timestamp_ms=1_600_000_000_000,
        space_taken_bytes=1000,
        shared_album=False,
    )
    values.update(overrides)
    return Candidate(**values)


def mirror_entry(**overrides) -> MirrorEntry:
    values = dict(
        media_key="KEY1",
        path=Path("photo.jpg"),
        kind="photo",
        size_bytes=1000,
        filename="photo.jpg",
        albums=(),
        copies=1,
        taken_timestamp_ms=1_600_000_000_000,
    )
    values.update(overrides)
    return MirrorEntry(**values)


class TestCandidateFromLibraryItem:
    def test_maps_the_ordinary_fields(self):
        item = {
            "id": "KEY1",
            "filename": "IMG_1.jpg",
            "kind": "photo",
            "timestamp_ms": 123,
            "space_taken_bytes": 456,
        }
        candidate = Candidate.from_library_item(item)
        assert candidate.media_key == "KEY1"
        assert candidate.filename == "IMG_1.jpg"
        assert candidate.kind == "photo"
        assert candidate.timestamp_ms == 123
        assert candidate.space_taken_bytes == 456
        assert candidate.shared_album is False
        assert candidate.saved_percent is None

    def test_detects_a_shared_album(self):
        item = {"id": "K", "metadata": {"albums": [{"id": "a", "shared": True}]}}
        assert Candidate.from_library_item(item).shared_album is True

    def test_ignores_albums_that_are_not_shared(self):
        item = {"id": "K", "metadata": {"albums": [{"id": "a", "shared": False}]}}
        assert Candidate.from_library_item(item).shared_album is False

    def test_missing_id_is_no_media_key(self):
        assert Candidate.from_library_item({}).media_key is None

    def test_missing_space_taken_bytes_is_unknown_not_zero(self):
        item = {"id": "K"}
        assert Candidate.from_library_item(item).space_taken_bytes is None


class TestCandidateFromMirrorEntry:
    def test_maps_the_ordinary_fields(self):
        entry = mirror_entry(media_key="KEY9", filename="a.jpg", kind="video",
                              taken_timestamp_ms=42)
        candidate = Candidate.from_mirror_entry(entry)
        assert candidate.media_key == "KEY9"
        assert candidate.filename == "a.jpg"
        assert candidate.kind == "video"
        assert candidate.timestamp_ms == 42

    def test_quota_is_unknown_a_takeout_export_cannot_know_it(self):
        assert Candidate.from_mirror_entry(mirror_entry()).space_taken_bytes is None

    def test_shared_album_status_is_unknown_and_defaults_false(self):
        # A Takeout export carries album names but no "is this shared" flag.
        assert Candidate.from_mirror_entry(mirror_entry()).shared_album is False

    def test_missing_media_key_carries_through(self):
        assert Candidate.from_mirror_entry(mirror_entry(media_key=None)).media_key is None

    def test_missing_timestamp_carries_through(self):
        entry = mirror_entry(taken_timestamp_ms=None)
        assert Candidate.from_mirror_entry(entry).timestamp_ms is None


class TestVerdict:
    def test_an_ordinary_candidate_is_allowed(self, tmp_path):
        settings = settings_for(tmp_path)
        assert verdict(settings, ordinary_candidate()) is None

    def test_no_media_key_is_refused(self, tmp_path):
        settings = settings_for(tmp_path)
        assert verdict(settings, ordinary_candidate(media_key=None)) == "no_media_key"

    def test_photos_only_refuses_a_video(self, tmp_path):
        settings = settings_for(tmp_path, photos_only=True)
        assert verdict(settings, ordinary_candidate(kind="video")) == "non_photo"

    def test_photos_only_allows_a_photo(self, tmp_path):
        settings = settings_for(tmp_path, photos_only=True)
        assert verdict(settings, ordinary_candidate(kind="photo")) is None

    def test_shared_album_is_refused_when_skip_shared_is_set(self, tmp_path):
        settings = settings_for(tmp_path, skip_shared="true")
        assert verdict(settings, ordinary_candidate(shared_album=True)) == "shared_album"

    def test_shared_album_is_allowed_when_skip_shared_is_off(self, tmp_path):
        settings = settings_for(tmp_path, skip_shared="false")
        assert verdict(settings, ordinary_candidate(shared_album=True)) is None

    def test_a_known_zero_space_taken_is_non_space_consuming(self, tmp_path):
        settings = settings_for(tmp_path)
        assert verdict(settings, ordinary_candidate(space_taken_bytes=0)) == "non_space_consuming"

    def test_unknown_space_taken_is_not_a_refusal(self, tmp_path):
        """The whole-library bug this module fixes: unknown must not mean refused."""

        settings = settings_for(tmp_path)
        assert verdict(settings, ordinary_candidate(space_taken_bytes=None)) is None

    def test_skip_non_space_consuming_can_be_disabled(self, tmp_path):
        settings = settings_for(tmp_path, skip_non_space_consuming="false")
        assert verdict(settings, ordinary_candidate(space_taken_bytes=0)) is None

    def test_a_false_space_consuming_flag_is_refused_on_its_own(self, tmp_path):
        """The library reports quota two ways and they disagree.

        An item can be flagged as consuming no quota while still reporting a
        positive byte count. Checking only the byte count lets it through, and
        replacing it spends quota to save nothing.
        """

        settings = settings_for(tmp_path)
        candidate = ordinary_candidate(space_consuming=False, space_taken_bytes=4096)
        assert verdict(settings, candidate) == "non_space_consuming"

    def test_an_unknown_space_consuming_flag_is_not_a_refusal(self, tmp_path):
        settings = settings_for(tmp_path)
        candidate = ordinary_candidate(space_consuming=None, space_taken_bytes=1000)
        assert verdict(settings, candidate) is None

    def test_a_string_space_taken_is_coerced_before_comparison(self, tmp_path):
        settings = settings_for(tmp_path)
        assert verdict(settings, ordinary_candidate(space_taken_bytes="0")) == "non_space_consuming"

    def test_an_unparseable_space_taken_is_unknown_not_zero(self, tmp_path):
        settings = settings_for(tmp_path)
        assert verdict(settings, ordinary_candidate(space_taken_bytes="unknown")) is None

    def test_the_space_consuming_flag_survives_from_a_library_item(self, tmp_path):
        """Regression: the flag was dropped when Candidate was introduced."""

        settings = settings_for(tmp_path)
        item = {
            "id": "KEY1",
            "kind": "photo",
            "filename": "IMG_1.jpg",
            "timestamp_ms": 1_600_000_000_000,
            "space_taken_bytes": 4096,
            "space_consuming": False,
        }
        assert Candidate.from_library_item(item).space_consuming is False
        assert verdict(settings, Candidate.from_library_item(item)) == "non_space_consuming"

    def test_exclusion_reason_is_delegated_to_verbatim(self, tmp_path):
        config = tmp_path / "shrink.toml"
        config.write_text(
            "[run]\nwork_dir = 'work'\n[exclude]\nname_globs = ['*.jpg']\n",
            encoding="utf-8",
        )
        settings = load_config(config)
        assert verdict(settings, ordinary_candidate(filename="IMG_1.jpg")) == "excluded_name"

    def test_missing_timestamp_is_the_exclusion_reasons_token(self, tmp_path):
        settings = settings_for(tmp_path)
        assert verdict(settings, ordinary_candidate(timestamp_ms=None)) == "missing_capture_date"

    def test_unencoded_savings_is_not_a_refusal(self, tmp_path):
        settings = settings_for(tmp_path, minimum_savings_percent=50)
        assert verdict(settings, ordinary_candidate(saved_percent=None)) is None

    def test_insufficient_savings_is_refused_once_known(self, tmp_path):
        settings = settings_for(tmp_path, minimum_savings_percent=50)
        assert verdict(settings, ordinary_candidate(saved_percent=10.0)) == "insufficient_savings"

    def test_sufficient_savings_is_allowed(self, tmp_path):
        settings = settings_for(tmp_path, minimum_savings_percent=50)
        assert verdict(settings, ordinary_candidate(saved_percent=60.0)) is None

    def test_no_media_key_takes_priority_over_every_other_refusal(self, tmp_path):
        settings = settings_for(tmp_path, photos_only=True, skip_shared="true")
        candidate = ordinary_candidate(media_key=None, kind="video", shared_album=True,
                                        space_taken_bytes=0)
        assert verdict(settings, candidate) == "no_media_key"


class TestRefusalVocabulary:
    """Every refusal `verdict` can reach must have a line a human can read.

    The encoder used to keep its own copy of this mapping and fall back to
    printing the bare token when the two drifted, which quietly undid the
    point of having one gate.
    """

    def _tokens_verdict_can_return(self, tmp_path) -> set[str]:
        """Drive `verdict` down every refusal branch and collect what it says."""

        config = tmp_path / "shrink.toml"
        config.write_text(
            "[run]\nwork_dir = 'work'\nphotos_only = true\nskip_shared = true\n"
            "minimum_savings_percent = 20\n"
            "[exclude]\nname_globs = ['*.raw']\n"
            "date_ranges = [{ start = 2019-01-01, end = 2019-12-31 }]\n",
            encoding="utf-8",
        )
        settings = load_config(config)
        in_2019 = 1_560_000_000_000
        cases = [
            ordinary_candidate(media_key=None),
            ordinary_candidate(kind="video"),
            ordinary_candidate(shared_album=True),
            ordinary_candidate(space_consuming=False),
            ordinary_candidate(filename="RAW_1.raw"),
            ordinary_candidate(timestamp_ms=None),
            ordinary_candidate(timestamp_ms="not-a-time"),
            ordinary_candidate(timestamp_ms=in_2019),
            ordinary_candidate(saved_percent=1.0),
        ]
        return {token for token in (verdict(settings, c) for c in cases) if token}

    def test_every_reachable_token_has_text(self, tmp_path):
        from photos_shrink.policy import REFUSAL_TEXT

        missing = self._tokens_verdict_can_return(tmp_path) - set(REFUSAL_TEXT)
        assert not missing, f"tokens with no readable text: {sorted(missing)}"

    def test_the_branches_cover_the_documented_vocabulary(self, tmp_path):
        """If this fails, either a refusal was removed or a case went stale."""

        from photos_shrink.policy import REFUSAL_TEXT

        assert self._tokens_verdict_can_return(tmp_path) == set(REFUSAL_TEXT)

    def test_explain_formats_the_savings_shortfall_with_its_numbers(self):
        from photos_shrink.policy import explain

        assert explain("insufficient_savings", percent=3.25, minimum=20.0) == (
            "insufficient savings: 3.2% < 20.0%"
        )

    def test_explain_marks_an_unknown_token_rather_than_passing_it_through(self):
        from photos_shrink.policy import explain

        assert explain("brand_new_refusal") == "refused: brand_new_refusal"
