"""Console look: banner, menus, messages and progress bars, all through one rich Console.
Message text passed to these helpers is printed as-is (game titles can contain [brackets]).
Messages from ok(), info(), warn() and error() also go in the log."""

import logging
import sys
import time

from .log import log


def _utf8_console():
    """Switches the console and Python's streams to UTF-8 before anything prints. Otherwise a
    cp1252 console or redirected output crashes on the logo, ✓ marks, the Steam QR code and game
    titles like ones with Greek letters. Characters that still can't print are replaced."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except (AttributeError, OSError):
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


_utf8_console()

from rich.box import ROUNDED  # noqa: E402 - after the console is switched to UTF-8
from rich.console import Console  # noqa: E402
from rich.markup import escape  # noqa: E402
from rich.padding import Padding  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.progress import (BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn,  # noqa: E402
                           TimeRemainingColumn)
from rich.table import Column, Table  # noqa: E402
from rich.text import Text  # noqa: E402

console = Console(highlight=False)

GOLD = "#e8b64c"
AMBER = "#c9952e"
TEAL = "#5fb8b0"
DIM = "grey50"

_LOGO = [
    "██╗      ██████╗ ██████╗ ███████╗",
    "██║     ██╔═══██╗██╔══██╗██╔════╝",
    "██║     ██║   ██║██████╔╝█████╗  ",
    "██║     ██║   ██║██╔══██╗██╔══╝  ",
    "███████╗╚██████╔╝██║  ██║███████╗",
    "╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝",
]
_LOGO_COLOURS = [GOLD, GOLD, "#dcaa42", "#d29f38", AMBER, "#8a6a2a"]


_header = None


def set_header(draw):
    """Sets what screen() draws at the top of every screen (the banner in the menu)."""
    global _header
    _header = draw


def screen(title=None):
    """Starts a new screen: clears the terminal and its scrollback, draws the header, then an
    optional title. Keeps the menu in place instead of piling output up the window."""
    if console.is_terminal:
        console.clear()
        if not console.legacy_windows:
            console.file.write("\x1b[3J")  # scrollback too, so it doesn't build up
            console.file.flush()
    if _header:
        _header()
    if title:
        heading(title)


def banner(version, statuses):
    """The logo, version and a sign-in line per store.
    statuses: [(store label, text, signed in)]."""
    console.print()
    for line, colour in zip(_LOGO, _LOGO_COLOURS):
        console.print(f"  {line}", style=f"bold {colour}")
    shown = f"v{version}" if version[:1].isdigit() else version
    console.print(f"  [{TEAL}]game file hashes from store manifests[/]  [{DIM}]{escape(shown)}[/]")
    console.print()
    table = Table.grid(padding=(0, 2))
    for label, text, ok in statuses:
        dot = "[green]●[/]" if ok else f"[{DIM}]○[/]"
        table.add_row(dot, f"[bold]{escape(label)}[/]",
                      f"[{'default' if ok else DIM}]{escape(text)}[/]")
    panel = Panel(table, box=ROUNDED, border_style=DIM, expand=False, padding=(0, 2))
    console.print(Padding(panel, (0, 0, 0, 2)))


def menu(title, items, back="Back"):
    """Shows a numbered menu on a fresh screen and returns the chosen key, or None for 0/back.
    items: [(key, label, hint)]."""
    screen()
    console.print(f"\n  [bold {GOLD}]{escape(title)}[/]")
    table = Table.grid(padding=(0, 1))
    table.add_column(width=1)
    table.add_column(justify="right", style=TEAL)
    table.add_column(style=DIM)
    table.add_column(min_width=18)
    table.add_column(style=DIM)
    for i, (_, label, hint) in enumerate(items, 1):
        table.add_row("", str(i), "›", escape(label), escape(hint or ""))
    table.add_row("", "0", "›", f"[{DIM}]{escape(back)}[/]", "")
    console.print(table)
    choice = ask("Choose", "1")
    if choice.isdigit() and 0 < int(choice) <= len(items):
        return items[int(choice) - 1][0]
    return None


def ask(prompt, default=None):
    suffix = f" [{DIM}]({escape(str(default))})[/]" if default is not None else ""
    v = console.input(f"\n  [{GOLD}]?[/] {escape(prompt)}{suffix} [{DIM}]›[/] ").strip()
    return v if v else (default if default is not None else "")


def confirm(prompt, default=True):
    v = ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
    return default if not v else v in ("y", "yes")


def heading(text):
    console.print()
    console.rule(f"[bold {GOLD}]{escape(text)}[/]", style=DIM, align="left")


def _line(mark, text, level):
    """A marked message, also logged. Long text wraps in line with itself, not under the mark."""
    log.log(level, text)
    t = Table.grid(padding=(0, 1))
    t.add_column(width=3, justify="right", no_wrap=True)
    t.add_column()
    t.add_row(mark, Text(text))
    console.print(t)


def ok(text):
    _line("[green]✓[/]", text, logging.INFO)


def info(text):
    _line(f"[{TEAL}]•[/]", text, logging.INFO)


def warn(text):
    _line("[yellow]![/]", text, logging.WARNING)


def error(text):
    _line("[red]✗[/]", text, logging.ERROR)


def pause():
    console.input(f"\n  [{DIM}]Press Enter to go back to the menu...[/]")


class Bar:
    """Progress bar with the current item shown after it. Use as a context manager.
    Messages printed with note() appear above the bar."""

    def __init__(self, label, total):
        self.total = total
        self._progress = Progress(
            TextColumn("  "),
            SpinnerColumn(style=TEAL),
            TextColumn(f"[bold]{escape(label)}[/]"),
            BarColumn(bar_width=30, style="grey23", complete_style=GOLD, finished_style="green"),
            MofNCompleteColumn(),
            TextColumn(f"[{DIM}]•[/]"),
            TimeRemainingColumn(),
            TextColumn("{task.fields[status]}", style=DIM, markup=False,
                       table_column=Column(no_wrap=True, overflow="ellipsis", max_width=40)),
            console=console,
        )
        self._task = None

    def __enter__(self):
        self._progress.start()
        self._task = self._progress.add_task("", total=self.total, status="")
        return self

    def __exit__(self, *exc):
        self.status("")
        self._progress.stop()

    def status(self, text):
        self._progress.update(self._task, status=text)

    def advance(self, n=1):
        self._progress.advance(self._task, n)

    def note(self, kind, text):
        {"ok": ok, "info": info, "warn": warn, "error": error}[kind](text)

    def wait(self, seconds, text):
        """Sleeps, showing '<text> in m:ss' as the status."""
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            mins, secs = divmod(int(left), 60)
            self.status(f"{text} in {mins}m{secs:02d}s")
            time.sleep(1)


def countdown(seconds, text):
    """Sleeps with a countdown line, for waits outside a progress bar."""
    with Progress(TextColumn("  "), SpinnerColumn(style=TEAL), TextColumn("{task.description}"),
                  console=console, transient=True) as p:
        task = p.add_task("", total=None)
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            mins, secs = divmod(int(left), 60)
            p.update(task, description=escape(f"{text} in {mins}m{secs:02d}s"))
            time.sleep(1)
