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
  log.py              the log file, shared by every store
  epic_gog.py         Epic and GOG
  amazon.py           Amazon Games
  export.py           CSV export for all stores
  submit.py           anonymous upload to the submission Worker
  ui.py               banner, menus, colours and progress bars
  steam/
    cli.py            Steam sign in and collect
    depot_downloader.py, manifest_parser.py, steam_api.py, db.py, config.py
assets/
  lore.ico            exe icon
```

## Command line

```
lore                                  menu
lore auth --store steam|epic|gog|amazon      sign in
lore collect [--store steam|epic|gog|amazon|all] [--limit N] [--only ID ...]
             [--platforms Windows Mac] [--gog-builds latest|all]
lore export out.csv [--store steam|epic|gog|amazon|all]
lore submit
lore list --store epic|gog
lore stats
lore --version
```

`--limit` and `--only` apply to Epic, GOG and Amazon. `--platforms` and `--gog-builds` apply to Epic
and GOG.

## Steam login

DepotDownloader saves the Steam login token in .NET isolated storage, which is keyed on the exe's
path. The packaged exe copies DepotDownloader to `lore_files\DepotDownloader\` and runs it from
there, so the path (and the login) stays the same between runs. Moving the Lore folder means
signing in to Steam again.

## Submissions

Submit uploads to a Cloudflare Worker, kept outside this repo. Set its URL as `SUBMIT_URL` in
`lore/submit.py`, or with the `LORE_SUBMIT_URL` environment variable for testing.

## Releases

Every push to `main` builds and tests the exe, then publishes a GitHub release with a `YYYY.M.N`
version. N starts at 1 each month. Pushes to `dev` and pull requests build and test only, with no
release.
