"""Lore's log: lore_files/logs/lore_<date>_<time>.log, one per run, for every store.

Every message shown on screen goes in it, plus detail that isn't shown, like DepotDownloader's
full output and error tracebacks. The file is only created once something is logged.
"""

import logging
from datetime import datetime

from .paths import files_dir

log = logging.getLogger("lore")
_path = None


def start():
    """Starts logging to this run's file, once. Returns the file's path."""
    global _path
    if _path is None:
        folder = files_dir() / "logs"
        folder.mkdir(exist_ok=True)
        _path = folder / f"lore_{datetime.now():%Y%m%d_%H%M%S}.log"
        handler = logging.FileHandler(_path, encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        log.propagate = False
    return _path


def path():
    """This run's log file, starting it if needed."""
    return start()
