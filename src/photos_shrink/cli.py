"""`photos-shrink`: one command per step of the run.

    photos-shrink probe   "D:/Takeout"     # what is in the export
    photos-shrink encode  "D:/Takeout"     # offline, resumable
    photos-shrink upload                   # official API
    photos-shrink replace --apply          # browser session

Every step takes `--help`. Order matters only in that each step reads what
the one before it wrote; all of them can be re-run, and the ones that change
anything ask for `--apply` first.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from .steps import api_setup, encode, mirror, probe, remove_copies, replace, upload

STEPS: dict[str, tuple[Callable[[], int], str]] = {
    "probe": (probe.main, "Read the export and report what it holds"),
    "mirror": (mirror.main, "Join the export to the live library, item by item"),
    "encode": (encode.main, "Encode the export into replacements, offline"),
    "upload": (upload.main, "Upload the replacements over the official API"),
    "replace": (replace.main, "Restore metadata, verify, then trash the originals"),
    "remove-copies": (remove_copies.main, "Trash uploads whose originals are staying"),
    "api-setup": (api_setup.main, "Check the OAuth client and token"),
}


def usage() -> None:
    print(__doc__.strip())
    print()
    print("Steps:")
    for name, (_, blurb) in STEPS.items():
        print(f"  {name:<14} {blurb}")


def main() -> int:
    # Dispatched by hand rather than with subparsers: each step owns a full
    # parser of its own, and a shared one here would answer `replace --help`
    # with this help instead of the step's.
    argv = sys.argv[1:]
    if argv and argv[0] in STEPS:
        sys.argv = [f"photos-shrink {argv[0]}", *argv[1:]]
        return STEPS[argv[0]][0]()
    usage()
    if not argv or argv[0] in ("-h", "--help"):
        return 0
    print()
    print(f"No step called {argv[0]!r}.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
