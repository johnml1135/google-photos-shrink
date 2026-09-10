# Metadata restoration and retained-original pilot

Approved scope: restore coordinates and owned shared-album membership, verify the
uploaded item, and try one replacement while retaining the original.

Implementation plan:

1. Add tests for shared-album payload selection and ownership exclusions, then
   implement the shared-album branch in the remote adapter.
2. Check the upstream location wire format; add coordinate validation, restoration,
   and verification tests before implementation. Do not remove an original when
   coordinate restoration or verification fails.
3. Add `--keep-originals` to the CLI and pipeline, covering fresh and resumed
   operations. Persist the uploaded identity and leave verified copies at
   `trash_ready`, so repeating a pilot never reuploads or trashes an original.
4. Review all changes, run the full test suite and lint, then run a limited live
   preview. If an eligible photo exists, run a one-item retained-original pilot.

Shared albums retain the existing album identity. New photo identities cannot
inherit comments, likes, direct-share links, or custom ordering. Other people's
photos remain excluded. Coordinates are the restoration target; Google-derived
place names and location provenance are not promised to remain identical.

Implementation finding: the upstream location setter needs viewport and place-ID
data that the normal item parser does not expose. Photo restoration therefore
embeds the captured coordinates in AVIF EXIF and verifies Google's interpretation
after upload. It does not invent undocumented setter fields. Video metadata is
copied by FFmpeg; Google-only video coordinates still require future support.
