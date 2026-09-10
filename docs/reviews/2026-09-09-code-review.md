# Code review — 2026-09-09

Scope: the app, tests, configuration, and documentation, including untracked files, against initial commit `1a9a135d3729a373a4ce6db5b8a9ad16ee9ead64`. The committed comparison is `git diff 1a9a135...HEAD`; the intervening commit is `2bd4b0c Document photos shrink implementation plan`. Standards and Spec were reviewed independently, followed by an independent safety review. Luna implemented the fixes; the primary agent inspected them and ran verification.

## Standards

Three findings addressed:

1. **Duplicated finalization logic** (judgment): fresh and resumed operations now use one verification/trash finalization method in `pipeline.py`.
2. **Incorrect documented timezone units**: the integration contract now specifies milliseconds, with conversion to seconds at the timestamp-setter boundary.
3. **Duplicated SHA-256 loops** (judgment): media, pipeline, and remote verification now share `integrity.sha256_file`.

No general coding-standard files were found. The spec explicitly endorses ordinary dictionaries at the remote boundary; that choice was not treated as a violation.

## Spec

Eleven findings addressed across the independent Spec review, primary inspection, and safety review:

1. **Authentication recovery**: definite browser setup, account-check, and account-mismatch failures before submission are retryable. Ambiguous submission outcomes retain their upload intent.
2. **Ownership**: an arbitrary owner actor ID or “saved to your photos” flag is no longer ownership proof. Explicit library ownership or a replacement uploaded by this run is required; explicit shared/unowned state takes precedence.
3. **Incomplete upload metadata**: missing or malformed source metadata no longer raises an uncontrolled indexing error for a known upload.
4. **Quota reporting**: the CSV explicitly labels file savings, includes known original quota consumption, and leaves estimated quota savings unknown.
5. **Library coverage and limits**: pagination no longer stops at an arbitrary candidate pool. Only savings-qualified plans count toward the limit.
6. **Resume safeguards**: pending operations honor current exclusions, sharing/quota policy, and savings thresholds; missing journal hashes fail closed.
7. **Estimate consistency**: photo estimates receive the same source GPS metadata as final encoding; resumed estimated savings remain consistent with estimated size.
8. **Unknown flags**: missing favorite/archive attributes stay unknown. The pinned parser's present-null convention still maps to false.
9. **Report integrity**: report paths cannot overwrite protected configuration, authentication, journal, browser, or media artifacts. Reports are replaced atomically, retaining the prior report if generation fails.
10. **Multiple video tracks**: unsupported files are rejected before encoding can silently discard additional video streams.
11. **Spreadsheet cells**: leading whitespace/control characters cannot hide a formula prefix, and empty CSV cells remain empty.

## Validation and practical limits

Automated verification uses generated media and fake Google responses; it does not change the Google Photos library. After integration, **106 tests passed** in 12.93 seconds, **Ruff passed**, and **the locked offline encoder doctor passed**. Tests cover real local AVIF/HEVC encoding, upload recovery, ownership, metadata, report protection, and unsupported video tracks.

The earlier retained-original live photo pilot succeeded, including capture time and GPS. Live shared-album restoration, video replacement, and trash remain unverified. A trashed item that Google no longer allows the adapter to query causes recovery to stop with the local backup retained; absence is never treated as proof of successful deletion.

The independent safety review concluded **GO for a user-controlled, one-item retained-original photo pilot**. Start with `uv run photos-shrink --limit 1 --keep-originals` and review the generated CSV before approving. If the selected candidate is a video, decline the prompt for this photo-only pilot. The limit applies to eligible replacements; finding the largest candidates requires scanning the library in full.

Summary: Standards 3 findings addressed (most consequential: duplicated finalization); Spec 11 findings addressed (most consequential: ownership eligibility and silent loss of extra video tracks).
