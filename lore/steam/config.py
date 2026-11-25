"""Steam settings in lore_files/steam_config.json: Web API key, SteamID64 and login username, so
they're only asked for once. The API key is low privilege and the rest is public, so a plain file
is fine.
"""

import json
import os

from ..paths import files_dir

DEFAULTS = {"api_key": "", "steam_id64": "", "steam_username": ""}


def _config_path():
    return os.path.join(files_dir(), "steam_config.json")


def load():
    path = _config_path()
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
