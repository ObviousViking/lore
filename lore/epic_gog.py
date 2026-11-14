"""Epic Games Store and GOG support: login, library listing, manifest download/parsing and the
SQLite store. Only manifests are downloaded - never game files.

  Epic: current build only (SHA1 per file; MD5/SHA256 on newer manifests).
  GOG:  current build, or every public build back through history (MD5 per file; SHA256 on some
        newer builds). All language depots are included.

Optional: pycryptodomex recovers file names from newer encrypted Epic manifests (hashes are
collected either way). Format/API details derived from Legendary (github.com/derrod/legendary)
and heroic-gogdl (github.com/Heroic-Games-Launcher/heroic-gogdl).
"""

import argparse
import base64
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

try:
    from Cryptodome.Cipher import AES  # pycryptodomex
except ImportError:
    try:
        from Crypto.Cipher import AES  # pycryptodome
    except ImportError:
        AES = None

from . import __version__ as VERSION
STORES = ("epic", "gog")

# Login sessions. Copies over sessions from the older game_hashes / epic_hashes tools once.
DATA_DIR = Path(os.environ.get("LORE_HOME", Path.home() / ".lore"))
if not DATA_DIR.exists():
    for _old in (Path.home() / ".game_hashes", Path.home() / ".epic_hashes"):
        if _old.exists():
            try:
                shutil.copytree(_old, DATA_DIR)
                if (DATA_DIR / "session.json").exists():
                    (DATA_DIR / "session.json").rename(DATA_DIR / "epic_session.json")
            except OSError:
                pass
            break

DEFAULT_DB = "lore.sqlite"
LEGACY_DBS = ("game_hashes.sqlite", "epic_hashes.sqlite")  # older tools' databases, reused if found


def default_db():
    """lore.sqlite, unless only a database from the older tools is here."""
    if Path(DEFAULT_DB).exists():
        return DEFAULT_DB
    return next((p for p in LEGACY_DBS if Path(p).exists()), DEFAULT_DB)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- HTTP base

class Http:
    user_agent = "lore/" + VERSION
    token_file = None

    def __init__(self, timeout=20):
        self.timeout = timeout
        self.session = None

    def _auth_header(self):
        return f"Bearer {self.session['access_token']}"

    def _req(self, url, data=None, headers=None, auth=True, raw=False, retries=4):
        hdrs = {"User-Agent": self.user_agent}
        if headers:
            hdrs.update(headers)
        if auth and self.session:
            hdrs["Authorization"] = self._auth_header()
        body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
        for attempt in range(retries):
            req = urllib.request.Request(url, data=body, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    content = r.read()
                    return content if raw else json.loads(content)
            except urllib.error.HTTPError as e:
                if e.code == 401 and auth and self.session and attempt == 0:
                    self.refresh()
                    hdrs["Authorization"] = self._auth_header()
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                detail = e.read()[:300].decode(errors="replace")
                raise RuntimeError(f"HTTP {e.code} for {url.split('?')[0]}: {detail}") from None
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Network error for {url.split('?')[0]}: {e.reason}") from None

    def _save(self, j):
        j["loginTime"] = time.time()
        self.session = j
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(j))
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass
        return j

    def logged_in(self):
        return self.token_file.exists()

    def resume(self):
        name = self.__class__.__name__
        if not self.token_file.exists():
            raise SystemExit(f"Not logged in to {name}. Use 'Epic/GOG - log in' in the menu, or run: "
                             f"lore auth --store {name.lower()}")
        self.session = json.loads(self.token_file.read_text())
        try:
            self.refresh()
        except RuntimeError as e:
            raise SystemExit(f"{name} session expired or invalid ({e}). Log in to {name} again.")


# --------------------------------------------------------------------------- Epic API

