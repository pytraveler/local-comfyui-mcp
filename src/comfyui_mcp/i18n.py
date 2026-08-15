"""Two languages for the half of this server a person reads.

The split is along who the text is for, not along which module it lives in:

*The model reads English, always.* All 45 tool docstrings, every `hint` in a tool
result and the MCP server instructions are English and are not touched by anything
here. They are part of an interface to a program, and a program that is told about
its tools in one language and answers questions about them in another is being made
worse for no gain.

*A person reads their own language.* The three settings windows, the installer and
`.env.example` — the whole surface somebody meets before the server has ever run.

`toolsets.py` is where the two met by accident: `RISKS` was English and the group
titles beside it were Russian, and both go out through `--list-tools` and the
startup log. That is settled here by making every one of them a `Text`.

**Both versions of a string live in the same place, side by side.** There are no
keys and no catalogue file. A key is a third thing that can go stale — present in
one language, misspelled in the other, or left behind when the sentence it named
was rewritten — and the project already refuses that shape of bug everywhere else
(the tool registry is filled by the decorators, the settings window reads the
catalogue rather than keeping a copy). `Text(en=…, ru=…)` cannot drift, because
there is nothing for it to drift from.

What *can* still go wrong is half a translation, so `Text` refuses to exist as one.
The check runs in `__post_init__` rather than in a test that walks a table, because
most of these strings are built inside a window's layout code where no test could
enumerate them — but building the window constructs every one of them, which is
what `scripts/check_windows.py` does.

Nothing here has any I/O and no module here imports Tk, so all of it is testable
offline, the same as `graph.py` and `toolsets.py`.
"""

from __future__ import annotations

import locale
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable

EN = "en"
RU = "ru"

LANGS: tuple[str, ...] = (EN, RU)

FALLBACK = EN


@dataclass(frozen=True)
class Text:
    """One string in every language, with no key naming it.

    Empty is allowed — `Group.warning` has nothing to say for some groups — but
    empty in one language and not the other is a half-finished translation and
    raises. That is the only mistake this shape still permits, so it is the one
    worth catching, and catching it at construction means merely opening a window
    checks every string the window draws.
    """

    en: str
    ru: str

    def __post_init__(self) -> None:
        if bool(self.en.strip()) != bool(self.ru.strip()):
            raise ValueError(f"half-translated text: en={self.en!r}, ru={self.ru!r}")

    def of(self, lang: str) -> str:
        return self.ru if lang == RU else self.en

    def __bool__(self) -> bool:
        return bool(self.en.strip())


def normalise(spec: str | None) -> str | None:
    """Reduce anything locale-shaped to one of `LANGS`, or None if it is not one.

    Handles what the several sources actually hand over: `ru`, `ru_RU.UTF-8`,
    `ru-RU`, and Windows' own `Russian_Russia.1251`. Matching on the first two
    letters covers the first three; the last needs the name, which is why the
    table is here rather than a two-character slice standing alone.
    """
    if not spec:
        return None
    text = spec.strip().lower().replace("_", "-")
    if not text or text in ("c", "posix"):
        return None
    head = text.split("-")[0].split(".")[0]
    if head in LANGS:
        return head
    for lang, name in ((RU, "russian"), (EN, "english")):
        if head.startswith(name):
            return lang
    return None


def detect() -> str:
    """The language of the machine, asked of it rather than assumed.

    Three sources in the order of how deliberate they are. `LANGUAGE`/`LC_ALL`/
    `LANG` are what somebody set on purpose and are honoured first even on Windows,
    where they are unusual but not unheard of. Then Windows' own UI language, which
    is the question actually being asked — `GetUserDefaultUILanguage` is the
    setting that decides what the rest of this person's programs are written in,
    and reading it needs no `setlocale` call and so has no global side effect.
    `locale` is the fallback for everywhere else.

    Total by construction: every branch is guarded, because a settings window that
    refuses to open over a locale lookup would be a worse failure than a window in
    the wrong language.
    """
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        found = normalise((os.environ.get(name) or "").split(":")[0])
        if found:
            return found

    if sys.platform == "win32":
        try:
            import ctypes

            primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
            if primary == 0x19:
                return RU
            if primary == 0x09:
                return EN
        except Exception:
            pass

    try:
        found = normalise(locale.getlocale()[0])
        if found:
            return found
    except Exception:
        pass

    return FALLBACK


def resolve(spec: str | None) -> str:
    """Turn a `COMFYUI_LANG` value into a language.

    Empty means "ask the machine", which is what makes the setting one nobody has
    to know about. A value that names no language we speak falls back to English
    rather than to the machine's own: somebody who wrote `COMFYUI_LANG=de` is
    telling us they do not want the local default, and honouring the half of that
    we understood is the more faithful reading.
    """
    if spec and spec.strip():
        return normalise(spec) or FALLBACK
    return detect()


PLAIN: dict[str, str] = {
    "—": "-",  # em dash
    "–": "-",  # en dash
    "…": "...",
    "«": '"',
    "»": '"',
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "→": "->",
    "•": "*",
    " ": " ",
}


def fold_punctuation(text: str) -> str:
    """The same sentence with typographic punctuation reduced to ASCII."""
    return "".join(PLAIN.get(ch, ch) for ch in text)


def can_encode(text: str, encoding: str | None) -> bool:
    """Whether this console could write `text` at all."""
    if not encoding:
        return True
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def printable(text: str, encoding: str | None) -> str:
    """`text`, made safe for a console that may not be able to spell it.

    The prose here is written with em dashes, and **cp866 has no em dash** — which
    is the default console codepage on a Russian Windows, so `--print` raised
    `UnicodeEncodeError` and printed nothing at all. The `.bat` launchers run
    `chcp 65001` first and so never saw it; running the module directly did.

    Folding only when the encoding cannot cope keeps a capable terminal's output
    exactly as written, and folding *punctuation* rather than reconfiguring the
    stream to `errors="replace"` is what keeps the result readable: a dash becomes
    a dash and not a question mark. Letters are never touched — in cp866 the
    Cyrillic is perfectly encodable and only the punctuation was ever the problem.
    """
    return text if can_encode(text, encoding) else fold_punctuation(text)


def from_args(args: Iterable[str] | None = None) -> str:
    """The language a settings window should draw in.

    `--lang=xx` wins, so a screenshot or `scripts/check_windows.py` can ask for the
    other language without editing anybody's `.env`. Otherwise `COMFYUI_LANG`,
    which is empty by default and therefore means the machine's own.
    """
    for arg in args or ():
        if arg.startswith("--lang="):
            return resolve(arg.split("=", 1)[1])
    from .config import load_config

    return resolve(load_config().lang)


def echo(text: str, stream: Any = None) -> None:
    """`print`, for prose, onto whatever console this actually is."""
    target = sys.stdout if stream is None else stream
    print(printable(text, getattr(target, "encoding", None)), file=target)


def speaker(lang: str) -> Callable[..., str]:
    """A `say` for one language, to be called with both versions of a string.

    Two forms, because both come up: `say(SOME_TEXT)` for the module-level constants
    that have to exist before a language is known, and `say(en=…, ru=…)` for the
    labels a window builds as it draws itself, which is most of them. The second is
    the reason there are no keys — the English and the Russian sit on one line, in
    the place that uses them.
    """
    resolved = resolve(lang) if lang not in LANGS else lang

    def say(text: Text | None = None, /, *, en: str = "", ru: str = "") -> str:
        return (text if text is not None else Text(en=en, ru=ru)).of(resolved)

    return say
