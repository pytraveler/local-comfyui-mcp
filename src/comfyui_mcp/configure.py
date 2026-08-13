"""The settings window: which tools this server offers.

Deliberately thin. Everything it decides - what the groups are, what a spec
means, which combinations are worth a warning, how a spec is written back - lives
in `toolsets.py` and `config.py`, under tests. This file reads that, draws it, and
writes one line to `.env`. The rule is the one the rest of the project follows for
anything that cannot be tested offline: keep it too simple to be wrong.

It holds no list of tools either. The catalogue comes from importing the server,
which is what fills `toolsets.REGISTRY` through the decorators, so a tool added
tomorrow appears here with its own docstring as its description and nobody has to
remember this file exists.

    .\\uv.exe run python -m comfyui_mcp.configure        # the window
    .\\uv.exe run python -m comfyui_mcp.configure --print  # the same, as text
    .\\uv.exe run python -m comfyui_mcp.configure --lang=en  # in English, whatever .env says

Changing the setting needs the MCP server restarted: tools are registered as the
module imports, and the client launched that process at the start of the session.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import i18n
from . import toolsets as T
from .config import PROJECT_ROOT, example_env_file, find_env_file, set_in_env_text

RISK_COLOUR = {
    "reads": "#2e7d32",
    "edits": "#1565c0",
    "writes": "#c62828",
    "runs": "#ef6c00",
    "process": "#6a1b9a",
}

RISK_LABEL = {
    "reads": i18n.Text(en="reads", ru="читает"),
    "edits": i18n.Text(en="edits (Ctrl+Z)", ru="правит (Ctrl+Z)"),
    "writes": i18n.Text(en="writes", ru="пишет"),
    "runs": i18n.Text(en="runs", ru="запускает"),
    "process": i18n.Text(en="process", ru="процесс"),
}


def catalogue(lang: str = i18n.FALLBACK) -> dict[str, Any]:
    """Every tool the server defines, with the switch's current answer for each."""
    from . import server  # noqa: F401 - importing is what fills the registry

    return T.catalogue(lang=lang)


def target_env_file(lang: str = "") -> Path:
    """The .env to write, seeded from the template if there is not one yet."""
    found = find_env_file()
    if found is not None:
        return found
    fresh = PROJECT_ROOT / ".env"
    template = example_env_file(lang)
    fresh.write_text(template.read_text(encoding="utf-8") if template.is_file() else "", encoding="utf-8")
    return fresh


def save(chosen: set[str], path: Path) -> str:
    """Write the spec for `chosen` into `path` and return it."""
    spec = T.compose(chosen)
    path.write_text(set_in_env_text(path.read_text(encoding="utf-8"), "COMFYUI_TOOLS", spec), encoding="utf-8")
    return spec


def as_text(data: dict[str, Any], lang: str = i18n.FALLBACK) -> str:
    """The same information for a terminal, and for a machine without Tk."""
    say = i18n.speaker(lang)
    lines = []
    for group in data["groups"]:
        live = sum(1 for t in group["tools"] if t["enabled"])
        mark = "x" if live == len(group["tools"]) else ("-" if live else " ")
        fixed = say(en=" (always)", ru=" (всегда)") if group["always"] else ""
        lines.append(f"[{mark}] {group['name']:<10} {live}/{len(group['tools'])}{fixed}  {group['summary']}")
        lines.append(f"      ! {group['warning']}")
        for entry in group["tools"]:
            lines.append(f"      {'x' if entry['enabled'] else ' '} {entry['name']}  ({entry['risk']})")
        lines.append("")
    for note in data["warnings"]:
        lines.append(f"{say(en='WARNING', ru='ВНИМАНИЕ')}: {note}")
    return "\n".join(lines)


