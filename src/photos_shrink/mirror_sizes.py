"""Every exported copy's size, by media key, read from the mirror.

An edited photo is exported twice under one media key and Google reports the
untouched copy's size, so the replace step has to know both. They come from the
mirror rather than another request, which is what keeps the check free.
"""

from __future__ import annotations

import csv
from pathlib import Path


def load_exported_sizes(mirror_path: Path) -> dict[str, set[int]]:
    """Media key -> every exported copy's size. Empty when the mirror predates sizes."""

    if not mirror_path.exists():
        return {}
    with open(mirror_path, encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if "sizes" not in (reader.fieldnames or []):
            return {}
        sizes: dict[str, set[int]] = {}
        for row in reader:
            key = row.get("media_key")
            if not key:
                continue
            sizes[key] = {int(part) for part in (row.get("sizes") or "").split() if part.isdigit()}
        return sizes
