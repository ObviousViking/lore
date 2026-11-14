"""Steam settings in steam_config.json next to the exe (repo root from source): the Steam Web API
key, the resolved SteamID64 and the Steam login username, so they're only asked for once. The API
key is low-privilege and the rest is public, so a plain file is enough.
"""

import json
import os
import sys

DEFAULTS = {"api_key": "", "steam_id64": "", "steam_username": ""}


def base_dir():
    """Folder the packaged exe lives in, or the repo root when run from source."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _config_path():
    return os.path.join(base_dir(), "steam_config.json")


def load():
    path = _config_path()
    legacy = os.path.join(base_dir(), "config.json")  # the standalone harvester's name for it
    if not os.path.exists(path) and os.path.exists(legacy):
        path = legacy
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = dict(DEFAULTS)
            merged.update({k: v for k, v in data.items() if k in DEFAULTS})
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULTS)


def save(updates):
    merged = load()
    merged.update(updates)
    with open(_config_path(), "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    return merged
