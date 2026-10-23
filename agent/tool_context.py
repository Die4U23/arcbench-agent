"""Bounded, read-only evidence that avoids discovery and diagnostic round trips."""
from __future__ import annotations

import json


def initial_file_inventory(tools) -> str:
    """Only disclose names allowed by the existing project path policy."""
    paths = []
    size = 0
    try:
        listing = tools.list_files(".")
        for path in listing.splitlines():
            if path.startswith(("...[", "(no files")):
                continue
            try:
                tools._resolve(path)
            except (ValueError, OSError):
                continue
            if len(paths) >= 80 or size + len(path) > 4000:
                break
            paths.append(path)
            size += len(path) + 1
    except (AttributeError, ValueError, OSError):
        return ""
    if not paths:
        return ""
    return ("\n\nInitial project file inventory (bounded snapshot; not file contents):\n"
            + json.dumps(paths, ensure_ascii=False)
            + "\nStart with a batch read of the relevant known files; no root listing is needed "
              "for these paths. Refresh discovery when new or unlisted files are relevant.")

