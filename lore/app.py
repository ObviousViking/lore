"""Lore - known-file hash sets from Steam, Epic Games Store, GOG and Amazon Games manifests.

Only manifests are downloaded, never game files.

Usage:
    lore                                             # menu
    lore auth --store steam|epic|gog|amazon          # sign in
    lore collect [--store steam|epic|gog|amazon|all] [--limit N] [--only ID ...]
                 [--platforms Windows Mac] [--gog-builds latest|all]
    lore export out.csv [--store steam|epic|gog|amazon|all]
    lore submit                                      # upload your hashes anonymously
    lore list --store epic|gog
    lore stats
"""

import argparse
import json
import sys
from datetime import datetime

from . import __version__, amazon, ui
from .epic_gog import (GOG, STORES, Epic, cmd_auth, cmd_collect, cmd_list, cmd_stats, default_db,
                       default_manifest_dir)
from .export import EXPORT_STORES, LABELS, cmd_export, export
from .log import log
from .log import path as log_path
from .log import start as start_log
from .paths import files_dir
from .submit import submit

ALL_STORES = ("epic", "gog", "amazon", "steam")  # collect order: Steam last as it's the slowest


def _steam():
    from .steam import cli
    return cli


def _signed_in(store):
    if store == "steam":
        return _steam().signed_in()
    if store == "amazon":
        return amazon.Amazon().logged_in()
    return (Epic() if store == "epic" else GOG()).logged_in()


def sign_in(args):
    if args.store == "steam":
        _steam().sign_in()
    elif args.store == "amazon":
        amazon.cmd_auth(args)
    else:
        cmd_auth(args)


def collect(args):
    """Collects the chosen store, or every signed-in store with Steam last."""
    if args.store != "all":
        stores = (args.store,)
    else:
        stores = [s for s in ALL_STORES if _signed_in(s)]
        if not stores:
            ui.warn("Not signed in to any store. Choose Sign in first.")
            return
        skipped = [LABELS[s] for s in ALL_STORES if s not in stores]
        if skipped:
            ui.info(f"Not signed in to {', '.join(skipped)}, skipping.")
    for s in stores:
        if s == "steam":
            ui.heading("Steam")
            _steam().collect()
        elif s == "amazon":
            amazon.cmd_collect(args)
        else:
            cmd_collect(argparse.Namespace(**{**vars(args), "store": s}))


# --------------------------------------------------------------------------- menu

def _status(store):
    """(text, signed in) for the banner."""
    if store == "steam":
        from .steam import config
        if _signed_in("steam"):
            return f"signed in as {config.load()['steam_username']}", True
        return "not signed in", False
    c = {"epic": Epic, "gog": GOG, "amazon": amazon.Amazon}[store]()
    if not c.token_file.exists():
        return "not signed in", False
    try:
        c.session = json.loads(c.token_file.read_text())
        return f"signed in as {c.display_name()}", True
    except Exception:
        return "sign in again", False


def _ns(db, **kw):
    base = dict(db=db, platforms=["Windows"], limit=None, only=None, delay=1.0,
                manifest_dir=default_manifest_dir(), include_third_party=False, gog_builds="latest",
                store="all")
    base.update(kw)
    return argparse.Namespace(**base)


_STORE_ITEMS = [("steam", "Steam", ""), ("epic", "Epic Games Store", ""), ("gog", "GOG", ""),
                ("amazon", "Amazon Games", "Prime Gaming")]


def _menu_sign_in(db):
    store = ui.menu("Sign in to", _STORE_ITEMS)
    if not store:
        return False
    ui.screen(f"Sign in · {LABELS[store]}")
    sign_in(_ns(db, store=store))
    return True


def _menu_collect(db):
    store = ui.menu("Collect hashes from",
                    [("all", "All stores", "Epic, GOG, Amazon, then Steam")] + _STORE_ITEMS)
    if not store:
        return False
    ui.screen("Collect hashes")
    collect(_ns(db, store=store))
    return True


def _menu_export(db):
    store = ui.menu("Export", [("all", "All stores", "")] + _STORE_ITEMS)
    if not store:
        return False
    ui.screen("Export CSV")
    out = files_dir() / f"lore_{store}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with ui.console.status("Exporting...", spinner_style=ui.TEAL):
        counts = export(str(out), EXPORT_STORES if store == "all" else (store,), db)
    if not sum(counts.values()):
        out.unlink(missing_ok=True)
        ui.warn("Nothing collected yet. Choose Collect hashes first.")
        return True
    for s, n in counts.items():
        if n:
            ui.info(f"{LABELS[s]}: {n:,} hashes")
    ui.ok(f"Saved to {out}")
    return True


