"""The Steam run: QR login, fetch the library, pull depot manifests game by game, export a CSV,
then offer the submission form. Login comes first so the rest is just waiting.

Console output also goes to a timestamped log file in lore_files (see setup_logging). During
login DepotDownloader's output, including the QR code, is shown live. During the scan it's
hundreds of lines per game, so it goes to the log only and the console shows one OK/SKIP/ERROR
line per game.
"""

import datetime
import logging
import os
import sys
import time
import webbrowser

from .. import __version__
from ..paths import files_dir
from . import config, db, depot_downloader, manifest_parser, steam_api

APP_TITLE = "Lore - Steam"
SUBMISSION_FORM_URL = "https://forms.gle/4WBn3drZLu5fmNrY8"
API_KEY_PAGE_URL = "https://steamcommunity.com/dev/apikey"

# Rate limits: wait a flat 20 minutes before each retry. Shorter waits often weren't enough and
# longer ones were excessive. The multiplier is 1 on purpose so the wait doesn't grow; the max is
# just a cap.
_RATE_LIMIT_INITIAL_BACKOFF = 1200
_RATE_LIMIT_BACKOFF_MULTIPLIER = 1
_RATE_LIMIT_MAX_BACKOFF = 1200
_RATE_LIMIT_MAX_RETRIES = 5
_INTER_GAME_DELAY = 5

log = logging.getLogger("harvester")


def _configure_console_encoding():
    """Switches the console and Python's streams to UTF-8. The Windows default codepage (often
    cp1252) can't print the QR code's block characters (U+2588), which would crash login.
    Unprintable characters are replaced rather than raising."""
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
    log_path = os.path.join(files_dir(), f"steam_log_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
    log.setLevel(logging.DEBUG)
    for h in list(log.handlers):  # avoid duplicate handlers if Steam is run twice from the menu
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
    log.info("You won't be asked for your password.\n")

    exe_path = depot_downloader.find_exe()
    if not exe_path:
        log.info("DepotDownloader.exe not found. This build is missing a file.")
        log.info("From source, run build.ps1 once first (see Development in the README).")
        sys.exit(1)
    depot_downloader.ensure_license_extracted()

    retries = 0
    while True:
        captured = []

        def on_line(line, captured=captured):
            captured.append(line)
            # info, not debug: this is the QR code and a few status lines, so show it live.
            log.info(line)

        exit_code = depot_downloader.login_qr(exe_path, on_line=on_line)
        if exit_code == 0:
            break

        # The QR approval can succeed and the rest of the login still get rate limited. Wait the
        # same 20 minutes as the scan does; retrying straight away would hit the limit again.
        if depot_downloader.is_rate_limited(captured) and retries < _RATE_LIMIT_MAX_RETRIES:
            retries += 1
            log.info(f"\nSteam rate limited the login ({retries}/{_RATE_LIMIT_MAX_RETRIES}).")
            _rate_limit_countdown(_RATE_LIMIT_INITIAL_BACKOFF, "login")
            log.info("")
            continue

        log.info(f"\nLogin failed (DepotDownloader exit code {exit_code}).")
        if not _confirm("Try again?"):
            sys.exit(1)
        log.info("")

    username = cfg.get("steam_username")
    if not username:
        log.info("\nLogged in.")
        while not username:
            username = input("Steam login username (not your display name): ").strip()
        config.save({"steam_username": username})
    else:
        log.info(f"\nLogged in as {username}.")

    return exe_path, username


# ---------------------------------------------------------------------- step: fetch games
def step_fetch_games(cfg):
    log.info("\n=== Your library ===")
    while True:
        api_key = _prompt("Steam Web API key (free from " + API_KEY_PAGE_URL + ")",
                           cfg.get("api_key"), secret_preview=True)
        profile = _prompt("Steam profile URL or SteamID64", cfg.get("steam_id64"))
        try:
            steam_id = steam_api.resolve_steam_id(profile, api_key)
            games = steam_api.get_owned_games(api_key, steam_id)
        except steam_api.SteamApiError as ex:
            log.info(f"Error: {ex}")
            log.debug("Fetching games failed", exc_info=True)
            cfg = {}  # don't offer the bad values as defaults again
            continue
        config.save({"api_key": api_key, "steam_id64": steam_id})
        log.info(f"Found {len(games)} games.")
        return games


# ---------------------------------------------------------------------- step: decide what to scan
def _games_needing_scan(harvester, games):
    """Works out what to scan. New games get a full scan. Partly covered games (a depot came back
    no_access, or an earlier run was interrupted) only ask for their missing depots. Fully
    covered games are skipped. Returns (to_scan, depot_ids_by_appid, scanned_before)."""
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
    """Waits `seconds` with a countdown. what: what's being retried, e.g. "'Half-Life'" or
    "login"."""
    log.info(f"  Rate limited by Steam. Retrying {what} in {seconds // 60}m{seconds % 60:02d}s...")
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
            log.info("\nAll games already scanned. Nothing to do.")
            return

        already = len(games) - len(to_scan)
        log.info(f"\n=== Scanning depots ===")
        if already:
            log.info(f"Skipping {already} game(s) already scanned.")
        log.info(f"{len(to_scan)} game(s) to scan, about 10-20 seconds each. Only file lists are "
                 f"downloaded, not games.")
        log.info("If Steam rate limits a request, it waits 20 minutes and retries.")
        log.info("You can close this window at any time. Next run carries on where it stopped.\n")

        if not _confirm("Start scanning?"):
            log.info("Not scanning.")
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
                        continue  # retry the same game (i not advanced)
                    failures.append((appid, name, f"still rate limited after {_RATE_LIMIT_MAX_RETRIES} "
                                                   "retries, will retry next run"))
                    log.info("  [SKIP] still rate limited, will retry next run")
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
                log.info("  [OK] no Windows depots for this account")
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

        log.info(f"\nScan finished. {new_runs} new manifest(s) recorded.")
        if updated_games:
            shown = updated_games[:20]
            more = f" ...and {len(updated_games) - 20} more" if len(updated_games) > 20 else ""
            log.info(f"New data for {len(updated_games)} previously scanned game(s): "
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
        out_path = os.path.join(files_dir(), filename)
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
    print("Builds a known-file hash set from your Steam library's depot manifests.")
    print("Only file lists are downloaded, not games. Nothing is uploaded; you submit the CSV")
    print("yourself at the end.")
    print(f"Log file: {log_path}\n")

    cfg = config.load()
    db_path = os.path.join(files_dir(), "hashharvester.db")
    scratch_dir = os.path.join(files_dir(), "manifests-scratch")

    try:
        exe_path, username = step_login(cfg)
        cfg = config.load()  # step_login may have just saved the username
        games = step_fetch_games(cfg)
        step_scan(exe_path, username, db_path, scratch_dir, games)
        out_path = step_export(db_path)

        print()
        if _confirm("Open the submission form now?"):
            webbrowser.open(SUBMISSION_FORM_URL)
            print("Upload this file on the form:")
            print(f"  {out_path}")
        else:
            print(f"CSV saved to: {out_path}")
    except KeyboardInterrupt:
        log.info("\n\nCancelled.")
        sys.exit(1)
    except Exception as ex:  # noqa: BLE001 - so the packaged exe never closes without a message
        log.debug("Unhandled error", exc_info=True)
        print(f"\nError: {ex}")
        print(f"Details in the log file: {log_path}")
        sys.exit(1)