class Epic(Http):
    CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"
    CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
    user_agent = "UELauncher/11.0.1-14907503+++Portal+Release-Live Windows/10.0.19041.1.256.64bit"
    OAUTH = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth"
    LAUNCHER = "https://launcher-public-service-prod06.ol.epicgames.com/launcher/api/public/assets"
    CATALOG = "https://catalog-public-service-prod06.ol.epicgames.com/catalog/api/shared/namespace"
    token_file = DATA_DIR / "epic_session.json"

    def _auth_header(self):
        return f"bearer {self.session['access_token']}"

    def _token(self, params):
        basic = base64.b64encode(f"{self.CLIENT_ID}:{self.CLIENT_SECRET}".encode()).decode()
        params["token_type"] = "eg1"
        saved = self.session
        self.session = None
        try:
            j = self._req(f"{self.OAUTH}/token", data=params, auth=False,
                          headers={"Authorization": f"basic {basic}",
                                   "Content-Type": "application/x-www-form-urlencoded"})
        except Exception:
            self.session = saved
            raise
        return self._save(j)

    def login_url(self):
        redirect = f"https://www.epicgames.com/id/api/redirect?clientId={self.CLIENT_ID}&responseType=code"
        return "https://www.epicgames.com/id/login?redirectUrl=" + urllib.parse.quote(redirect)

    def login_with_code(self, code):
        return self._token({"grant_type": "authorization_code", "code": code})

    def refresh(self):
        return self._token({"grant_type": "refresh_token",
                            "refresh_token": self.session["refresh_token"]})

    def display_name(self):
        return self.session.get("displayName", "?") if self.session else "?"

    def account_id(self):
        return self.session.get("account_id", "")

    def assets(self, platform):
        return self._req(f"{self.LAUNCHER}/{platform}?label=Live")

    def catalog_item(self, namespace, item_id):
        q = urllib.parse.urlencode(dict(id=item_id, includeDLCDetails="true",
                                        includeMainGameDetails="true", country="US", locale="en"))
        return self._req(f"{self.CATALOG}/{namespace}/bulk/items?{q}").get(item_id)

    def manifest(self, namespace, item_id, app_name, platform):
        j = self._req(f"{self.LAUNCHER}/v2/platform/{platform}/namespace/{namespace}"
                      f"/catalogItem/{item_id}/app/{app_name}/label/Live")
        el = j["elements"][0]
        expected = el["hash"]
        last_err = None
        for m in el["manifests"]:
            url = m["uri"]
            if m.get("queryParams"):
                url += "?" + "&".join(f"{p['name']}={p['value']}" for p in m["queryParams"])
            try:
                data = self._req(url, auth=False, raw=True, retries=2)
            except RuntimeError as e:
                last_err = e
                continue
            if hashlib.sha1(data).hexdigest() != expected:
                last_err = RuntimeError("manifest SHA1 mismatch")
                continue
            return data, expected, el.get("secrets") or {}
        raise last_err or RuntimeError("no manifest URLs returned")


# --------------------------------------------------------------------------- GOG API

class GOG(Http):
    CLIENT_ID = "46899977096215655"
    CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
    REDIRECT = "https://embed.gog.com/on_login_success?origin=client"
    CDN = "https://gog-cdn-fastly.gog.com"
    CONTENT = "https://content-system.gog.com"
    token_file = DATA_DIR / "gog_session.json"

    def login_url(self):
        return ("https://auth.gog.com/auth?" + urllib.parse.urlencode(dict(
            client_id=self.CLIENT_ID, redirect_uri=self.REDIRECT,
            response_type="code", layout="client2")))

    def _token(self, extra):
        q = urllib.parse.urlencode(dict(client_id=self.CLIENT_ID, client_secret=self.CLIENT_SECRET, **extra))
        j = self._req(f"https://auth.gog.com/token?{q}", auth=False)
        if "access_token" not in j:
            raise RuntimeError(f"GOG token error: {j}")
        old_name = (self.session or {}).get("username")
        self._save(j)
        if old_name:
            self.session["username"] = old_name
        return j

    def login_with_code(self, code):
        self._token(dict(grant_type="authorization_code", code=code, redirect_uri=self.REDIRECT))
        try:
            ud = self._req("https://embed.gog.com/userData.json")
            self.session["username"] = ud.get("username", "?")
            self.token_file.write_text(json.dumps(self.session))
        except Exception:
            pass
        return self.session

    def refresh(self):
        return self._token(dict(grant_type="refresh_token", refresh_token=self.session["refresh_token"]))

    def display_name(self):
        return (self.session or {}).get("username", "?")

    def account_id(self):
        return str((self.session or {}).get("user_id", ""))

    def owned(self):
        return self._req("https://embed.gog.com/user/data/games").get("owned", [])

    def product(self, pid):
        try:
            return self._req(f"https://api.gog.com/products/{pid}?locale=en-US", auth=False, retries=2)
        except RuntimeError:
            return {}

    def builds(self, pid, os_name, generation):
        try:
            return self._req(f"{self.CONTENT}/products/{pid}/os/{os_name}/builds?generation={generation}"
                             ).get("items", [])
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return []
            raise

    def fetch(self, url):
        """Return (raw_bytes, parsed_json). Handles zlib-compressed or plain JSON."""
        raw = self._req(url, auth=False, raw=True)
        try:
            return raw, json.loads(zlib.decompress(raw, 15))
        except zlib.error:
            return raw, json.loads(raw)


