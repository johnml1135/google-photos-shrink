# Google Photos Shrink

A Python command that inventories Google Photos, writes a CSV for review, and replaces eligible items one at a time with smaller copies. Photos use **1500 pixels on the shorter side**, AVIF quality 60, without upscaling. Videos fit within 1920×1080 (or 1080×1920 for portrait) and use HEVC in MP4.

This uses the unofficial [Google Photos Python web client](https://github.com/xob0t/google_photos_web_client), pinned to a reviewed revision, for library operations. The official Google Photos API cannot read your pre-existing library, cannot delete anything, and cannot replace an item's bytes — `mediaItems.patch` accepts only `description`. It *can* upload new items via the `photoslibrary.appendonly` scope. See [Working from a Google Takeout export](#working-from-a-google-takeout-export).

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
| `--photos-only` | Exclude videos, including pending video replacements. |
| `--newest-first` | Stream newest candidates and stop once the batch has enough eligible plans. |
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

For a ten-photo pilot without a full-library scan:

```powershell
uv run --locked photos-shrink --photos-only --newest-first --limit 10 --keep-originals
```

Newest-first selection continues past exclusions, unsupported files, and insufficient savings until it has enough eligible plans or reaches the end of the library. Existing eligible pending replacements count toward the limit. Planning and the CSV still come before any upload. Configure these choices permanently with `[run].photos_only` and `[run].selection_order` (`"largest"` or `"newest"`).

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

The default work directory, `.photos-shrink/`, holds the private cookie export, the API token and client credentials, the upload browser profile, the SQLite journal, and `photos-shrink.csv`. Per-item originals and encoded outputs live one level down in `items/`, so the things a human has to find and replace by hand stay visible in the root rather than buried among hash-named directories. Keep the directory between runs: the journal identifies replacements by remote identity and content hash, and the backups support recovery. The process lock prevents two runs from changing the same journal concurrently.

On a session error, export fresh cookies from normal Chrome and rerun the same command. Cookie expiry dates do not guarantee that Google will continue accepting a session. If the upload outcome is ambiguous, the app stops until it can reconcile the exact content hash; replacing cookies does not bypass that safeguard. Pressing Ctrl+C stops a foreground run, and the next run uses the saved journal to resume.

During active runs, `[google].session_refresh_seconds = 300` enables best-effort refresh between operations. The app reloads its dedicated background Chrome session, checks the account, updates the HTTP cookies, and reloads Google's request tokens. A failed read can trigger one refresh-and-retry. Upload and deletion requests are never automatically replayed, and refresh does not reload the page while an upload may be in progress. Set the interval to `0` to disable automatic refresh. This is not an always-on keepalive service: Google can still require a fresh manual login, and the exported cookie file is preserved.

Google may briefly serve a JPEG while a newly uploaded AVIF is still processing. The app waits for the exact uploaded bytes before accepting the replacement. Configure that wait with `[google].upload_timeout_seconds` (default `300`) and `upload_poll_seconds` (default `5`). If it times out, the journal preserves the upload intent; rerunning reconciles the existing copy by its content hash.

With automatic refresh enabled, startup can recover from an expired cookie export using the app's saved Chrome profile. That recovery preserves the export and verifies the browser and HTTP account identities before proceeding. If both sessions have expired, a new manual cookie export is still required.

Cookies, browser state, local media, generated reports, dependency caches, and test scratch directories are ignored by Git. Share source code and redacted diagnostics, not the work directory.

## What replacement preserves

Replacement creates a **new Google Photos item**. Supported capture timestamps, descriptions, favorites, archive state, and album membership are restored and checked. With `skip_shared = false`, owned photos can be added back to existing shared albums. Photos owned by others remain excluded. Existing item links, comments, likes, edit history, manual face labels, album cover choices, and custom album ordering are not guaranteed to transfer. HDR, motion photos, animations, RAW, and unusual media need explicit support rather than silently discarding their features.

For photos, the encoder stamps Google Photos latitude and longitude into the replacement's EXIF GPS fields, including when those coordinates were absent from the downloaded original. After upload, the tool checks that Google reads the same location (within 0.0000001 degrees). Videos retain embedded metadata; a Google-only video location is not currently stamped. Missing or changed coordinates prevent trash. Google-derived place names and location provenance may differ. A failed metadata operation or verification retains the original; the CSV records the failure. The undocumented interface still requires a successful live pilot before broad use.

Google Photos can recompress browser uploads when Storage saver is selected. By default, `[google].auto_original_quality = true` selects and verifies Original quality in the website settings before each upload, preserving the files this app has already compressed. It leaves Original quality selected. Set this option to `false` to leave Google's setting alone. This preference does not change encoding settings or retroactively restore previously recompressed uploads. The tool still verifies replacement bytes and metadata before trashing any original.

File savings are not necessarily Google storage savings. Some existing items consume no quota; reuploading them can increase quota usage. Known non-quota items are skipped by default. The CSV labels its savings as file bytes and includes original quota consumption when known; quota savings remain unknown. Estimated file sizes and actual encoded sizes are reported separately.

## Working from a Google Takeout export

Replacing items one at a time through a single browser session makes every batch
depend on that session surviving download, encoding, upload, and verification for
each item. A [Google Takeout](https://takeout.google.com/) export removes the two
longest phases from that dependency: the bulk download happens once through a
supported channel, and encoding then runs entirely offline.

`photos_shrink.takeout` reads an extracted export. Google separates each file
from its metadata and the sidecar naming is lossy — `.supplemental-metadata.json`
is truncated to an arbitrary prefix (`.supple.json`, sometimes `.s.json`),
duplicate counters migrate across the extension (`IMG_0001(1).jpg` pairs with
`IMG_0001.jpg(1).json`), and `-edited` suffixes are clipped to `-edi` or shorter.
Every de-truncation is self-validating: a candidate is accepted only when it
resolves to a sidecar that exists, and an ambiguous prefix resolves to nothing.
A wrong guess yields "no sidecar" rather than another item's timestamps.

**Files with no resolvable timestamp must be quarantined, not uploaded.** Without
one, Google dates the upload to the day it arrives and the timeline is scrambled.

### Verified API behaviour

Confirmed against Google's documentation on 2026-09-14:

| Question | Answer |
| --- | --- |
| Update an existing item's bytes? | No. `mediaItems.patch` accepts only `description` in `updateMask`. |
| Delete library items? | No. The API has no delete method. |
| Upload new items? | Yes, via `photoslibrary.appendonly`: raw bytes to `/v1/uploads` for a token, then `mediaItems.batchCreate`. |
| Read the existing library? | No. Only items the app itself created. |
| Is AVIF accepted? | Yes: `AVIF, BMP, GIF, HEIC, ICO, JPG, PNG, TIFF, WEBP, some RAW`. |
| Does upload honour Storage saver? | No — uploads are "stored in full resolution at original quality". |

Limits: photos 200 MB, videos 20 GB, upload tokens valid 24 hours.

There is therefore no in-place update by any route. Replacing an item is always
upload-new followed by delete-old, and deletion stays on the browser adapter.

### Checking an export

Takeout files are joined to library items by SHA-256 of their bytes. That join is
unproven until measured against your own export, so check it before trusting it:

```powershell
uv run python tools/takeout_probe.py "D:/path/to/Takeout" --sample 25
```

The probe reports export inventory (file counts, size by kind, sidecar coverage,
albums) and then hash-matches a stratified sample against your library. It
uploads nothing, trashes nothing, and modifies nothing. Add `--offline` to skip
Google entirely and see the inventory alone.

A high match rate means the hash join is sound and deletion can be targeted
precisely. A low rate means Takeout is rewriting bytes, and a weaker join such as
filename and timestamp is not sufficient grounds to delete anything.

### Taking stock of the export

```powershell
uv run python tools/takeout_mirror.py "D:/path/to/Takeout"
```

Writes one row per **library item** to `mirror.csv` — media key, capture time,
albums, location, size and path — plus a summary of what can be replaced safely
and what cannot.

One row per item, not per file: Takeout exports a photo once for every album it
belongs to *and* again under its date bucket, so counting files overstates a
library. Everything downstream selects from this mirror, which is what stops the
same photo being encoded and uploaded several times.

### Encoding

```powershell
uv run python tools/takeout_encode.py "D:/path/to/Takeout"            # everything
uv run python tools/takeout_encode.py "D:/path/to/Takeout" --limit 500 # a batch
uv run python tools/takeout_encode.py "D:/path/to/Takeout" --kinds video
```

Encodes largest first, verifies each output, and writes `encoded.csv` for the
uploader. Entirely offline — it contacts nothing, uploads nothing, deletes
nothing — so it is safe to run against a partially copied export while the rest
is still downloading.

**A full run is resumable.** Output names are deterministic, so an item already
encoded is reused instead of re-encoded, and the CSV is flushed as it goes.
Re-running after an interruption, a crash, or a reboot is always safe and skips
the work already done. Photos are encoded before videos, so most of the savings
land early even if a long video pass is cut short.

Items are refused rather than silently mangled when they have no capture time
(they would be dated "today" on upload), no media key (the original could never
be identified again), or more than one frame — MPO files and motion photos.
Google's own Storage saver leaves MPF JPEGs uncompressed too.

Use `--work` to keep outputs off the system drive; a large library's encodes
still run to gigabytes even at ~90% savings.

### Uploading over the official API

Exported cookies expire quickly and without warning, which makes them a poor
foundation for long runs. The official API authenticates with a stored OAuth
refresh token instead, so it survives across runs. Everything the API is
permitted to do therefore needs no cookies at all:

| Step | Authentication |
| --- | --- |
| Download originals | Google Takeout — no credentials in this app at all |
| Encode | none; entirely local |
| Upload replacements | **OAuth** (`photoslibrary.appendonly`) |
| Verify the uploaded item | **OAuth** (`photoslibrary.readonly.appcreateddata`) |
| Group replacements into an album | **OAuth** (albums the app creates) |
| **Delete the originals** | **Exported cookies.** The API has no delete method. |

Deletion is the only step that still requires a browser session, and it is the
cheapest one: no downloads, no encoding, no uploads, so a batch of deletions
finishes in a short burst rather than a run lasting hours.

#### One-time setup

Google has no API key for your own account: anything reaching personal photos
needs OAuth consent, so the credentials have to be created once in the Google
Cloud Console. A wizard walks through every click and checks the result:

```bash
bash tools/setup_google_api.sh
```

It creates the project, enables the Photos Library API, publishes the consent
screen, collects the OAuth client id and secret into
`.photos-shrink/api-client.env`, runs the consent flow, and verifies the stored
token before finishing. The project is an empty container — no billing, no code,
no app review — and you never need to open it again.

To do it by hand instead: create a project, enable the **Photos Library API**,
publish the OAuth consent screen, create an OAuth client of type **Desktop app**,
then:

```powershell
$env:PHOTOS_API_CLIENT_ID     = "....apps.googleusercontent.com"
$env:PHOTOS_API_CLIENT_SECRET = "...."
uv run python tools/api_setup.py
```

Either route stores a refresh token at `.photos-shrink/api-token.json`. Keep it
private — it grants upload access to your account, and like the cookie file it
is ignored by Git. Verify it later with
`uv run python tools/api_setup.py --check`.

**Publish the consent screen to "In production."** While it is left in
"Testing", Google expires refresh tokens after seven days and the client will
report `invalid_grant`. Publishing removes that limit; an unverified personal
app still works, behind a warning screen you accept once.

Uploads through this path are stored at original quality and are not subject to
the Storage saver transcoding that affects browser uploads.

### Replacing the originals

```powershell
uv run python tools/takeout_replace.py --journal G:/takeout-work/journal.json   # dry run
uv run python tools/takeout_replace.py --journal G:/takeout-work/journal.json --apply
```

Restores the original's capture time, description, favourite and archive state,
location and album membership onto the replacement, verifies it, and only then
moves the original to Google Photos trash. This is the sequence the main
pipeline uses, in the same order, and it is the closest thing to replacement
that exists — no API or web client can swap an item's bytes in place.

This step needs the browser session, because the API can neither write album
membership nor delete. It is nonetheless short: the mirror already supplies each
media key, so no library scan is needed — just a few small requests per photo
with no file transfer. That is what keeps the phase inside one session.

**Dry run is the default.** `--apply` is required before anything changes, and
`--keep-originals` restores and verifies without ever trashing. Nothing is
trashed whose replacement was not found by content hash and matched against the
original's identity. Every original also remains in the Takeout export on disk,
so a mistake is recoverable by re-uploading.

### The whole sequence

Once set up, a library run is five commands:

```powershell
# 1. Inventory the export (offline)
uv run python tools/takeout_mirror.py "D:/path/to/Takeout"

# 2. Encode it (offline, resumable, hours for a large library)
uv run python tools/takeout_encode.py "D:/path/to/Takeout"

# 3. Upload the replacements (OAuth; no cookies)
uv run python tools/takeout_upload.py --report G:/takeout-work/encoded.csv `
    --journal G:/takeout-work/journal.json --album "photos-shrink batch 1"

# 4. See what replacement would do (changes nothing)
uv run python tools/takeout_replace.py --journal G:/takeout-work/journal.json

# 5. Replace: restore metadata, verify, trash the originals (browser session)
uv run python tools/takeout_replace.py --journal G:/takeout-work/journal.json --apply
```

Steps 2 and 3 are resumable and safe to re-run; both track work by the library's
own media key, so an interrupted run never uploads a photo twice. Step 5 is the
only one that removes anything, and only after each replacement has been
verified individually.

Work in batches rather than all at once. Uploading an entire library before
deleting anything temporarily *increases* storage, since both copies exist until
step 5 runs.

### What this route still cannot preserve

Because replacement always creates a new item, face and people groupings, shared
album comments and likes, and existing item links are lost — no export or API
restores them. Album membership is recoverable from the export's folder structure.
Weigh this against Google's built-in **Recover storage**, which is lossless to all
of that but only compresses to 16 MP for photos and 1080p for video.

## Development

```powershell
uv run pytest
uv run ruff check .
```

Automated tests use fake Google responses and generated local media. They do not sign in, upload personal files, or delete Google Photos items. An authenticated pilot is necessary to validate the current undocumented Google interface with your account.
