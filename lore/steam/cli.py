"""Steam sign in (QR login, API key and profile) and collect (pull depot manifests game by game).
DepotDownloader saves the login token in the Windows user profile, so collect doesn't need the
QR code again until the token expires.

Everything also goes in Lore's log (see lore/log.py), including DepotDownloader's full output. On
screen, login shows DepotDownloader's output live (it includes the QR code) and the scan shows a
progress bar with only problems printed above it.
"""

import os
import shutil
import time

from .. import log as lorelog
from .. import ui
from ..export import steam_db_path
from ..log import log
from ..paths import files_dir
from . import config, db, depot_downloader, manifest_parser, steam_api

API_KEY_PAGE_URL = "https://steamcommunity.com/dev/apikey"

# Rate limits: wait a flat 20 minutes before each retry. Shorter waits often weren't enough and
# longer ones were excessive.
_RATE_LIMIT_WAIT = 1200
_RATE_LIMIT_MAX_RETRIES = 5
_INTER_GAME_DELAY = 5

# ---------------------------------------------------------------------- sign in
def _find_exe():
    exe_path = depot_downloader.find_exe()
    if not exe_path:
        raise RuntimeError("DepotDownloader.exe not found. This build is missing a file. "
                           "From source, run build.ps1 once first (see DEVELOPMENT.md).")
    return exe_path


def step_login(cfg):
    ui.info("Scan the QR code below with the Steam mobile app and approve the login.")
    ui.info("You won't be asked for your password.")
    ui.console.print()

    exe_path = _find_exe()
    retries = 0
    while True:
        captured = []

        def on_line(line, captured=captured):
            captured.append(line)
            log.debug(line)
            ui.console.print(f"  {line}", markup=False, soft_wrap=True)

        exit_code = depot_downloader.login_qr(exe_path, on_line=on_line)
        if exit_code == 0:
            break

        # The QR approval can succeed and the rest of the login still get rate limited. Wait the
        # same 20 minutes as the scan does; retrying straight away would hit the limit again.
        if depot_downloader.is_rate_limited(captured) and retries < _RATE_LIMIT_MAX_RETRIES:
            retries += 1
            ui.warn(f"Steam rate limited the login ({retries}/{_RATE_LIMIT_MAX_RETRIES}).")
            ui.countdown(_RATE_LIMIT_WAIT, "Retrying login")
            continue

        ui.error(f"Login failed (DepotDownloader exit code {exit_code}).")
        if not ui.confirm("Try again?"):
            raise RuntimeError("Steam sign in cancelled.")

    username = depot_downloader.account_name(captured)
    if not username:
        username = cfg.get("steam_username")
        if not username or not ui.confirm(f"Is your Steam login username {username}?"):
            username = ""
            while not username:
                username = ui.ask("Steam login username (not your display name)")
    ui.console.print()
    ui.ok(f"Logged in as {username}.")
    config.save({"steam_username": username})
    return exe_path, username


def _masked(key):
    return key[:4] + "..." + key[-4:] if len(key) > 8 else key


def step_fetch_games(cfg):
    ui.info("Lore needs a Steam Web API key and your profile to list your games.")
    ui.info(f"Get a free key at {API_KEY_PAGE_URL} (use localhost as the domain name).")
    while True:
        saved_key = cfg.get("api_key") or ""
        api_key = ui.ask("Steam Web API key", _masked(saved_key) if saved_key else None)
        if saved_key and api_key == _masked(saved_key):
            api_key = saved_key
        profile = ui.ask("Steam profile URL or SteamID64", cfg.get("steam_id64") or None)
        try:
            steam_id = steam_api.resolve_steam_id(profile, api_key)
            games = steam_api.get_owned_games(api_key, steam_id)
        except steam_api.SteamApiError as ex:
            ui.error(str(ex))
            log.debug("Fetching games failed", exc_info=True)
            cfg = {}  # don't offer the bad values as defaults again
            continue
        config.save({"api_key": api_key, "steam_id64": steam_id})
        return games


# ---------------------------------------------------------------------- collect
def _games_needing_scan(steam_db, games):
    """Works out what to scan. New games get a full scan. Partly covered games (a depot came back
    no_access, or an earlier run was interrupted) only ask for their missing depots. Fully
    covered games are skipped. Returns (to_scan, depot_ids_by_appid, scanned_before)."""
    overview = {g["appid"]: g for g in steam_db.games_overview()}
    scanned_before = {appid for appid, g in overview.items() if g["pulled_depot_count"] > 0}
    to_scan = []
    depot_ids_by_appid = {}
    for g in games:
        info = overview.get(g["appid"])
        if info is None or info["depot_count"] == 0:
            to_scan.append(g)
        elif info["pulled_depot_count"] < info["depot_count"]:
            to_scan.append(g)
            depot_ids_by_appid[g["appid"]] = steam_db.missing_depot_ids(g["appid"])
    return to_scan, depot_ids_by_appid, scanned_before


def _record_manifests(steam_db, manifest_files, appid, name, failures):
    new_runs = 0
    for manifest_file in manifest_files:
        try:
            depot_id, manifest_id, files = manifest_parser.parse_manifest_file(manifest_file)
        except manifest_parser.ManifestParseError as ex:
            failures.append((appid, name, str(ex)))
            continue
        steam_db.upsert_depot(depot_id, appid, os_="windows")
        if steam_db.has_manifest_run(depot_id, manifest_id):
            continue
        steam_db.insert_manifest_run(depot_id, manifest_id, files)
        new_runs += 1
    return new_runs


def _cleanup_dir(path):
    shutil.rmtree(path, ignore_errors=True)