def galaxy_path(h):
    return h if "/" in h else f"{h[0:2]}/{h[2:4]}/{h}"


# --------------------------------------------------------------------------- Epic manifest parsing

def read_fstring(bio):
    n = struct.unpack("<i", bio.read(4))[0]
    if n < 0:
        s = bio.read(-n * 2 - 2).decode("utf-16-le")
        bio.read(2)
    elif n > 0:
        s = bio.read(n - 1).decode("ascii", errors="replace")
        bio.read(1)
    else:
        s = ""
    return s


def u32(bio):
    return struct.unpack("<I", bio.read(4))[0]


def u8(bio):
    return bio.read(1)[0]


def nonzero_hex(b):
    return b.hex() if b and any(b) else None


def parse_epic_manifest(data, secrets=None):
    """Return (build_version, [file dicts], notes)."""
    if data[:1] == b"{":
        return parse_json_manifest(data)

    bio = BytesIO(data)
    if u32(bio) != 0x44BEC00C:
        raise ValueError("not an Epic manifest (bad magic)")
    header_size = u32(bio)
    u32(bio)  # uncompressed size
    u32(bio)  # compressed size
    body_sha = bio.read(20)
    stored_as = u8(bio)
    version = u32(bio)
    secret_guid, enc_tag = None, None
    if version >= 22:
        secret_guid = struct.unpack("<IIII", bio.read(16))
        enc_tag = bio.read(16)
    bio.seek(header_size)
    body = bio.read()
    if stored_as & 1:
        body = zlib.decompress(body)
        if hashlib.sha1(body).digest() != body_sha:
            raise ValueError("manifest body hash mismatch")
    b = BytesIO(body)

    # Meta
    meta_start = b.tell()
    meta_size = u32(b)
    data_version = u8(b)
    feature_level = u32(b)
    u8(b)  # is_file_data
    u32(b)  # app_id
    read_fstring(b)  # app_name
    build_version = read_fstring(b)
    b.seek(meta_start + meta_size)

    # Chunk data list - not needed, skip by size
    cdl_start = b.tell()
    b.seek(cdl_start + u32(b))

    # File manifest list
    fml_start = b.tell()
    fml_size = u32(b)
    fml_version = u8(b)
    count = u32(b)
    files = [dict(path="", symlink=None, sha1=None, md5=None, sha256=None,
                  executable=0, size=0) for _ in range(count)]
    for f in files:
        f["path"] = read_fstring(b)
    for f in files:
        f["symlink"] = read_fstring(b) or None
    for f in files:
        f["sha1"] = nonzero_hex(b.read(20))
    for f in files:
        f["executable"] = 1 if u8(b) & 0x4 else 0
    for f in files:
        for _ in range(u32(b)):
            read_fstring(b)  # install tags
    for f in files:
        for _ in range(u32(b)):
            part_start = b.tell()
            part_size = u32(b)
            b.read(16)  # chunk guid
            u32(b)      # offset in chunk
            f["size"] += u32(b)
            b.seek(part_start + part_size)
    if fml_version >= 1:
        for f in files:
            if u32(b):
                f["md5"] = nonzero_hex(b.read(16))
        for f in files:
            read_fstring(b)  # mime type
    if fml_version >= 2:
        for f in files:
            f["sha256"] = nonzero_hex(b.read(32))
    b.seek(fml_start + fml_size)

    notes = ""
    if stored_as & 2:
        notes = decrypt_names(b, feature_level, files, secret_guid, enc_tag, secrets or {})
    return build_version, files, notes


def decrypt_names(b, feature_level, files, secret_guid, enc_tag, secrets):
    """Newer manifests encrypt file paths. Hashes are unaffected."""
    def placeholder(reason):
        for i, f in enumerate(files):
            if not f["path"]:
                f["path"] = f"<encrypted:{i}>"
        return reason

    if AES is None:
        return placeholder("encrypted paths (install pycryptodomex to recover)")
    if feature_level < 24:
        return placeholder("encrypted paths (unsupported layout)")
    try:
        cf_start = b.tell()
        b.seek(cf_start + u32(b))  # custom fields
        u32(b); u8(b)              # encrypted data size, version
        blob = BytesIO(b.read(u32(b)))
        h_start = blob.tell()
        h_size = u32(blob); u32(blob)
        h_stored = u8(blob); u32(blob); u32(blob)
        iv = blob.read(u32(blob))
        blob.seek(h_start + h_size)
        ciphertext = blob.read(u32(blob))
        key_hex = secrets.get("".join(f"{g:08X}" for g in secret_guid))
        if not key_hex:
            return placeholder("encrypted paths (no key from API)")
        plain = AES.new(bytes.fromhex(key_hex), AES.MODE_GCM, nonce=iv) \
            .decrypt_and_verify(ciphertext, enc_tag)
        if h_stored & 1:
            plain = zlib.decompress(plain)
        p = BytesIO(plain)
        read_fstring(p); read_fstring(p)            # launch exe / command
        for _ in range(u32(p)):
            read_fstring(p)                         # prereq ids
        for _ in range(5):
            read_fstring(p)                         # prereq name/path/args, uninstall path/args
        for f in files:
            f["path"] = read_fstring(p)
            f["symlink"] = read_fstring(p) or None
        return "paths decrypted"
    except Exception as e:
        return placeholder(f"encrypted paths (decrypt failed: {e})")


