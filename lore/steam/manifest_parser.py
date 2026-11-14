"""Parses the text file DepotDownloader writes for `-manifest-only` runs. Format (checked
against real DepotDownloader output):

    Content Manifest for Depot 228990

    Manifest ID / date     : 1234567890123456789 / 1/1/2026 12:00:00 AM
    Total number of files  : 42
    ...

              Size Chunks File SHA                                 Flags Name
            123456      3 0123456789abcdef0123456789abcdef01234567     0 bin\\game.exe
    ...
"""

import os
import re

_DEPOT_HEADER = re.compile(r"^Content Manifest for Depot\s+(\d+)\s*$")
_MANIFEST_HEADER = re.compile(r"^Manifest ID / date\s*:\s*(\d+)\s*/")
_FILE_ROW = re.compile(
    r"^\s*(?P<size>\d+)\s+(?P<chunks>\d+)\s+(?P<sha1>[0-9a-fA-F]{40})\s+(?P<flags>[0-9a-fA-F]+)\s+(?P<name>.+?)\s*$"
)


class ManifestParseError(Exception):
    pass


def parse_manifest_file(path):
    """Returns (depot_id: int, manifest_id: str, files: list[dict]) where each file dict has
    sha1, filename, size, chunks, flags."""
    depot_id = None
    manifest_id = None
    files = []

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")

            if depot_id is None:
                m = _DEPOT_HEADER.match(line)
                if m:
                    depot_id = int(m.group(1))
                    continue

            if manifest_id is None:
                m = _MANIFEST_HEADER.match(line)
                if m:
                    manifest_id = m.group(1)
                    continue

            m = _FILE_ROW.match(line)
            if m:
                files.append({
                    "sha1": m.group("sha1").lower(),
                    "filename": m.group("name"),
                    "size": int(m.group("size")),
                    "chunks": int(m.group("chunks")),
                    "flags": int(m.group("flags"), 16),
                })

    if depot_id is None or manifest_id is None:
        # Fall back to the filename convention (manifest_{depotId}_{manifestId}.txt).
        base = os.path.splitext(os.path.basename(path))[0]
        parts = base.split("_")
        if len(parts) >= 3 and parts[0] == "manifest":
            depot_id = depot_id if depot_id is not None else int(parts[1])
            manifest_id = manifest_id if manifest_id is not None else parts[2]
        else:
            raise ManifestParseError(f"Could not determine depot/manifest id from '{path}'.")

    return depot_id, manifest_id, files
