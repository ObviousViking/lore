"""Steam support: the start-to-finish harvest flow. Runs start-to-finish in the
main thread — no server, no background threads, no browser UI: QR-login, fetch the library, pull
depot manifests game by game with live progress, export a CSV, optionally open the submission
form. Flow order deliberately matches how a coworker would actually want to move through this:
log in first (that's the slow/scary-looking step, get it out of the way), then everything else is
just waiting and pressing Enter.

Everything printed to the console also gets written, with more detail, to a timestamped log file
next to this program — see setup_logging(). During login, DepotDownloader's own output (including
the QR code itself) shows on the console live — there's little of it and all of it matters. During
the scan loop, by contrast, it's hundreds of lines per game and would bury the progress counter in
scroll, so there it goes to the log file only (DEBUG) and the console just gets a one-line
OK/SKIP/ERROR per game.
"""

import datetime
import logging
import os
import sys
import time
import webbrowser

from .. import __version__
from . import config, db, depot_downloader, manifest_parser, steam_api

APP_TITLE = "Lore - Steam"
SUBMISSION_FORM_URL = "https://forms.gle/4WBn3drZLu5fmNrY8"
API_KEY_PAGE_URL = "https://steamcommunity.com/dev/apikey"

# Flat 20-minute wait on every retry, no escalation — the original 15min/45min/60min escalating
# version (inherited from the original GUI prototype) ramped up too aggressively in practice: 15min
# often wasn't enough to clear the limit, and jumping straight to 45min+ after that was excessive.
# _RATE_LIMIT_BACKOFF_MULTIPLIER is 1 (not a typo) specifically to keep every retry at the same
# 20 minutes rather than growing; _RATE_LIMIT_MAX_BACKOFF is just a safety cap, unreachable at
# that multiplier.
_RATE_LIMIT_INITIAL_BACKOFF = 1200
_RATE_LIMIT_BACKOFF_MULTIPLIER = 1
_RATE_LIMIT_MAX_BACKOFF = 1200
_RATE_LIMIT_MAX_RETRIES = 5
_INTER_GAME_DELAY = 5

log = logging.getLogger("harvester")


def _configure_console_encoding():
    """Windows consoles default to a legacy codepage (often cp1252) for Python's stdout/stderr —
    fine for plain text, but it can't encode the QR code's Unicode block characters (█, U+2588)
    that DepotDownloader's captured output decodes to (see depot_downloader.py's
    _console_encoding — that handles *decoding* DepotDownloader's own OEM-codepage bytes
    correctly; this is the separate matter of *re-encoding* that already-correct text back out
    to our own console). Without this, relaying the QR code to the console crashes mid-login.
    Force UTF-8 at both the OS console level and in Python's stream wrapping, with "replace" as a
    last-resort fallback so an unusual terminal shows a replacement glyph instead of crashing.
    """
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except (AttributeError, OSError):
        pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def setup_logging():
    _configure_console_encoding()
    log_path = os.path.join(config.base_dir(), f"steam_log_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
    log.setLevel(logging.DEBUG)
    for h in list(log.handlers):  # running Steam twice from the menu shouldn't double up handlers
        h.close()
        log.removeHandler(h)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console_handler)

    return log_path


# ---------------------------------------------------------------------- small prompt helpers
def _prompt(text, default=None, secret_preview=False):
    if default:
        shown = default
        if secret_preview and len(default) > 8:
            shown = default[:4] + "..." + default[-4:]
        value = input(f"{text} [{shown}, Enter to keep]: ").strip()
    else:
        value = input(f"{text}: ").strip()
    return value or default or ""


def _confirm(text, default_yes=True):
    hint = "Y/n" if default_yes else "y/N"
    value = input(f"{text} [{hint}]: ").strip().lower()
    if not value:
        return default_yes
    return value in ("y", "yes")


