# Google Photos Shrink

A Python command that inventories Google Photos, writes a CSV for review, and replaces eligible items one at a time with smaller copies. Photos use **1500 pixels on the shorter side**, AVIF quality 60, without upscaling. Videos fit within 1920×1080 (or 1080×1920 for portrait) and use HEVC in MP4.

This uses the unofficial [Google Photos Python web client](https://github.com/xob0t/google_photos_web_client), pinned to a reviewed revision, for library operations. The official Google Photos API cannot read your pre-existing library or replace/delete its files.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), [FFmpeg and ffprobe](https://ffmpeg.org/download.html), and Google Chrome. The app requires Python 3.12 or newer; uv can provision Python and installs the locked dependencies on first run. FFmpeg must include the `libx265` and `aac` encoders. Both FFmpeg executables must be on PATH, or set their absolute paths in `shrink.toml`.

This working copy already has portable FFmpeg configured under `.cache/tools/`. That ignored directory is not shipped with the repository; a fresh clone needs its own FFmpeg installation and updated `[tools]` paths.

Clone the repository and enter it:

```powershell
git clone https://github.com/johnml1135/google-photos-shrink.git
cd google-photos-shrink
```

Edit `[tools]` in `shrink.toml` for your installation. If FFmpeg is on PATH, use:

```toml
[tools]
ffmpeg = "ffmpeg"
ffprobe = "ffprobe"
```

Set `[google].account_index` to the account number in your Google Photos URL (`/u/0/`, `/u/1/`, etc.). The checked-in working configuration uses account 1; change it for your own session. Export cookies as described below, then check the installation and run a small retained-original pilot:

```powershell
uv run --locked photos-shrink --doctor
uv run --locked photos-shrink --limit 1 --keep-originals
```

`--doctor` checks local encoders and AVIF support. The normal run validates the Google session; the encoder check alone does not prove the cookies are valid.

Google blocks automated browser sign-in. In your normal signed-in Chrome profile, use a trusted local cookie-export tool to export the Google Photos cookies in Netscape `cookies.txt` format to `.photos-shrink/cookies.txt` (the configured `google.cookies_file`). Keep that file private: it grants account access and is ignored by Git. When it expires, export a fresh file. The program uses a dedicated browser profile only to upload and checks that its account identity matches the exported session. Normal runs preserve the exported cookie file. Do not paste cookies into issues, logs, or chat.

Uploads run in background Chrome by default (`[google].browser_headless = true`); library reads and metadata operations use HTTP requests. Set `browser_headless = false` to inspect the upload window when debugging. Sign in manually in your normal Chrome profile before exporting cookies. The upload flow opens **Create and add photos**, then **Import photos from your computer**.

## Workflow

| Command option | Behavior |
| --- | --- |
| `--plan-only` | Write the CSV; local downloads and estimates are allowed, but no remote changes. |
| `--keep-originals` | Upload and verify replacements while retaining originals. |
| No mode option | Ask for approval, then upload, verify, and trash each eligible original. |
| `--limit 10` | Limit the batch to ten eligible media items, including pending operations. |
| `--yes` | Apply without the terminal approval prompt. |
| `--config FILE` | Load another TOML configuration. |
| `--report FILE.csv` | Choose a protected, atomically written CSV report location. |

```powershell
uv run photos-shrink --plan-only
uv run photos-shrink
```

Planning reads your library and may download and encode local files to estimate savings. It does not upload, change metadata, or trash anything. The normal command writes the CSV and pauses for approval before remote changes. Review the proposed old/new filenames, formats, dimensions, sizes, estimates, and skip reasons. Actual encoded savings are checked again before uploading.

Use `--config path/to/shrink.toml` for another configuration. `--yes` explicitly approves applying the generated plan without the terminal prompt. Keep this off until you have reviewed a small pilot.

`--report path/to/report.csv` changes the report location. Use a `.csv` filename outside protected authentication, journal, and media paths. Inside the work directory, reports must be directly in its root. The previous CSV is replaced only after the new report is fully written.

`--limit 3` selects at most three items that meet the savings threshold, including pending replacements. The library is scanned in full so exclusions and previous replacements cannot hide later candidates; this can take time on a large library. Candidates are considered largest first. Originals and output files remain on disk, so allow enough local free space for the selected batch.

Use `uv run photos-shrink --limit 1 --keep-originals` for a pilot that uploads and verifies a replacement while retaining its original. The journal remembers the uploaded copy; repeating with `--keep-originals` verifies it again without trashing. A later normal run can finish that pending replacement after verification.

The replacement sequence retains a local original, encodes and verifies a smaller file, uploads it, restores supported metadata, verifies the remote replacement, and only then moves the original to Google Photos trash. It never empties trash. A SQLite journal and content hashes allow interrupted runs to resume and recognize their own replacements. Preserve the work directory and backups between runs.

If an interrupted upload cannot be found by its exact content hash, the operation stays pending and stops rather than uploading again. Inspect Google Photos before resolving that case; do not delete the journal to force a retry. Changes to encoding settings also hold pending operations until the original settings are restored.

## Configuration

Edit **`shrink.toml`**. Paths are relative to that file. The default is one worker with a pause between items. Date exclusions use capture dates in the configured timezone, include both endpoints, and accept multiple ranges. Name exclusions use case-insensitive glob patterns; any exclusion match skips the item.

Examples for `[exclude]`:

```toml
timezone = "America/New_York"
date_ranges = [
  { start = "2020-06-01", end = "2020-06-30" },
  { start = "2023-12-20", end = "2024-01-05" },
]
name_globs = ["*wedding*", "KEEP_*", "*original*"]
```

A 4000×3000 photo becomes 2000×1500. A 12000×3000 panorama becomes 6000×1500. A 1600×1200 photo is not enlarged, though compression may still reduce its size. Video frame rates are preserved unless a cap is configured.

The working configuration requires at least 20% file savings, pauses 10 seconds between items, and uses two encoder threads. It enables restoration into owned shared albums; set `skip_shared = true` to exclude shared-album items. The batch limit applies to both photos and videos.

## Files and recovery

The default work directory, `.photos-shrink/`, holds the private cookie export, upload browser profile, original backups, encoded outputs, SQLite journal, and `photos-shrink.csv`. Keep it between runs: the journal identifies replacements by remote identity and content hash, and the backups support recovery. The process lock prevents two runs from changing the same journal concurrently.

On a session error, export fresh cookies from normal Chrome and rerun the same command. Cookie expiry dates do not guarantee that Google will continue accepting a session. If the upload outcome is ambiguous, the app stops until it can reconcile the exact content hash; replacing cookies does not bypass that safeguard. Pressing Ctrl+C stops a foreground run, and the next run uses the saved journal to resume.

Cookies, browser state, local media, generated reports, dependency caches, and test scratch directories are ignored by Git. Share source code and redacted diagnostics, not the work directory.

## What replacement preserves

Replacement creates a **new Google Photos item**. Supported capture timestamps, descriptions, favorites, archive state, and album membership are restored and checked. With `skip_shared = false`, owned photos can be added back to existing shared albums. Photos owned by others remain excluded. Existing item links, comments, likes, edit history, manual face labels, album cover choices, and custom album ordering are not guaranteed to transfer. HDR, motion photos, animations, RAW, and unusual media need explicit support rather than silently discarding their features.

For photos, the encoder stamps Google Photos latitude and longitude into the replacement's EXIF GPS fields, including when those coordinates were absent from the downloaded original. After upload, the tool checks that Google reads the same location (within 0.0000001 degrees). Videos retain embedded metadata; a Google-only video location is not currently stamped. Missing or changed coordinates prevent trash. Google-derived place names and location provenance may differ. A failed metadata operation or verification retains the original; the CSV records the failure. The undocumented interface still requires a successful live pilot before broad use.

Google Photos can recompress browser uploads when Storage saver is selected. The tool must verify that the remote copy matches its expectations before trashing an original. If it cannot, it stops that replacement and retains the original. It does not silently change your account-wide backup-quality setting.

File savings are not necessarily Google storage savings. Some existing items consume no quota; reuploading them can increase quota usage. Known non-quota items are skipped by default. The CSV labels its savings as file bytes and includes original quota consumption when known; quota savings remain unknown. Estimated file sizes and actual encoded sizes are reported separately.

## Development

```powershell
uv run pytest
uv run ruff check .
```

Automated tests use fake Google responses and generated local media. They do not sign in, upload personal files, or delete Google Photos items. An authenticated pilot is necessary to validate the current undocumented Google interface with your account.
