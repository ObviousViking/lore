"""SQLite storage for Steam. One row per (hash, filename) sighting, with no deduplication: the
same SHA1 can appear under different file names and games (shared DLLs, redistributables).
"""

import csv
import datetime
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    appid INTEGER PRIMARY KEY,
    name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS depots (
    depot_id INTEGER PRIMARY KEY,
    appid    INTEGER NOT NULL REFERENCES games(appid),
    name     TEXT,
    os       TEXT,
    arch     TEXT,
    language TEXT,
    status   TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS manifest_runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    depot_id                INTEGER NOT NULL REFERENCES depots(depot_id),
    manifest_id             TEXT NOT NULL,
    collected_at            TEXT NOT NULL,
    file_count              INTEGER NOT NULL,
    total_uncompressed_size INTEGER NOT NULL,
    UNIQUE (depot_id, manifest_id)
);

CREATE TABLE IF NOT EXISTS hash_sightings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sha1            TEXT NOT NULL,
    filename        TEXT NOT NULL,
    size            INTEGER NOT NULL,
    chunk_count     INTEGER NOT NULL,
    flags           INTEGER NOT NULL,
    manifest_run_id INTEGER NOT NULL REFERENCES manifest_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_hash_sightings_sha1 ON hash_sightings(sha1);
CREATE INDEX IF NOT EXISTS idx_depots_appid ON depots(appid);
"""

# One row per sighting, not grouped by sha1. A hash seen in two games or files exports as two
# rows, so every place it was seen reaches the CSV.
_EXPORT_QUERY = """
    SELECT
        hs.sha1,
        hs.size,
        hs.filename,
        d.appid,
        g.name
    FROM hash_sightings hs
    JOIN manifest_runs mr ON mr.id = hs.manifest_run_id
    JOIN depots d ON d.depot_id = mr.depot_id
    JOIN games g ON g.appid = d.appid
    ORDER BY hs.sha1
"""


class HarvesterDb:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Adds columns missing from older databases. CREATE TABLE IF NOT EXISTS won't add
        columns to an existing table, so each new column needs a guarded ALTER TABLE here."""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(depots)").fetchall()}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE depots ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'")

    def upsert_game(self, appid, name):
        self.conn.execute(
            "INSERT INTO games (appid, name) VALUES (?, ?) "
            "ON CONFLICT(appid) DO UPDATE SET name = excluded.name",
            (appid, name),
        )
        self.conn.commit()

    def upsert_depot(self, depot_id, appid, os_=None, arch=None, language=None, name=None):
        self.conn.execute(
            "INSERT INTO depots (depot_id, appid, name, os, arch, language, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ok') "
            "ON CONFLICT(depot_id) DO UPDATE SET appid = excluded.appid, name = excluded.name, "
            "os = excluded.os, arch = excluded.arch, language = excluded.language, status = 'ok'",
            (depot_id, appid, name, os_, arch, language),
        )
        self.conn.commit()

    def has_any_manifest_run(self, depot_id):
        cur = self.conn.execute("SELECT 1 FROM manifest_runs WHERE depot_id = ? LIMIT 1", (depot_id,))
        return cur.fetchone() is not None

    def mark_depot_unavailable(self, depot_id, appid):
        """Records a depot DepotDownloader reported as "not available from this account"
        (usually DLC or licence gated), so it's known to exist even though it wasn't pulled.
        Does nothing if the depot already has manifest data, so a one-off denial doesn't
        overwrite a good earlier pull."""
        if self.has_any_manifest_run(depot_id):
            return
        self.conn.execute(
            "INSERT INTO depots (depot_id, appid, status) VALUES (?, ?, 'no_access') "
            "ON CONFLICT(depot_id) DO UPDATE SET status = 'no_access'",
            (depot_id, appid),
        )
        self.conn.commit()

    def games_overview(self):
        """One dict per game: appid, depot_count, and pulled_depot_count (depots with at least
        one manifest run)."""
        cur = self.conn.execute(
            "SELECT g.appid, COUNT(d.depot_id), "
            "COUNT(CASE WHEN EXISTS (SELECT 1 FROM manifest_runs mr WHERE mr.depot_id = d.depot_id) "
            "THEN 1 END) "
            "FROM games g LEFT JOIN depots d ON d.appid = g.appid GROUP BY g.appid"
        )
        return [{"appid": a, "depot_count": n, "pulled_depot_count": p} for a, n, p in cur]

    def missing_depot_ids(self, appid):
        """Depots of this game with no manifest run yet, including no_access ones (access can
        change, e.g. after buying DLC)."""
        cur = self.conn.execute(
            "SELECT depot_id FROM depots d WHERE appid = ? AND NOT EXISTS "
            "(SELECT 1 FROM manifest_runs mr WHERE mr.depot_id = d.depot_id) ORDER BY depot_id",
            (appid,),
        )
        return [row[0] for row in cur]

    def has_manifest_run(self, depot_id, manifest_id):
        cur = self.conn.execute(
            "SELECT 1 FROM manifest_runs WHERE depot_id = ? AND manifest_id = ?",
            (depot_id, manifest_id),
        )
        return cur.fetchone() is not None

    def insert_manifest_run(self, depot_id, manifest_id, files):
        total_size = sum(f["size"] for f in files)
        cur = self.conn.execute(
            "INSERT INTO manifest_runs (depot_id, manifest_id, collected_at, file_count, total_uncompressed_size) "
            "VALUES (?, ?, ?, ?, ?)",
            (depot_id, manifest_id, datetime.datetime.now(datetime.UTC).isoformat(), len(files), total_size),
        )
        run_id = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO hash_sightings (sha1, filename, size, chunk_count, flags, manifest_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(f["sha1"], f["filename"], f["size"], f["chunks"], f["flags"], run_id) for f in files],
        )
        self.conn.commit()
        return run_id

    def stats(self):
        cur = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM games), (SELECT COUNT(*) FROM depots), "
            "(SELECT COUNT(*) FROM manifest_runs), (SELECT COUNT(*) FROM hash_sightings), "
            "(SELECT COUNT(DISTINCT sha1) FROM hash_sightings)"
        )
        games, depots, runs, sightings, unique = cur.fetchone()
        return {
            "games": games, "depots": depots, "manifest_runs": runs,
            "hash_sightings": sightings, "unique_hashes": unique,
        }

    def export_rows(self):
        """Yields (sha1, size, filename, appid, game) for every sighting, sorted by sha1."""
        cur = self.conn.execute(_EXPORT_QUERY)
        while True:
            chunk = cur.fetchmany(5000)
            if not chunk:
                break
            yield from chunk

    def export_csv(self, path):
        count = 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["sha1", "size", "filename", "appid", "game"])
            for row in self.export_rows():
                writer.writerow(row)
                count += 1
        return count

    def close(self):
        self.conn.close()
