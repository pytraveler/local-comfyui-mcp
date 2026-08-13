"""The other settings window: where ComfyUI is and how to reach it.

Two windows on purpose, because they answer different questions at different
moments. This one is the step straight after `install.bat` - nothing works until
`COMFYUI_ROOT` points at a real install, and `comfy_status` returns a
`config_warning` saying so. `configure.bat` is the later, optional one: which
tools the server is allowed to offer.

Built the same way as its sibling and for the same reason - everything decided
here is a plain function under test (`check`, `root_candidates`, `launch_scripts`,
`read_env`), and the Tk half only draws them. A window is the one part of this
project that cannot be tested offline, so it is kept too thin to be wrong.

Three things it does that a text editor does not:

*It asks the running ComfyUI where it lives.* `/system_stats` reports `argv`, but
`argv[0]` is routinely relative (`ComfyUI\\main.py`) and says nothing about the
root. The absolute answer is in `/internal/folder_paths`: every registered model
directory is absolute, and the ones belonging to this install run through
`<root>\\ComfyUI\\models\\...`. Taking the prefix before that `ComfyUI` segment gives
the root, and a mapped-in directory from elsewhere - `...\\modelsArchive\\models\\
checkpoints` on the machine this was written against - has no such segment and
drops out by itself.

*It lists the launch scripts that are actually there* rather than trusting the
default name. `run_nvidia_gpu.bat` is one of several a portable build ships.

*It connects.* A form can only say the port is a number; the button says whether
ComfyUI answers on it and which version did.

    .\\uv.exe run python -m comfyui_mcp.configure_comfy          # the window
    .\\uv.exe run python -m comfyui_mcp.configure_comfy --print  # current values
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import i18n
from .config import PROJECT_ROOT, example_env_file, find_env_file, set_in_env_text
from .i18n import Text

PROBE_TIMEOUT = 6.0

POLL_MS = 120

COMFY_DIR_NAME = "ComfyUI"


@dataclass(frozen=True)
class Field:
    key: str
    label: Text
    kind: str  # dir | text | port | script | secret
    hint: Text = Text(en="", ru="")


FIELDS: tuple[Field, ...] = (
    Field(
        "COMFYUI_ROOT",
        Text(en="The ComfyUI folder", ru="Папка ComfyUI"),
        "dir",
        Text(
            en="The root of the portable build - the folder holding ComfyUI\\ and the launch .bat.",
            ru="Корень portable-сборки - та, внутри которой лежат ComfyUI\\ и .bat запуска.",
        ),
    ),
    Field(
        "COMFYUI_LAUNCH_SCRIPT",
        Text(en="What starts it", ru="Чем запускать"),
        "script",
        Text(
            en="The .bat the comfy_start tool runs. The list is what was found in the folder above.",
            ru="Этот .bat запускает инструмент comfy_start. Список - то, что нашлось в папке выше.",
        ),
    ),
    Field(
        "COMFYUI_HOST",
        Text(en="Host", ru="Хост"),
        "text",
        Text(en="Where ComfyUI listens. Usually 127.0.0.1.", ru="Где слушает ComfyUI. Обычно 127.0.0.1."),
    ),
    Field(
        "COMFYUI_PORT",
        Text(en="Port", ru="Порт"),
        "port",
        Text(en="ComfyUI's port. 8188 by default.", ru="Порт ComfyUI. По умолчанию 8188."),
    ),
    Field(
        "COMFYUI_WORKFLOWS_DIR",
        Text(en="Workflows folder", ru="Папка воркфлоу"),
        "dir",
        Text(
            en="Where the API-format files live. Empty means workflows\\ in this project.",
            ru="Где лежат файлы в API-формате. Пусто - workflows\\ в этом проекте.",
        ),
    ),
    Field(
        "COMFYUI_EXPORT_DIR",
        Text(en="Exports folder", ru="Папка экспорта"),
        "dir",
        Text(
            en="Where save_workspace writes a UI export. Empty means exports\\ in this project.",
            ru="Куда save_workspace пишет UI-экспорт. Пусто - exports\\ в этом проекте.",
        ),
    ),
    Field(
        "COMFYUI_DOWNLOAD_TOKEN",
        Text(en="Token for private repositories", ru="Токен для закрытых репозиториев"),
        "secret",
        Text(
            en="HuggingFace or Civitai, for gated models. Sent only to the origin host.",
            ru="HuggingFace или Civitai, для gated-моделей. Уходит только на исходный хост.",
        ),
    ),
)

FIELD: dict[str, Field] = {f.key: f for f in FIELDS}

DEFAULTS: dict[str, str] = {
    "COMFYUI_HOST": "127.0.0.1",
    "COMFYUI_PORT": "8188",
    "COMFYUI_LAUNCH_SCRIPT": "run_nvidia_gpu.bat",
    "COMFYUI_WORKFLOWS_DIR": str(PROJECT_ROOT / "workflows"),
    "COMFYUI_EXPORT_DIR": str(PROJECT_ROOT / "exports"),
}


def read_env(text: str) -> dict[str, str]:
    """The raw `KEY=value` pairs in a .env, comments and blanks ignored.

    Raw rather than `load_config()`, because the window edits the file: a field
    showing a default that is not written down would write it down on save, and
    a .env full of values nobody chose is one nobody can read afterwards.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def current(lang: str = "") -> tuple[dict[str, str], Path]:
    """What is in the .env now, and which file that is."""
    path = find_env_file()
    if path is None:
        template = example_env_file(lang)
        path = PROJECT_ROOT / ".env"
        path.write_text(template.read_text(encoding="utf-8") if template.is_file() else "", encoding="utf-8")
    values = read_env(path.read_text(encoding="utf-8"))
    return {field.key: values.get(field.key, "") for field in FIELDS}, path


