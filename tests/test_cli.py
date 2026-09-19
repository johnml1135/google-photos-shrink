"""Tests for the one command every step is reached through.

A step used to be a script run by path, so nothing could call one: the CLI
rewrote `sys.argv` and handed the step nothing. A step now takes its arguments,
which is what lets these tests exist at all.
"""

from __future__ import annotations

import pytest

from photos_shrink import cli


@pytest.fixture
def no_arguments(monkeypatch):
    monkeypatch.setattr("sys.argv", ["photos-shrink"])


class TestDispatch:
    def test_every_listed_step_is_callable(self):
        for name, (run, blurb) in cli.STEPS.items():
            assert callable(run), name
            assert blurb, name

    def test_a_step_receives_its_own_arguments(self, monkeypatch):
        seen = {}

        def remember(argv):
            seen["argv"] = argv
            return 0

        monkeypatch.setitem(cli.STEPS, "probe", (remember, "test"))
        monkeypatch.setattr("sys.argv", ["photos-shrink", "probe", "D:/Takeout", "--sample", "25"])
        assert cli.main() == 0
        assert seen["argv"] == ["D:/Takeout", "--sample", "25"]

    def test_a_step_that_fails_returns_its_own_code(self, monkeypatch):
        monkeypatch.setitem(cli.STEPS, "probe", (lambda argv: 2, "test"))
        monkeypatch.setattr("sys.argv", ["photos-shrink", "probe"])
        assert cli.main() == 2

    def test_no_step_prints_the_steps_and_asks_for_one(self, capsys, no_arguments):
        assert cli.main() == 0
        printed = capsys.readouterr().out
        for name in cli.STEPS:
            assert name in printed

    def test_an_unknown_step_is_refused_by_name(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["photos-shrink", "encde"])
        assert cli.main() == 2
        assert "encde" in capsys.readouterr().out

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_help_is_not_an_error(self, capsys, monkeypatch, flag):
        monkeypatch.setattr("sys.argv", ["photos-shrink", flag])
        assert cli.main() == 0

    def test_a_step_keeps_its_own_help(self, capsys, monkeypatch):
        """`replace --help` must answer for the step, not for this dispatcher."""

        monkeypatch.setattr("sys.argv", ["photos-shrink", "replace", "--help"])
        with pytest.raises(SystemExit) as exit_code:
            cli.main()
        assert exit_code.value.code == 0
        printed = capsys.readouterr().out
        assert "--keep-originals" in printed
        assert "remove-copies" not in printed
