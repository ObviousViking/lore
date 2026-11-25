"""Amazon Games (Prime Gaming) support: sign in, library and manifest download/parsing. Only
manifests are downloaded, never game files. Each manifest lists every file with its SHA256.

Builds are stored in the same SQLite tables as Epic and GOG (store = 'amazon'). Protocol details
are based on nile (github.com/imLinguin/nile), the Amazon backend of Heroic Games Launcher.
"""

import base64
import hashlib
import json
import lzma
import os
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path

from . import ui
from .epic_gog import DATA_DIR, insert_build, log_error, open_db

DEVICE_TYPE = "A2UMVHOX7UP4V7"
MARKETPLACE_ID = "ATVPDKIKX0DER"
APP_NAME = "AGSLauncher for Windows"
APP_VERSION = "1.0.0"
USER_AGENT = "AGSLauncher/1.0.0"
LAUNCHER_AGENT = "com.amazon.agslauncher.win/3.0.9202.1"
ENTITLEMENTS_URL = "https://gaming.amazon.com/api/distribution/entitlements"
DISTRIBUTION_URL = "https://gaming.amazon.com/api/distribution/v2/public"
TARGET = "com.amazon.animusdistributionservice"


class AmazonError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status  # HTTP status, or None for network errors


def _request(url, body=None, headers=None, raw=False, retries=3):
    """POSTs JSON when body is given, else GETs. Retries server errors and network drops."""
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                content = r.read()
                return content if raw else json.loads(content)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            detail = e.read()[:300].decode(errors="replace")
            raise AmazonError(f"HTTP {e.code} for {url.split('?')[0]}: {detail}", e.code) from None
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise AmazonError(f"Network error for {url.split('?')[0]}: {e.reason}") from None


# --------------------------------------------------------------------------- sign in