def step_scan(exe_path, username, db_path, scratch_dir, games):
    steam_db = db.SteamDb(db_path)
    try:
        to_scan, depot_ids_by_appid, scanned_before = _games_needing_scan(steam_db, games)
        if not to_scan:
            ui.ok(f"All {len(games)} games already collected. Nothing to do.")
            return

        already = len(games) - len(to_scan)
        if already:
            ui.info(f"{already} game(s) already collected, skipping them.")
        ui.info(f"{len(to_scan)} game(s) to scan, about 10-20 seconds each. If Steam rate limits a "
                "request, Lore waits 20 minutes and retries.")
        ui.info("You can close this window at any time. Next run carries on where it stopped.")
        ui.console.print()

        new_runs = 0
        failures = []
        updated_games = []
        rate_limit_retries = {}
        stopped = False

        with ui.Bar("Steam", len(to_scan)) as bar:
            i = 0
            while i < len(to_scan):
                game = to_scan[i]
                appid, name = game["appid"], game["name"]
                bar.status(name)
                log.info(f"[{i + 1}/{len(to_scan)}] {name}")
                steam_db.upsert_game(appid, name)
                runs_before_this_game = new_runs

                app_scratch = os.path.join(scratch_dir, str(appid))
                captured = []

                def on_line(line, captured=captured):
                    captured.append(line)
                    log.debug(f"  {line}")

                try:
                    exit_code, manifest_files = depot_downloader.scan_app(
                        exe_path, appid, username, app_scratch, on_line=on_line,
                        depot_ids=depot_ids_by_appid.get(appid),
                    )
                except Exception as ex:  # noqa: BLE001 - keep going; this game just fails
                    failures.append((appid, name, str(ex)))
                    bar.note("error", f"{name}: {ex}")
                    bar.advance()
                    i += 1
                    continue

                for denied_id in depot_downloader.denied_depot_ids(captured):
                    steam_db.mark_depot_unavailable(denied_id, appid)

                if exit_code != 0:
                    _cleanup_dir(app_scratch)

                    if depot_downloader.needs_sign_in(captured):
                        stopped = True
                        break

                    if depot_downloader.is_rate_limited(captured):
                        retries = rate_limit_retries.get(appid, 0)
                        if retries < _RATE_LIMIT_MAX_RETRIES:
                            rate_limit_retries[appid] = retries + 1
                            log.info(f"  rate limited, waiting {_RATE_LIMIT_WAIT}s")
                            bar.wait(_RATE_LIMIT_WAIT, "Rate limited, retrying")
                            continue  # retry the same game (i not advanced)
                        failures.append((appid, name, "still rate limited, will retry next run"))
                        bar.note("warn", f"{name}: still rate limited, will retry next run")
                        rate_limit_retries.pop(appid, None)
                        bar.advance()
                        i += 1
                        continue

                    failures.append((appid, name, f"DepotDownloader exit code {exit_code}"))
                    bar.note("error", f"{name}: DepotDownloader exit code {exit_code}")
                    bar.advance()
                    i += 1
                    continue

                bar.status(name)
                time.sleep(_INTER_GAME_DELAY)

                n = _record_manifests(steam_db, manifest_files, appid, name, failures)
                new_runs += n
                log.info(f"  [OK] {n} depot(s) recorded" if manifest_files
                         else "  [OK] no Windows depots for this account")
                if appid in scanned_before and new_runs > runs_before_this_game:
                    updated_games.append(name)

                _cleanup_dir(app_scratch)
                bar.advance()
                i += 1

        ui.console.print()
        if stopped:
            ui.error("Steam sign in has expired. Choose Sign in > Steam, then collect again.")
        ui.ok(f"Steam: {new_runs} new manifest(s) recorded.")
        if updated_games:
            shown = ", ".join(updated_games[:20])
            more = f" and {len(updated_games) - 20} more" if len(updated_games) > 20 else ""
            ui.info(f"New builds for {len(updated_games)} game(s): {shown}{more}")
        if failures:
            ui.warn(f"{len(failures)} game(s) skipped. They'll be tried again next run.")
            for appid, name, reason in failures:
                log.info(f"  [{appid}] {name}: {reason}")
    finally:
        steam_db.close()


# ---------------------------------------------------------------------- entry points
def signed_in():
    """True if Steam sign in has been done (the saved login token itself can't be checked)."""
    cfg = config.load()
    return bool(cfg["steam_username"] and cfg["api_key"] and cfg["steam_id64"])


def _run(step):
    """Runs a step. Errors are shown with the log file path, never raised."""
    log_path = lorelog.start()
    try:
        step()
    except KeyboardInterrupt:
        ui.console.print()
        ui.warn("Stopped. Progress so far is saved.")
    except Exception as ex:  # noqa: BLE001 - so the packaged exe never closes without a message
        log.exception("Steam step failed")
        ui.error(str(ex))
        ui.info(f"Details in the log file: {log_path}")


def sign_in():
    """QR login, then the API key and profile. Saves all three for collect."""
    def step():
        step_login(config.load())
        games = step_fetch_games(config.load())
        ui.ok(f"Signed in to Steam. {len(games)} games in your library.")
    _run(step)


def collect():
    """Scans the library with the saved sign in."""
    if not signed_in():
        ui.warn("Not signed in to Steam. Choose Sign in > Steam first.")
        return

    def step():
        cfg = config.load()
        exe_path = _find_exe()
        with ui.console.status("Fetching your Steam library...", spinner_style=ui.TEAL):
            try:
                games = steam_api.get_owned_games(cfg["api_key"], cfg["steam_id64"])
            except steam_api.SteamApiError as ex:
                ui.error(str(ex))
                ui.info("Choose Sign in > Steam to check your API key and profile.")
                return
        step_scan(exe_path, cfg["steam_username"], str(steam_db_path()),
                  os.path.join(files_dir(), "manifests-scratch"), games)
    _run(step)
