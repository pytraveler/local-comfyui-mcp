"""Which of the server's tools are offered, and the switch that narrows them.

Every tool is registered through `server.tool(group, risk)` rather than
`mcp.tool()` directly, so the group and the risk class are declared in the one
place anybody editing a tool is already looking at. Nothing here holds a list of
tool names: `REGISTRY` is filled by those decorators as `server.py` imports, which
is what keeps a new tool from being invisible to the settings window. The rule is
the one the rest of the project follows - a second copy of a list goes stale and
then lies about itself.

Three reasons this exists, and only the last is about safety:

*Context.* All 48 schemas reach the model at the start of every session. Somebody
who only ever runs workflow files pays for twenty canvas tools they never call.

*The decision is made when nobody is in a hurry.* A permission prompt raised in
the middle of a task is answered by someone who wants the task to finish. A
checkbox set beforehand is a different conversation.

*One meaning across clients.* Three client configs are committed here and each has
its own permission model. A refusal on this side means the same thing in all of them.

What this is **not** is a security boundary. On/off is a coarse grid: the risk in
`download_model` is not that it exists but that it can be pointed anywhere, and
that is answered by `COMFYUI_DOWNLOAD_ALLOW_HOSTS` rather than by a checkbox.
What ships enabled by default is the real posture - most people never open the
settings - so this narrows an install, it does not secure one.

The spec (`COMFYUI_TOOLS`) is a comma-separated list of group or tool names, each
optionally prefixed with `-`:

    all                          everything (the default)
    -download,-process           everything except those two groups
    status,workflows,run         only these
    workspace,-remove_workspace_nodes    a group, less one tool

A tool name beats a group name whatever the order, so the last line needs no
thought about precedence. `status` is always on: it is where a caller finds out
that the rest was switched off and how to switch it back, and a server that
cannot say that is indistinguishable from one that never had the tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .i18n import FALLBACK, Text

RISKS: dict[str, Text] = {
    "reads": Text(
        en="changes nothing",
        ru="ничего не меняет",
    ),
    "edits": Text(
        en="changes the canvas; one call is one Ctrl+Z",
        ru="меняет холст; один вызов - один Ctrl+Z",
    ),
    "writes": Text(
        en="creates or replaces files; no undo",
        ru="создаёт или заменяет файлы; отката нет",
    ),
    "runs": Text(
        en="occupies the GPU, or ends work already in flight",
        ru="занимает GPU или обрывает уже идущую работу",
    ),
    "process": Text(
        en="starts, stops or replaces ComfyUI, or reloads the browser tab",
        ru="запускает, останавливает или заменяет ComfyUI, перезагружает вкладку",
    ),
}


@dataclass(frozen=True)
class Group:
    """A set of tools that are wanted or not wanted together."""

    name: str
    title: Text
    risk: str
    summary: Text
    warning: Text = Text(en="", ru="")
    always: bool = False


GROUPS: tuple[Group, ...] = (
    Group(
        name="status",
        title=Text(en="Status", ru="Состояние"),
        risk="reads",
        summary=Text(
            en="Whether ComfyUI is running, and what its VRAM and queue look like.",
            ru="Запущена ли ComfyUI, что с VRAM и очередью.",
        ),
        warning=Text(
            en=(
                "Always on: this is where a caller finds out that the rest was switched "
                "off, and what switches it back on."
            ),
            ru="Всегда включено: отсюда видно, что остальное выключено, и чем включается обратно.",
        ),
        always=True,
    ),
    Group(
        name="workflows",
        title=Text(en="Workflows and reference", ru="Воркфлоу и справочники"),
        risk="reads",
        summary=Text(
            en="Reading workflow files, node schemas and model lists; viewing images.",
            ru="Чтение файлов воркфлоу, схем нод, списков моделей, просмотр картинок.",
        ),
        warning=Text(
            en="Read-only. Workflow files are read, never rewritten.",
            ru="Только чтение. Файлы воркфлоу читаются, но не переписываются.",
        ),
    ),
    Group(
        name="logs",
        title=Text(en="Logs", ru="Логи"),
        risk="reads",
        summary=Text(
            en="ComfyUI's console and the browser's - the only place a failed node says anything.",
            ru="Консоль ComfyUI и консоль браузера - единственное место, где видно упавшую ноду.",
        ),
        warning=Text(
            en=(
                "Read-only, but it reads everything: if a pack prints a token or a path to "
                "the console, that reaches the answer. Switch it off if your console holds "
                "things it should not."
            ),
            ru=(
                "Только чтение, но читается всё подряд: если пак печатает в консоль токен или путь, "
                "это попадёт в ответ. Выключайте, если в консоли бывает лишнее."
            ),
        ),
    ),
    Group(
        name="workspace",
        title=Text(en="Canvas: reading", ru="Холст: чтение"),
        risk="writes",
        summary=Text(
            en=(
                "The graph open in the browser: reading, diagnosis, screenshot, navigation, "
                "saving to a file."
            ),
            ru="Граф, открытый в браузере: чтение, диагностика, скриншот, навигация, сохранение в файл.",
        ),
        warning=Text(
            en=(
                "Changes only what is on screen: the selection and the current subgraph. "
                "Except save_workspace, which writes a file into exports/ or workflows/."
            ),
            ru=(
                "Меняет только то, что видно: выделение и текущий подграф. "
                "Исключение - save_workspace: он пишет файл в exports/ или workflows/."
            ),
        ),
    ),
    Group(
        name="edit",
        title=Text(en="Canvas: editing", ru="Холст: правка"),
        risk="writes",
        summary=Text(
            en="Values, properties, labels, links, nodes, layout and groups on the open canvas.",
            ru="Значения, свойства, метки, связи, ноды, раскладка и группы на открытом холсте.",
        ),
        warning=Text(
            en=(
                "One call is one Ctrl+Z, and the undo stack is shared with yours. "
                "Except load_workspace, which replaces the whole canvas and cannot be undone "
                "(a backup is written to exports/ first)."
            ),
            ru=(
                "Один вызов - один Ctrl+Z, и стек отмены общий с вашим. "
                "Исключение - load_workspace: он заменяет холст целиком, и отменить это нельзя "
                "(перед заменой пишется резервная копия в exports/)."
            ),
        ),
    ),
    Group(
        name="run",
        title=Text(en="Running", ru="Запуск"),
        risk="runs",
        summary=Text(
            en="Queueing a workflow, progress, results, interrupting, unloading models.",
            ru="Постановка воркфлоу в очередь, прогресс, результаты, прерывание, выгрузка моделей.",
        ),
        warning=Text(
            en=(
                "Occupies the GPU for minutes and writes files into output/. interrupt ends a "
                "run already counting - the finished steps are lost and the models reload."
            ),
            ru=(
                "Занимает GPU на минуты и пишет файлы в output/. interrupt обрывает уже идущий "
                "счёт - сделанные шаги теряются, модели грузятся заново."
            ),
        ),
    ),
    Group(
        name="download",
        title=Text(en="Downloading models", ru="Загрузка моделей"),
        risk="writes",
        summary=Text(
            en="Fetching model files into the folders ComfyUI reads them from.",
            ru="Скачивание файлов моделей в папки, из которых их читает ComfyUI.",
        ),
        warning=Text(
            en=(
                "Writes gigabytes from a URL that a note in somebody else's workflow can "
                "suggest. No undo. Narrow COMFYUI_DOWNLOAD_ALLOW_HOSTS if you leave it on."
            ),
            ru=(
                "Пишет гигабайты по ссылке, которую может подсказать заметка в чужом воркфлоу. "
                "Отката нет. Сузьте COMFYUI_DOWNLOAD_ALLOW_HOSTS, если оставляете включённым."
            ),
        ),
    ),
    Group(
        name="process",
        title=Text(en="Process and tab", ru="Процесс и вкладка"),
        risk="process",
        summary=Text(
            en="Starting, stopping and restarting ComfyUI; reloading the browser tab.",
            ru="Запуск, остановка и перезапуск ComfyUI; перезагрузка вкладки браузера.",
        ),
        warning=Text(
            en=(
                "A restart loses the queue and unloads the models; a tab reload loses unsaved "
                "canvas edits. Neither can be undone."
            ),
            ru=(
                "Перезапуск теряет очередь и выгружает модели, а перезагрузка вкладки - "
                "несохранённые правки холста. Отката нет ни у того, ни у другого."
            ),
        ),
    ),
)

BY_NAME: dict[str, Group] = {g.name: g for g in GROUPS}
ALWAYS: frozenset[str] = frozenset(g.name for g in GROUPS if g.always)


_NO_POLL = Text(
    en="a run with nothing to poll - nothing shows whether it is still counting",
    ru="запуск без опроса прогресса - не видно, идёт ли счёт",
)

DEPENDS: tuple[tuple[str, tuple[str, ...], Text], ...] = (
    ("run_workflow", ("get_progress",), _NO_POLL),
    ("run_workspace", ("get_progress",), _NO_POLL),
    (
        "run_workflow",
        ("get_result", "show_image"),
        Text(
            en="the result comes back as file paths, with nothing to look at them with",
            ru="результат вернётся путями, но посмотреть их будет нечем",
        ),
    ),
    (
        "download_model",
        ("get_download_progress",),
        Text(
            en="a background download with nothing to poll - nothing shows whether it is moving",
            ru="загрузка в фоне без опроса - не видно, идёт ли она",
        ),
    ),
    (
        "run_workspace",
        ("get_workspace_graph",),
        Text(
            en="queueing the canvas blind: what actually goes into the queue cannot be read",
            ru="запуск холста вслепую: что именно уходит в очередь, не прочитать",
        ),
    ),
    (
        "load_workspace",
        ("get_workspace_graph",),
        Text(
            en="replacing the canvas blind: what came out of it cannot be read",
            ru="замена холста вслепую: что получилось, не прочитать",
        ),
    ),
)

_EDIT_NEEDS_READ = "get_workspace_graph"


@dataclass(frozen=True)
class Selection:
    """A parsed `COMFYUI_TOOLS` spec, ready to be asked about one tool at a time.

    Asked rather than resolved up front, because the decorators run as `server.py`
    imports and there is no moment before that at which the full list of tools
    exists. Unknown tokens are therefore reported afterwards by `unknown`.
    """

    spec: str
    base_on: bool = True
    on: frozenset[str] = frozenset()
    off: frozenset[str] = frozenset()

    def allows(self, tool: str, group: str) -> bool:
        if group in ALWAYS:
            return True
        if tool in self.off:
            return False
        if tool in self.on:
            return True
        if group in self.off:
            return False
        if group in self.on:
            return True
        return self.base_on

    @property
    def tokens(self) -> frozenset[str]:
        return self.on | self.off

    @property
    def narrowed(self) -> bool:
        return bool(self.tokens) or not self.base_on


def parse(spec: str) -> Selection:
    """Read a `COMFYUI_TOOLS` spec.

    An empty spec is "everything", and so is one that starts with a `-` token -
    `-download` plainly means "all but that". A spec that starts with a positive
    token means "only these", which is the other thing a person writes.
    """
    on: set[str] = set()
    off: set[str] = set()
    base: bool | None = None

    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        negative = token.startswith("-")
        name = token.lstrip("-").strip()
        if not name:
            continue
        if name == "all":
            base = not negative
            on.clear()
            off.clear()
            continue
        if base is None:
            base = negative
        (off if negative else on).add(name)
        (on if negative else off).discard(name)

    return Selection(
        spec=spec,
        base_on=True if base is None else base,
        on=frozenset(on),
        off=frozenset(off),
    )


@dataclass(frozen=True)
class Tool:
    name: str
    group: str
    risk: str
    enabled: bool
    summary: str = ""


REGISTRY: list[Tool] = []


def record(name: str, group: str, risk: str, enabled: bool, summary: str = "") -> Tool:
    """Note a tool as `server.py` defines it, whether or not it was registered."""
    if group not in BY_NAME:
        raise ValueError(f"{name}: unknown tool group {group!r}; known: {', '.join(BY_NAME)}")
    if risk not in RISKS:
        raise ValueError(f"{name}: unknown risk class {risk!r}; known: {', '.join(RISKS)}")
    entry = Tool(name=name, group=group, risk=risk, enabled=enabled, summary=summary)
    REGISTRY.append(entry)
    return entry


def enabled_names(registry: Iterable[Tool] | None = None) -> set[str]:
    return {t.name for t in (REGISTRY if registry is None else registry) if t.enabled}


def unknown(selection: Selection, registry: Iterable[Tool] | None = None) -> list[str]:
    """Tokens in the spec that name neither a group nor a tool.

    Worth reporting rather than ignoring: a typo silently leaves a group switched
    on, and nothing else on this side would ever mention it.
    """
    known = set(BY_NAME) | {t.name for t in (REGISTRY if registry is None else registry)}
    return sorted(selection.tokens - known)


def warnings(registry: Iterable[Tool] | None = None, lang: str = FALLBACK) -> list[str]:
    """Combinations that leave a switched-on tool without the tool it points at."""
    entries = list(REGISTRY if registry is None else registry)
    known = {t.name for t in entries}
    live = {t.name for t in entries if t.enabled}
    out: list[str] = []

    for tool, needs, why in DEPENDS:
        if tool not in live:
            continue
        present = [n for n in needs if n in known]
        missing = [n for n in present if n not in live]
        if present and len(missing) == len(present):  
            gone = ", ".join(missing)
            out.append(
                Text(
                    en=f"{tool} is on and {gone} is not: {why.en}",
                    ru=f"{tool} включён, а {gone} - нет: {why.ru}",
                ).of(lang)
            )

    editing = [t.name for t in entries if t.group == "edit" and t.enabled]
    if editing and _EDIT_NEEDS_READ in known and _EDIT_NEEDS_READ not in live:
        out.append(
            Text(
                en=(
                    f"canvas editing is on and {_EDIT_NEEDS_READ} is not: "
                    "writing into a graph without reading it first can only be guesswork"
                ),
                ru=(
                    f"правка холста включена, а {_EDIT_NEEDS_READ} - нет: "
                    "писать в граф, не прочитав его, можно только наугад"
                ),
            ).of(lang)
        )
    return out


def catalogue(registry: Iterable[Tool] | None = None, lang: str = FALLBACK) -> dict[str, Any]:
    """Everything the settings window needs, in the order it should draw it.

    Resolved to one language on the way out rather than handing `Text` objects to
    the caller: this is also what `--list-tools` prints, and a JSON payload with a
    two-language object at every leaf would be answering a question nobody asked.
    """
    entries = list(REGISTRY if registry is None else registry)
    by_group: dict[str, list[Tool]] = {}
    for entry in entries:
        by_group.setdefault(entry.group, []).append(entry)

    return {
        "lang": lang,
        "risks": {name: text.of(lang) for name, text in RISKS.items()},
        "groups": [
            {
                "name": g.name,
                "title": g.title.of(lang),
                "risk": g.risk,
                "summary": g.summary.of(lang),
                "warning": g.warning.of(lang),
                "always": g.always,
                "tools": [
                    {
                        "name": t.name,
                        "risk": t.risk,
                        "enabled": t.enabled,
                        "summary": t.summary,
                    }
                    for t in by_group.get(g.name, [])
                ],
            }
            for g in GROUPS
        ],
        "warnings": warnings(entries, lang),
    }


def compose(chosen: Iterable[str], registry: Iterable[Tool] | None = None) -> str:
    """The shortest spec that selects exactly `chosen`.

    Written back to `.env` by the settings window, so it has to be legible: a
    group whose tools are all wanted becomes one token, and a whole install with
    nothing switched off becomes `all` rather than a list of eight.
    """
    entries = list(REGISTRY if registry is None else registry)
    wanted = set(chosen)
    by_group: dict[str, list[str]] = {}
    for entry in entries:
        by_group.setdefault(entry.group, []).append(entry.name)

    tokens: list[str] = []
    for group in GROUPS:
        names = by_group.get(group.name, [])
        if not names or group.always:
            continue
        live = [n for n in names if n in wanted]
        if not live:
            continue
        if len(live) == len(names):
            tokens.append(group.name)
        else:
            tokens.extend(sorted(live))

    every = [n for names in by_group.values() for n in names]
    if all(n in wanted for n in every):
        return "all"
    return ",".join(tokens) if tokens else "-all"