def save(values: dict[str, str], path: Path) -> None:
    """Write every field back, one line each, leaving the rest of the file alone."""
    text = path.read_text(encoding="utf-8")
    for field in FIELDS:
        text = set_in_env_text(text, field.key, values.get(field.key, "").strip())
    path.write_text(text, encoding="utf-8")


@dataclass(frozen=True)
class Problem:
    key: str
    severity: str  # error | note
    message: Text


def check(values: dict[str, str]) -> list[Problem]:
    """Everything worth saying about a set of values, worst first.

    `error` means the server will not work as configured; `note` means it will,
    but something is not where it was said to be. Neither blocks saving - a root
    on a drive that is not mounted yet is a real situation, and refusing to
    record the user's own answer helps nobody.
    """
    out: list[Problem] = []
    root = values.get("COMFYUI_ROOT", "").strip()
    script = values.get("COMFYUI_LAUNCH_SCRIPT", "").strip()

    if not root:
        out.append(
            Problem(
                "COMFYUI_ROOT",
                "error",
                Text(
                    en="not set - without it comfy_start and comfy_status do not work",
                    ru="не задана - без неё comfy_start и comfy_status не работают",
                ),
            )
        )
    elif not Path(root).is_dir():
        out.append(
            Problem(
                "COMFYUI_ROOT",
                "error",
                Text(en=f"no such folder: {root}", ru=f"нет такой папки: {root}"),
            )
        )
    elif not (Path(root) / COMFY_DIR_NAME).is_dir():
        out.append(
            Problem(
                "COMFYUI_ROOT",
                "error",
                Text(
                    en=(
                        f"there is no {COMFY_DIR_NAME}\\ inside it - this looks like one level "
                        f"below the root of the portable build rather than the root itself"
                    ),
                    ru=(
                        f"внутри нет {COMFY_DIR_NAME}\\ - это, похоже, не корень portable-сборки, "
                        f"а папка уровнем ниже"
                    ),
                ),
            )
        )
    elif script and not (Path(root) / script).is_file():
        out.append(
            Problem(
                "COMFYUI_LAUNCH_SCRIPT",
                "note",
                Text(en=f"there is no {script} in that folder", ru=f"файла {script} в этой папке нет"),
            )
        )

    port = values.get("COMFYUI_PORT", "").strip()
    if port:
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            out.append(
                Problem(
                    "COMFYUI_PORT",
                    "error",
                    Text(
                        en=f"has to be a number from 1 to 65535, not {port!r}",
                        ru=f"должен быть числом 1-65535, а не {port!r}",
                    ),
                )
            )

    for key in ("COMFYUI_WORKFLOWS_DIR", "COMFYUI_EXPORT_DIR"):
        value = values.get(key, "").strip()
        if value and not Path(value).is_dir():
            out.append(
                Problem(
                    key,
                    "note",
                    Text(
                        en="the folder is not there - it will be created on the first write",
                        ru="папки нет - она будет создана при первой записи",
                    ),
                )
            )

    return sorted(out, key=lambda p: p.severity != "error")