def parse_json_manifest(data):
    """Old-style JSON manifests. Numbers are stored as concatenated %03d bytes."""
    def blob(s):
        return bytes(int(s[i:i + 3]) for i in range(0, len(s), 3))

    j = json.loads(data)
    files = []
    for fm in j.get("FileManifestList", []):
        size = sum(int.from_bytes(blob(cp["Size"]), "little") for cp in fm.get("FileChunkParts", []))
        files.append(dict(path=fm.get("Filename", ""), symlink=None,
                          sha1=nonzero_hex(blob(fm["FileHash"])), md5=None, sha256=None,
                          executable=1 if fm.get("bIsUnixExecutable") else 0, size=size))
    return j.get("BuildVersionString", ""), files, "json manifest"


# --------------------------------------------------------------------------- GOG manifest parsing

class GogDepotCache:
    """Depot manifests are shared between builds; fetch each one once (memory + disk)."""
    def __init__(self, gog, root, delay):
        self.gog, self.root, self.delay, self.mem = gog, Path(root), delay, {}

    def get(self, url, key):
        if key in self.mem:
            return self.mem[key]
        path = self.root / key
        if path.exists():
            raw = path.read_bytes()
            try:
                obj = json.loads(zlib.decompress(raw, 15))
            except zlib.error:
                obj = json.loads(raw)
        else:
            raw, obj = self.gog.fetch(url)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            time.sleep(self.delay)
        self.mem[key] = obj
        return obj


def gog_v2_files(meta, cache):
    files, notes = [], []
    depots = list(meta.get("depots", []))
    if meta.get("offlineDepot"):
        od = dict(meta["offlineDepot"]); od.setdefault("languages", ["*"]); od["_offline"] = True
        depots.append(od)
    for d in depots:
        h = d["manifest"]
        try:
            dm = cache.get(f"{GOG.CDN}/content-system/v2/meta/{galaxy_path(h)}", f"v2/{galaxy_path(h)}")
        except Exception as e:
            notes.append(f"depot {h} failed: {e}")
            continue
        langs = ",".join(d.get("languages", []))
        for it in dm.get("depot", {}).get("items", []):
            t = it.get("type")
            if t == "DepotFile":
                chunks = it.get("chunks", [])
                flags = list(it.get("flags") or [])
                if d.get("_offline"):
                    flags.append("offline")
                md5 = it.get("md5") or (chunks[0].get("md5") if len(chunks) == 1 else None)
                files.append(dict(path=it["path"].replace("\\", "/"), symlink=None,
                                  sha1=None, md5=md5, sha256=it.get("sha256"),
                                  executable=1 if "executable" in flags else 0,
                                  size=sum(c.get("size", 0) for c in chunks),
                                  product_id=str(d.get("productId", "")), languages=langs,
                                  flags=",".join(flags) or None))
            elif t == "DepotLink":
                files.append(dict(path=it["path"].replace("\\", "/"), symlink=it.get("target"),
                                  sha1=None, md5=None, sha256=None, executable=0, size=0,
                                  product_id=str(d.get("productId", "")), languages=langs, flags="link"))
    return files, "; ".join(notes)


def gog_v1_files(meta, platform, cache):
    files, notes = [], []
    prod = meta.get("product", {})
    ts = prod.get("timestamp")
    for d in prod.get("depots", []):
        if d.get("redist") or not d.get("manifest"):
            continue
        gid = d["gameIDs"][0]
        try:
            dm = cache.get(f"{GOG.CDN}/content-system/v1/manifests/{gid}/{platform}/{ts}/{d['manifest']}",
                           f"v1/{gid}/{platform}/{ts}/{d['manifest']}")
        except Exception as e:
            notes.append(f"depot {d['manifest']} failed: {e}")
            continue
        langs = ",".join(d.get("languages", []))
        for rec in dm.get("depot", {}).get("files", []):
            if "directory" in rec or "path" not in rec:
                continue
            flags = [f for f in ("support", "executable") if rec.get(f)]
            files.append(dict(path=rec["path"].lstrip("/").replace("\\", "/"), symlink=None,
                              sha1=None, md5=rec.get("hash"), sha256=None,
                              executable=1 if rec.get("executable") else 0, size=int(rec.get("size", 0)),
                              product_id=str(gid), languages=langs, flags=",".join(flags) or None))
    return files, "; ".join(notes)


