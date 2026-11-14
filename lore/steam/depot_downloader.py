"""Shells out to a built DepotDownloader.exe (embedded in the packaged exe, or built from source
into tools/DepotDownloader/publish by build.ps1 when running from source). We don't link against it as a
library — it's GPL-2.0 licensed and isn't published as one anyway — so the CLI is the integration
surface, same approach as the original C# prototype. See the README's licensing note.

QR-code login only — no password/2FA/email-code path. That's what let _run_interactive below stay
plain line-buffered: the only prompts DepotDownloader ever sends without a trailing newline
(Console.Write, blocking on Console.ReadLine — see Program.cs/Steam3Session.cs) are the
username/password and 2FA/email-code prompts this tool no longer uses. The QR code itself renders
via Console.WriteLine (confirmed in Steam3Session.cs's DisplayQrCode) — it and everything else
this tool actually sends terminates its lines normally.
"""

import ctypes
import os
import re
import shutil
import subprocess
import sys

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _console_encoding():
    """.NET's Console class writes redirected output using the OS's OEM/DOS codepage, not UTF-8
    — confirmed by capturing DepotDownloader's raw QR-code bytes: the block characters come
    through as 0xDB, CP437's single-byte "full block" glyph, which decoding as UTF-8 mangles into
    a run of replacement characters. Query the real OEM codepage via Windows rather than
    hardcoding cp437, since it varies by locale."""
    try:
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except (AttributeError, OSError):
        return "cp437"  # non-Windows/no ctypes fallback — shouldn't happen, this tool is Windows-only


_ENCODING = _console_encoding()

# DepotDownloader's CLI hard-requires -app on every invocation (Program.cs exits immediately with
# "Error: -app not specified!" otherwise) — there's no dedicated "just log in" mode. So the login
# step "primes" a session against Valve's public SDK example app (480, Spacewar; free for every
# account, no purchase needed) with -manifest-only so nothing is actually downloaded. The real
# point is just completing the login handshake so -remember-password has something to cache for
# every later scan_app call against the account's real games.
_LOGIN_PRIMING_APP_ID = 480


