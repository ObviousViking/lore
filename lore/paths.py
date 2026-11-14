"""Where Lore keeps its files. Everything it writes goes in a lore_files folder next to the exe
(next to the repo root when run from source). Logins stay in %USERPROFILE%\\.lore.
"""

import shutil
import sys
from pathlib import Path

FILES_DIR_NAME = "lore_files"

# Data that older versions kept next to the exe. Moved into lore_files on startup.
_OLD_FILES = ("steam_config.json", "hashharvester.db", "lore.sqlite", "game_hashes.sqlite",
              "epic_hashes.sqlite", "manifests", "DepotDownloader-LICENSE.txt")


def app_dir():
    """Folder the packaged exe is in, or the repo root when run from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def files_dir():
    """The lore_files folder, created if needed."""
    d = app_dir() / FILES_DIR_NAME
    d.mkdir(exist_ok=True)
    return d


def move_old_files():
    """Moves data from older versions into lore_files. Skips anything already there."""
    src = app_dir()
    names = [n for n in _OLD_FILES if (src / n).exists()]
    if not names:
        return
    dest = files_dir()
    for name in names:
        if not (dest / name).exists():
            try:
                shutil.move(str(src / name), str(dest / name))
            except OSError:
                pass
