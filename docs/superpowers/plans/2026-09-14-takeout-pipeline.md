# Takeout-sourced shrink pipeline

Supersedes the browser-only in-place replacement approach for bulk work. The
browser adapter is retained, but only for the one operation no API offers.

## Verified API contract (checked 2026-09-14)

Confirmed against Google's official documentation, not assumed:

| Question | Answer | Consequence |
|---|---|---|
| Can an item's bytes be updated in place? | **No.** `mediaItems.patch` accepts only `description` in `updateMask`. | There is no "update". Every replacement is upload-new + delete-old. |
| Can the API delete library items? | **No.** No delete method exists. | Deletion stays on the browser adapter (`remote.trash`, `remote.py:1017`). |
| Can the API still upload? | **Yes**, via `photoslibrary.appendonly`. Two-step: raw bytes to `/v1/uploads` for an upload token, then `mediaItems.batchCreate`. | Upload leaves the unsupported path. |
| Can the API read the library? | **No**, only items the app itself created (post-2025 scope removal). | Cannot enumerate or verify against the full library via API. Verification stays on the browser adapter. |
| Is AVIF accepted? | **Yes**: `AVIF, BMP, GIF, HEIC, ICO, JPG, PNG, TIFF, WEBP, some RAW`. | Current photo encoding is unchanged. |
| Does upload honour Storage Saver? | **No** — "stored in full resolution at original quality". | Removes the transcode that corrupted the 2026-09-10 pilot upload. Verify empirically on upload #1 regardless. |

Limits: photos 200 MB, videos 20 GB, upload tokens valid 24 hours.

## Why Takeout changes the architecture

The 2026-09-10 run died at photo 9 of 100 because a single long-lived browser
session had to survive download, encode, upload and verify for every item.
Session expiry mid-upload left an ambiguous `upload_intent` row.

Takeout moves the bulk work off that session:

1. **Download** — Takeout, one bulk legitimate export.
2. **Encode** — fully offline. No session, no rate limit, arbitrarily resumable.
3. **Upload** — `appendonly` API. Short, independent, retryable requests.
4. **Delete** — browser adapter, short bursts, only after per-item verification.

The unsupported surface shrinks to step 4 alone.

## The join key

Takeout sidecars do not carry the library media key, so files are joined to
library items by **SHA-256 of the file bytes** via the existing
`find_uploaded()` (`remote.py:714`, `GetRemoteMatchesByHash`).

**This is unproven and gates everything.** `.cache/takeout_probe.py` measures the
hash match rate on a stratified sample. Read-only; uploads, trashes and modifies
nothing. If the rate is high the join is sound. If it is low, Takeout is
rewriting bytes and we fall back to filename + timestamp + dimensions, which is
too weak to authorise deletion without much stronger guardrails.

## Sidecar resolution

Google's sidecar naming is lossy: `.supplemental-metadata.json` truncated to an
arbitrary prefix (`.supple.json`, `.s.json`), duplicate counters migrated across
the extension (`IMG(1).jpg` -> `IMG.jpg(1).json`), and `-edited` suffixes clipped
to `-edi` or shorter. `takeout.py` resolves these, and **every de-truncation is
self-validating** — a candidate is accepted only when it resolves to a sidecar
that exists, and an ambiguous prefix resolves to nothing. A wrong guess yields
"no sidecar", never another item's timestamps.

Files with no resolvable timestamp must be **quarantined, not uploaded**: without
one, Google dates them to the upload day and the timeline is destroyed.

## Ordering, and what is not negotiable

Delete only after the replacement is verified present and correct, per item.
Never a global purge: a failure mid-batch must never leave the library deleted
and the replacements absent. Originals are retained locally from the Takeout
archive regardless, so deletion is recoverable by re-upload.

## Known permanent losses

Delete-and-reupload cannot preserve, by any route:

- **Face/people groupings** — not in Takeout, not settable via API.
- **Shared albums, their comments and likes** — links others hold will break.
- **Motion photos** — Takeout splits them; `media.py:133` already skips these.

Album membership *is* recoverable (sidecar folder structure plus `albums.create`
and `batchCreate`). These losses are the price of the aggressive photo target and
should be weighed against running Google's built-in Recover storage instead,
which is free and lossless to the database but caps at 16 MP / 1080p.

## Status

- `takeout.py` + 29 tests: complete, 169 tests pass, Ruff clean.
- `.cache/takeout_probe.py`: complete, smoke-tested offline.
- Pending: the probe against the real export; then the encode/upload/delete loop.
- Carried over: 2 unresolved `upload_intent` rows and 8 verified replacements
  from the 2026-09-10 browser pilot; originals all retained.