def run_window(data: dict[str, Any], env_file: Path, lang: str = i18n.FALLBACK) -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    say = i18n.speaker(lang)

    root = tk.Tk()
    root.title(say(en="comfyui-mcp - which tools to offer", ru="comfyui-mcp - какие инструменты предлагать"))
    root.geometry("880x760")
    root.minsize(640, 380)

    picked: dict[str, tk.BooleanVar] = {}
    group_state: dict[str, tk.StringVar] = {}
    group_of: dict[str, str] = {}

    head = ttk.Frame(root, padding=(12, 10, 12, 4))
    head.pack(fill="x")
    ttk.Label(
        head,
        text=say(
            en=(
                "A switched-off tool is not registered at all: its schema never reaches the model's "
                "context and it cannot be called. What is switched off shows in comfy_status - so the "
                "model does not conclude that this server cannot do it."
            ),
            ru=(
                "Выключенный инструмент не регистрируется вовсе: его схема не попадает в контекст модели, "
                "и вызвать его нельзя. Что именно выключено, видно в comfy_status - так что модель не "
                "решит, будто сервер этого не умеет."
            ),
        ),
        wraplength=840,
        justify="left",
    ).pack(anchor="w")
    ttk.Label(
        head,
        text=say(
            en=(
                "This is not protection. A checkbox answers \"do I want this capability\", not \"what is "
                "allowed inside it\"; the COMFYUI_DOWNLOAD_* settings in the same .env answer the second."
            ),
            ru=(
                "Это не защита. Галочка отвечает на вопрос \"нужна ли мне такая возможность\", а не \"что "
                "внутри неё допустимо\"; на второй отвечают COMFYUI_DOWNLOAD_* в том же .env."
            ),
        ),
        wraplength=840,
        justify="left",
        foreground="#666666",
    ).pack(anchor="w", pady=(4, 0))

    footer = ttk.Frame(root, padding=(12, 4, 12, 10))
    footer.pack(side="bottom", fill="x")

    body = ttk.Frame(root)
    body.pack(fill="both", expand=True, padx=12, pady=6)
    canvas = tk.Canvas(body, highlightthickness=0)
    bar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    window = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-e.delta // 120, "units"))

    notes = tk.Text(footer, height=4, wrap="word", relief="flat", background=root.cget("background"))
    notes.pack(fill="x")
    notes.tag_configure("warn", foreground="#c62828")
    notes.configure(state="disabled")
    spec_line = ttk.Label(footer, text="", foreground="#444444")
    spec_line.pack(anchor="w", pady=(2, 6))

    def chosen() -> set[str]:
        return {name for name, var in picked.items() if var.get()}

    def refresh(*_a: Any) -> None:
        live = chosen()
        for group in data["groups"]:
            names = [t["name"] for t in group["tools"]]
            on = sum(1 for n in names if n in live)
            group_state[group["name"]].set("on" if on == len(names) else ("off" if not on else "part"))
        registry = [
            T.Tool(name=t["name"], group=g["name"], risk=t["risk"], enabled=t["name"] in live)
            for g in data["groups"]
            for t in g["tools"]
        ]
        notes.configure(state="normal")
        notes.delete("1.0", "end")
        for note in T.warnings(registry, lang):
            notes.insert("end", f"* {note}\n", "warn")
        notes.configure(state="disabled")
        spec_line.configure(text=f"COMFYUI_TOOLS={T.compose(live, registry)}    ->  {env_file}")

    def toggle_group(name: str) -> None:
        want = group_state[name].get() != "on"
        for tool, group in group_of.items():
            if group == name:
                picked[tool].set(want)
        refresh()

    for group in data["groups"]:
        box = ttk.LabelFrame(inner, padding=(10, 6))
        box.pack(fill="x", pady=5)

        header = ttk.Frame(box)
        header.pack(fill="x")
        state = tk.StringVar(value="on")
        group_state[group["name"]] = state
        check = ttk.Checkbutton(
            header,
            text=f"  {group['title']}",
            variable=state,
            onvalue="on",
            offvalue="off",
            command=lambda n=group["name"]: toggle_group(n),
        )
        check.pack(side="left")
        if group["always"]:
            check.state(["disabled"])
        badge = RISK_LABEL.get(group["risk"])
        tk.Label(
            header,
            text=f" {badge.of(lang) if badge else group['risk']} ",
            fg="white",
            bg=RISK_COLOUR.get(group["risk"], "#555555"),
            font=("Segoe UI", 8),
        ).pack(side="left", padx=8)
        count = len(group["tools"])
        ttk.Label(
            header,
            text=say(en=f"{count} tools", ru=f"{count} шт."),
            foreground="#888888",
        ).pack(side="left")

        ttk.Label(box, text=group["summary"], wraplength=780, justify="left").pack(anchor="w", padx=24)
        ttk.Label(
            box,
            text=group["warning"],
            wraplength=780,
            justify="left",
            foreground=RISK_COLOUR.get(group["risk"], "#555555"),
        ).pack(anchor="w", padx=24, pady=(2, 4))

        grid = ttk.Frame(box)
        grid.pack(fill="x", padx=24)
        for index, entry in enumerate(group["tools"]):
            var = tk.BooleanVar(value=entry["enabled"])
            picked[entry["name"]] = var
            group_of[entry["name"]] = group["name"]
            item = ttk.Checkbutton(grid, text=entry["name"], variable=var, command=refresh)
            item.grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 18))
            if group["always"]:
                item.state(["disabled"])

    def on_save() -> None:
        spec = save(chosen(), env_file)
        messagebox.showinfo(
            say(en="Saved", ru="Сохранено"),
            say(
                en=(
                    f"COMFYUI_TOOLS={spec}\n\nWritten to {env_file}.\n\n"
                    "Restart the MCP server: tools are registered as the module imports, and the "
                    "client launched that process at the start of the session."
                ),
                ru=(
                    f"COMFYUI_TOOLS={spec}\n\nЗаписано в {env_file}.\n\n"
                    "Перезапустите MCP-сервер: инструменты регистрируются при импорте модуля, "
                    "и клиент запустил этот процесс в начале сессии."
                ),
            ),
        )
        root.destroy()

    def on_reset() -> None:
        for var in picked.values():
            var.set(True)
        refresh()

    buttons = ttk.Frame(footer)
    buttons.pack(fill="x")
    ttk.Button(buttons, text=say(en="Save to .env", ru="Сохранить в .env"), command=on_save).pack(side="right")
    ttk.Button(buttons, text=say(en="Cancel", ru="Отмена"), command=root.destroy).pack(side="right", padx=6)
    ttk.Button(buttons, text=say(en="Enable all", ru="Включить всё"), command=on_reset).pack(side="left")

    refresh()
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    lang = i18n.from_args(args)
    say = i18n.speaker(lang)
    data = catalogue(lang)
    if "--print" in args:
        i18n.echo(as_text(data, lang))
        return 0
    try:
        run_window(data, target_env_file(lang), lang)
    except ImportError:
        i18n.echo(as_text(data, lang))
        i18n.echo(
            say(
                en=(
                    "\nThe window did not open: this Python build has no tkinter.\n"
                    "Edit COMFYUI_TOOLS in .env by hand - the format is described in .env.example."
                ),
                ru=(
                    "\nОкно не открылось: в этой сборке Python нет tkinter.\n"
                    "Правьте COMFYUI_TOOLS в .env вручную - формат описан в .env.example."
                ),
            ),
            sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
