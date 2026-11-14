# Lore

Builds known-file hash sets from your Steam, Epic Games Store and GOG libraries. No game files are
downloaded.

Each store publishes a manifest for every build, listing every file with its hash. Lore downloads
those manifests for the games you own and saves the hashes.

| Store | Hashes | Builds |
|---|---|---|
| Steam | SHA1 | Latest build of each Windows depot |
| Epic | SHA1, plus MD5 and SHA256 on newer manifests | Latest build. Each run adds any new builds. |
| GOG | MD5, plus SHA256 on some newer builds | Latest build, or all public builds |

## Running it

Download `lore.exe` from the [latest release](../../releases/latest) and double-click it. There's
nothing to install. If Windows shows "Windows protected your PC", click **More info**, then
**Run anyway**.

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

1. **Log in.** Scan the QR code with the Steam mobile app. You don't enter a password. The first
   time, it asks for your Steam login username (not your display name).
2. **Library.** Enter a Steam Web API key and your profile URL. Get a free key at
   https://steamcommunity.com/dev/apikey, using `localhost` as the domain name.
3. **Scan.** Takes about 10-20 seconds per game. If Steam rate limits a request, Lore waits
   20 minutes and retries.
4. **Export.** Saves `hashes_<date>_<time>.csv` in `lore_files` and offers to open the submission
   form.

You can close it at any time. The next run skips games that are already done.

### Epic and GOG

1. **Log in** (option 2), once per store. Your browser opens. Paste back the code (Epic) or the
   final page URL (GOG).
2. **Collect** (option 4) all titles, the first N, or specific IDs from **List** (option 3). Builds
   already collected are skipped.
3. **Export** (option 6) saves a CSV in `lore_files`.

### Files

Lore keeps its files in a `lore_files` folder next to the exe. Logins are kept separately.

| What | Where |
|---|---|
| Steam | `lore_files\steam_config.json`, `hashharvester.db`, `steam_log_*.txt` |
| Epic/GOG | `lore_files\lore.sqlite` and `lore_files\manifests\` |
| Exports | `lore_files\hashes_*.csv` (Steam), `epic_hashes_*.csv` / `gog_hashes_*.csv` (Epic/GOG) |
| Epic/GOG logins | `%USERPROFILE%\.lore\`. Keep these private, like passwords. |

If you used an older version or the older tools (`game_hashes`, `epic_hashes`, `hashharvester`),
put `lore.exe` in the same folder. It moves the old data into `lore_files` when it starts.

## Notes

- The hashes come straight from the stores' manifests. Lore doesn't hash any files itself.
- Epic and GOG support is based on [Legendary](https://github.com/derrod/legendary) and
  [heroic-gogdl](https://github.com/Heroic-Games-Launcher/heroic-gogdl).
- DepotDownloader is GPL-2.0. Lore runs it as a separate program and copies its licence into
  `lore_files`.
