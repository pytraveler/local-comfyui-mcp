r"""One window over the scripts in the project root.

    .\\uv.exe run python -m comfyui_mcp.launcher          # the window
    .\\uv.exe run python -m comfyui_mcp.launcher --print  # the same, as text
    .\\uv.exe run python -m comfyui_mcp.launcher --lang=en

**It holds no list of scripts**, for the reason nothing else here holds a list of
tools, of synonyms, or of files to ship: a list is a second copy of what the
project consists of, and it goes stale the first time something is added - in the
direction nobody notices, since a missing button looks exactly like a feature that
was never written.

Instead each script says for itself that it belongs in the menu, on one line, at
the top of the file:

    REM LAUNCHER 10: "Install" "Установка"
    #   LAUNCHER 10: "Install" "Установка"

Three things follow, and each is the point rather than a side effect. A new script
appears on its own. `lang.bat` and `logo.bat` do not, because they are called by
other scripts rather than run and so carry no marker - no exclusion list decides
that, their own silence does. And `minify_bridge` and `release` show here while
they are absent from a release, which needs no flag either: they are simply not in
the directory being scanned.

Both languages sit on the same line, as they do in `i18n.Text` and in `call :say`,
so there is no key to go stale between them.

The window only ever *starts* a script, in a console of its own, and does not wait
for it: the scripts are interactive - `pause`, `choice`, a settings window of their
own - and their output is theirs to show. That is also why nothing here needed an
unattended mode adding to `install.bat`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import i18n
from .config import PROJECT_ROOT

MARKER = re.compile(
    r'^[ \t]*(?:REM|#)[ \t]*LAUNCHER[ \t]*(\d*)[ \t]*:[ \t]*"([^"]*)"[ \t]+"([^"]*)"[ \t]*$',
    re.MULTILINE,
)

WINDOWS_SUFFIXES = (".bat", ".cmd", ".ps1")
POSIX_SUFFIXES = (".sh",)

TERMINALS = (
    ("x-terminal-emulator", ("-e",)),
    ("gnome-terminal", ("--",)),
    ("konsole", ("-e",)),
    ("xfce4-terminal", ("-x",)),
    ("alacritty", ("-e",)),
    ("kitty", ()),
    ("xterm", ("-e",)),
)


@dataclass(frozen=True)
class Entry:
    """A script that asked to be in the menu."""

    path: Path
    order: int
    label: i18n.Text

    @property
    def name(self) -> str:
        return self.path.name


def parse_marker(text: str) -> tuple[int, i18n.Text] | None:
    """The order and the two labels, or None when the file did not ask to appear."""
    found = MARKER.search(text)
    if not found:
        return None
    order, en, ru = found.groups()
    if not en.strip() or not ru.strip():
        return None
    return (int(order) if order else 10_000), i18n.Text(en=en, ru=ru)


def suffixes(name: str = os.name) -> tuple[str, ...]:
    return WINDOWS_SUFFIXES if name == "nt" else POSIX_SUFFIXES


def discover(root: Path | None = None, name: str = os.name) -> list[Entry]:
    """Every script in the root that carries a marker, in menu order.

    Only this platform's kind of script: the two halves are twins, and offering
    both would put each entry in the menu twice.
    """
    root = root or PROJECT_ROOT
    found: list[Entry] = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in suffixes(name):
            continue
        try:
            marked = parse_marker(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if marked:
            found.append(Entry(path=path, order=marked[0], label=marked[1]))
    return sorted(found, key=lambda e: (e.order, e.name.lower()))


def console_command(path: Path, name: str = os.name, which=shutil.which) -> list[str]:
    """How to start this script in a console window of its own.

    `which` is injected so the POSIX branch can be tested on a machine that has
    none of these terminals, or all of them.
    """
    if name == "nt":
        if path.suffix.lower() == ".ps1":
            return [
                "cmd", "/c", "start", "", "powershell",
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path),
            ]
        return ["cmd", "/c", "start", "", str(path)]

    for terminal, flags in TERMINALS:
        if which(terminal):
            return [terminal, *flags, "bash", str(path)]
    return ["bash", str(path)]


def launch(entry: Entry, root: Path | None = None) -> subprocess.Popen:
    root = root or PROJECT_ROOT
    return subprocess.Popen(console_command(entry.path), cwd=str(root))


def as_text(entries: list[Entry], lang: str = i18n.FALLBACK) -> str:
    width = max((len(e.name) for e in entries), default=0)
    return "\n".join(f"  {e.name:<{width}}  {e.label.of(lang)}" for e in entries)


def run_window(entries: list[Entry], lang: str = i18n.FALLBACK) -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    say = i18n.speaker(lang)

    root = tk.Tk()
    root.title(say(en="comfyui-mcp - scripts", ru="comfyui-mcp - скрипты"))
    root.geometry("620x520")
    root.minsize(480, 320)

    footer = ttk.Frame(root, padding=(12, 4, 12, 10))
    footer.pack(side="bottom", fill="x")
    ttk.Button(
        footer,
        text=say(en="Close", ru="Закрыть"),
        command=root.destroy,
    ).pack(side="right")
    ttk.Label(
        footer,
        text=say(
            en="Each one opens in a console window of its own.",
            ru="Каждый открывается в своём окне консоли.",
        ),
        foreground="#666666",
    ).pack(side="left")

    head = ttk.Frame(root, padding=(12, 10, 12, 2))
    head.pack(fill="x")
    ttk.Label(
        head,
        text=say(
            en="Everything here can be run directly as well - this window only saves "
               "remembering the names.",
            ru="Всё это можно запускать и напрямую - окно лишь избавляет от необходимости "
               "помнить имена.",
        ),
        wraplength=580,
        justify="left",
    ).pack(anchor="w")

    body = ttk.Frame(root, padding=(12, 6, 12, 6))
    body.pack(fill="both", expand=True)

    if not entries:
        ttk.Label(
            body,
            text=say(
                en="No script in this folder carries a LAUNCHER marker.",
                ru="Ни один скрипт в этой папке не несёт метку LAUNCHER.",
            ),
            foreground="#c62828",
            wraplength=580,
        ).pack(anchor="w")

    def start(entry: Entry) -> None:
        try:
            launch(entry)
        except OSError as exc:
            messagebox.showerror(
                say(en="Could not start it", ru="Не удалось запустить"),
                f"{entry.name}\n\n{exc}",
            )

    for entry in entries:
        row = ttk.Frame(body)
        row.pack(fill="x", pady=3)
        ttk.Button(
            row,
            text=entry.label.of(lang),
            width=34,
            command=lambda e=entry: start(e),
        ).pack(side="left")
        ttk.Label(row, text=entry.name, foreground="#666666").pack(side="left", padx=(10, 0))

    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    lang = i18n.from_args(args)
    entries = discover()

    if "--print" in args:
        i18n.echo(as_text(entries, lang))
        return 0

    run_window(entries, lang)
    return 0


if __name__ == "__main__":
    sys.exit(main())