def launch_scripts(root: str) -> list[str]:
    """The .bat files sitting in the portable root, most likely first."""
    folder = Path(root)
    if not root or not folder.is_dir():
        return []
    found = sorted(entry.name for entry in folder.glob("*.bat") if entry.is_file())
    return sorted(found, key=lambda name: (0 if "nvidia" in name.lower() else 1, name))


def root_candidates(folders: Iterable[str]) -> list[str]:
    """Roots implied by a list of absolute model directories, most cited first.

    A path belonging to this install runs through `<root>\\ComfyUI\\...`, so the
    part before that segment is the root. A directory mapped in from elsewhere by
    extra_model_paths.yaml has no such segment and disappears without a rule of
    its own - which is what makes this safe to run on a machine whose checkpoints
    live on another drive.
    """
    votes: Counter[str] = Counter()
    for raw in folders:
        parts = Path(raw).parts
        for index in range(len(parts) - 1, 0, -1):
            if parts[index] == COMFY_DIR_NAME:
                votes[str(Path(*parts[:index]))] += 1
                break
    return [root for root, _ in votes.most_common()]


def probe(host: str, port: str) -> dict[str, Any]:
    """Ask ComfyUI on host:port who it is. Never raises: the answer is the report."""
    import httpx

    url = f"http://{host or '127.0.0.1'}:{port or '8188'}"
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT) as http:
            answer = http.get(f"{url}/system_stats")
            answer.raise_for_status()
            stats = answer.json()
            folders: dict[str, list[str]] = {}
            try:
                folders = http.get(f"{url}/internal/folder_paths").json()
            except Exception:  # noqa: BLE001 - older ComfyUI; the version alone still answers
                folders = {}
    except Exception as exc:  # noqa: BLE001 - "not answering" is the useful answer
        return {"ok": False, "url": url, "error": str(exc) or exc.__class__.__name__}

    system = stats.get("system") or {}
    every = [path for paths in folders.values() for path in paths]
    return {
        "ok": True,
        "url": url,
        "version": system.get("comfyui_version") or "?",
        "roots": root_candidates(every),
    }


def as_text(values: dict[str, str], path: Path, lang: str = i18n.FALLBACK) -> str:
    say = i18n.speaker(lang)
    empty = say(en="empty", ru="пусто")
    lines = [f"{path}", ""]
    for field in FIELDS:
        value = values.get(field.key, "")
        shown = "*" * 8 if field.kind == "secret" and value else value
        default = DEFAULTS.get(field.key, "")
        blank = f"({empty} -> {default})" if default else f"({empty})"
        lines.append(f"{field.key:<28} {shown or blank}")
    problems = check(values)
    if problems:
        lines.append("")
        for problem in problems:
            mark = say(en="ERROR", ru="ОШИБКА") if problem.severity == "error" else say(en="note", ru="заметка")
            lines.append(f"{mark}: {problem.key} - {problem.message.of(lang)}")
    return "\n".join(lines)


