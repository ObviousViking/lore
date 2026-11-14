# Lore

**Known-file hash sets from your Steam, Epic Games Store and GOG libraries, without downloading
a single game file.**

Each store already publishes a manifest listing every file in every build, with a hash for each
file. Lore pulls just those manifests for the games you own and turns them into hash sets.

| Store | Hashes | Builds |
|---|---|---|
| Steam | SHA1 | current build of each Windows depot, via [DepotDownloader](https://github.com/SteamRE/DepotDownloader) |
| Epic | SHA1, plus MD5/SHA256 on newer manifests | current build; history builds up across runs |
| GOG | MD5, plus SHA256 on some newer builds | current build, or every public build back through history |

## Running it

Download `lore.exe` from the [latest release](../../releases/latest) and double-click it.
It's one self-contained file with nothing to install. If Windows shows "Windows protected your
PC", click **More info**, then **Run anyway**.

```
  1) Steam - log in, scan library, export CSV
  2) Epic/GOG - log in
  3) Epic/GOG - list library
  4) Epic/GOG - collect hashes
  5) Epic/GOG - stats
  6) Epic/GOG - export CSV
  7) Epic/GOG - clear cache
```

### Steam

One guided run:

1. **Log in.** Scan the QR code with the Steam mobile app; no password is typed anywhere. The
   first time, it asks for your Steam *login* username (not your display name).
2. **Fetch your library.** It asks for a Steam Web API key (free from
   https://steamcommunity.com/dev/apikey; for "Domain Name" enter `localhost`) and your profile URL.
3. **Scan.** It confirms first, then works through your games with an `[x/total]` counter,
   roughly 10-20 seconds per game. If Steam rate-limits a request, it waits 20 minutes and retries
   on its own.
4. **Export.** It writes `hashes_<timestamp>.csv` next to the exe and offers to open the
   submission form.

You can close it at any point. Nothing is lost: games already scanned are skipped next time,
and partly scanned games only fetch their missing depots.

### Epic and GOG

Log in once per store (option 2). It opens your browser; you then paste back the code (Epic)
or the final URL (GOG). After that, use **Collect** for all titles, the first N, or specific IDs
from **List**. Each build is stored once, so re-running collects only what changed. **Export**
writes a CSV.

### Command line

```
lore                                  interactive menu
lore steam                            the Steam run above
lore auth --store epic|gog
lore list --store epic|gog
lore collect [--store epic|gog|all] [--limit N] [--only ID ...]
             [--platforms Windows Mac] [--gog-builds latest|all]
lore stats
lore export out.csv [--store epic|gog|all]
lore --version
```

### Where things are saved

| What | Where |
|---|---|
| Epic/GOG hashes | `lore.sqlite` and `manifests/` in the current folder (next to the exe when double-clicked) |
| Epic/GOG logins | `%USERPROFILE%\.lore\` (treat these like passwords) |
| Steam settings, hashes, logs | `steam_config.json`, `hashharvester.db`, `steam_log_*.txt` next to the exe |
| Exports | `hashes_*.csv` (Steam), `epic_hashes_*.csv` / `gog_hashes_*.csv` (Epic/GOG) |

Data from the older standalone tools (`game_hashes`, `epic_hashes`, `hashharvester`) is picked up
automatically: drop `lore.exe` in the same folder.

## Development

Needs Python 3.11+, git and the .NET 9 SDK (`winget install Microsoft.DotNet.SDK.9`).

```
pip install -r requirements.txt
.\build.ps1          # -> dist\lore.exe
python run.py        # run from source (after build.ps1 has built DepotDownloader once)
```

`build.ps1` clones DepotDownloader into `tools/` (gitignored) at a pinned commit, builds it as a
self-contained exe and embeds it in `lore.exe`. To move to a newer DepotDownloader, change
`$ddCommit` in `build.ps1`.

```
lore/
  app.py            menu and command line
  epic_gog.py       Epic + GOG: auth, library, manifest parsing, SQLite
  steam/
    cli.py          the Steam run: login, fetch, scan, export
    depot_downloader.py, manifest_parser.py, steam_api.py, db.py, config.py
```

### Releases

Every push to `main` builds the exe on `windows-latest`, stamps a `YYYY.M.N` version (the build
number resets monthly), smoke-tests it, tags the commit and publishes a GitHub release with
`lore.exe` attached. Pushes to `dev` and pull requests build only.

## Notes

- **Trust:** the hashes are the stores' own published values. Lore doesn't re-hash
  downloaded content, which is what makes it fast.
- **Credits:** Epic/GOG manifest and API handling is derived from
  [Legendary](https://github.com/derrod/legendary) and
  [heroic-gogdl](https://github.com/Heroic-Games-Launcher/heroic-gogdl).
- **Licensing:** DepotDownloader is GPL-2.0. Lore runs it as a separate process rather than
  linking it, and writes its LICENSE next to the exe on first run.
