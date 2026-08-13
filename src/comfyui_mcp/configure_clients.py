"""The third settings window: registering this server with an MCP client.

Eleven clients, and the reason this is a window rather than eleven templates is
that they vary along **three axes and no more**:

*The entry* - how one server is described - comes in exactly two forms. Either
the executable is a string with its arguments in a separate list (`command`,
`args`, `env`), or the whole command line is one array (`command: [...]`,
`environment`, and a `timeout` in milliseconds). Everything here is one or the
other. `cursor` is the first form plus `type: "stdio"`: Cursor's documentation
calls that field required in its table and omits it from its own example, so it
goes to Cursor and to nobody else - an extra key is harmless where it is
optional, a missing one is not, and nothing promises the others tolerate one.

*The container* - where in the document that entry sits - is `mcpServers`,
`mcp`, `mcp.servers` (OpenClaw nests it) or `mcp_servers` (Hermes, Codex).

*The file format* is JSON, YAML (Hermes) or TOML (Codex).

`timeout` is computed from `COMFYUI_RUN_TIMEOUT` rather than written down, so it
cannot drift below it. OpenCode's own default is 5000 ms - five seconds for a
generation - which is why omitting the field is not an option there.

**Every path here is a default, not a fact.** A client can move its config file
in any release, and a table of paths compiled from memory is precisely the sort
of thing that goes stale and then lies. So the path is an editable field with a
browse button, the shape is a dropdown, and the config is on screen before
anything is written. What this window really saves is the *shape* and the
absolute paths into this checkout - the two things tedious to get right by hand.

**An existing file is merged, never replaced - and not at all if it has comments
or is not JSON.** Parsing and reserialising a config full of explanatory comments
throws all of them away; YAML and TOML are emitted here but never re-read, because a
round-trip through a parser this narrow would quietly drop whatever else the
user keeps in that file. Both cases fall back to "copy this and paste it in",
and say which one applies.

    .\\uv.exe run python -m comfyui_mcp.configure_clients          # the window
    .\\uv.exe run python -m comfyui_mcp.configure_clients --print  # every config as text
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import i18n
from .config import PROJECT_ROOT, load_config
from .i18n import Text

SERVER_NAME = "comfyui"

TIMEOUT_MARGIN_S = 60

CLAUDE = "claude"
CURSOR = "cursor"
LOCAL = "local"
OPENCLAW = "openclaw"
HERMES = "hermes"
CODEX = "codex"

JSON = "json"
YAML = "yaml"
TOML = "toml"


@dataclass(frozen=True)
class Shape:
    """One combination of container, entry form and file format."""

    name: str
    root: tuple[str, ...]
    array_command: bool
    fmt: str = JSON
    summary: Text = Text(en="", ru="")


SHAPES: dict[str, Shape] = {
    CLAUDE: Shape(
        CLAUDE,
        ("mcpServers",),
        False,
        JSON,
        Text(en="mcpServers, command as a string + args, env", ru="mcpServers, command строкой + args, env"),
    ),
    CURSOR: Shape(
        CURSOR,
        ("mcpServers",),
        False,
        JSON,
        Text(en='the same plus type: "stdio"', ru='то же плюс type: "stdio"'),
    ),
    LOCAL: Shape(
        LOCAL,
        ("mcp",),
        True,
        JSON,
        Text(en='mcp, type: "local", command as an array, timeout', ru='mcp, type: "local", command массивом, timeout'),
    ),
    OPENCLAW: Shape(
        OPENCLAW,
        ("mcp", "servers"),
        False,
        JSON,
        Text(en="mcp.servers, nested two levels deep", ru="mcp.servers, вложенный на два уровня"),
    ),
    HERMES: Shape(HERMES, ("mcp_servers",), False, YAML, Text(en="mcp_servers, YAML", ru="mcp_servers, YAML")),
    CODEX: Shape(CODEX, ("mcp_servers",), False, TOML, Text(en="mcp_servers, TOML", ru="mcp_servers, TOML")),
}


@dataclass(frozen=True)
class Client:
    name: str
    title: str
    shape: str
    location: str = ""
    project_file: str = ""
    note: Text = Text(en="", ru="")


CLIENTS: tuple[Client, ...] = (
    Client(
        name="claude-code",
        title="Claude Code",
        shape=CLAUDE,
        location="~/.claude.json",
        project_file=".mcp.json",
        note=Text(
            en=(
                "The repository already holds .mcp.json - that one works when the client is started "
                "from this folder. The file below makes the server available everywhere. It is Claude "
                "Code's whole state rather than only MCP, but merging into it is safe: the other keys "
                "survive and the previous file goes to .bak. Without editing a file at all: "
                "claude mcp add-json comfyui '<json>'"
            ),
            ru=(
                "В репозитории уже лежит .mcp.json - он работает, когда клиент запущен из этой папки. "
                "Файл ниже делает сервер доступным везде. Это общее состояние Claude Code, а не "
                "только MCP, но дописывание безопасно: остальные ключи сохраняются, прежний файл "
                "уходит в .bak. Альтернатива без правки файла - claude mcp add-json comfyui '<json>'"
            ),
        ),
    ),
    Client(
        name="cursor",
        title="Cursor",
        shape=CURSOR,
        location="~/.cursor/mcp.json",
        note=Text(
            en=(
                "This path is the one shared across projects; a per-project config lives in "
                ".cursor/mcp.json of that folder, and the schema is the same. On type: \"stdio\" "
                "Cursor's documentation contradicts itself: its field table calls it required and its "
                "own example leaves it out. It is written here - it cannot be harmful as a spare key, "
                "and it can be harmful as a missing one. If something rejects it, switch the form to "
                "claude."
            ),
            ru=(
                "Путь - общий для всех проектов; проектный лежит в .cursor/mcp.json той папки, "
                "схема у них одна. Про type: \"stdio\" документация Cursor противоречит сама себе: "
                "в таблице полей он обязателен, а в её же примере отсутствует. Здесь он пишется - "
                "лишним он быть не может, а недостающим может. Если что-то его не примет, "
                "переключите формат на claude."
            ),
        ),
    ),
    Client(
        name="kilo",
        title="Kilo Code",
        shape=CLAUDE,
        location="%APPDATA%/Code/User/globalStorage/kilocode.kilo-code/settings/mcp_settings.json",
        project_file="kilo.jsonc",
        note=Text(
            en=(
                "Kilo Code uses two forms, and that is not a typo: the global mcp_settings.json is "
                "mcpServers (measured against the file the extension creates for itself), while the "
                "project's kilo.jsonc in this repository is mcp / type: local. For the project one, "
                "switch the form to local. The global file opens from the Edit Global MCP button in "
                "the MCP Servers panel."
            ),
            ru=(
                "У Kilo Code две формы, и это не опечатка: глобальный mcp_settings.json - это "
                "mcpServers (проверено по файлу, который расширение создаёт само), а проектный "
                "kilo.jsonc из этого репозитория - mcp / type: local. Для проектного переключите "
                "формат на local. Путь к глобальному открывается кнопкой Edit Global MCP в панели "
                "MCP Servers."
            ),
        ),
    ),
    Client(
        name="opencode",
        title="OpenCode",
        shape=LOCAL,
        location="~/.config/opencode/opencode.json",
        project_file="opencode.jsonc",
        note=Text(
            en=(
                "On Windows it is ~ as well, so C:\\Users\\<you>\\.config\\opencode\\. Configs are "
                "merged rather than replacing one another, so adding your own key under mcp is enough."
            ),
            ru=(
                "На Windows тоже ~, то есть C:\\Users\\<вы>\\.config\\opencode\\. Конфиги сливаются, "
                "а не заменяют друг друга, так что достаточно добавить свой ключ в mcp."
            ),
        ),
    ),
    Client(
        name="lmstudio",
        title="LM Studio",
        shape=CLAUDE,
        location="~/.lmstudio/mcp.json",
        note=Text(
            en=(
                "The same file opens inside the app: the Program tab -> Install -> Edit mcp.json. If "
                "that button is not there, your build predates MCP support - 0.3.17 or newer is needed."
            ),
            ru=(
                "Тот же файл открывается в приложении: вкладка Program -> Install -> Edit mcp.json. "
                "Если этой кнопки нет, MCP в вашей сборке ещё не появился - нужна 0.3.17 или новее."
            ),
        ),
    ),
    Client(
        name="cherry",
        title="Cherry Studio",
        shape=CLAUDE,
        note=Text(
            en=(
                "No file to edit: a server is added under Settings -> MCP. There is a JSON import "
                "there - press \"Copy block\" and paste. The server type is stdio."
            ),
            ru=(
                "Файл не правится: сервер добавляется в Настройки -> MCP. Там есть импорт JSON - "
                "нажмите \"Копировать блок\" и вставьте. Тип сервера - stdio."
            ),
        ),
    ),
    Client(
        name="mimo",
        title="MiMo Code",
        shape=LOCAL,
        note=Text(
            en=(
                "Exactly OpenCode's form: mcp, type: \"local\", command as an array, environment, "
                "timeout. The file is called mimocode.jsonc or mimocode.json, but the documentation "
                "does not say where it lives - give the path yourself, with the \"Browse...\" button. "
                "MiMo puts $schema: https://mimo.xiaomi.com/mimocode/config.json at the top of its own."
            ),
            ru=(
                "Форма ровно та же, что у OpenCode: mcp, type: \"local\", command массивом, "
                "environment, timeout. Файл называется mimocode.jsonc или mimocode.json, но где он "
                "лежит, документация не пишет - укажите путь сами, кнопкой \"Обзор...\". В шапку своего "
                "файла MiMo кладёт $schema: https://mimo.xiaomi.com/mimocode/config.json"
            ),
        ),
    ),
    Client(
        name="openclaw",
        title="OpenClaw",
        shape=OPENCLAW,
        location="~/.openclaw/openclaw.json",
        note=Text(
            en=(
                "The only one whose container is nested two levels deep: mcp.servers, not mcp. The "
                "file holds all of OpenClaw's settings, which is what makes merging into it especially "
                "right - everything else survives. Without editing a file: openclaw mcp add ..."
            ),
            ru=(
                "Единственный, у кого контейнер вложен на два уровня: mcp.servers, а не mcp. "
                "Файл общий для всех настроек OpenClaw, поэтому дописывание сюда особенно уместно - "
                "остальное сохраняется. Альтернатива без правки файла: openclaw mcp add ..."
            ),
        ),
    ),
    Client(
        name="hermes",
        title="Hermes",
        shape=HERMES,
        location="~/.hermes/config.yaml",
        note=Text(
            en=(
                "YAML, under the mcp_servers key. Hermes discovers the tools itself at startup and "
                "names them mcp_<server>_<tool>. The file is only generated here: this module cannot "
                "parse YAML, so a block has to be added to an existing config.yaml by hand - \"Copy "
                "block\" hands over the piece to paste."
            ),
            ru=(
                "YAML, ключ mcp_servers. Инструменты Hermes находит на старте сам и называет их "
                "mcp_<сервер>_<инструмент>. Файл здесь только генерируется: разбирать YAML этот "
                "модуль не умеет, поэтому в уже существующий config.yaml блок нужно дописать руками "
                "- \"Копировать блок\" отдаёт готовый кусок."
            ),
        ),
    ),
    Client(
        name="codex",
        title="Codex",
        shape=CODEX,
        location="~/.codex/config.toml",
        note=Text(
            en=(
                "TOML, the [mcp_servers.comfyui] table; environment variables go in a sub-table of "
                "their own, [mcp_servers.comfyui.env] - which is how the documentation's own example "
                "writes it. The per-project variant is .codex/config.toml in the project folder. The "
                "file need not be touched at all: codex mcp add comfyui -- <command> <args...>, taking "
                "command and args from the block below."
            ),
            ru=(
                "TOML, таблица [mcp_servers.comfyui]; переменные окружения идут отдельной "
                "подтаблицей [mcp_servers.comfyui.env] - так в примере самой документации. "
                "Проектный вариант - .codex/config.toml в папке проекта. Можно вообще не трогать "
                "файл: codex mcp add comfyui -- <command> <args...>, взяв command и args из блока ниже."
            ),
        ),
    ),
    Client(
        name="llamacpp",
        title="llama.cpp",
        shape=CLAUDE,
        location=str(PROJECT_ROOT / "llamacpp-mcp.json"),
        note=Text(
            en=(
                "llama-server has an MCP client of its own, and it expects a \"Cursor-compatible "
                "format\" - which is the claude form. The file has no permanent home; it is handed "
                "over by a flag:\n"
                "    llama-server --mcp-servers-config <path> ...\n"
                "or through LLAMA_ARG_MCP_SERVERS_CONFIG, to leave the launch command alone. There is "
                "also --mcp-servers-json for the same JSON inline. The flag is marked experimental and "
                "limits --cors-origins to localhost by itself; --agent and --tools additionally switch "
                "on built-in tools such as exec_shell_command - a separate decision that has nothing "
                "to do with this server."
            ),
            ru=(
                "У llama-server свой MCP-клиент, и он ждёт \"Cursor-compatible format\" - это как раз "
                "форма claude. Постоянного места у файла нет, он передаётся флагом:\n"
                "    llama-server --mcp-servers-config <путь> ...\n"
                "или через LLAMA_ARG_MCP_SERVERS_CONFIG, чтобы не править команду запуска. Есть и "
                "--mcp-servers-json для того же JSON прямо в строке. Флаг помечен experimental и "
                "сам ограничивает --cors-origins до localhost; --agent и --tools включают ещё и "
                "встроенные инструменты вроде exec_shell_command - это отдельное решение, к этому "
                "серверу отношения не имеющее."
            ),
        ),
    ),
)

BY_NAME: dict[str, Client] = {c.name: c for c in CLIENTS}

LLAMA_CPP_FLAG = "--mcp-servers-config"

FOOTER_NOTE = Text(
    en=(
        "A client picks a written config up only after a restart. llama.cpp is the exception: "
        f"it is handed the path with the {LLAMA_CPP_FLAG} flag."
    ),
    ru=(
        "Записанный конфиг клиент подхватит только после перезапуска. "
        f"llama.cpp - исключение: ему путь передаётся флагом {LLAMA_CPP_FLAG}."
    ),
)

NOT_CLIENTS_HEADING = Text(
    en=(
        "Not clients but MCP servers: these go *into* the config of one of the above, "
        "not the other way round."
    ),
    ru=(
        "Не клиенты, а MCP-серверы: их дописывают в конфиг одного из перечисленных выше, "
        "а не наоборот."
    ),
)

NOT_CLIENTS: tuple[tuple[str, Text], ...] = (
    (
        "Harness",
        Text(en="harness-mcp-v2 - the Harness platform's own server.", ru="harness-mcp-v2 - сервер платформы Harness."),
    ),
    (
        "Ollama",
        Text(
            en="OllamaMCPServer exposes Ollama's models as tools; Ollama itself is an inference engine.",
            ru="OllamaMCPServer отдаёт модели Ollama инструментами; сама Ollama - движок инференса.",
        ),
    ),
    (
        "vLLM",
        Text(
            en="vllm-mcp exposes multimodal generation as a tool.",
            ru="vllm-mcp отдаёт мультимодальную генерацию инструментом.",
        ),
    ),
)

ENGINE_NOTE = Text(
    en=(
        "An inference engine does not attach tools itself - the program talking to it does. Point "
        "Cherry Studio, LM Studio or Hermes at its endpoint as an OpenAI-compatible provider, and "
        "the MCP config to generate is that program's own, from the list above."
    ),
    ru=(
        "Движок инференса сам инструменты не подключает - это делает программа, которая в него "
        "ходит. Укажите его endpoint как OpenAI-совместимого провайдера в Cherry Studio, LM Studio "
        "или Hermes, и MCP-конфигом будет их собственный, из списка выше."
    ),
)


def interpreter(root: Path | None = None) -> Path:
    base = PROJECT_ROOT if root is None else root
    return base / (".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python")


def timeout_ms(run_timeout_s: float) -> int:
    """The client's per-call ceiling, derived rather than written down.

    It must outlast `COMFYUI_RUN_TIMEOUT` or the client gives up while ComfyUI is
    still generating - and the queue keeps running, so the work is not even saved
    by the failure. Computing it is what stops the two from drifting apart.
    """
    return int((run_timeout_s + TIMEOUT_MARGIN_S) * 1000)


def shape_of(name: str) -> Shape:
    try:
        return SHAPES[name]
    except KeyError:
        raise ValueError(f"unknown shape {name!r}; known: {', '.join(SHAPES)}") from None


def entry(shape: str, root: Path | None = None, run_timeout_s: float = 900.0) -> dict[str, Any]:
    """One server entry, in the form that client wants."""
    form = shape_of(shape)
    base = PROJECT_ROOT if root is None else root
    python = str(interpreter(base))
    env = {"PYTHONPATH": str(base / "src")}
    if form.array_command:
        return {
            "type": "local",
            "command": [python, "-m", "comfyui_mcp.server"],
            "environment": env,
            "enabled": True,
            "timeout": timeout_ms(run_timeout_s),
        }
    made: dict[str, Any] = {"command": python, "args": ["-m", "comfyui_mcp.server"], "env": env}
    return {"type": "stdio", **made} if shape == CURSOR else made


def root_path(shape: str) -> tuple[str, ...]:
    return shape_of(shape).root


def nest(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """`("mcp", "servers")` and a value become `{"mcp": {"servers": value}}`."""
    for key in reversed(path):
        value = {key: value}
    return value


def document(shape: str, root: Path | None = None, run_timeout_s: float = 900.0) -> dict[str, Any]:
    """A whole config file containing just this server."""
    return nest(root_path(shape), {SERVER_NAME: entry(shape, root, run_timeout_s)})


def render_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def render_yaml(obj: dict[str, Any], indent: int = 0) -> str:
    lines: list[str] = []
    pad = "  " * indent
    for key, value in obj.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(render_yaml(value, indent + 1))
        elif isinstance(value, list):
            lines.append(f"{pad}{key}:")
            lines.extend(f"{pad}  - {_yaml_scalar(item)}" for item in value)
        else:
            lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
    return "\n".join(line for line in lines if line)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    text = str(value)
    if "'" in text:  # a literal string cannot contain one, so escape properly
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    # A TOML literal string honours no escapes, which is what a Windows path wants.
    return f"'{text}'"


def render_toml(obj: dict[str, Any], prefix: tuple[str, ...] = ()) -> str:
    """Emit tables depth-first: scalars for this table, then its sub-tables."""
    blocks: list[str] = []
    scalars = {k: v for k, v in obj.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in obj.items() if isinstance(v, dict)}
    if scalars and prefix:
        head = "[" + ".".join(prefix) + "]"
        blocks.append(head + "\n" + "\n".join(f"{k} = {_toml_scalar(v)}" for k, v in scalars.items()))
    for key, value in tables.items():
        blocks.append(render_toml(value, prefix + (key,)))
    return "\n\n".join(block for block in blocks if block)


def render(obj: dict[str, Any], fmt: str = JSON) -> str:
    if fmt == JSON:
        return render_json(obj)
    if fmt == YAML:
        return render_yaml(obj)
    if fmt == TOML:
        return render_toml(obj)
    raise ValueError(f"unknown format {fmt!r}")


class MergeRefused(Exception):
    """The existing file must not be rewritten. The message says what to do instead.

    Carries both languages rather than a formatted string, because the refusal is
    shown to a person while `str(exc)` still has to mean something in a traceback
    or a log. English is what `str` gives; `of(lang)` is what the window draws.
    """

    def __init__(self, message: Text) -> None:
        self.message = message
        super().__init__(message.en)

    def of(self, lang: str) -> str:
        return self.message.of(lang)


COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|(//[^\n]*|/\*.*?\*/)', re.S)


def has_comments(text: str) -> bool:
    """Whether a JSONC file carries comments, string contents not counted.

    A URL inside a value contains `//` and is not a comment; matching strings
    first and looking only at what falls outside them is what tells them apart.
    """
    return any(match.group(1) for match in COMMENT.finditer(text))


def strip_comments(text: str) -> str:
    return COMMENT.sub(lambda m: "" if m.group(1) else m.group(0), text)


def _not_an_object(key: str) -> Text:
    return Text(
        en=f'the key "{key}" is in the file, but it is not an object',
        ru=f'ключ "{key}" в файле есть, но это не объект',
    )


def merge(existing: str, shape: str, server: dict[str, Any], name: str = SERVER_NAME) -> str:
    """Put `server` into an existing config, leaving everything else in place."""
    form = shape_of(shape)
    if form.fmt != JSON:
        raise MergeRefused(
            Text(
                en=(
                    f"{form.fmt.upper()} is only generated here, never parsed. "
                    "Copy the block and add it to the file by hand."
                ),
                ru=(
                    f"{form.fmt.upper()} здесь только генерируется, но не разбирается. "
                    "Скопируйте блок и допишите его в файл руками."
                ),
            )
        )
    if has_comments(existing):
        raise MergeRefused(
            Text(
                en=(
                    "this file carries comments, and rebuilding the JSON would wipe them. "
                    "Copy the block and paste it by hand - that way nothing is lost."
                ),
                ru=(
                    "в этом файле есть комментарии, а пересборка JSON их сотрёт. "
                    "Скопируйте блок и вставьте руками - так ничего не потеряется."
                ),
            )
        )
    try:
        data = json.loads(existing) if existing.strip() else {}
    except json.JSONDecodeError as exc:
        raise MergeRefused(
            Text(
                en=f"the file does not parse as JSON ({exc.msg}, line {exc.lineno})",
                ru=f"файл не разбирается как JSON ({exc.msg}, строка {exc.lineno})",
            )
        ) from exc
    if not isinstance(data, dict):
        raise MergeRefused(
            Text(
                en="the top level of the file is not an object - there is nowhere to add to",
                ru="на верхнем уровне файла не объект - дописывать некуда",
            )
        )

    holder: dict[str, Any] = data
    for key in form.root[:-1]:
        step = holder.get(key)
        if step is not None and not isinstance(step, dict):
            raise MergeRefused(_not_an_object(key))
        holder = holder.setdefault(key, {}) if step is None else step
    last = form.root[-1]
    block = holder.get(last)
    if block is not None and not isinstance(block, dict):
        raise MergeRefused(_not_an_object(last))
    holder[last] = {**(block or {}), name: server}
    return render_json(data) + "\n"


def expand(location: str) -> str:
    """A client's config path with `~` and %VARS% resolved, or '' if there is none."""
    if not location:
        return ""
    return str(Path(os.path.expandvars(location)).expanduser())