# ---------------------------------------------------------------------- step: QR login
def step_login(cfg):
    log.info("=== Logging into Steam ===")
    log.info("Scan the QR code below with the Steam mobile app and approve the login.")
    log.info("(No password is ever typed into this tool.)\n")

    exe_path = depot_downloader.find_exe()
    if not exe_path:
        log.info("Couldn't find DepotDownloader.exe — this build is missing a required file.")
        log.info("See the README's maintainer setup section if you're running this from source.")
        sys.exit(1)
    depot_downloader.ensure_license_extracted()

    retries = 0
    while True:
        captured = []

        def on_line(line, captured=captured):
            captured.append(line)
            # info, not debug: unlike the scan loop (hundreds of lines per game — real clutter if
            # shown live), this is the QR code itself plus a couple of status lines. That's
            # exactly what needs to be on screen right now, not relegated to the log file only.
            log.info(line)

        exit_code = depot_downloader.login_qr(exe_path, on_line=on_line)
        if exit_code == 0:
            break

        # The QR approval itself can succeed (DepotDownloader even prints the account name at
        # that point) and *then* get rate-limited finishing the login handshake — same Steam
        # throttling the scan loop hits, so it gets the same flat-20min backoff instead of
        # immediately offering to retry, which would likely just hit the same limit again.
        if depot_downloader.is_rate_limited(captured) and retries < _RATE_LIMIT_MAX_RETRIES:
            retries += 1
            log.info(f"\nSteam rate-limited this login attempt ({retries}/{_RATE_LIMIT_MAX_RETRIES}).")
            _rate_limit_countdown(_RATE_LIMIT_INITIAL_BACKOFF, "login")
            log.info("")
            continue

        log.info(f"\nLogin didn't complete (DepotDownloader exited with code {exit_code}).")
        if not _confirm("Try again?"):
            sys.exit(1)
        log.info("")

    username = cfg.get("steam_username")
    if not username:
        log.info("\nLogin succeeded!")
        while not username:
            username = input("What's your Steam login username? (the one you just used — "
                              "not your display name): ").strip()
        config.save({"steam_username": username})
    else:
        log.info(f"\nLogin succeeded, as '{username}'.")

    return exe_path, username


# ---------------------------------------------------------------------- step: fetch games
def step_fetch_games(cfg):
    log.info("\n=== Your library ===")
    while True:
        api_key = _prompt("Steam Web API key (get one free at " + API_KEY_PAGE_URL + ")",
                           cfg.get("api_key"), secret_preview=True)
        profile = _prompt("Your Steam profile URL (or SteamID64)", cfg.get("steam_id64"))
        try:
            steam_id = steam_api.resolve_steam_id(profile, api_key)
            games = steam_api.get_owned_games(api_key, steam_id)
        except steam_api.SteamApiError as ex:
            log.info(f"That didn't work: {ex}")
            log.debug("Fetching games failed", exc_info=True)
            cfg = {}  # don't keep re-offering bad cached values as the default
            continue
        config.save({"api_key": api_key, "steam_id64": steam_id})
        log.info(f"Found {len(games)} games.")
        return games


# ---------------------------------------------------------------------- step: decide what to scan
def _games_needing_scan(harvester, games):
    """Combines what used to be separate "new games only" / "fill in missing depots" modes into
    one pass: a game never touched before gets full discovery, one that's partially covered (some
    depot came back no_access, or a prior run was interrupted) gets just its missing depots
    re-asked for. Nothing to pick between — the CLI doesn't ask, it just does whatever's needed to
    get everything fully covered."""
    overview = {g["appid"]: g for g in harvester.games_overview()}
    scanned_before = {appid for appid, g in overview.items() if g["pulled_depot_count"] > 0}
    to_scan = []
    depot_ids_by_appid = {}
    for g in games:
        info = overview.get(g["appid"])
        if info is None or info["depot_count"] == 0:
            to_scan.append(g)
        elif info["pulled_depot_count"] < info["depot_count"]:
            to_scan.append(g)
            depot_ids_by_appid[g["appid"]] = harvester.missing_depot_ids(g["appid"])
    return to_scan, depot_ids_by_appid, scanned_before


