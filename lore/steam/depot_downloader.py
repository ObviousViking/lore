"""Runs DepotDownloader.exe as a separate process. It's embedded in the packaged exe, or built
into tools/DepotDownloader/publish by build.ps1 when running from source. It's GPL-2.0 and not a
library, so we only use its command line (see the README's licensing note).

Login is QR code only. That keeps output reading line by line: the only DepotDownloader prompts
without a trailing newline are the password and 2FA prompts, which QR login never shows. The QR
code itself is printed with Console.WriteLine.
"""

import ctypes
import filecmp
import os
import re
import shutil
import subprocess
import sys

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _console_encoding():
    """.NET writes redirected console output in the OEM codepage, not UTF-8 (the QR code's
    block character arrives as 0xDB in cp437). The OEM codepage varies by locale, so ask
    Windows for it."""
    try:
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except (AttributeError, OSError):
        return "cp437"  # not on Windows; shouldn't happen


_ENCODING = _console_encoding()

# DepotDownloader always needs -app and has no login-only mode. Login uses app 480 (Spacewar,
# Valve's free example app) with -manifest-only, so nothing is downloaded. It just completes the
# login so -remember-password can cache the session for later scan_app calls.
_LOGIN_PRIMING_APP_ID = 480


def _install_bundled():
    """Copies the bundled DepotDownloader (and its LICENSE) to lore_files/DepotDownloader and
    returns the exe path there, or None when not running as the packaged exe.

    It has to run from a fixed path: DepotDownloader saves the Steam login token in .NET
    isolated storage, which is keyed on the exe's location. PyInstaller extracts to a new temp
    folder each launch, so running the bundled copy directly loses the login every time."""
    if not (getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")):
        return None
    src = os.path.join(sys._MEIPASS, "DepotDownloader")
    if not os.path.exists(os.path.join(src, "DepotDownloader.exe")):
        return None
    from ..paths import files_dir

    dest = os.path.join(files_dir(), "DepotDownloader")
    os.makedirs(dest, exist_ok=True)
    for name in ("DepotDownloader.exe", "LICENSE"):
        s, d = os.path.join(src, name), os.path.join(dest, name)
        if os.path.exists(s) and not (os.path.exists(d) and filecmp.cmp(s, d, shallow=False)):
            shutil.copyfile(s, d)  # missing, or a new Lore version bundles a different build
    return os.path.join(dest, "DepotDownloader.exe")


def find_exe():
    """Finds DepotDownloader.exe. Checks, in order: the copy installed from the packaged exe, a
    DepotDownloader folder next to the program (older packaging), then tools/DepotDownloader/
    publish* in this or a parent folder (running from source). Returns None if not found."""
    installed = _install_bundled()
    if installed:
        return installed

    from ..paths import app_dir

    base = str(app_dir())
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


def _run_interactive(exe_path, args, on_line):
    """Runs DepotDownloader and calls on_line(str) for each output line as it arrives. Blocks
    until it exits and returns the exit code."""
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
    """QR code login. No password is typed; approving in the Steam mobile app also covers 2FA.
    -remember-password caches the session for later scan_app calls.

    DepotDownloader rejects -qr together with -username, so no username is passed here. The
    caller reads it from the login output afterwards (see account_name), or asks for it if it
    isn't there."""
    args = [
        "-app", str(_LOGIN_PRIMING_APP_ID), "-manifest-only",
        "-qr", "-remember-password", "-loginid", str(os.getpid() % 2_000_000_000),
    ]
    return _run_interactive(exe_path, args, on_line)


# Steam's rate-limit EResult codes. DepotDownloader doesn't handle them specially; they show up
# in its normal output, e.g. "Got depot key for 481 result: RateLimitExceeded".
_RATE_LIMIT_MARKERS = ("RateLimitExceeded", "LimitExceeded")


# Printed when there's no saved login token for the username, or Steam rejected it.
_SIGN_IN_MARKERS = ("Enter account password for", "Access token was rejected")

# Lines that name the account after a QR login.
_ACCOUNT_RES = (re.compile(r"Logging '([^']+)' into Steam3"),
                re.compile(r"-username (\S+) -remember-password"))


def needs_sign_in(lines):
    """True if DepotDownloader couldn't use the saved Steam login."""
    return any(marker in line for line in lines for marker in _SIGN_IN_MARKERS)


def account_name(lines):
    """The Steam account name from QR login output, or None."""
    for line in lines:
        for pattern in _ACCOUNT_RES:
            m = pattern.search(line)
            if m:
                return m.group(1)
    return None


def is_rate_limited(lines):
    """True if any output line mentions a rate-limit code, so the caller can wait and retry
    rather than treat the app as unavailable."""
    return any(marker in line for line in lines for marker in _RATE_LIMIT_MARKERS)


# Printed by DepotDownloader when the app has a depot this account's licence doesn't cover (e.g.
# DLC or region locked). The scan still exits 0 with that depot left out, so this line is the
# only sign it exists.
_DENIED_DEPOT_RE = re.compile(r"Depot (\d+) is not available from this account\.")


def denied_depot_ids(lines):
    """Depot IDs reported as not available to this account."""
    ids = set()
    for line in lines:
        m = _DENIED_DEPOT_RE.search(line)
        if m:
            ids.add(int(m.group(1)))
    return ids


def scan_app(exe_path, appid, username, output_dir, on_line, depot_ids=None):
    """Runs `-app {appid} -manifest-only` for Windows depots, writing one
    manifest_{depotId}_{manifestId}.txt per depot into output_dir. No game files are downloaded.

    By default it covers every depot of the app, which is how new depots are found. With
    depot_ids it only fetches those (current manifest of each), which is quicker when just a few
    are missing. Returns (exit_code, [manifest file paths])."""
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