class Amazon:
    token_file = DATA_DIR / "amazon_session.json"

    def __init__(self):
        self.session = None

    def logged_in(self):
        return self.token_file.exists()

    def load(self):
        self.session = json.loads(self.token_file.read_text())
        return self.session

    def _save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(self.session))
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def display_name(self):
        return (self.session or {}).get("name") or "?"

    def start_login(self):
        """Returns (login URL, pending state) for finish_login."""
        serial = uuid.uuid1().hex.upper()
        client_id = f"{serial}#{DEVICE_TYPE}".encode("ascii").hex()
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=")
        params = {
            "openid.ns": "http://specs.openid.net/auth/2.0",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.mode": "checkid_setup",
            "openid.oa2.scope": "device_auth_access",
            "openid.ns.oa2": "http://www.amazon.com/ap/ext/oauth/2",
            "openid.oa2.response_type": "code",
            "openid.oa2.code_challenge_method": "S256",
            "openid.oa2.client_id": f"device:{client_id}",
            "language": "en_US",
            "marketPlaceId": MARKETPLACE_ID,
            "openid.return_to": "https://www.amazon.com",
            "openid.pape.max_auth_age": 0,
            "openid.assoc_handle": "amzn_sonic_games_launcher",
            "pageId": "amzn_sonic_games_launcher",
            "openid.oa2.code_challenge": challenge.decode(),
        }
        url = "https://amazon.com/ap/signin?" + urllib.parse.urlencode(params)
        return url, {"serial": serial, "client_id": client_id, "verifier": verifier.decode()}

    def finish_login(self, redirect_url, pending):
        """Registers this install as a device using the code from the redirect URL."""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query)
        code = (query.get("openid.oa2.authorization_code") or [""])[0]
        if not code:
            raise AmazonError("That URL has no sign-in code. Copy the full address of the page "
                              "you end up on after signing in.")
        res = _request("https://api.amazon.com/auth/register", body={
            "auth_data": {
                "authorization_code": code,
                "client_domain": "DeviceLegacy",
                "client_id": pending["client_id"],
                "code_algorithm": "SHA-256",
                "code_verifier": pending["verifier"],
                "use_global_authentication": False,
            },
            "registration_data": {
                "app_name": APP_NAME,
                "app_version": APP_VERSION,
                "device_model": "Windows",
                "device_name": None,
                "device_serial": pending["serial"],
                "device_type": DEVICE_TYPE,
                "domain": "Device",
                "os_version": "10.0.19044.0",
            },
            "requested_extensions": ["customer_info", "device_info"],
            "requested_token_type": ["bearer", "mac_dms"],
            "user_context_map": {},
        })
        ok = res["response"]["success"]
        bearer = ok["tokens"]["bearer"]
        self.session = {
            "access_token": bearer["access_token"],
            "refresh_token": bearer["refresh_token"],
            "expires_at": time.time() + int(bearer["expires_in"]),
            "device_serial": ok["extensions"]["device_info"]["device_serial_number"],
            "name": ok["extensions"]["customer_info"].get("given_name") or "?",
        }
        self._save()

    def resume(self):
        """Loads the saved sign in, refreshing the access token if it has expired."""
        if not self.logged_in():
            raise SystemExit("Not signed in to Amazon. Choose Sign in in the menu, or run: "
                             "lore auth --store amazon")
        self.load()
        if time.time() > self.session["expires_at"] - 60:
            try:
                res = _request("https://api.amazon.com/auth/token", body={
                    "source_token": self.session["refresh_token"],
                    "source_token_type": "refresh_token",
                    "requested_token_type": "access_token",
                    "app_name": APP_NAME,
                    "app_version": APP_VERSION,
                })
            except AmazonError as e:
                raise SystemExit(f"Amazon sign in has expired ({e}). Sign in to Amazon "
                                 "again.")
            self.session["access_token"] = res["access_token"]
            self.session["expires_at"] = time.time() + int(res["expires_in"])
            self._save()

    def _distribution(self, operation, body):
        url = ENTITLEMENTS_URL if operation == "GetEntitlements" else DISTRIBUTION_URL
        service = ("entitlement.AnimusEntitlementsService" if operation == "GetEntitlements"
                   else "external.AnimusDistributionService")
        return _request(url, body={"Operation": operation, **body}, headers={
            "X-Amz-Target": f"{TARGET}.{service}.{operation}",
            "x-amzn-token": self.session["access_token"],
            "UserAgent": LAUNCHER_AGENT,
            "Content-Encoding": "amz-1.0",
        })

    def library(self):
        """Owned games, one entitlement per product."""
        games, next_token = {}, None
        hardware_hash = hashlib.sha256(self.session["device_serial"].encode()).hexdigest().upper()
        while True:
            res = self._distribution("GetEntitlements", {
                "clientId": "Sonic",
                "syncPoint": None,
                "nextToken": next_token,
                "maxResults": 50,
                "productIdFilter": None,
                "keyId": "d5dc8b8b-86c8-4fc4-ae93-18c0def5314d",
                "hardwareHash": hardware_hash,
            })
            for ent in res.get("entitlements", []):
                product = ent.get("product") or {}
                if product.get("id") and product["id"] not in games:
                    games[product["id"]] = {"entitlement_id": ent["id"], "product_id": product["id"],
                                            "title": product.get("title") or product["id"]}
            next_token = res.get("nextToken")
            if not next_token:
                break
        return sorted(games.values(), key=lambda g: g["title"].lower())

    def download_info(self, entitlement_id):
        """(versionId, downloadUrl) for the game's current build."""
        res = self._distribution("GetGameDownload", {"entitlementId": entitlement_id})
        return res["versionId"], res["downloadUrl"]


def manifest_url(download_url):
    u = urllib.parse.urlparse(download_url)
    return urllib.parse.urlunparse(u._replace(path=u.path + "/manifest.proto"))


# --------------------------------------------------------------------------- manifest parsing

def _varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def _fields(buf):
    """Decodes one protobuf message into {field number: [values]}. Length-delimited values stay
    bytes; varints are ints."""
    out, i = {}, 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            n, i = _varint(buf, i)
            value, i = buf[i:i + n], i + n
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        out.setdefault(field, []).append(value)
    return out


