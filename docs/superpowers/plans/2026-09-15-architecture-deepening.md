# Architecture deepening — the three Strong findings

Source: architecture review of `main @ c4b9c90`, 15 September 2026.
Vocabulary: module, interface, implementation, depth, seam, adapter, leverage, locality.

## Constraint that binds all three

A whole-library encode is in flight (2,020 of 10,184 items, ~4h remaining) writing
`G:/takeout-work/encoded.csv`, and 500 uploaded items are awaiting replacement in
`takeout-upload-journal.json`. **The column set of `encoded.csv` and the key set of the
journal must not change.** Fields may be added; nothing may be renamed or removed.

---

## 01 — Collapse the replacement gate into one module

**Problem.** Two modules each own a function called `skip_reason`, with reversed parameter
order, different input shapes and different refusals:

- `pipeline.py:36` `skip_reason(settings, item: dict)` — exclusions, `photos_only`,
  `shared_album`, `non_space_consuming`.
- `takeout_encode.py:46` `skip_reason(entry: MirrorEntry, settings)` — timestamp,
  media key, exclusions.

The encoder's docstring claims "the configured date and name exclusions apply here exactly
as they do to the main pipeline"; `photos_only`, `shared_album` and `non_space_consuming`
are absent. 500 items cleared the encode gate and meet `non_space_consuming` for the first
time at replace, after quota was already spent.

**Deepening.** One module, `photos_shrink/policy.py`, answering one question over one
candidate type.

```python
@dataclass(frozen=True)
class Candidate:
    media_key: str | None
    filename: str
    kind: str                       # "photo" | "video" | "unknown"
    timestamp_ms: int | None
    space_taken_bytes: int | None   # None when not yet known
    shared_album: bool
    saved_percent: float | None = None   # known only after encoding

    @classmethod
    def from_library_item(cls, item: dict) -> Candidate: ...
    @classmethod
    def from_mirror_entry(cls, entry: MirrorEntry) -> Candidate: ...

def verdict(settings: Settings, candidate: Candidate) -> str | None:
    """Return why this photo must not be replaced, or None when it may be."""
```

Refusal tokens, canonical and unchanged from the pipeline's existing spelling:
`no_timestamp`, `no_media_key`, `excluded_by_date`/`excluded_by_name` (whatever
`Settings.exclusion_reason` already returns), `photos_only`, `shared_album`,
`non_space_consuming`, `insufficient_savings`.

A refusal that cannot yet be evaluated is not a refusal: `space_taken_bytes is None`
means unknown, not zero, and `saved_percent is None` means not yet encoded.

**Requirements.**

1. `pipeline.skip_reason(settings, item)` keeps its name, signature and exact return
   strings, and delegates to `verdict`.
2. `takeout_encode.skip_reason` is deleted; the encoder calls `verdict`.
3. The encoder's savings floor check moves into `verdict` via `saved_percent`.
4. Existing tests asserting `"non_space_consuming"` and `"shared_album"` still pass
   unchanged.

**Wins.** locality: one module decides what is touched. leverage: one interface, four call
sites. Refusals land before quota is spent.

---

## 02 — Give the run's ledger one module

**Problem.** The Takeout route's state lives in an `encoded.csv` and a
`takeout-upload-journal.json` whose schemas exist only as string literals at their write
sites, read back by hand in three tools. `StateStore` (`state.py`, 159 lines) already
implements this stage machine with a file lock, an account check and atomic commits, and
no tool imports it. Along the way the duplication is: config + work_dir resolution in 5
spellings, the progress prefix in 6 copies, the ffprobe lookup in 3, journal read/write
byte-identical in 2, remote setup/teardown in 2.

**Deferred, and why.** Moving the backing store to `StateStore` requires an account id and
takes a file lock, and would invalidate an in-flight encode and 500 pending replacements.
The backing store is therefore **not** changed in this pass. What changes is that the
schema stops being string literals and becomes one module's interface, so the store behind
it can be swapped in one place once the run is idle.

**Deepening.** `photos_shrink/ledger.py` owns the schema and the I/O:

```python
@dataclass
class UploadRecord:
    source: Path
    output: Path
    media_key: str
    output_sha256: str
    media_item_id: str
    verified: str            # "ok" | "mismatch" | "unverified"
    ...
    replaced: str | None

class UploadJournal:
    @classmethod
    def load(cls, path: Path) -> UploadJournal
    def record(self, record: UploadRecord) -> None      # writes atomically
    def pending_replacement(self) -> list[UploadRecord]
    def already_uploaded_keys(self) -> set[str]
```

**Requirements.**

1. The on-disk JSON is byte-compatible with the existing journal: same keys, same values,
   unknown keys preserved on round-trip.
2. Writes are atomic (temp file + replace), as `takeout_encode.write_report` already does
   and the journal currently does not.
3. `already_uploaded_keys` replaces the inline `done_keys` comprehension.
4. `pending_replacement` replaces the inline `verified == "ok" and not replaced` filter.
5. The `encoded.csv` column set is unchanged.

**Wins.** locality: the schema changes in one module. leverage: one interface, three tools.
Two concurrent runs stop clobbering the journal.

---

## 03 — Lift the replacement out of `main()`

**Problem.** `tools/takeout_replace.py` is the only code that trashes a photo. Its safety
property is the *order* of ten refusal points, and that order sits in a `main()` no test
can reach — `tests/test_takeout_replace.py:17` has to hand-load the script by path because
it is not a module. Three pure functions are tested; the order is not. `main():152` proves
identity and discards the proof on the next line:

```python
original = confirm_original(remote, source, media_key)
original = remote.get_item(media_key)
```

**Deepening.** `photos_shrink/replacement.py`:

```python
@dataclass(frozen=True)
class Outcome:
    status: str      # "replaced" | "kept" | "refused" | "would_replace"
    detail: str
    original_media_key: str | None = None

def replace_one(library, job, *, settings, ffprobe,
                apply: bool, keep_originals: bool) -> Outcome:
    """Restore metadata onto the replacement and trash the original.

    `library` is any adapter offering find_uploaded, get_item, restore_metadata,
    verify_replacement, trash and is_trashed — the cookie session in production,
    a fake in tests.
    """
```

The ten steps move inside, in this order, each a refusal point:

1. the job names a media key
2. `confirm_original` — hash the exported original, require it to resolve to that key
3. `get_item` — fetch the full library item, **carrying the proof forward**
4. `verdict` — the gate from 01
5. `find_uploaded(output)` — the replacement must resolve by its own content hash
6. `check_identity` — distinct id and distinct dedup key
7. `output_info_for` — hash and path the verification requires
8. `restore_metadata`
9. `verify_replacement`
10. `trash` + `is_trashed` confirmation

**Requirements.**

1. `main()` becomes argparse, a loop over `replace_one`, and reporting. No safety logic.
2. `tests/test_takeout_replace.py` imports `photos_shrink.replacement` normally; the
   `importlib.util.spec_from_file_location` hack is deleted.
3. Tests assert the *order*, not only the parts: `trash` is never called when verification
   fails, `restore_metadata` is never called when the gate refuses, and nothing is called
   at all when `apply` is false.
4. `takeout_replace.check_identity` and `Pipeline._check_identity` (verbatim duplicates
   with different exception types) collapse to one.
5. Dry run remains the default.

**Wins.** The interface becomes the test surface. A fake adapter replaces live cookies.
Every refusal path provable offline.
