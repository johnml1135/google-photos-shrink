# Google Photos Shrink implementation plan

Approved behavior: one Python command, one TOML configuration, CSV before remote changes, multiple date/name exclusions, slow serial compression/replacement, resumable state. Photos use AVIF quality 60, shorter edge at most 1500, no upscaling. Videos use HEVC MP4 CRF 28, 1080 short/1920 long bounds, preserve frame rate by default. Preserve capture timestamps and supported associations; never claim identity/comments/faces survive replacement.

## Integration contract

Package: `photos_shrink` under `src/`. Use ordinary JSON-compatible dictionaries at the remote/pipeline seam, to avoid coupling to upstream parser classes.

Remote item keys: `id`, `dedup_key`, `filename`, `size_bytes`, `width`, `height`, `kind` (`photo`/`video`), `timestamp_ms` (capture UTC), `timezone_offset` (milliseconds, converted to seconds only for the timestamp setter), `duration_seconds` (nullable), `mime_type`, `metadata` (dictionary with `albums` list of {id,title,shared}, `description`, `favorite`, `archived`, `latitude`, `longitude`), `skip_reason` (nullable). Unknown ownership/sharing/motion metadata must not silently become safe. All raw metadata saved to state; downloads must be original bytes.

`media.py`: `probe(path, ffprobe='ffprobe') -> dict` (kind, width,height,size_bytes,format,codec,duration_seconds,has_audio, skip_reason); `target_dimensions(width,height,kind,settings) -> tuple[int,int]`; `encode(source,destination,settings) -> dict` (probe result, output sha256), `estimate(source,settings,work_dir) -> dict` (estimated_bytes,method), `verify(source,output,settings) -> dict`. `settings` dictionary contains `photos`, `videos`, `tools`; keys as example config below. Photos exact encoding for estimates, video samples. Verification fully decodes output, dimensions/duration/audio, no enlargement. Preserve EXIF/ICC where supported, skip unhandled HDR/motion/animation/RAW safely. No shell=True. Never overwrite originals.

`remote.py`: `GooglePhotosRemote(settings)`; methods `login()`, `account_id() -> str` stable identity (not account index), `list_items() -> Iterable[dict]`, `get_item(id) -> dict`, `download(item,destination) -> None`, `find_uploaded(path) -> dict|None` (exact content hash, NOT filename), `upload(path) -> dict` (use browser upload then resolve exact hash), `restore_metadata(original,replacement) -> None`, `verify_replacement(original,replacement,output_info) -> None`, `trash(item) -> None`, `is_trashed(item) -> bool`, `close()`. Failed/malformed upstream responses fail closed. Check remote readiness, metadata, dimensions, size and identity before trash. Skip shared/unsupported associations by default, record reason. No Google account actions during development without user's active login. Use gpwc pinned to 651aa268d50529cb6128c8a42c9cd4ce668f2189. Browser uploads only when actually applying; login via visible dedicated persistent Chromium/Chrome profile, cookie export Netscape format (local secret), imported cookies also supported. Cookie authentication may expire; no secret logging. Verify browser and gpwc same account before upload.

`config.py`, `state.py`, `pipeline.py`, `cli.py`: validated TOML, SQLite journal bound to stable account ID and settings fingerprint, exclusive process lock, report generation, sequential state machine. CLI default `uv run photos-shrink`, subcommands/options login/doctor/plan-only/yes/config. Default run writes whole CSV and asks before uploads/trash; --yes explicitly bypasses. Dry plan may download/encode locally, never remote mutations. Run largest first, skip insufficient savings, retain local originals. Capture original snapshot and hash before upload. Persist upload intent/hash before request; reconcile exact hash on retry and never duplicate blindly. Persist stage before trash, verify both remote replacement and local backup again on resume. Already replaced outputs skipped even if later scanned as input; no filename-only dedup. Changed encoding config does not reset already replaced identity tracking. Never empty trash. CSV distinguish estimated and actual file savings from quota savings; sanitize spreadsheet formulas. Exclude dates inclusive in configured timezone; missing dates skipped. Paths relative to config directory. One worker, configurable pause and low encoder threads, bounded retries, progress printed.