def parse_manifest(data):
    """Returns the files in an Amazon manifest.proto: [{path, size, sha256}].

    Layout: 4-byte big-endian header size, ManifestHeader (field 1: compression, 1 = lzma), then
    the Manifest body: packages (1) > files (2) > path (1), size (3), hash (5: algorithm 1,
    value 2; algorithm 0 = sha256)."""
    header_size = struct.unpack(">I", data[:4])[0]
    header = _fields(data[4:4 + header_size])
    body = data[4 + header_size:]
    compression = _fields(header[1][0]).get(1, [0])[0] if 1 in header else 0
    if compression == 1:
        body = lzma.decompress(body)
    elif compression != 0:
        raise ValueError(f"unknown manifest compression {compression}")

    files = []
    for package in _fields(body).get(1, []):
        for raw in _fields(package).get(2, []):
            f = _fields(raw)
            h = _fields(f[5][0]) if 5 in f else {}
            sha256 = h[2][0].hex() if h.get(1, [0])[0] == 0 and 2 in h else None
            files.append(dict(path=f.get(1, [b""])[0].decode("utf-8", "replace").replace("\\", "/"),
                              size=f.get(3, [0])[0], sha1=None, md5=None, sha256=sha256,
                              executable=0, symlink=None))
    return files


# --------------------------------------------------------------------------- commands

def cmd_auth(args):
    amazon = Amazon()
    url, pending = amazon.start_login()
    ui.info("Sign in to Amazon in the browser window that opens. If it doesn't open, use this "
            "link:")
    ui.console.print(f"    {url}", style=ui.DIM, markup=False, soft_wrap=True)
    ui.info("After signing in you land on amazon.com. Copy the full URL from the address bar.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    redirect = ui.ask("Paste the URL").strip('"')
    if not redirect:
        raise SystemExit("No URL entered.")
    amazon.finish_login(redirect, pending)
    ui.ok(f"Signed in to Amazon as {amazon.display_name()}.")


def cmd_collect(args):
    db = open_db(args.db)
    ui.heading("Amazon")
    c = collect_amazon(db, args)
    ui.ok(f"Amazon: {c['new']} new build(s), {c['same']} unchanged, {c['skipped']} not downloadable")
    if c["errors"]:
        ui.warn(f"{c['errors']} error(s). They'll be tried again next run.")


def collect_amazon(db, args):
    amazon = Amazon()
    amazon.resume()
    mdir = Path(args.manifest_dir) / "amazon"
    mdir.mkdir(parents=True, exist_ok=True)
    with ui.console.status("Fetching your Amazon library...", spinner_style=ui.TEAL):
        lib = amazon.library()
    if args.only:
        lib = [g for g in lib if g["product_id"] in args.only]
    if args.limit:
        lib = lib[: args.limit]
    c = dict(new=0, same=0, skipped=0, errors=0)
    with ui.Bar("Amazon", len(lib)) as bar:
        for g in lib:
            bar.status(g["title"])
            try:
                version, download_url = amazon.download_info(g["entitlement_id"])
                time.sleep(args.delay)
            except AmazonError as ex:
                log_error(db, "amazon", g["product_id"], "Windows", ex)
                if ex.status and 400 <= ex.status < 500 and ex.status != 429:
                    c["skipped"] += 1  # not downloadable through Amazon, e.g. a key for another store
                else:
                    c["errors"] += 1
                    bar.note("error", f"{g['title']}: {ex}")
                bar.advance()
                continue
            if db.execute("SELECT 1 FROM builds WHERE store='amazon' AND app_name=? AND "
                          "store_build_id=?", (g["product_id"], version)).fetchone():
                c["same"] += 1
                bar.advance()
                continue
            try:
                data = _request(manifest_url(download_url), raw=True)
                files = parse_manifest(data)
            except Exception as ex:
                c["errors"] += 1
                bar.note("error", f"{g['title']}: {ex}")
                log_error(db, "amazon", g["product_id"], "Windows", ex)
                bar.advance()
                continue
            m_sha1 = hashlib.sha1(data).hexdigest()
            m_path = mdir / f"{g['product_id']}_{version}.proto"
            m_path.write_bytes(data)
            ok = insert_build(db, "amazon", dict(
                app_name=g["product_id"], title=g["title"], platform="Windows",
                build_version=version, manifest_sha1=m_sha1, manifest_path=str(m_path),
                account_id="", store_build_id=version), files)
            c["new" if ok else "same"] += 1
            time.sleep(args.delay)
            bar.advance()
    return c
