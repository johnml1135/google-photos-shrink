# Session refresh and bounded photo pilot

User-approved scope: refresh authentication automatically and continue the ten-photo retained-original pilot using the updated cookie export.

## Session maintenance

Use the existing dedicated background Chrome profile. Between operations, when the configured interval has elapsed, reload Google Photos, verify the expected account and Photos origin, and transfer Google's current cookies into the HTTP client. Reload the HTTP client's page-derived request tokens and verify its account before accepting the refreshed session. Preserve the manually exported cookie file. Default interval: 300 seconds; zero disables automatic refresh.

Refresh is best effort during active runs, not a guarantee of indefinite authentication or an always-on service. If Google requires a new login, stop with a clear error. Do not overwrite a known session with a failed or mismatched refresh. Do not reload the upload page while an upload may still be in progress.

Known read-only RPCs may refresh and retry once after failure. Mutating requests must not be replayed automatically. Diagnostics identify the failed operation without logging tokens, cookies, or response bodies.

Live validation exposed an expired export after a successful refresh while the saved Chrome profile remained authenticated. Startup therefore falls back to that existing profile without reseeding stale cookies. An ephemeral cookie file bridges the file-only gpwc constructor and is removed afterward; the manual export remains untouched. Both browser and HTTP identities must agree before journal account binding.

Live uploads also exposed delayed original-byte availability: Google initially served a JPEG for an uploaded AVIF, then returned the exact AVIF later. Poll the same resolved replacement within the upload deadline until its downloaded SHA-256 matches. Keep refresh suppressed throughout that wait and preserve ambiguous upload intent on timeout.

## Ten-photo pilot

Add `--photos-only` and `--newest-first`. Keep the default largest-first full scan unchanged. The explicit newest-first mode streams candidates and stops after the configured number of savings-qualified plans, including eligible pending operations. Skipped items and unsuccessful estimates do not consume the limit. All planning and CSV writing precede uploads. The pilot uses `--limit 10 --photos-only --newest-first --keep-originals --yes`.

Register account-bound journaled replacement identities with the remote adapter so a resumed replacement does not require a full inventory scan to establish provenance. Explicit foreign/shared ownership still blocks the operation; remote identity, byte hash, metadata, and local backup verification remain mandatory.

## Verification

Luna agents implement the two bounded areas; the primary reviews and integrates them. Test refresh timing, cookie/token propagation, identity mismatch rollback, preservation of the manual export, read retry limits, no mutation replay, upload refresh suppression, photo-only pending operations, streaming limits, and default largest-first behavior. Run the full suite and encoder doctor, exercise a live refresh with the updated cookies, then run the retained-original pilot and inspect its CSV and journal.

## Live validation outcome

134 tests pass; Ruff and encoder doctor pass. Background refresh and startup recovery from the saved Chrome profile both authenticated successfully without modifying the manual cookie export. Five pilot replacements passed a fresh read-only audit of exact bytes and supported metadata. Every original remains retained.

The sixth submitted replacement was converted by Google's selected Storage saver mode into JPEG, with both reported and downloaded size differing from the uploaded AVIF. Its durable upload intent remains available for controlled recovery; four further plans are unsubmitted. The ten-photo pilot is not complete.

The user subsequently authorized switching Storage saver off and making this automatic and configurable. `google.auto_original_quality = true` selects and verifies Original quality in the website settings before file submission. False leaves the preference alone. Account and route validation precede changes; failure prevents submission. Planning alone does not change the preference. The selected preference persists. Recover only the known rejected pilot copy after verifying original/local hashes and backing up its journal; retain the camera original and all verified replacements.

The quality-preflight implementation passes 140 tests and Ruff. The user’s existing Chrome session was used to select Original quality and confirm it remained selected after reloading. The script's cookie export and saved browser profile had both become unauthenticated before the recovery attempt, so no recovery trash or additional uploads occurred. A fresh manual cookie export is required to resume the five remaining pilot photos.
