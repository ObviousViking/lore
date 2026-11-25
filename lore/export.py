"""CSV export for all stores in one format. Steam comes from steam.sqlite, Epic, GOG and
Amazon from lore.sqlite. Rows without a hash, empty files and symlinks are left out.
"""

import csv
import gzip
import io
import sqlite3
from pathlib import Path

from .epic_gog import open_db
from .paths import files_dir

EXPORT_STORES = ("steam", "epic", "gog", "amazon")
LABELS = {"steam": "Steam", "epic": "Epic", "gog": "GOG", "amazon": "Amazon"}

COLUMNS = ["store", "sha1", "md5", "sha256", "size", "path", "title", "app_id", "platform",
           "build_version", "store_build_id", "branch", "languages", "product_id", "flags",
           "manifest_sha1", "collected_utc"]

# Steam has no MD5/SHA256 or build version. store_build_id is the depot's manifest ID and
# product_id the depot ID. Directories have an all-zero SHA1 and size 0.
_STEAM_QUERY = """
    SELECT 'steam', hs.sha1, NULL, NULL, hs.size, REPLACE(hs.filename, '\\', '/'), g.name,
           CAST(d.appid AS TEXT), 'Windows', NULL, mr.manifest_id, NULL, d.language,
           CAST(d.depot_id AS TEXT), CAST(hs.flags AS TEXT), NULL, mr.collected_at
    FROM hash_sightings hs
    JOIN manifest_runs mr ON mr.id = hs.manifest_run_id
    JOIN depots d ON d.depot_id = mr.depot_id
    JOIN games g ON g.appid = d.appid
    WHERE hs.size > 0 AND hs.sha1 != '0000000000000000000000000000000000000000'
    ORDER BY g.name, d.depot_id, hs.filename
"""

_EPIC_GOG_QUERY = """
    SELECT b.store, f.sha1, f.md5, f.sha256, f.size, f.path, b.title, b.app_name, b.platform,
           b.build_version, b.store_build_id, b.branch, f.languages, f.product_id, f.flags,
           b.manifest_sha1, b.collected_utc
    FROM files f JOIN builds b ON b.id = f.build_id
    WHERE b.store = ? AND f.symlink_to IS NULL AND f.size > 0
      AND COALESCE(f.sha1, f.md5, f.sha256) IS NOT NULL
"""


def steam_db_path():
    return files_dir() / "steam.sqlite"


def _steam_rows():
    path = steam_db_path()
    if not path.exists():
        return
    db = sqlite3.connect(path)
    try:
        yield from db.execute(_STEAM_QUERY)
    finally:
        db.close()


def _store_rows(store, epic_gog_db):
    if store == "steam":
        yield from _steam_rows()
    else:
        yield from open_db(epic_gog_db).execute(_EPIC_GOG_QUERY, (store,))


def export(out, stores, epic_gog_db):
    """Writes the given stores to out. Returns {store: row count}."""
    counts = {}
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for store in stores:
            n = 0
            for row in _store_rows(store, epic_gog_db):
                w.writerow(row)
                n += 1
            counts[store] = n
    return counts


def write_gzip_parts(out_dir, stores, epic_gog_db, part_bytes):
    """Writes the given stores as gzipped CSV parts of about part_bytes each, every part with
    the header row. Returns ([part paths], {store: row count})."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts, counts = [], {}
    gz = writer = raw = None

    def new_part():
        nonlocal gz, writer, raw
        path = out_dir / f"part{len(parts) + 1:03d}.csv.gz"
        parts.append(path)
        raw = open(path, "wb")
        gz = gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6)
        writer = csv.writer(io.TextIOWrapper(gz, encoding="utf-8", newline="", write_through=True))
        writer.writerow(COLUMNS)

    def close_part():
        gz.close()
        raw.close()

    new_part()
    rows_in_part = 0
    for store in stores:
        n = 0
        for row in _store_rows(store, epic_gog_db):
            writer.writerow(row)
            n += 1
            rows_in_part += 1
            if rows_in_part % 5000 == 0 and raw.tell() >= part_bytes:
                close_part()
                new_part()
                rows_in_part = 0
        counts[store] = n
    close_part()
    if rows_in_part == 0 and len(parts) > 1:  # split landed exactly on the last row
        parts.pop().unlink()
    return parts, counts


def cmd_export(args):
    stores = EXPORT_STORES if args.store == "all" else (args.store,)
    counts = export(args.out, stores, args.db)
    for store, n in counts.items():
        print(f"  {LABELS[store]:6} {n:>12,} rows")
    print(f"Wrote {sum(counts.values()):,} rows to {Path(args.out).resolve()}")
    return counts