# ---------------------------------------------------------------------- step: scan
def _record_manifests(harvester, manifest_files, appid, name, failures):
    new_runs = 0
    for manifest_file in manifest_files:
        try:
            depot_id, manifest_id, files = manifest_parser.parse_manifest_file(manifest_file)
        except manifest_parser.ManifestParseError as ex:
            failures.append((appid, name, str(ex)))
            continue
        harvester.upsert_depot(depot_id, appid, os_="windows")
        if harvester.has_manifest_run(depot_id, manifest_id):
            continue
        harvester.insert_manifest_run(depot_id, manifest_id, files)
        new_runs += 1
    return new_runs


def _cleanup_dir(path):
    import shutil
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _rate_limit_countdown(seconds, what):
    """what: a phrase describing what's being retried, quoted by the caller if it's a proper
    name (e.g. "'Half-Life'") or left plain otherwise (e.g. "login")."""
    log.info(f"  Rate limited by Steam — waiting {seconds // 60}m{seconds % 60:02d}s before retrying "
              f"{what}...")
    remaining = seconds
    last_update = 0.0
    while remaining > 0:
        time.sleep(1)
        remaining -= 1
        now = time.monotonic()
        if now - last_update >= 30 or remaining == 0:
            mins, secs = divmod(remaining, 60)
            print(f"\r  ...{mins}m{secs:02d}s remaining    ", end="", flush=True)
            last_update = now
    print()


def step_scan(exe_path, username, db_path, scratch_dir, games):
    harvester = db.HarvesterDb(db_path)
    try:
        to_scan, depot_ids_by_appid, scanned_before = _games_needing_scan(harvester, games)

        if not to_scan:
            log.info("\nEverything's already fully scanned — nothing new to pull.")
            return

        already = len(games) - len(to_scan)
        log.info(f"\n=== Scanning depots ===")
        if already:
            log.info(f"{already} game(s) already fully covered from a previous run — skipping those.")
        log.info(f"Pulling {len(to_scan)} game(s). This only reads file lists (no game content is "
                  f"downloaded) and can take a while — roughly 10-20s per game, longer if Steam "
                  f"rate-limits a request (that's normal on a big library, not a bug; it backs off "
                  f"and retries on its own).")
        log.info("If you need to stop partway through, just close this window — closing it does "
                  "stop the scan, nothing keeps running once the window's gone, but nothing's lost "
                  "either: run this again later and already-scanned games are skipped "
                  "automatically, picking up only where you left off.\n")

        if not _confirm("Begin pulling depots?"):
            log.info("Okay, not scanning. Run this again when you're ready.")
            sys.exit(0)

        new_runs = 0
        failures = []
        updated_games = []
        rate_limit_retries = {}
        backoff = _RATE_LIMIT_INITIAL_BACKOFF

        i = 0
        total = len(to_scan)
        while i < total:
            game = to_scan[i]
            appid, name = game["appid"], game["name"]
            log.info(f"[{i + 1}/{total}] {name}")
            harvester.upsert_game(appid, name)
            runs_before_this_game = new_runs

            app_scratch = os.path.join(scratch_dir, str(appid))
            captured = []

            def on_line(line, captured=captured):
                captured.append(line)
                log.debug(f"  {line}")

            depot_ids = depot_ids_by_appid.get(appid)
            try:
                exit_code, manifest_files = depot_downloader.scan_app(
                    exe_path, appid, username, app_scratch, on_line=on_line, depot_ids=depot_ids,
                )
            except Exception as ex:  # noqa: BLE001 - keep the loop going; this game just fails
                failures.append((appid, name, str(ex)))
                log.info(f"  [ERROR] {ex}")
                i += 1
                continue

            for denied_id in depot_downloader.denied_depot_ids(captured):
                harvester.mark_depot_unavailable(denied_id, appid)

            if exit_code != 0:
                _cleanup_dir(app_scratch)

                if depot_downloader.is_rate_limited(captured):
                    retries = rate_limit_retries.get(appid, 0)
                    if retries < _RATE_LIMIT_MAX_RETRIES:
                        rate_limit_retries[appid] = retries + 1
                        _rate_limit_countdown(backoff, f"'{name}'")
                        backoff = min(backoff * _RATE_LIMIT_BACKOFF_MULTIPLIER, _RATE_LIMIT_MAX_BACKOFF)
                        continue  # retry the same game — don't advance i
                    failures.append((appid, name, f"still rate limited after {_RATE_LIMIT_MAX_RETRIES} "
                                                   "retries — run this again later to pick it up"))
                    log.info("  [SKIP] still rate limited after several retries — will pick this up "
                             "on the next run")
                    rate_limit_retries.pop(appid, None)
                    i += 1
                    continue

                failures.append((appid, name, f"DepotDownloader exited with code {exit_code}"))
                log.info(f"  [ERROR] DepotDownloader exited with code {exit_code}")
                i += 1
                continue

            backoff = _RATE_LIMIT_INITIAL_BACKOFF
            time.sleep(_INTER_GAME_DELAY)

            if not manifest_files:
                log.info("  [OK] nothing to pull (no depots for this account/platform)")
                _cleanup_dir(app_scratch)
                i += 1
                continue

            new_runs_this_game = _record_manifests(harvester, manifest_files, appid, name, failures)
            new_runs += new_runs_this_game
            log.info(f"  [OK] {new_runs_this_game} depot(s) recorded")

            if appid in scanned_before and new_runs > runs_before_this_game:
                updated_games.append(name)

            _cleanup_dir(app_scratch)
            i += 1

        log.info(f"\nScan finished. {new_runs} new manifest run(s) recorded.")
        if updated_games:
            shown = updated_games[:20]
            more = f" ...and {len(updated_games) - 20} more" if len(updated_games) > 20 else ""
            log.info(f"New depot data found for {len(updated_games)} game(s) already on record: "
                     f"{', '.join(shown)}{more}")
        if failures:
            log.info(f"{len(failures)} app(s) skipped:")
            for appid, name, reason in failures:
                log.info(f"  [{appid}] {name}: {reason}")
    finally:
        harvester.close()


