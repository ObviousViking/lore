"""Lore - known-file hash sets from Steam, Epic Games Store and GOG manifests.

Only manifests are downloaded, never game files.

Usage:
    lore                                  # interactive menu
    lore steam                            # Steam: QR login, scan library, export CSV
    lore auth --store epic|gog            # one-off login
    lore list --store epic|gog
    lore collect [--store epic|gog|all] [--limit N] [--only ID ...]
                 [--platforms Windows Mac] [--gog-builds latest|all]
    lore stats
    lore export out.csv [--store epic|gog|all]
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .epic_gog import (GOG, STORES, Epic, cmd_auth, cmd_collect, cmd_export, cmd_list, cmd_stats,
                       default_db, open_db)


def run_steam(args=None):
    from .steam import cli
    cli.run()


# --------------------------------------------------------------------------- interactive menu

def _ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    v = input(f"{prompt}{suffix}: ").strip()
    return v if v else (default if default is not None else "")


def _status(c):
    if not c.token_file.exists():
        return "not logged in"
    try:
        c.session = json.loads(c.token_file.read_text())
        return f"logged in as {c.display_name()}"
    except Exception:
        return "session unreadable - log in again"


def _steam_status():
    from .steam import config
    name = config.load().get("steam_username")
    return f"logged in previously as {name}" if name else "not set up yet"


def _choose_store(allow_all=False):
    opts = "1) Epic   2) GOG" + ("   3) Both" if allow_all else "")
    print(f"\n  Store:  {opts}")
    c = _ask("  Choose", "3" if allow_all else "1")
    return {"1": "epic", "2": "gog", "3": "all" if allow_all else "epic"}.get(c, "epic")


def _choose_platforms():
    print("\n  Platforms:  1) Windows   2) Windows + Mac   3) Mac only")
    return {"2": ["Windows", "Mac"], "3": ["Mac"]}.get(_ask("  Choose", "1"), ["Windows"])


def _ns(db, **kw):
    base = dict(db=db, platforms=["Windows"], limit=None, only=None, delay=1.0,
                manifest_dir="manifests", include_third_party=False, gog_builds="latest", store="epic")
    base.update(kw)
    return argparse.Namespace(**base)


def _collect_menu(db):
    store = _choose_store(allow_all=True)
    print("\n  Collect\n  -------")
    print("  1) All titles")
    print("  2) Limit - first N titles (testing)")
    print("  3) Specific titles by ID (see List)")
    print("  0) Back")
    c = _ask("  Choose", "1")
    if c == "0":
        return
    a = _ns(db, store=store, platforms=_choose_platforms())
    if store in ("gog", "all"):
        print("\n  GOG builds:  1) Latest only   2) All builds (full version history - much slower first run)")
        a.gog_builds = "all" if _ask("  Choose", "1") == "2" else "latest"
    if c == "2":
        try:
            a.limit = int(_ask("  How many titles (per store)", "5"))
        except ValueError:
            print("  Not a number."); return
    elif c == "3":
        ids = _ask("  ID(s), space separated")
        if not ids:
            return
        a.only = ids.split()
    cmd_collect(a)


def _clear_menu(db):
    print("\n  Clear cache (Epic / GOG)\n  ------------------------")
    print("  1) Title cache        (re-fetch titles/metadata next run)")
    print("  2) Error log")
    print("  3) Log out of Epic")
    print("  4) Log out of GOG")
    print("  5) GOG depot cache    (re-download shared GOG depot manifests)")
    print("  6) EVERYTHING         (database, saved manifests, both logins - full reset)")
    print("  0) Back")
    c = _ask("  Choose", "0")
    if c == "1":
        d = open_db(db); n = d.execute("DELETE FROM catalog").rowcount; d.commit()
        print(f"  Cleared {n} cached titles.")
    elif c == "2":
        d = open_db(db); n = d.execute("DELETE FROM errors").rowcount; d.commit()
        print(f"  Cleared {n} error records.")
    elif c in ("3", "4"):
        cl = Epic() if c == "3" else GOG()
        if cl.token_file.exists():
            cl.token_file.unlink(); print("  Logged out.")
        else:
            print("  Not logged in.")
    elif c == "5":
        shutil.rmtree(Path("manifests") / "gog" / "depots", ignore_errors=True)
        print("  GOG depot cache cleared.")
    elif c == "6":
        print("  This deletes ALL collected Epic/GOG hashes, build history and saved manifests.")
        if _ask("  Type DELETE to confirm") == "DELETE":
            if Path(db).exists():
                Path(db).unlink()
            shutil.rmtree("manifests", ignore_errors=True)
            for cl in (Epic(), GOG()):
                if cl.token_file.exists():
                    cl.token_file.unlink()
            print("  Everything cleared.")
        else:
            print("  Cancelled.")


def interactive(db):
    actions = {
        "1": ("Steam - log in, scan library, export CSV", run_steam),
        "2": ("Epic/GOG - log in", lambda: cmd_auth(_ns(db, store=_choose_store()))),
        "3": ("Epic/GOG - list library", lambda: (lambda s: cmd_list(_ns(
            db, store=s, platforms=_choose_platforms() if s == "epic" else ["Windows"])))(_choose_store())),
        "4": ("Epic/GOG - collect hashes", lambda: _collect_menu(db)),
        "5": ("Epic/GOG - stats", lambda: cmd_stats(_ns(db))),
        "6": ("Epic/GOG - export CSV", lambda: (lambda s: cmd_export(_ns(
            db, store=s, out=_ask("  Output file", f"{s}_hashes_{datetime.now():%Y%m%d}.csv"
                                  .replace("all_", "epic_gog_")))))(_choose_store(allow_all=True))),
        "7": ("Epic/GOG - clear cache", lambda: _clear_menu(db)),
    }
    while True:
        print("\n" + "=" * 60)
        print(f"  Lore {__version__} - game file hashes from store manifests")
        print(f"  Steam: {_steam_status()}")
        print(f"  Epic:  {_status(Epic())}")
        print(f"  GOG:   {_status(GOG())}")
        print(f"  Epic/GOG database: {Path(db).resolve()}")
        print("=" * 60)
        for k, (label, _) in actions.items():
            print(f"  {k}) {label}")
        print("  0) Exit")
        c = _ask("\nChoose")
        if c in ("0", "q", "exit"):
            return
        if c not in actions:
            print("Invalid choice."); continue
        try:
            actions[c][1]()
        except SystemExit as e:
            if isinstance(e.code, str):
                print(e.code)
        except KeyboardInterrupt:
            print("\nInterrupted - progress so far is saved.")
        except Exception as e:
            print(f"\nError: {e}")
        input("\nPress Enter to return to the menu...")


# --------------------------------------------------------------------------- command line

def _utf8_output():
    """Game titles aren't ASCII-safe; without this, redirected output or a legacy console
    codepage (cp1252) crashes on the first title containing e.g. a Greek letter."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main():
    _utf8_output()
    ap = argparse.ArgumentParser(prog="lore", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"lore {__version__}")
    ap.add_argument("--db", default=None, help="Epic/GOG database path (default: ./lore.sqlite)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help="Epic/GOG database path")
    sub = ap.add_subparsers(dest="cmd")
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[common], **k)

    sub.add_parser("steam", help="Steam: QR login, scan library, export CSV").set_defaults(func=run_steam)

    s = sub.add_parser("auth", help="Epic/GOG: log in (one-off per store)")
    s.add_argument("--store", choices=STORES, default="epic")
    s.set_defaults(func=cmd_auth)

    s = sub.add_parser("list", help="Epic/GOG: list library")
    s.add_argument("--store", choices=STORES, default="epic")
    s.add_argument("--platforms", nargs="+", default=["Windows"], choices=["Windows", "Mac"])
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("collect", help="Epic/GOG: snapshot manifests")
    s.add_argument("--store", choices=STORES + ("all",), default="all")
    s.add_argument("--platforms", nargs="+", default=["Windows"], choices=["Windows", "Mac"])
    s.add_argument("--gog-builds", choices=["latest", "all"], default="latest")
    s.add_argument("--limit", type=int, help="first N titles per store (testing)")
    s.add_argument("--only", nargs="+", help="specific app name(s) / GOG product ID(s)")
    s.add_argument("--delay", type=float, default=1.0, help="seconds between API requests (default 1)")
    s.add_argument("--manifest-dir", default="manifests")
    s.add_argument("--include-third-party", action="store_true", help="Epic: try Ubisoft/EA titles too")
    s.set_defaults(func=cmd_collect)

    sub.add_parser("stats", help="Epic/GOG: database summary").set_defaults(func=cmd_stats)

    s = sub.add_parser("export", help="Epic/GOG: export hashes to CSV")
    s.add_argument("out")
    s.add_argument("--store", choices=STORES + ("all",), default="all")
    s.set_defaults(func=cmd_export)

    args = ap.parse_args()
    args.db = args.db or default_db()
    if not args.cmd:
        interactive(args.db)
        return
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted - progress so far is saved.")
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"Error: {e}")