# --------------------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS builds (
    id INTEGER PRIMARY KEY, app_name TEXT NOT NULL, title TEXT, platform TEXT NOT NULL,
    build_version TEXT, manifest_sha1 TEXT NOT NULL, is_dlc INTEGER, base_title TEXT,
    file_count INTEGER, total_bytes INTEGER, manifest_path TEXT, notes TEXT,
    collected_utc TEXT NOT NULL, account_id TEXT, tool_version TEXT,
    UNIQUE (app_name, platform, manifest_sha1));
CREATE TABLE IF NOT EXISTS files (
    build_id INTEGER NOT NULL REFERENCES builds(id), path TEXT NOT NULL, size INTEGER,
    sha1 TEXT, md5 TEXT, sha256 TEXT, executable INTEGER, symlink_to TEXT);
CREATE TABLE IF NOT EXISTS catalog (
    namespace TEXT, item_id TEXT, json TEXT, fetched_utc TEXT, PRIMARY KEY (namespace, item_id));
CREATE TABLE IF NOT EXISTS errors (app_name TEXT, platform TEXT, error TEXT, when_utc TEXT);
"""

# Columns added in v2.0 - applied to older databases automatically.
MIGRATIONS = [
    ("builds", "store", "TEXT DEFAULT 'epic'"),
    ("builds", "store_build_id", "TEXT"),
    ("builds", "branch", "TEXT"),
    ("builds", "published_utc", "TEXT"),
    ("files", "product_id", "TEXT"),
    ("files", "languages", "TEXT"),
    ("files", "flags", "TEXT"),
    ("errors", "store", "TEXT DEFAULT 'epic'"),
]


def open_db(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    db.executescript("""
        CREATE INDEX IF NOT EXISTS idx_files_sha1 ON files(sha1);
        CREATE INDEX IF NOT EXISTS idx_files_md5 ON files(md5);
        CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
        CREATE INDEX IF NOT EXISTS idx_files_build ON files(build_id);
        CREATE INDEX IF NOT EXISTS idx_builds_store ON builds(store, app_name, platform, store_build_id);
    """)
    db.commit()
    return db


def log_error(db, store, app, platform, err):
    db.execute("INSERT INTO errors (app_name, platform, error, when_utc, store) VALUES (?,?,?,?,?)",
               (app, platform, str(err), utcnow(), store))
    db.commit()


def insert_build(db, store, rec, files):
    """rec: dict of build columns. Returns True if inserted, False if duplicate content."""
    try:
        cur = db.execute(
            "INSERT INTO builds (store, app_name, title, platform, build_version, manifest_sha1, is_dlc,"
            " base_title, file_count, total_bytes, manifest_path, notes, collected_utc, account_id,"
            " tool_version, store_build_id, branch, published_utc) VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (store, rec["app_name"], rec["title"], rec["platform"], rec["build_version"],
             rec["manifest_sha1"], rec.get("is_dlc", 0), rec.get("base_title"), len(files),
             sum(f["size"] for f in files), rec["manifest_path"], rec.get("notes"), utcnow(),
             rec["account_id"], f"lore/{VERSION}", rec.get("store_build_id"),
             rec.get("branch"), rec.get("published_utc")))
    except sqlite3.IntegrityError:
        return False
    db.executemany(
        "INSERT INTO files (build_id, path, size, sha1, md5, sha256, executable, symlink_to,"
        " product_id, languages, flags) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(cur.lastrowid, f["path"], f["size"], f["sha1"], f["md5"], f["sha256"], f["executable"],
          f["symlink"], f.get("product_id"), f.get("languages"), f.get("flags")) for f in files])
    db.commit()
    return True


def catalog_cached(db, ns, item_id, fetch):
    row = db.execute("SELECT json FROM catalog WHERE namespace=? AND item_id=?", (ns, item_id)).fetchone()
    if row:
        return json.loads(row[0])
    meta = fetch() or {}
    db.execute("INSERT OR REPLACE INTO catalog VALUES (?,?,?,?)", (ns, item_id, json.dumps(meta), utcnow()))
    db.commit()
    return meta


# --------------------------------------------------------------------------- Epic library + collect

def epic_third_party(meta):
    ca = meta.get("customAttributes", {})
    return (ca.get("ThirdPartyManagedApp", {}).get("value")
            or ca.get("ThirdPartyManagedProvider", {}).get("value"))


def epic_library(epic, db, platforms):
    apps = {}
    for p in platforms:
        for a in epic.assets(p):
            if a.get("namespace") == "ue":
                continue
            e = apps.setdefault(a["appName"], dict(app_name=a["appName"], namespace=a["namespace"],
                                                   item_id=a["catalogItemId"], platforms=[]))
            e["platforms"].append(p)
    print(f"Epic: {len(apps)} apps in library, fetching titles (cached after first run)...")
    out = []
    for e in apps.values():
        meta = catalog_cached(db, e["namespace"], e["item_id"],
                              lambda: epic.catalog_item(e["namespace"], e["item_id"]))
        if any(c.get("path") == "mods" for c in meta.get("categories", [])):
            continue
        e["title"] = meta.get("title", e["app_name"])
        e["is_dlc"] = "mainGameItem" in meta
        e["base_title"] = meta.get("mainGameItem", {}).get("title") if e["is_dlc"] else None
        e["note"] = epic_third_party(meta)
        out.append(e)
    out.sort(key=lambda x: ((x["base_title"] or x["title"]).lower(), x["is_dlc"], x["title"].lower()))
    return out


def collect_epic(db, args):
    epic = Epic(); epic.resume()
    mdir = Path(args.manifest_dir) / "epic"; mdir.mkdir(parents=True, exist_ok=True)
    lib = epic_library(epic, db, args.platforms)
    if args.only:
        lib = [e for e in lib if e["app_name"] in args.only]
    if args.limit:
        lib = lib[: args.limit]
    print(f"Epic: {len(lib)} titles to check\n")
    c = dict(new=0, same=0, skipped=0, errors=0)
    for i, e in enumerate(lib, 1):
        for p in e["platforms"]:
            label = f"[Epic {i}/{len(lib)}] {e['title']} ({p})"
            if e["note"] and not args.include_third_party:
                c["skipped"] += 1; print(f"{label}: skipped, third-party ({e['note']})"); continue
            try:
                data, m_sha1, secrets = epic.manifest(e["namespace"], e["item_id"], e["app_name"], p)
            except Exception as ex:
                c["errors"] += 1; print(f"{label}: ERROR {ex}")
                log_error(db, "epic", e["app_name"], p, ex); time.sleep(args.delay); continue
            if db.execute("SELECT 1 FROM builds WHERE app_name=? AND platform=? AND manifest_sha1=?",
                          (e["app_name"], p, m_sha1)).fetchone():
                c["same"] += 1; print(f"{label}: unchanged"); time.sleep(args.delay); continue
            try:
                build_version, files, notes = parse_epic_manifest(data, secrets)
            except Exception as ex:
                c["errors"] += 1; print(f"{label}: PARSE ERROR {ex}")
                log_error(db, "epic", e["app_name"], p, f"parse: {ex}"); continue
            m_path = mdir / f"{e['app_name']}_{p}_{m_sha1}.manifest"
            m_path.write_bytes(data)
            insert_build(db, "epic", dict(
                app_name=e["app_name"], title=e["title"], platform=p, build_version=build_version,
                manifest_sha1=m_sha1, is_dlc=int(e["is_dlc"]), base_title=e["base_title"],
                manifest_path=str(m_path), notes=notes, account_id=epic.account_id()), files)
            c["new"] += 1
            extra = f" ({notes})" if notes and notes != "paths decrypted" else ""
            print(f"{label}: NEW build {build_version} - {len(files)} files{extra}")
            time.sleep(args.delay)
    return c


# --------------------------------------------------------------------------- GOG library + collect

GOG_OS = {"Windows": "windows", "Mac": "osx"}


def gog_library(gog, db):
    ids = gog.owned()
    print(f"GOG: {len(ids)} products owned, fetching titles (cached after first run)...")
    out = []
    for pid in ids:
        meta = catalog_cached(db, "gog", str(pid), lambda: gog.product(pid))
        csc = meta.get("content_system_compatibility") or {}
        gtype = meta.get("game_type", "?")
        out.append(dict(app_name=str(pid), title=meta.get("title") or f"GOG product {pid}",
                        game_type=gtype, is_dlc=gtype == "dlc",
                        platforms=[p for p, o in GOG_OS.items() if csc.get(o)],
                        note=None if gtype == "game" else gtype))
    out.sort(key=lambda x: (x["is_dlc"], x["title"].lower()))
    return out


def gog_select_builds(gog, pid, os_name, mode):
    items = gog.builds(pid, os_name, 2)
    if mode == "all" or not items:
        seen = {b["build_id"] for b in items}
        items += [b for b in gog.builds(pid, os_name, 1) if b["build_id"] not in seen]
    items.sort(key=lambda b: b.get("date_published") or "", reverse=True)
    if mode == "all":
        return items
    main = [b for b in items if not b.get("branch")]
    return (main or items)[:1]


def collect_gog(db, args):
    gog = GOG(); gog.resume()
    mroot = Path(args.manifest_dir) / "gog"
    cache = GogDepotCache(gog, mroot / "depots", delay=min(args.delay, 0.25))
    lib = gog_library(gog, db)
    if args.only:
        lib = [e for e in lib if e["app_name"] in args.only]
    games = [e for e in lib if e["game_type"] == "game"]
    others = len(lib) - len(games)
    if args.limit:
        games = games[: args.limit]
    print(f"GOG: {len(games)} games to check ({others} DLC/packs/other skipped - DLC files are in "
          f"their base game's depots). Builds: {args.gog_builds}\n")
    c = dict(new=0, same=0, skipped=0, errors=0)
    for i, e in enumerate(games, 1):
        if not e["platforms"]:
            c["skipped"] += 1
            print(f"[GOG {i}/{len(games)}] {e['title']}: skipped, no Galaxy content-system builds "
                  f"(installer-only title)")
            continue
        for p in [p for p in args.platforms if p in e["platforms"]]:
            os_name = GOG_OS[p]
            label = f"[GOG {i}/{len(games)}] {e['title']} ({p})"
            try:
                builds = gog_select_builds(gog, e["app_name"], os_name, args.gog_builds)
                time.sleep(args.delay)
            except Exception as ex:
                c["errors"] += 1; print(f"{label}: ERROR {ex}")
                log_error(db, "gog", e["app_name"], p, ex); continue
            if not builds:
                c["skipped"] += 1; print(f"{label}: no builds listed"); continue
            for b in builds:
                bid = str(b["build_id"])
                ver = b.get("version_name") or bid
                bl = f"{label} build {ver}" + (f" [{b['branch']}]" if b.get("branch") else "")
                if db.execute("SELECT 1 FROM builds WHERE store='gog' AND app_name=? AND platform=?"
                              " AND store_build_id=?", (e["app_name"], p, bid)).fetchone():
                    c["same"] += 1; print(f"{bl}: already collected"); continue
                try:
                    raw, meta = gog.fetch(b["link"])
                    gen = int(b.get("generation") or meta.get("version") or 2)
                    if gen == 1:
                        files, notes = gog_v1_files(meta, os_name, cache)
                    else:
                        files, notes = gog_v2_files(meta, cache)
                except Exception as ex:
                    c["errors"] += 1; print(f"{bl}: ERROR {ex}")
                    log_error(db, "gog", e["app_name"], p, f"build {bid}: {ex}"); continue
                m_sha1 = hashlib.sha1(raw).hexdigest()
                m_path = mroot / e["app_name"] / os_name / f"{bid}.meta"
                m_path.parent.mkdir(parents=True, exist_ok=True)
                m_path.write_bytes(raw)
                ok = insert_build(db, "gog", dict(
                    app_name=e["app_name"], title=e["title"], platform=p, build_version=ver,
                    manifest_sha1=m_sha1, manifest_path=str(m_path), notes=f"gen{gen}" + (f"; {notes}" if notes else ""),
                    account_id=gog.account_id(), store_build_id=bid, branch=b.get("branch"),
                    published_utc=b.get("date_published")), files)
                if ok:
                    c["new"] += 1
                    print(f"{bl}: NEW - {len(files)} files (gen{gen}){' - ' + notes if notes else ''}")
                else:
                    c["same"] += 1; print(f"{bl}: identical to an already collected build")
                time.sleep(args.delay)
    return c


# --------------------------------------------------------------------------- commands

def client(store):
    return Epic() if store == "epic" else GOG()


def cmd_auth(args):
    store = args.store
    c = client(store)
    url = c.login_url()
    if store == "epic":
        print("\n1. Log in to Epic in the browser window that opens (or open this URL):\n")
        print("   " + url + "\n")
        print('2. You will then see a short page of text. Copy the value of "authorizationCode"')
        print("   (or just copy the whole thing).\n")
        prompt = "Paste authorizationCode: "
    else:
        print("\n1. Log in to GOG in the browser window that opens (or open this URL):\n")
        print("   " + url + "\n")
        print("2. After login you land on a mostly blank page. Copy the FULL URL from the")
        print("   address bar - it contains ...on_login_success?origin=client&code=XXXX\n")
        prompt = "Paste the URL (or just the code): "
    try:
        webbrowser.open(url)
    except Exception:
        pass
    val = input(prompt).strip().strip('"')
    if store == "epic" and val.startswith("{"):
        val = json.loads(val).get("authorizationCode", "")
    if store == "gog" and "code=" in val:
        val = urllib.parse.parse_qs(urllib.parse.urlparse(val).query).get("code", [""])[0]
    if not val:
        raise SystemExit("No code entered.")
    c.login_with_code(val)
    print(f"\nLogged in to {store.upper()} as {c.display_name()}.")
    print(f"Session saved to {c.token_file} - treat this file like a password.")


def cmd_list(args):
    db = open_db(args.db)
    if args.store == "epic":
        epic = Epic(); epic.resume()
        lib = epic_library(epic, db, args.platforms)
    else:
        gog = GOG(); gog.resume()
        lib = gog_library(gog, db)
    for e in lib:
        tag = " [DLC]" if e["is_dlc"] else ""
        note = f" [{e['note']}]" if e.get("note") and e["note"] != "dlc" else ""
        plats = ", ".join(e["platforms"]) or "no Galaxy builds"
        print(f"{e['app_name']:40}  {e['title']}{tag}{note}  ({plats})")
    print(f"\n{len(lib)} items. Use the ID in the first column with 'Specific titles'.")


def cmd_collect(args):
    db = open_db(args.db)
    stores = STORES if args.store == "all" else (args.store,)
    totals = {}
    for s in stores:
        if args.store == "all" and not client(s).logged_in():
            print(f"{s.upper()}: not logged in, skipping.\n")
            continue
        print(f"\n===== {s.upper()} =====")
        totals[s] = collect_epic(db, args) if s == "epic" else collect_gog(db, args)
    print("\n" + "-" * 52)
    for s, c in totals.items():
        print(f"{s.upper():5} new: {c['new']}  unchanged: {c['same']}  skipped: {c['skipped']}  "
              f"errors: {c['errors']}")


def cmd_stats(args):
    db = open_db(args.db)
    print(f"{'':16}{'EPIC':>12}{'GOG':>12}{'TOTAL':>12}")
    rows = [
        ("Titles", "SELECT COUNT(DISTINCT app_name) FROM builds WHERE store {}"),
        ("Builds", "SELECT COUNT(*) FROM builds WHERE store {}"),
        ("File records", "SELECT COUNT(*) FROM files f JOIN builds b ON b.id=f.build_id WHERE b.store {}"),
        ("Unique SHA1", "SELECT COUNT(DISTINCT f.sha1) FROM files f JOIN builds b ON b.id=f.build_id WHERE b.store {}"),
        ("Unique MD5", "SELECT COUNT(DISTINCT f.md5) FROM files f JOIN builds b ON b.id=f.build_id WHERE b.store {}"),
        ("Unique SHA256", "SELECT COUNT(DISTINCT f.sha256) FROM files f JOIN builds b ON b.id=f.build_id WHERE b.store {}"),
        ("Errors logged", "SELECT COUNT(*) FROM errors WHERE store {}"),
    ]
    for label, q in rows:
        vals = [db.execute(q.format("= ?"), (s,)).fetchone()[0] for s in STORES]
        total = db.execute(q.format("IS NOT NULL OR 1")).fetchone()[0]
        print(f"{label:16}{vals[0]:>12,}{vals[1]:>12,}{total:>12,}")


def cmd_export(args):
    db = open_db(args.db)
    where = "" if args.store == "all" else "AND b.store = ?"
    params = () if args.store == "all" else (args.store,)
    cur = db.execute(
        "SELECT b.store, f.sha1, f.md5, f.sha256, f.size, f.path, b.title, b.app_name, b.platform,"
        " b.build_version, b.store_build_id, b.branch, f.languages, f.product_id, f.flags,"
        " b.manifest_sha1, b.collected_utc FROM files f JOIN builds b ON b.id=f.build_id"
        " WHERE f.symlink_to IS NULL AND f.size > 0"
        " AND COALESCE(f.sha1, f.md5, f.sha256) IS NOT NULL " + where, params)
    n = 0
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["store", "sha1", "md5", "sha256", "size", "path", "title", "app_id", "platform",
                    "build_version", "store_build_id", "branch", "languages", "product_id", "flags",
                    "manifest_sha1", "collected_utc"])
        for row in cur:
            w.writerow(row); n += 1
    print(f"Wrote {n:,} rows to {Path(args.out).resolve()}")
