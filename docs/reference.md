# Reference

The long version: what each step checks, what Google's API can and cannot do,
and what was measured rather than assumed. The README is the short version.

## What replacement preserves

Replacement creates a **new Google Photos item**. Capture timestamps,
descriptions, favorites, archive state and album membership are restored and
verified before any original is trashed. Location is whatever the encoded file
carries; it is not checked. Item links,
comments, likes, edit history, face labels, album cover choices, and custom album
ordering do not transfer.

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
uv run photos-shrink probe "D:/path/to/Takeout" --sample 25
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
uv run photos-shrink mirror "D:/path/to/Takeout"
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
uv run photos-shrink encode "D:/path/to/Takeout"            # everything
uv run photos-shrink encode "D:/path/to/Takeout" --limit 500 # a batch
uv run photos-shrink encode "D:/path/to/Takeout" --kinds video
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
uv run photos-shrink api-setup
```

Either route stores a refresh token at `.photos-shrink/api-token.json`. Keep it
private — it grants upload access to your account, and like the cookie file it
is ignored by Git. Verify it later with
`uv run photos-shrink api-setup --check`.

**Publish the consent screen to "In production."** While it is left in
"Testing", Google expires refresh tokens after seven days and the client will
report `invalid_grant`. Publishing removes that limit; an unverified personal
app still works, behind a warning screen you accept once.

Uploads through this path are stored at original quality and are not subject to
the Storage saver transcoding that affects browser uploads.

## Replacing the originals

```powershell
uv run photos-shrink replace   # dry run
uv run photos-shrink replace --apply
```

Restores the original's capture time, description, favourite and archive state
and album membership onto the replacement, re-reads it to confirm, and only then
moves the original to Google Photos trash. It is the closest thing to
replacement that exists — no API or web client can swap an item's bytes in
place.

This step needs the browser session, because the API can neither write album
membership nor delete, and that session lasts about fifteen minutes. So it works
in batches (`--batch`, default 100): each read, fix and trash carries the whole
batch in a handful of requests, rather than a round trip per photo. The mirror
already supplies each media key, so no library scan is needed, and nothing is
downloaded. A photo that fails a check is left alone and the rest of its batch
carries on.

**Dry run is the default.** `--apply` is required before anything changes, and
`--keep-originals` restores and verifies without ever trashing, and leaves the
journal unchanged. Nothing is
trashed whose replacement was not found by content hash and matched against the
original's identity. Every original also remains in the Takeout export on disk,
so a mistake is recoverable by re-uploading.

## Removing extra copies

A photo someone else shared into the library -- through partner sharing or a
shared album -- costs their storage, not yours, and so does an old "High quality"
upload. Replacing one only adds a copy on your storage, so every route refuses
them: the encoder and uploader from the sidecar's `googlePhotosOrigin`, the
replace step from the live quota.

Uploads made before that check existed are cleaned up with:

```powershell
uv run photos-shrink remove-copies   # dry run
uv run photos-shrink remove-copies --apply
```

It asks the replace step's gate of every upload not yet replaced, and for each
refused original trashes only the replacement this tool uploaded -- found by
the content hash of the encoded file, distinct from the original, and confirmed
in the bin. Originals are never touched.
