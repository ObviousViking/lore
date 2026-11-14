# Development

Needs Python 3.11+, git and the .NET 9 SDK (`winget install Microsoft.DotNet.SDK.9`).

```
pip install -r requirements.txt
.\build.ps1          # builds dist\lore.exe
python run.py        # runs from source (run build.ps1 once first)
```

`build.ps1` clones [DepotDownloader](https://github.com/SteamRE/DepotDownloader) into `tools/`
at a fixed commit, builds it and bundles it into `lore.exe`. To update it, change `$ddCommit` in
`build.ps1`. Run from source, Lore writes `lore_files` to the repo root (gitignored).

```
lore/
  app.py              menu and command line
  paths.py            file locations
  epic_gog.py         Epic and GOG
  steam/
    cli.py            Steam scan
    depot_downloader.py, manifest_parser.py, steam_api.py, db.py, config.py
assets/
  lore.ico            exe icon
```

## Command line

```
lore                                  menu
lore steam                            Steam scan
lore auth --store epic|gog
lore list --store epic|gog
lore collect [--store epic|gog|all] [--limit N] [--only ID ...]
             [--platforms Windows Mac] [--gog-builds latest|all]
lore stats
lore export out.csv [--store epic|gog|all]
lore --version
```

## Releases

Every push to `main` builds and tests the exe, then publishes a GitHub release with a `YYYY.M.N`
version. N starts at 1 each month. Pushes to `dev` and pull requests build and test only, with no
release.
