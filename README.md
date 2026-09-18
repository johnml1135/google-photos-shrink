# Google Photos Shrink

Shrinks a Google Photos library by re-encoding it from a
[Google Takeout](https://takeout.google.com/) export and replacing each original
with a smaller copy. Photos become AVIF at 1500 pixels on the shorter side,
quality 60, never upscaled. Videos become AV1 (SVT-AV1, CRF 36) inside
1920x1080. Measured on one 10,184-item library: 25.2 GB down to 2.3 GB, and its
121 encodable videos from 3.56 GB to 1.08 GB.

Three credentials do three jobs, because no single one can do all of them:

| Phase | Needs | Why |
| --- | --- | --- |
| Read the library | nothing | Takeout sidecars carry each item's media key |
| Encode | nothing | entirely offline, resumable, hours for a full library |
| Upload replacements | OAuth | the official API uploads at original quality |
| Restore metadata, trash originals | browser cookies | the official API can do neither |

The official API cannot read your existing library, cannot delete anything, and
cannot replace an item's bytes. So the last phase runs on an exported browser
session through the unofficial
[Google Photos web client](https://github.com/xob0t/google_photos_web_client),
pinned to a reviewed revision. That session lasts about fifteen minutes, which
is why everything that can be done without it is done first.

**Nothing is deleted until its replacement is proven.** An original is trashed
only after the item its media key names matches the exported file's size, the
replacement resolves by its own content hash to a distinct item, and that
replacement has been re-read carrying the original's capture time and albums.
Dry run is the default everywhere. Every original also stays in the Takeout
export on disk, so a mistake is recoverable by re-upload.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/),
[FFmpeg and ffprobe](https://ffmpeg.org/download.html), and Google Chrome.
Python 3.12 or newer; uv provisions Python and the locked dependencies on first
run. FFmpeg must include `libsvtav1` and `aac` -- gyan's "essentials" build does
not carry SVT-AV1 -- and both executables must be on PATH or named by absolute
path in `shrink.toml`.

Then edit **`shrink.toml`**, which is commented throughout. The settings worth
knowing before a first run:

- `[run] data_dir` -- where the gigabytes live: encoded output, the CSVs and the
  upload journal. `work_dir` keeps the small private things (cookies, API token)
  beside the repo.
- `[photos]` and `[videos]` -- the encoder settings above. Changing them makes
  existing outputs stale.
- `[run] minimum_savings_percent` -- an encode that saves less is not uploaded.
- `[run] skip_shared`, `skip_non_space_consuming` -- what to leave alone; see
  "What it refuses" below.

## The run

Every step is `photos-shrink <step>`, and every step takes `--help`.

```powershell
uv run photos-shrink probe   "D:/path/to/Takeout" --sample 25  # what is in the export
uv run photos-shrink mirror  "D:/path/to/Takeout"              # join it to the live library
uv run photos-shrink encode  "D:/path/to/Takeout"              # offline, resumable, hours
bash tools/setup_google_api.sh                                 # once: OAuth client + token
uv run photos-shrink upload  --album "photos-shrink batch 1"   # official API
uv run photos-shrink replace                                   # dry run: what would happen
uv run photos-shrink replace --apply                           # restore metadata, trash originals
uv run photos-shrink remove-copies --apply                     # trash uploads whose originals stay
```

`replace` and `remove-copies` need the browser session: export `cookies.txt` from
normal Chrome into the work directory first. Both stop and say so when the
session dies, and both resume where they stopped -- an item that failed is never
trashed and never marked, so re-running simply tries it again.

Work in batches on a first run. `--limit` bounds any step, and the upload step
takes about three API requests per item against a quota of 10,000 a day.

## What it refuses

Refusals are the point, not a fault: each one is an item where replacing would
cost you something. Expect a large share of a real library to be refused.

- **Shared into your library by someone else** -- it costs their storage, not
  yours, so a copy would only add to your bill.
- **Already free** (`non_space_consuming`) -- Storage-saver-era items that count
  nothing against your quota. Replacing one spends quota to save none.
- **In a shared album** -- Google refuses to add an item to a shared album, so a
  replacement would silently drop the photo out of an album other people see.
  Set `skip_shared = false` only if you accept that.
- **No resolvable capture time** -- without one Google dates the upload to the
  day it arrives. The encoder carries EXIF through, and `replace` restores the
  original's date onto the replacement before trashing anything.

Anything refused keeps its original, which leaves the uploaded copy as an extra
copy on your storage: `remove-copies` trashes those.

## Files and recovery

The work directory holds the cookie export, the API token and client, the upload
browser profile, the mirror and encode CSVs, and the upload journal. Keep it
between runs: the journal identifies replacements by remote identity and content
hash, and it is what makes every phase resumable.

On a session error, export fresh cookies and re-run. Cookie expiry dates do not
guarantee Google keeps accepting a session. `[google] session_refresh_seconds`
enables best-effort refresh between operations; set it to `0` to disable.
Cookies, browser state, local media, reports and caches are all gitignored.

Space is not freed until the bin is emptied, and Google's storage figures can
take hours to catch up.

## What this route cannot preserve

Replacement always creates a new item, so face and people groupings, shared
album comments and likes, and existing links to items are lost -- no export or
API restores them. Album membership is recoverable from the export's folder
structure. Weigh this against Google's built-in **Recover storage**, which keeps
all of that but only compresses to 16 MP for photos and 1080p for video.

## Development

```powershell
uv run pytest
uv run ruff check .
```

Tests use fake Google responses and generated local media: they never sign in,
upload anything, or delete anything. [docs/reference.md](docs/reference.md) has
the long version -- what each step checks, what the API can and cannot do, and
what was measured rather than assumed.

MIT licensed. See [LICENSE](LICENSE).