def preview(client: Client, shape: str = "", run_timeout_s: float = 900.0) -> str:
    """What would be written for this client, in its own format."""
    name = shape or client.shape
    return render(document(name, None, run_timeout_s), shape_of(name).fmt)


def write(path: Path, shape: str, server: dict[str, Any]) -> Text:
    """Create or merge, keeping a .bak of anything that was there. Returns a report."""
    form = shape_of(shape)
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        merged = merge(existing, shape, server)
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(existing, encoding="utf-8")
        path.write_text(merged, encoding="utf-8")
        return Text(
            en=f"added to the existing file; the previous one is kept as {backup.name}",
            ru=f"дописано в существующий файл, копия прежнего - {backup.name}",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    body = render(nest(form.root, {SERVER_NAME: server}), form.fmt)
    path.write_text(body + "\n", encoding="utf-8")
    return Text(en="file created", ru="файл создан")


def as_text(run_timeout_s: float = 900.0, lang: str = i18n.FALLBACK) -> str:
    say = i18n.speaker(lang)
    nowhere = say(
        en="(no path - give your own or copy the block)",
        ru="(путь не задан - укажите свой или скопируйте блок)",
    )
    already = say(en="already in the repository:", ru="в репозитории уже есть:")
    lines: list[str] = []
    for client in CLIENTS:
        where = expand(client.location) or nowhere
        lines.append(f"=== {client.title} [{client.shape}, {shape_of(client.shape).fmt}]")
        lines.append(f"    {where}")
        if client.project_file:
            lines.append(f"    {already} {client.project_file}")
        lines.append("")
        lines.append(preview(client, run_timeout_s=run_timeout_s))
        lines.append("")
    lines.append(f"--- {NOT_CLIENTS_HEADING.of(lang)}")
    for title, why in NOT_CLIENTS:
        lines.append(f"    {title}: {why.of(lang)}")
    lines.append(f"    {ENGINE_NOTE.of(lang)}")
    lines.append("")
    lines.append(FOOTER_NOTE.of(lang))
    return "\n".join(lines)


def run_window(run_timeout_s: float, lang: str = i18n.FALLBACK) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    say = i18n.speaker(lang)

    root = tk.Tk()
    root.title(say(en="comfyui-mcp - connecting a client", ru="comfyui-mcp - подключение к клиентам"))
    root.geometry("900x720")
    root.minsize(680, 420)

    footer = ttk.Frame(root, padding=(12, 8))
    footer.pack(side="bottom", fill="x")
    ttk.Separator(root, orient="horizontal").pack(side="bottom", fill="x")

    head = ttk.Frame(root, padding=(12, 10, 12, 4))
    head.pack(fill="x")
    ttk.Label(
        head,
        text=say(
            en=(
                f"{len(CLIENTS)} clients, and they differ in three things: how the server is described "
                "(two forms), where that entry is put, and what format the file is - JSON, YAML or "
                "TOML. What would be written is shown below."
            ),
            ru=(
                f"Клиентов {len(CLIENTS)}, а различаются они тремя вещами: как описан сервер "
                "(две формы), куда эта запись кладётся и в каком формате файл - JSON, YAML или TOML. "
                "Внизу то, что будет записано."
            ),
        ),
        wraplength=860,
        justify="left",
    ).pack(anchor="w")
    ttk.Label(
        head,
        text=say(
            en=(
                "The path and the form can be corrected: a client may have moved them, and this list "
                "is a guess rather than the truth. An existing JSON file is added to and the previous "
                "one goes to .bak; YAML and TOML are only generated, and the block is added to a "
                "finished file by hand."
            ),
            ru=(
                "Путь и форму можно поправить: клиент мог их сменить, а список здесь - предположение, "
                "а не истина. Существующий JSON дополняется, прежний уходит в .bak; YAML и TOML "
                "только генерируются, в готовый файл блок дописывается руками."
            ),
        ),
        wraplength=860,
        justify="left",
        foreground="#666666",
    ).pack(anchor="w", pady=(4, 0))

    picked = tk.StringVar(value=CLIENTS[0].name)
    path_var = tk.StringVar()
    shape_var = tk.StringVar()

    chooser = ttk.Frame(root, padding=(12, 4))
    chooser.pack(fill="x")
    for index, client in enumerate(CLIENTS):
        ttk.Radiobutton(
            chooser, text=client.title, value=client.name, variable=picked
        ).grid(row=index // 4, column=index % 4, sticky="w", padx=(0, 20), pady=1)
    ttk.Label(
        chooser,
        text="\n".join(
            [NOT_CLIENTS_HEADING.of(lang)]
            + [f"    * {title}: {why.of(lang)}" for title, why in NOT_CLIENTS]
            + [f"    {ENGINE_NOTE.of(lang)}"]
        ),
        wraplength=850,
        justify="left",
        foreground="#777777",
    ).grid(row=(len(CLIENTS) + 3) // 4, column=0, columnspan=4, sticky="w", pady=(8, 0))

    detail = ttk.Frame(root, padding=(12, 4))
    detail.pack(fill="x")
    note = ttk.Label(detail, text="", wraplength=860, justify="left", foreground="#555555")
    note.pack(anchor="w", pady=(0, 6))

    line = ttk.Frame(detail)
    line.pack(fill="x")
    ttk.Label(line, text=say(en="File:", ru="Файл:")).pack(side="left")
    path_entry = ttk.Entry(line, textvariable=path_var)
    path_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))

    def browse() -> None:
        fmt = shape_of(shape_var.get() or client_now().shape).fmt
        chosen = filedialog.asksaveasfilename(
            title=say(en="The client's config", ru="Конфиг клиента"),
            defaultextension=f".{fmt}",
            confirmoverwrite=False,
        )
        if chosen:
            path_var.set(str(Path(chosen)))

    ttk.Button(line, text=say(en="Browse...", ru="Обзор..."), width=10, command=browse).pack(side="left")
    ttk.Label(line, text=say(en="  form:", ru="  форма:")).pack(side="left")
    ttk.Combobox(
        line, textvariable=shape_var, values=list(SHAPES), width=10, state="readonly"
    ).pack(side="left", padx=(4, 0))

    shape_line = ttk.Label(detail, text="", wraplength=860, justify="left", foreground="#555555")
    shape_line.pack(anchor="w", pady=(4, 0))

    state = ttk.Label(detail, text="", wraplength=860, justify="left")
    state.pack(anchor="w", pady=(2, 0))

    caption = ttk.Label(root, text="", padding=(12, 6, 12, 0), foreground="#444444")
    caption.pack(anchor="w")
    text = tk.Text(root, wrap="none", height=14, font=("Consolas", 9))
    text.pack(fill="both", expand=True, padx=12, pady=(2, 0))

    ttk.Label(
        footer, text=FOOTER_NOTE.of(lang), wraplength=460, justify="left", foreground="#555555"
    ).pack(side="left")

    def client_now() -> Client:
        return BY_NAME[picked.get()]

    def body() -> dict[str, Any]:
        return entry(shape_var.get() or client_now().shape, None, run_timeout_s)

    def refresh(*_a: Any) -> None:
        client = client_now()
        form = shape_of(shape_var.get() or client.shape)
        note.configure(text=client.note.of(lang))
        shape_line.configure(
            text=say(
                en=f"The \"{form.name}\" form: {form.summary.en}",
                ru=f"Форма \"{form.name}\": {form.summary.ru}",
            )
        )
        caption.configure(
            text=say(
                en=f"What would be written ({form.fmt.upper()}):",
                ru=f"Что будет записано ({form.fmt.upper()}):",
            )
        )
        text.delete("1.0", "end")
        text.insert("1.0", preview(client, shape_var.get(), run_timeout_s))

        target = path_var.get().strip()
        if not target:
            state.configure(
                text=say(
                    en="No path - use \"Copy block\" and paste it inside the program itself.",
                    ru="Путь не задан - \"Копировать блок\" и вставить в самой программе.",
                ),
                foreground="#555555",
            )
            return
        path = Path(target)
        if not path.exists():
            state.configure(
                text=say(
                    en=f"No such file - it will be created whole, as {form.fmt.upper()}.",
                    ru=f"Файла нет - будет создан целиком, в формате {form.fmt.upper()}.",
                ),
                foreground="#2e7d32",
            )
            return
        try:
            merge(path.read_text(encoding="utf-8"), shape_var.get() or client.shape, body())
        except MergeRefused as exc:
            state.configure(text=f"! {exc.of(lang)}", foreground="#ef6c00")
            return
        except OSError as exc:
            unreadable = say(en="cannot be read", ru="не читается")
            state.configure(text=f"[x] {unreadable}: {exc}", foreground="#c62828")
            return
        state.configure(
            text=say(
                en=(
                    "The file is there - the entry will be added to what it already holds, and the "
                    "previous version goes to .bak."
                ),
                ru=(
                    "Файл есть - запись будет дописана к тому, что в нём уже лежит, "
                    "прежний уйдёт в .bak."
                ),
            ),
            foreground="#2e7d32",
        )

    def switch(*_a: Any) -> None:
        client = client_now()
        shape_var.set(client.shape)
        path_var.set(expand(client.location))
        refresh()

    picked.trace_add("write", switch)
    path_var.trace_add("write", refresh)
    shape_var.trace_add("write", refresh)

    def copy_json() -> None:
        root.clipboard_clear()
        root.clipboard_append(text.get("1.0", "end-1c"))
        state.configure(
            text=say(en="The block is on the clipboard.", ru="Блок скопирован в буфер обмена."),
            foreground="#2e7d32",
        )

    def save() -> None:
        target = path_var.get().strip()
        if not target:
            title = client_now().title
            messagebox.showinfo(
                say(en="Nowhere to write", ru="Некуда писать"),
                say(
                    en=(
                        f"No path is set for \"{title}\". Either give one - with \"Browse...\" or by hand - "
                        "or copy the block and paste it inside the program itself."
                    ),
                    ru=(
                        f"Для \"{title}\" путь не задан. Либо укажите его - кнопкой \"Обзор...\" "
                        "или руками, - либо скопируйте блок и вставьте в самой программе."
                    ),
                ),
            )
            return
        try:
            report = write(Path(target), shape_var.get() or client_now().shape, body())
        except MergeRefused as exc:
            messagebox.showwarning(say(en="Not written", ru="Не записано"), exc.of(lang))
            return
        except OSError as exc:
            messagebox.showwarning(say(en="Not written", ru="Не записано"), str(exc))
            return
        messagebox.showinfo(
            say(en="Done", ru="Готово"),
            say(
                en=f"{report.en}\n\n{target}\n\nRestart the client so it picks the server up.",
                ru=f"{report.ru}\n\n{target}\n\nПерезапустите клиента, чтобы он подхватил сервер.",
            ),
        )
        refresh()

    ttk.Button(footer, text=say(en="Write to file", ru="Записать в файл"), command=save).pack(side="right")
    ttk.Button(footer, text=say(en="Copy block", ru="Копировать блок"), command=copy_json).pack(
        side="right", padx=6
    )
    ttk.Button(footer, text=say(en="Close", ru="Закрыть"), command=root.destroy).pack(side="right")

    switch()
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    lang = i18n.from_args(args)
    say = i18n.speaker(lang)
    run_timeout_s = load_config().run_timeout
    if "--print" in args:
        i18n.echo(as_text(run_timeout_s, lang))
        return 0
    try:
        run_window(run_timeout_s, lang)
    except ImportError:
        i18n.echo(as_text(run_timeout_s, lang))
        i18n.echo(
            say(
                en=(
                    "\nThe window did not open: this Python build has no tkinter.\n"
                    "Copy the block you need out of the listing above."
                ),
                ru=(
                    "\nОкно не открылось: в этой сборке Python нет tkinter.\n"
                    "Скопируйте нужный блок из вывода выше."
                ),
            ),
            sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
