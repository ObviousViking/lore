"""The two Steam Web API calls Lore needs. Uses urllib only, so no extra dependencies.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

_BARE_STEAMID = re.compile(r"^\d{17}$")
_PROFILES_URL = re.compile(r"steamcommunity\.com/profiles/(\d{17})", re.IGNORECASE)
_ID_URL = re.compile(r"steamcommunity\.com/id/([^/\s]+)", re.IGNORECASE)


class SteamApiError(Exception):
    pass


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "steam-hash-harvester"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SteamApiError(f"HTTP {e.code} {e.reason} from Steam: {body[:300]}") from e
    except urllib.error.URLError as e:
        raise SteamApiError(f"Network error reaching Steam: {e.reason}") from e


def resolve_steam_id(raw_input, api_key):
    """Turns a pasted profile URL, bare SteamID64, or vanity name into a SteamID64."""
    text = raw_input.strip()

    if _BARE_STEAMID.match(text):
        return text

    m = _PROFILES_URL.search(text)
    if m:
        return m.group(1)

    m = _ID_URL.search(text)
    vanity = m.group(1) if m else text

    url = ("https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/"
           f"?key={urllib.parse.quote(api_key)}&vanityurl={urllib.parse.quote(vanity)}&format=json")
    data = _get_json(url)
    resp = data.get("response", {})
    if resp.get("success") == 1 and resp.get("steamid"):
        return resp["steamid"]

    raise SteamApiError(
        f"Couldn't resolve '{raw_input}' to a Steam account. Double-check the profile URL — open "
        "the Steam app, click your name, click your profile, and copy the URL from the browser."
    )


def get_owned_games(api_key, steam_id64):
    url = ("https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
           f"?key={urllib.parse.quote(api_key)}&steamid={urllib.parse.quote(steam_id64)}"
           "&format=json&include_appinfo=true&include_played_free_games=true")
    data = _get_json(url)
    games = data.get("response", {}).get("games", [])

    if not games:
        raise SteamApiError(
            "GetOwnedGames returned zero games. Common causes: the SteamID doesn't match this API "
            "key's account and your profile is private, or 'Game details' privacy isn't set to "
            "Public (Steam > Edit Profile > Privacy Settings)."
        )

    return [{"appid": g["appid"], "name": g.get("name") or f"App {g['appid']}"} for g in games]