## Settings shape

`[photos] short_edge=1500, format='avif', quality=60`;
`[videos] long_edge=1920, short_edge=1080, codec='hevc', crf=28, preset='slow', max_fps=0, audio_bitrate_kbps=96`;
`[tools] ffmpeg='ffmpeg', ffprobe='ffprobe'`;
`[google] cookies_file='.photos-shrink/cookies.txt', browser_profile='.photos-shrink/browser', browser_channel='chrome', account_index=0`;
`[run] work_dir='.photos-shrink', pause_seconds=10, minimum_savings_percent=20, threads=2, skip_shared=true`;
`[exclude] timezone='America/New_York', date_ranges=[{start='YYYY-MM-DD',end='YYYY-MM-DD'}], name_globs=[]`.

## Tasks and ownership

1. Media Luna: implement media.py and test_media.py with real tiny fixtures/generated encodes, short-side panoramas, no upscaling, audio/duration/rotation verification, sample estimate labeling. First demonstrate failing tests.
2. Remote Luna: inspect/install gpwc, implement remote.py/auth.py and tests using upstream-shaped fixtures; browser login/cookie storage/upload and exact hash resolution. Test malformed response, account mismatch, no trash on uncertainty. Report limitations, no fabricated integration success.
3. Pipeline Luna: implement config/state/pipeline/cli/package metadata/example config and corresponding tests; stub backend integration exercising all crash boundaries. No changes to media/remote. TDD safeguards around deletion and resume.
4. Primary review: inspect all diffs for requirements then correctness; integrate, run tests and CLI, real AVIF/HEVC local smoke tests, dependency/encoder doctor. Fix findings via Luna. Document exact setup, limitations, workflow and test evidence in README. Live Google test requires login and must be distinguished from local verification.

## Evidence checkpoints

- Confirmed: upstream client exposes listing, original URLs, metadata setters, trash and exact hash query; no upload function in payload module.
- Confirmed: installed Python 3.12/3.13/3.14 available outside sandbox; network install requires escalation. gpwc current HEAD pinned above.
- Confirmed on 2026-09-09: an authenticated retained-original photo pilot uploaded and verified a 294,024-byte AVIF from a 5,634,639-byte JPEG (94.78% file reduction), including capture time and GPS. Original retained. Live shared-album restoration, video replacement, and trash remain unverified.
- Confirmed: pinned gpwc installed and imports, required payload constructors present. Pillow 12.3 supports AVIF; generated 2000x1500 noise image encodes and fully decodes. Portable FFmpeg 9.0.1 includes libx265 and AAC.
- Confirmed by experiment: Netscape session-cookie expiry `0` is not sent by requests; blank expiry is sent. Auth implementation must export session cookies with blank expiry and test the actual MozillaCookieJar/requests path.
- Primary review gates: immutable journal-backed recovery for in-flight operations; no re-encoding or blind upload after uncertain upload; require distinct new identity before metadata changes; verify exact remote bytes and metadata before trash; include skipped items and proposed formats/dimensions in pre-approval CSV; enforce configured savings threshold; handle missing/unknown dates and ownership conservatively.
- Integrated Luna implementations after primary review. Installed the package and generated `uv.lock`; `uv run --locked photos-shrink --doctor` passes in this working copy. Automated coverage includes generated AVIF/HEIC/HEVC media, strict simulated remote replacement, upload crash stages, tampered backups/output, cookie expiry semantics, and metadata/identity validation.
- Current limits: Google-only video GPS, HDR, motion photos, and unsupported media are not supported. Photo GPS stamping and owned shared-album restoration are implemented; see the metadata-restoration plan. An unresolved upload hash remains pending without a blind retry. Google blocks automated sign-in, so authentication uses cookies exported manually from normal Chrome; uploads use background Chrome.
- Historical first-version local verification: 46 tests passed, including real FFmpeg video tests; lint and encoder doctor passed. Subsequent review evidence is recorded separately.