# ---------------------------------------------------------------------- step: export
def step_export(db_path):
    log.info("\n=== Exporting ===")
    harvester = db.HarvesterDb(db_path)
    try:
        filename = f"hashes_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv"
        out_path = os.path.join(config.base_dir(), filename)
        count = harvester.export_csv(out_path)
    finally:
        harvester.close()
    log.info(f"Wrote {count:,} hash sighting(s) to {out_path}")
    return out_path


# ---------------------------------------------------------------------- entry point
def run():
    """Login -> fetch library -> scan -> export -> offer the submission form."""
    log_path = setup_logging()

    print(f"{APP_TITLE} {__version__}")
    print("=" * len(APP_TITLE))
    print("Builds an NSRL-style known-hash set from your Steam library's depot manifests.")
    print("Nothing is uploaded automatically — this reads file lists only (no game content is")
    print("downloaded) and writes one CSV for you to submit yourself at the end.")
    print(f"Log file: {log_path}\n")

    cfg = config.load()
    db_path = os.path.join(config.base_dir(), "hashharvester.db")
    scratch_dir = os.path.join(config.base_dir(), "manifests-scratch")

    try:
        exe_path, username = step_login(cfg)
        cfg = config.load()  # pick up the username step_login may have just saved
        games = step_fetch_games(cfg)
        step_scan(exe_path, username, db_path, scratch_dir, games)
        out_path = step_export(db_path)

        print()
        if _confirm("Open the submission form to upload your CSV now?"):
            webbrowser.open(SUBMISSION_FORM_URL)
            print("Upload this file through the page that just opened:")
            print(f"  {out_path}")
        else:
            print(f"No problem — that file's waiting whenever you're ready: {out_path}")
    except KeyboardInterrupt:
        log.info("\n\nCancelled.")
        sys.exit(1)
    except Exception as ex:  # noqa: BLE001 - last resort so a packaged build never dies silently
        log.debug("Unhandled error", exc_info=True)
        print(f"\nSomething went wrong: {ex}")
        print(f"Full details were written to the log file: {log_path}")
        sys.exit(1)
