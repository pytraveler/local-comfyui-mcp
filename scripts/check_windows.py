r"""Check that all three settings windows keep their buttons on screen, in both languages.

    .\uv.exe run python scripts\check_windows.py
    .\uv.exe run python scripts\check_windows.py --lang=ru   # one language only

Not part of the offline suite, for the reason `check_bridge.py` is not: it needs
something the suite deliberately does without - here a display, and a real Tk
event loop to lay anything out. Everything the windows *decide* is a plain
function under test; this covers the one thing that is not, and it exists because
that gap shipped a real bug. `configure_comfy.bat` opened with the Save button
below the bottom edge, and `configure.bat` did the same once the window was made
smaller.

The cause is worth stating, since it is easy to write again: **pack hands out
space in call order.** A footer packed last with `side="bottom"` is drawn at the
bottom, but it is also the last to be given any room, so it is the first thing
squeezed out once the content above is taller than the window. Packing it first
reserves its strip; everything else then fills what is left.

Each window is built, resized twice - once at its natural size and once smaller
than its content - and the committing button is measured against the window it
sits in. Nothing is clicked and nothing is saved.

**Every window is built in every language**, which is the other half of what this
covers: a translation changes the length of every string in a window, and the
first thing a longer one costs is the row at the bottom. Building each `Text` is
also what checks it - a half-translated one raises in its constructor - so this
script is the closest thing there is to a test of the strings themselves.

The labels below are the one place this script names what the windows say. That
is deliberate rather than a duplicate: this checks what somebody sees, so the
visible text is the thing to look for, and a label that changed without this
changing should be noticed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tkinter as tk  # noqa: E402
from tkinter import ttk  # noqa: E402

from comfyui_mcp import i18n  # noqa: E402
from comfyui_mcp.i18n import Text  # noqa: E402

SAVE = Text(en="Save to", ru="Сохранить")
WRITE = Text(en="Write to file", ru="Записать")
PROBE = Text(en="Check", ru="Проверить")
CLOSE = Text(en="Close", ru="Закрыть")


def buttons(widget: tk.Misc) -> list[ttk.Button]:
    found: list[ttk.Button] = []
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button):
            found.append(child)
        found.extend(buttons(child))
    return found


def measure(
    build: Callable[[Callable[[tk.Tk], None]], None], sizes: list[str], label: str
) -> list[dict[str, Any]]:
    """Run `build`, and at each size report where the committing button ended up."""
    seen: list[dict[str, Any]] = []

    def inspect(root: tk.Tk) -> None:
        for size in sizes:
            root.geometry(size)
            root.update_idletasks()
            root.update()
            save = [b for b in buttons(root) if label in b.cget("text")]
            if not save:
                seen.append({"size": size, "found": False})
                continue
            button = save[0]
            bottom = button.winfo_rooty() - root.winfo_rooty() + button.winfo_height()
            seen.append(
                {
                    "size": size,
                    "found": True,
                    "visible": bool(button.winfo_ismapped()),
                    "bottom": bottom,
                    "height": root.winfo_height(),
                }
            )
        root.destroy()

    build(inspect)
    return seen


def with_stub_mainloop(inspect: Callable[[tk.Tk], None]):
    """Replace mainloop with one pass of the inspector, then restore it."""
    original = tk.Tk.mainloop

    def once(self: tk.Tk, n: int = 0) -> None:
        inspect(self)

    tk.Tk.mainloop = once  # type: ignore[method-assign]
    return original


def run(name: str, opener: Callable[[], None], sizes: list[str], label: str) -> bool:
    def build(inspect: Callable[[tk.Tk], None]) -> None:
        original = with_stub_mainloop(inspect)
        try:
            opener()
        finally:
            tk.Tk.mainloop = original  # type: ignore[method-assign]

    ok = True
    for report in measure(build, sizes, label):
        if not report.get("found"):
            print(f"  [!  ] {report['size']}: no button matching {label!r} - has the label changed?")
            ok = False
            continue
        fits = report["visible"] and report["bottom"] <= report["height"]
        ok = ok and fits
        mark = "OK " if fits else "!  "
        print(
            f"  [{mark}] {report['size']}: button bottom {report['bottom']}px, window {report['height']}px"
            + ("" if fits else "  <- clipped")
        )
    print(f"  {'[OK]' if ok else '[!] '} {name}")
    return ok


def check_probe_roundtrip(lang: str) -> bool:
    """Press the Check button for real and see the answer arrive on the UI thread.

    The probe runs on a worker and comes back through a queue that a timer
    drains, because touching Tk from the worker is the classic way to get a hang
    that only happens on somebody else's machine. Nothing about that path is
    visible until it fails, and when it fails the button simply stays on "..."
    forever - hence a check rather than trust.
    """
    import time

    from comfyui_mcp import configure_comfy

    label = PROBE.of(lang)
    original = configure_comfy.probe
    configure_comfy.probe = lambda host, port: (  # type: ignore[assignment]
        time.sleep(0.3) or {"ok": True, "url": "stub", "version": "TEST", "roots": []}
    )
    seen: dict[str, Any] = {}

    def inspect(root: tk.Tk) -> None:
        def walk(widget: tk.Misc, out: list[tk.Misc]) -> list[tk.Misc]:
            for child in widget.winfo_children():
                out.append(child)
                walk(child, out)
            return out

        widgets = walk(root, [])
        button = [b for b in buttons(root) if b.cget("text") == label][0]
        button.invoke()
        root.update()
        seen["busy"] = button.cget("text") != label
        deadline = time.time() + 5
        while time.time() < deadline and button.cget("text") != label:
            root.update()
            time.sleep(0.05)
        seen["returned"] = button.cget("text") == label
        seen["reported"] = any(
            isinstance(w, ttk.Label) and "TEST" in str(w.cget("text")) for w in widgets
        )
        root.destroy()

    try:
        values, path = configure_comfy.current(lang)
        restore = with_stub_mainloop(inspect)
        try:
            configure_comfy.run_window(values, path, lang)
        finally:
            tk.Tk.mainloop = restore  # type: ignore[method-assign]
    finally:
        configure_comfy.probe = original  # type: ignore[assignment]

    ok = all(seen.get(key) for key in ("busy", "returned", "reported"))
    print(f"  [{'OK ' if seen.get('busy') else '!  '}] the button is busy while the request is out")
    print(f"  [{'OK ' if seen.get('returned') else '!  '}] the button is free again after the answer")
    print(f"  [{'OK ' if seen.get('reported') else '!  '}] the version reached the window")
    return ok


def check_language(lang: str) -> bool:
    from comfyui_mcp import configure, configure_clients, configure_comfy, launcher

    print(f"=== {lang}")
    print("Window: the launcher")
    # Its labels live in the scripts rather than in the module, so building it is
    # also the only check that every marker in the root speaks both languages.
    menu = run(
        "launcher",
        lambda: launcher.run_window(launcher.discover(), lang),
        ["620x520", "480x320"],
        CLOSE.of(lang),
    )

    print("\nWindow: where ComfyUI is")
    values, path = configure_comfy.current(lang)
    comfy = run(
        "configure_comfy",
        lambda: configure_comfy.run_window(values, path, lang),
        ["780x700", "640x380"],
        SAVE.of(lang),
    )

    print("\nWindow: which tools")
    tools = run(
        "configure",
        lambda: configure.run_window(configure.catalogue(lang), path, lang),
        ["880x760", "700x420"],
        SAVE.of(lang),
    )

    print("\nWindow: connecting a client")
    clients = run(
        "configure_clients",
        lambda: configure_clients.run_window(900.0, lang),
        ["900x720", "700x440"],
        WRITE.of(lang),
    )

    print("\nAnswer from the worker thread")
    roundtrip = check_probe_roundtrip(lang)

    if not (menu and comfy and tools and clients):
        print('\n  A button is clipped - the footer must be packed side="bottom" BEFORE the body.')
    if not roundtrip:
        print("\n  The worker's answer did not arrive - see drain()/in_background in configure_comfy.py.")
    return menu and comfy and tools and clients and roundtrip


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    asked = [a.split("=", 1)[1] for a in args if a.startswith("--lang=")]
    languages = [i18n.resolve(asked[0])] if asked else list(i18n.LANGS)

    results = {}
    for index, lang in enumerate(languages):
        if index:
            print()
        results[lang] = check_language(lang)

    print()
    if all(results.values()):
        print(f"Buttons stay on screen at both sizes in {', '.join(results)}; the worker's answer arrives.")
        return 0
    print("Failed in: " + ", ".join(lang for lang, ok in results.items() if not ok))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