def run_window(values: dict[str, str], path: Path, lang: str = i18n.FALLBACK) -> None:
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    say = i18n.speaker(lang)

    root = tk.Tk()
    root.title(say(en="comfyui-mcp - where ComfyUI is", ru="comfyui-mcp - где ComfyUI"))
    root.geometry("780x700")
    root.minsize(620, 360)

    bound: dict[str, tk.StringVar] = {f.key: tk.StringVar(value=values.get(f.key, "")) for f in FIELDS}
    results: queue.Queue = queue.Queue()
    outstanding = [0]

    def in_background(work, done) -> None:
        """Run `work` off the UI thread and hand the result back on it.

        Through a queue rather than calling `after` from the worker: touching Tk
        from a second thread is the classic way to get a hang that only happens
        on somebody else's machine.
        """

        def run() -> None:
            try:
                results.put((done, work()))
            except Exception as exc:  # noqa: BLE001 - reported, never raised into Tk
                results.put((done, {"ok": False, "error": str(exc)}))

        outstanding[0] += 1
        threading.Thread(target=run, daemon=True).start()
        root.after(POLL_MS, drain)

    def drain() -> None:
        """Poll only while something is in flight.

        A timer that reschedules itself forever outlives the window: the pending
        callback fires against a destroyed interpreter and Tcl reports an invalid
        command name. Nothing is waiting at idle anyway - the probes are the only
        thing that ever crosses threads.
        """
        while not results.empty():
            callback, payload = results.get()
            outstanding[0] -= 1
            callback(payload)
        if outstanding[0] > 0 and root.winfo_exists():
            root.after(POLL_MS, drain)

    footer = ttk.Frame(root, padding=(12, 8))
    footer.pack(side="bottom", fill="x")
    ttk.Separator(root, orient="horizontal").pack(side="bottom", fill="x")

    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    canvas = tk.Canvas(frame, highlightthickness=0)
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
    body = ttk.Frame(canvas, padding=12)
    body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    holder = canvas.create_window((0, 0), window=body, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(holder, width=e.width))
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-e.delta // 120, "units"))

    ttk.Label(
        body,
        text=say(
            en=(
                "Without COMFYUI_ROOT the server starts but finds no ComfyUI: comfy_status returns "
                "config_warning, and comfy_start has nothing to launch. The other fields can be left "
                "empty - what would be used instead is shown in brackets."
            ),
            ru=(
                "Без COMFYUI_ROOT сервер запускается, но не находит ComfyUI: comfy_status возвращает "
                "config_warning, а comfy_start запускать нечего. Остальные поля можно оставить пустыми - "
                "в скобках то, что подставится."
            ),
        ),
        wraplength=720,
        justify="left",
    ).pack(anchor="w", pady=(0, 10))

    status: dict[str, ttk.Label] = {}
    browse_label = say(en="Browse...", ru="Обзор...")

    def field_row(field: Field) -> ttk.Frame:
        box = ttk.Frame(body)
        box.pack(fill="x", pady=(0, 8))
        ttk.Label(box, text=field.label.of(lang), font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(
            box, text=field.hint.of(lang), foreground="#666666", wraplength=720, justify="left"
        ).pack(anchor="w")
        return box

    def browse_into(key: str, title: str):
        """A command for a Browse... button. A named closure, because building one
        inline evaluates the dialog at layout time and the window never appears."""

        def choose() -> None:
            picked = filedialog.askdirectory(title=title)
            if picked:
                bound[key].set(str(Path(picked)))

        return choose

    box = field_row(FIELD["COMFYUI_ROOT"])
    line = ttk.Frame(box)
    line.pack(fill="x", pady=(2, 0))
    ttk.Entry(line, textvariable=bound["COMFYUI_ROOT"]).pack(side="left", fill="x", expand=True)
    ttk.Button(
        line,
        text=browse_label,
        width=10,
        command=browse_into(
            "COMFYUI_ROOT",
            say(en="The root of the portable ComfyUI build", ru="Корень portable-сборки ComfyUI"),
        ),
    ).pack(side="left", padx=(6, 0))

    detect_label = say(en="Find", ru="Найти")

    def found(result: dict[str, Any]) -> None:
        detect.state(["!disabled"])
        detect.configure(text=detect_label)
        if not result.get("ok"):
            messagebox.showwarning(
                say(en="No answer", ru="Не отвечает"),
                say(
                    en=(
                        f"ComfyUI does not answer on {result.get('url', '')}:\n{result.get('error', '')}\n\n"
                        "Start it and try again - or name the folder by hand."
                    ),
                    ru=(
                        f"ComfyUI не отвечает на {result.get('url', '')}:\n{result.get('error', '')}\n\n"
                        "Запустите её и попробуйте снова - или укажите папку вручную."
                    ),
                ),
            )
            return
        roots = result.get("roots") or []
        if not roots:
            messagebox.showinfo(
                say(en="Could not work it out", ru="Не удалось определить"),
                say(
                    en=(
                        f"ComfyUI {result['version']} answers, but its folder cannot be derived from "
                        "the model paths. Name it by hand."
                    ),
                    ru=(
                        f"ComfyUI {result['version']} отвечает, но её папку по путям моделей не вывести. "
                        "Укажите вручную."
                    ),
                ),
            )
            return
        bound["COMFYUI_ROOT"].set(roots[0])

    def detect_root() -> None:
        detect.state(["disabled"])
        detect.configure(text="...")
        host, port = bound["COMFYUI_HOST"].get(), bound["COMFYUI_PORT"].get()
        in_background(lambda: probe(host, port), found)

    detect = ttk.Button(line, text=detect_label, command=detect_root, width=10)
    detect.pack(side="left", padx=(6, 0))
    status["COMFYUI_ROOT"] = ttk.Label(box, text="", wraplength=720, justify="left")
    status["COMFYUI_ROOT"].pack(anchor="w", pady=(2, 0))

    box = field_row(FIELD["COMFYUI_LAUNCH_SCRIPT"])
    script_box = ttk.Combobox(box, textvariable=bound["COMFYUI_LAUNCH_SCRIPT"])
    script_box.pack(fill="x", pady=(2, 0))
    status["COMFYUI_LAUNCH_SCRIPT"] = ttk.Label(box, text="", foreground="#ef6c00")
    status["COMFYUI_LAUNCH_SCRIPT"].pack(anchor="w")

    box = ttk.Frame(body)
    box.pack(fill="x", pady=(0, 8))
    ttk.Label(box, text=say(en="Host and port", ru="Хост и порт"), font=("Segoe UI", 9, "bold")).pack(anchor="w")
    ttk.Label(
        box,
        text=say(
            en="Where ComfyUI listens. Usually 127.0.0.1:8188.",
            ru="Где слушает ComfyUI. Обычно 127.0.0.1:8188.",
        ),
        foreground="#666666",
    ).pack(anchor="w")
    line = ttk.Frame(box)
    line.pack(fill="x", pady=(2, 0))
    ttk.Entry(line, textvariable=bound["COMFYUI_HOST"], width=24).pack(side="left")
    ttk.Label(line, text=" : ").pack(side="left")
    ttk.Entry(line, textvariable=bound["COMFYUI_PORT"], width=8).pack(side="left")

    test_label = say(en="Check", ru="Проверить")

    def checked(result: dict[str, Any]) -> None:
        test.state(["!disabled"])
        test.configure(text=test_label)
        if result.get("ok"):
            answers = say(en="ComfyUI answers", ru="отвечает ComfyUI")
            probe_line.configure(text=f"[ok] {answers} {result['version']}", foreground="#2e7d32")
        else:
            silent = say(en="no answer", ru="нет ответа")
            probe_line.configure(text=f"[x] {result.get('error') or silent}", foreground="#c62828")

    def test_connection() -> None:
        test.state(["disabled"])
        test.configure(text="...")
        host, port = bound["COMFYUI_HOST"].get(), bound["COMFYUI_PORT"].get()
        in_background(lambda: probe(host, port), checked)

    test = ttk.Button(line, text=test_label, command=test_connection, width=12)
    test.pack(side="left", padx=(10, 0))
    probe_line = ttk.Label(box, text="")
    probe_line.pack(anchor="w", pady=(2, 0))
    status["COMFYUI_PORT"] = ttk.Label(box, text="", foreground="#c62828")
    status["COMFYUI_PORT"].pack(anchor="w")

    for key in ("COMFYUI_WORKFLOWS_DIR", "COMFYUI_EXPORT_DIR"):
        field = FIELD[key]
        box = field_row(field)
        line = ttk.Frame(box)
        line.pack(fill="x", pady=(2, 0))
        ttk.Entry(line, textvariable=bound[key]).pack(side="left", fill="x", expand=True)
        ttk.Button(
            line, text=browse_label, width=10, command=browse_into(key, field.label.of(lang))
        ).pack(side="left", padx=(6, 0))
        status[key] = ttk.Label(box, text="", foreground="#ef6c00")
        status[key].pack(anchor="w")

    box = field_row(FIELD["COMFYUI_DOWNLOAD_TOKEN"])
    line = ttk.Frame(box)
    line.pack(fill="x", pady=(2, 0))
    token = ttk.Entry(line, textvariable=bound["COMFYUI_DOWNLOAD_TOKEN"], show="*")
    token.pack(side="left", fill="x", expand=True)
    shown = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        line,
        text=say(en="show", ru="показать"),
        variable=shown,
        command=lambda: token.configure(show="" if shown.get() else "*"),
    ).pack(side="left", padx=(6, 0))

    def collect() -> dict[str, str]:
        return {key: var.get() for key, var in bound.items()}

    def refresh(*_a: Any) -> None:
        values = collect()
        scripts = launch_scripts(values["COMFYUI_ROOT"])
        script_box.configure(values=scripts)
        for label in status.values():
            label.configure(text="")
        problems = check(values)
        for problem in problems:
            label = status.get(problem.key)
            if label is not None and not label.cget("text"):
                label.configure(
                    text=("[x] " if problem.severity == "error" else "! ") + problem.message.of(lang),
                    foreground="#c62828" if problem.severity == "error" else "#ef6c00",
                )
        if not any(p.key == "COMFYUI_ROOT" for p in problems):
            inside = say(en=f"{COMFY_DIR_NAME}\\ is inside", ru=f"внутри есть {COMFY_DIR_NAME}\\")
            found_bats = say(en=f", .bat files found: {len(scripts)}", ru=f", .bat найдено: {len(scripts)}")
            status["COMFYUI_ROOT"].configure(
                text=f"[ok] {inside}" + (found_bats if scripts else ""),
                foreground="#2e7d32",
            )

    for var in bound.values():
        var.trace_add("write", refresh)

    ttk.Label(footer, text=f"-> {path}", foreground="#444444").pack(side="left")

    def on_save() -> None:
        values = collect()
        errors = [p for p in check(values) if p.severity == "error"]
        if errors:
            listed = "\n".join(f"* {p.key}: {p.message.of(lang)}" for p in errors)
            anyway = say(en="Save anyway?", ru="Всё равно сохранить?")
            if not messagebox.askyesno(say(en="There are problems", ru="Есть проблемы"), f"{listed}\n\n{anyway}"):
                return
        save(values, path)
        messagebox.showinfo(
            say(en="Saved", ru="Сохранено"),
            say(
                en=(
                    f"Written to {path}.\n\nRestart the MCP server: the settings are read once, "
                    "when the process starts."
                ),
                ru=(
                    f"Записано в {path}.\n\nПерезапустите MCP-сервер: настройки читаются один раз, "
                    "при старте процесса."
                ),
            ),
        )
        root.destroy()

    ttk.Button(footer, text=say(en="Save to .env", ru="Сохранить в .env"), command=on_save).pack(side="right")
    ttk.Button(footer, text=say(en="Cancel", ru="Отмена"), command=root.destroy).pack(side="right", padx=6)

    refresh()
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    lang = i18n.from_args(args)
    say = i18n.speaker(lang)
    values, path = current(lang)
    if "--print" in args:
        i18n.echo(as_text(values, path, lang))
        return 0
    try:
        run_window(values, path, lang)
    except ImportError:
        i18n.echo(as_text(values, path, lang))
        i18n.echo(
            say(
                en=(
                    "\nThe window did not open: this Python build has no tkinter.\n"
                    f"Edit {path} by hand - every variable is explained in .env.example."
                ),
                ru=(
                    "\nОкно не открылось: в этой сборке Python нет tkinter.\n"
                    f"Правьте {path} вручную - пояснения к каждой переменной в .env.example."
                ),
            ),
            sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