def _menu_submit(db):
    ui.screen("Submit")
    submit(db)
    return True


def interactive(db):
    """The menu. Every screen starts from a cleared terminal with the banner at the top."""
    ui.set_header(lambda: ui.banner(
        __version__, [(LABELS[s], *_status(s)) for s in ("steam", "epic", "gog", "amazon")]))
    actions = [
        ("1", "Sign in", "Steam, Epic, GOG, Amazon", _menu_sign_in),
        ("2", "Collect hashes", "downloads file lists, not games", _menu_collect),
        ("3", "Export CSV", "save your hashes to a file", _menu_export),
        ("4", "Submit", "upload your hashes anonymously", _menu_submit),
    ]
    while True:
        choice = ui.menu("Menu", [(key, label, hint) for key, label, hint, _ in actions], back="Exit")
        if choice is None:
            ui.screen()
            return
        fn = next(f for key, _, _, f in actions if key == choice)
        try:
            if not fn(db):
                continue  # backed out of a sub-menu
        except SystemExit as e:
            if isinstance(e.code, str):
                ui.error(e.code)
        except KeyboardInterrupt:
            ui.console.print()
            ui.warn("Stopped. Progress so far is saved.")
        except Exception as e:
            log.exception("Menu action failed")
            ui.error(str(e))
            ui.info(f"Details in the log file: {log_path()}")
        ui.pause()


# --------------------------------------------------------------------------- command line

def main():
    start_log()
    ap = argparse.ArgumentParser(prog="lore", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"lore {__version__}")
    ap.add_argument("--db", default=None, help="Epic/GOG database path (default: lore_files/lore.sqlite)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help="Epic/GOG database path")
    sub = ap.add_subparsers(dest="cmd")
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[common], **k)

    s = sub.add_parser("auth", help="sign in to a store")
    s.add_argument("--store", choices=EXPORT_STORES, default="steam")
    s.set_defaults(func=sign_in)

    s = sub.add_parser("collect", help="collect hashes (all signed-in stores by default)")
    s.add_argument("--store", choices=EXPORT_STORES + ("all",), default="all")
    s.add_argument("--platforms", nargs="+", default=["Windows"], choices=["Windows", "Mac"],
                   help="Epic/GOG only")
    s.add_argument("--gog-builds", choices=["latest", "all"], default="latest")
    s.add_argument("--limit", type=int, help="Epic/GOG/Amazon: first N titles per store (testing)")
    s.add_argument("--only", nargs="+", help="Epic/GOG/Amazon: specific app name(s) / product ID(s)")
    s.add_argument("--delay", type=float, default=1.0, help="seconds between API requests (default 1)")
    s.add_argument("--manifest-dir", default=default_manifest_dir())
    s.add_argument("--include-third-party", action="store_true", help="Epic: try Ubisoft/EA titles too")
    s.set_defaults(func=collect)

    s = sub.add_parser("export", help="export hashes to CSV (all stores by default)")
    s.add_argument("out")
    s.add_argument("--store", choices=EXPORT_STORES + ("all",), default="all")
    s.set_defaults(func=cmd_export)

    sub.add_parser("submit", help="upload your hashes anonymously").set_defaults(
        func=lambda a: submit(a.db))

    s = sub.add_parser("list", help="Epic/GOG: list library")
    s.add_argument("--store", choices=STORES, default="epic")
    s.add_argument("--platforms", nargs="+", default=["Windows"], choices=["Windows", "Mac"])
    s.set_defaults(func=cmd_list)

    sub.add_parser("stats", help="Epic/GOG: database summary").set_defaults(func=cmd_stats)

    args = ap.parse_args()
    args.db = args.db or default_db()
    if not args.cmd:
        interactive(args.db)
        return
    try:
        args.func(args)
    except KeyboardInterrupt:
        ui.warn("Stopped. Progress so far is saved.")
    except SystemExit:
        raise
    except Exception as e:
        log.exception("Command failed")
        ui.error(str(e))
        ui.info(f"Details in the log file: {log_path()}")
        sys.exit(1)
