# Google Photos Shrink

Shrinks a Google Photos library by re-encoding it from a [Google Takeout](https://takeout.google.com/)
export and replacing each original with a smaller copy. Photos become AVIF at
**1500 pixels on the shorter side**, quality 60, never upscaled. Videos fit within
1920x1080 (or 1080x1920 for portrait) as HEVC in MP4.

On a 10,184-item library that is 25.2 GB down to 2.3 GB.

Three different credentials do three different jobs, because no single one can do
all of them:

| Phase | Needs | Why |
| --- | --- | --- |
| Read the library | nothing | Takeout sidecars carry each item's media key |
| Encode | nothing | entirely offline, resumable, hours for a full library |
| Upload replacements | OAuth | the official API uploads at original quality |
| Restore metadata, trash originals | browser cookies | the official API can do neither |

The official Google Photos API cannot read your pre-existing library, cannot
delete anything, and cannot replace an item's bytes -- `mediaItems.patch` accepts
only `description`. So the last phase runs on an exported browser session through
the unofficial [Google Photos web client](https://github.com/xob0t/google_photos_web_client),
pinned to a reviewed revision. That session expires in about fifteen minutes,
which is why everything that can be done without it is done first.

**Nothing is deleted until its replacement has been proven.** An original is
trashed only after its own bytes resolve in the library to the media key its
sidecar claimed, the replacement resolves by its own content hash to a distinct
item, and metadata restoration has been verified. Dry run is the default. Every
original also stays in the Takeout export on disk, so even a mistake is
recoverable by re-upload.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/),
[FFmpeg and ffprobe](https://ffmpeg.org/download.html), and Google Chrome.
Requires Python 3.12 or newer; uv provisions Python and the locked dependencies on
first run. FFmpeg must include the `libx265` and `aac` encoders, and both
executables must be on PATH or given absolute paths in `shrink.toml`.

## Configuration

Edit **`shrink.toml`**. Paths are relative to that file. Date exclusions use
capture dates in the configured timezone, include both endpoints, and accept
multiple ranges. Name exclusions are case-insensitive globs. Any exclusion match
skips the item, on every route.

```toml
[exclude]
timezone = "America/New_York"
date_ranges = [
  { start = "2020-06-01", end = "2020-06-30" },
  { start = "2023-12-20", end = "2024-01-05" },
]
name_globs = ["*wedding*", "KEEP_*", "*original*"]
```

Scaling is proportional and never upscales: a 4000x3000 photo becomes 2000x1500,
a 12000x3000 panorama becomes 6000x1500, and a 1600x1200 photo is left at its
dimensions though compression may still shrink it. The shipped configuration
requires at least 20% file savings and skips items in shared albums.

`photos_shrink.policy` is the single gate. Every route -- mirror, encode, upload,
replace -- asks it the same question about the same candidate type, so an item the
configuration says to leave alone is left alone everywhere. A value it cannot yet
know is never a refusal: a Takeout export genuinely cannot see live quota or album
sharing, so the encoder reports those as unknown and the replace step asks again
against the live item.

**File savings are not necessarily Google storage savings.** Some existing items
consume no quota, and re-uploading them increases usage. Those are refused by
default, at replace time, when the live library can actually answer.

## What replacement preserves

Replacement creates a **new Google Photos item**. Capture timestamps,
descriptions, favorites, archive state, album membership, and (for photos) GPS
coordinates are restored and verified before any original is trashed. Item links,
comments, likes, edit history, face labels, album cover choices, and custom album
ordering do not transfer.

## Files and recovery

The work directory holds the private cookie export, the API token and client
credentials, the upload browser profile, the mirror and encode CSVs, and the
upload journal. Keep it between runs: the journal identifies replacements by
remote identity and content hash, and it is what makes every phase resumable.

On a session error, export fresh cookies from normal Chrome and rerun. Cookie
expiry dates do not guarantee Google will keep accepting a session.
`[google].session_refresh_seconds = 300` enables best-effort refresh between
operations; set it to `0` to disable. Cookies, browser state, local media,
generated reports and caches are all gitignored.

## How it works

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

## Verified API behaviour

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

## Checking an export

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

## Taking stock of the export

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

## Encoding

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

## Uploading over the official API

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

### One-time setup

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

## Replacing the originals

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

## The whole sequence

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

## What this route still cannot preserve

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
