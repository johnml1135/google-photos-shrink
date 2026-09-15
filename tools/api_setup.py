"""One-time OAuth setup for the official Google Photos API.

Run once. Opens Google's consent page, catches the redirect on loopback, and
stores a refresh token under the work directory. After this the uploader
authenticates without any exported browser cookies.

Credentials come from the environment so they never land in the repo:

    $env:PHOTOS_API_CLIENT_ID     = "....apps.googleusercontent.com"
    $env:PHOTOS_API_CLIENT_SECRET = "...."

Pass --check to verify an existing token instead of authorizing again.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from photos_shrink.config import load_config
from photos_shrink.photos_api import PhotosApiClient, PhotosApiError


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorize the Google Photos API")
    parser.add_argument("--config", default="shrink.toml")
    parser.add_argument("--check", action="store_true", help="Verify the stored token only")
    parser.add_argument("--no-browser", action="store_true", help="Print the URL, do not open it")
    args = parser.parse_args()

    client_id = os.environ.get("PHOTOS_API_CLIENT_ID", "")
    client_secret = os.environ.get("PHOTOS_API_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print(
            "Set PHOTOS_API_CLIENT_ID and PHOTOS_API_CLIENT_SECRET first.\n"
            "Create them in the Google Cloud Console as an OAuth client of type\n"
            "'Desktop app', with the Photos Library API enabled. See the README.",
            file=sys.stderr,
        )
        return 2

    settings = load_config(args.config)
    token_path = Path(settings.run["work_dir"]) / "api-token.json"

    api = PhotosApiClient(client_id, client_secret, token_path)

    if args.check:
        try:
            api.access_token()
        except PhotosApiError as exc:
            print(f"Stored credentials are not usable: {exc}", file=sys.stderr)
            return 1
        print(f"Stored credentials at {token_path} are valid.")
        return 0

    if token_path.exists():
        print(f"A token already exists at {token_path}; it will be replaced.", flush=True)

    try:
        api.authorize(open_browser=not args.no_browser)
    except PhotosApiError as exc:
        print(f"Authorization failed: {exc}", file=sys.stderr)
        return 1

    print(f"\nRefresh token stored at {token_path}")
    print("Keep it private: it grants upload access to your Google Photos account.")
    print(
        "\nIf the OAuth consent screen is still in 'Testing', Google will expire this\n"
        "token in seven days. Publish it to 'In production' to avoid that."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
