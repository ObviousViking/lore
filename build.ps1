# Builds dist\lore.exe: one console exe covering Steam, Epic and GOG, with DepotDownloader
# (used for Steam) embedded and extracted to a temp dir at runtime.
#
# Needs: git, .NET 9 SDK, Python 3.11+ with `pip install -r requirements.txt`.
# Run from anywhere: .\build.ps1   (also used by the GitHub Actions release workflow)

# Not "Stop": Windows PowerShell 5.1 treats native stderr (PyInstaller logs there) as an error
# when redirected. Each native call checks $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$here = $PSScriptRoot

# DepotDownloader is built from source (it handles Steam logins, so no prebuilt binaries).
# Pinned to the upstream commit this tool was tested against - bump deliberately.
$ddRepo = "https://github.com/SteamRE/DepotDownloader.git"
$ddCommit = "1e8e20c72cad64deeb70443a758dda5b750bb119"
$ddSrc = Join-Path $here "tools\DepotDownloader"
$ddPublish = Join-Path $ddSrc "publish"
$dist = Join-Path $here "dist"
$work = Join-Path ([IO.Path]::GetTempPath()) "lore-build"  # outside OneDrive, which locks build files

if (-not (Test-Path (Join-Path $ddSrc ".git"))) {
    Write-Host "Cloning DepotDownloader @ $ddCommit..."
    git clone --quiet $ddRepo $ddSrc
    if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
}
git -C $ddSrc -c advice.detachedHead=false checkout --quiet $ddCommit
if ($LASTEXITCODE -ne 0) { throw "git checkout $ddCommit failed" }

Write-Host "Publishing DepotDownloader (self-contained, single-file)..."
dotnet publish (Join-Path $ddSrc "DepotDownloader\DepotDownloader.csproj") `
    -c Release -r win-x64 --self-contained true `
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfContained=true `
    -o $ddPublish
if ($LASTEXITCODE -ne 0) { throw "DepotDownloader publish failed" }
Remove-Item (Join-Path $ddPublish "*.pdb") -ErrorAction SilentlyContinue
Copy-Item (Join-Path $ddSrc "LICENSE") (Join-Path $ddPublish "LICENSE") -Force -ErrorAction Stop

Write-Host "Building lore.exe..."
python -m PyInstaller --noconfirm --clean --onefile --console --name lore `
    --hidden-import Cryptodome.Cipher.AES `
    --add-binary "$(Join-Path $ddPublish 'DepotDownloader.exe');DepotDownloader" `
    --add-data "$(Join-Path $ddPublish 'LICENSE');DepotDownloader" `
    --distpath $dist --workpath $work --specpath $work `
    (Join-Path $here "run.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

Write-Host "`nBuilt: $(Join-Path $dist 'lore.exe')"
