"""Where Lore keeps its files. Everything it writes goes in a lore_files folder next to the exe
(next to the repo root when run from source). Logins stay in %USERPROFILE%\\.lore.
"""

import sys
from pathlib import Path

FILES_DIR_NAME = "lore_files"


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