def find_exe():
    """Looks for DepotDownloader in order: bundled inside this PyInstaller onefile exe (see
    build.ps1's --add-binary — this is what makes the whole tool a single file to hand out),
    then next to this program as a sibling folder (older packaging convention, still supported),
    then the dev-time layout (tools/DepotDownloader/publish*) by walking up from here. Covers
    packaged single-exe, packaged folder-style, and running from source in this repo."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # PyInstaller onefile re-extracts its whole bundle — including anything added via
        # --add-binary — to a fresh temp dir every launch, valid for this process's lifetime.
        # No need to copy it anywhere further; just run it from there.
        bundled = os.path.join(sys._MEIPASS, "DepotDownloader", "DepotDownloader.exe")
        if os.path.exists(bundled):
            return bundled

    from . import config

    base = config.base_dir()
    candidates = [os.path.join(base, "DepotDownloader", "DepotDownloader.exe")]

    d = base
    for _ in range(8):
        for sub in ("publish-standalone", "publish"):
            candidates.append(os.path.join(d, "tools", "DepotDownloader", sub, "DepotDownloader.exe"))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent

    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def ensure_license_extracted():
    """Writes DepotDownloader's LICENSE (GPL-2.0) out next to this program if it isn't there
    already. Only meaningful for the single-exe build, where nothing else ships alongside it —
    the license would otherwise be locked inside PyInstaller's ephemeral per-run temp dir and
    never actually reach whoever the exe gets sent to. Idempotent/cheap; safe to call every launch.
    """
    if not (getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")):
        return
    from . import config

    dest = os.path.join(config.base_dir(), "DepotDownloader-LICENSE.txt")
    if os.path.exists(dest):
        return
    bundled = os.path.join(sys._MEIPASS, "DepotDownloader", "LICENSE")
    if os.path.exists(bundled):
        shutil.copyfile(bundled, dest)


def _run_interactive(exe_path, args, on_line):
    """Runs DepotDownloader, reading its output line-by-line and calling on_line(str) with each
    complete line as it arrives (for display/logging). Blocking — call this from a background
    thread if the caller needs to stay responsive; the CLI's own main loop just calls it directly.
    """
    proc = subprocess.Popen(
        [exe_path, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )
    assert proc.stdout is not None

    for raw_line in proc.stdout:
        on_line(raw_line.decode(_ENCODING, errors="replace").rstrip("\r\n"))

    proc.wait()
    return proc.returncode


def login_qr(exe_path, on_line):
    """QR-code login: no password ever typed into this tool or DepotDownloader — -qr skips
    straight past the password prompt and the mobile-app approval doubles as 2FA.
    -remember-password caches the resulting session for every later non-interactive call.

    Note: DepotDownloader rejects -qr combined with -username (hard CLI error), so this
    deliberately omits it — the account name is only known *after* the scan resolves it, which is
    why the caller asks for it afterward and passes it explicitly to scan_app for every later call.
    """
    args = [
        "-app", str(_LOGIN_PRIMING_APP_ID), "-manifest-only",
        "-qr", "-remember-password", "-loginid", str(os.getpid() % 2_000_000_000),
    ]
    return _run_interactive(exe_path, args, on_line)


# Steam's EResult codes for temporary/permanent throttling. DepotDownloader has no special
# handling for either — they just show up inline in its generic "result: {EResult}" lines (e.g.
# "Got depot key for 481 result: RateLimitExceeded"), since C# renders an enum as its member name.
_RATE_LIMIT_MARKERS = ("RateLimitExceeded", "LimitExceeded")


def is_rate_limited(lines):
    """True if any captured output line from a scan_app call mentions one of Steam's rate-limit
    EResult codes. Used by the scan loop to back off and retry instead of treating a throttled
    app the same as a genuinely unowned/unavailable one."""
    return any(marker in line for line in lines for marker in _RATE_LIMIT_MARKERS)


# ContentDownloader.cs prints this verbatim (Console.WriteLine("Depot {0} is not available from
# this account.", depotId)) when PICS lists a depot for the app but this account's license
# doesn't cover it — e.g. a DLC-gated or region-restricted depot. The app's overall scan can
# still exit 0 with that depot simply missing from the output, so without parsing this line such
# a depot would be entirely invisible to us: not pulled, and not even known to exist.
_DENIED_DEPOT_RE = re.compile(r"Depot (\d+) is not available from this account\.")


def denied_depot_ids(lines):
    """Depot IDs DepotDownloader reported as denied to this account during a scan_app call."""
    ids = set()
    for line in lines:
        m = _DENIED_DEPOT_RE.search(line)
        if m:
            ids.add(int(m.group(1)))
    return ids


def scan_app(exe_path, appid, username, output_dir, on_line, depot_ids=None):
    """Runs `-app {appid} -manifest-only`, which by default auto-enumerates every depot for that
    app (filtered to Windows) — the only way to discover a depot we've never seen before — and
    writes one manifest_{depotId}_{manifestId}.txt per depot into output_dir. No game content is
    downloaded either way.

    depot_ids, when given, adds `-depot <id> <id> ...` — DepotDownloader's CLI accepts a
    space-separated list after a single -depot flag (see Program.cs's GetParameterList), which
    restricts this call to exactly those depot IDs instead of asking it to enumerate the app's
    full depot list itself. Each one still resolves to whatever its *current* manifest is (we
    never pin an old one). This is the "fill in what's missing" fast path: it can't discover a
    depot outside the given list, but for a game with many depots where only a couple are still
    unpulled, it skips re-querying the ones we already have — one login either way, less work
    per depot. Returns (exit_code, [manifest file paths])."""
    os.makedirs(output_dir, exist_ok=True)
    args = [
        "-app", str(appid), "-manifest-only", "-remember-password",
        "-username", username, "-os", "windows", "-dir", output_dir,
        "-loginid", str(os.getpid() % 2_000_000_000),
    ]
    if depot_ids:
        args += ["-depot", *(str(d) for d in depot_ids)]
    code = _run_interactive(exe_path, args, on_line)

    manifest_files = []
    for root, _dirs, files in os.walk(output_dir):
        for fn in files:
            if fn.startswith("manifest_") and fn.endswith(".txt"):
                manifest_files.append(os.path.join(root, fn))
    return code, manifest_files
