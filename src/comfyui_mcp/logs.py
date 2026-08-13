"""Reading ComfyUI's own console buffer - pure, no I/O, fully unit-tested.

ComfyUI keeps the tail of everything its process prints. `app/logger.py` replaces
`sys.stdout` and `sys.stderr` with a `LogInterceptor` that appends every write to a
`deque` *and* passes it through to the real console, and `/internal/logs/raw` serves
that deque. It is what the frontend's terminal panel reads.

Two properties of that buffer decide everything here:

- **Entries are writes, not lines.** One `logging` record arrives as a single write
  holding its whole formatted text, newlines included - so a traceback is one entry.
  A bare `print(..., end="")` is a write holding a fragment of a line, and the rest
  of that line arrives in a later write with a different timestamp. Neither shape can
  be shown to a caller as-is, so `to_lines` reassembles them.

- **It is capacity-bounded and there is no way to ask how big it is.**
  `setup_logger(capacity=300)`, called from `main.py` with no capacity argument and
  no CLI flag behind it. Measured on this install: a plain startup produced almost
  exactly 300 writes, which means the import diagnostics - the most valuable thing in
  here - start being evicted as soon as anything else is logged. A reader that cannot
  see that has no way to tell "no import errors" from "the evidence scrolled out", so
  the fullness of the buffer is reported as part of the answer rather than inferred.

Levels come from `ColoredFormatter`, which writes `[LEVELNAME] ` ahead of the message
wrapped in ANSI colour. Plenty of output carries no tag at all: ComfyUI-Manager prints
directly, and a custom node dying at import prints a raw traceback to stderr. Those
lines are the point of the whole exercise, so an untagged line is a first-class line
here and never dropped for lacking a level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

COMFY_LOG_CAPACITY = 300

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

NOTABLE = (
    "IMPORT FAILED",
    "Cannot import",
    "Traceback (most recent call last)",
    "No module named",
)

NOTABLE_SHOWN = 20

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LEVEL_TAG = re.compile(r"^\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]\s?")


@dataclass(frozen=True)
class LogLine:
    """One console line, with the level it was logged at if it had one."""

    t: str
    level: str
    text: str

    @property
    def clock(self) -> str:
        """Just the time out of an ISO timestamp - the date is never in question."""
        return self.t[11:19] if len(self.t) >= 19 else self.t

    def format(self) -> str:
        tag = f"[{self.level}] " if self.level else ""
        return f"{self.clock} {tag}{self.text}".rstrip()


def to_lines(entries: Iterable[Any]) -> list[LogLine]:
    """Reassemble ComfyUI's writes into lines, stripping colour.

    A write holding several lines yields several; a line spread over several writes
    yields one, stamped with the time the *first* of them arrived, since that is when
    whatever produced it started saying so.

    Continuation lines inherit the level of the tag that opened their write and nothing
    further. One `logging` record is one write, so a traceback carries the ERROR of the
    line above it - which is what a caller filtering on ERROR is asking for - while a
    later untagged write stays untagged, because nothing connects it to that record.
    """
    out: list[LogLine] = []
    pending = ""  
    pending_t = ""
    inherited = ""

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("m")
        if not isinstance(raw, str) or not raw:
            continue
        stamp = str(entry.get("t", ""))
        segments = _ANSI.sub("", raw).split("\n")

        inherited = ""
        for segment in segments[:-1]:
            inherited = _emit(pending + segment, pending_t or stamp, inherited, out)
            pending, pending_t = "", ""
        tail = segments[-1]
        if tail:
            if not pending:
                pending_t = stamp
            pending += tail

    if pending:
        _emit(pending, pending_t, inherited, out)
    return out


def _emit(text: str, stamp: str, inherited: str, out: list[LogLine]) -> str:
    """Append one line and return the level the next continuation should inherit."""
    match = _LEVEL_TAG.match(text)
    if match:
        level = match.group(1)
        text = text[match.end() :]
    else:
        level = inherited
    out.append(LogLine(t=stamp, level=level, text=text.rstrip("\r")))
    return level


EXTENSION_FAILURE = "Error loading extension"


def from_entries(entries: Iterable[Any]) -> list[LogLine]:
    """Lines out of the browser's console ring, which needs no reassembly.

    The page records one entry per `console.*` call, level and all, so unlike
    ComfyUI's stdout buffer there is nothing to stitch back together. A stack trace
    stays inside its entry rather than becoming several lines: one console call is
    one event, and counting a ten-frame stack as ten warnings would be a lie.
    """
    lines: list[LogLine] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text:
            continue
        source = entry.get("source")
        if isinstance(source, str) and source:
            text = f"{source}: {text}"
        level = entry.get("level")
        lines.append(
            LogLine(
                t=str(entry.get("t", "")),
                level=level if level in LEVELS else "",
                text=text,
            )
        )
    return lines


def failed_extensions(lines: Iterable[LogLine]) -> list[str]:
    """The extension files the frontend could not import, in the order they failed.

    Worth pulling out of the noise on its own: a frontend extension that did not load
    takes its widgets and its right-click menu with it, and the node it belongs to
    still appears in `/object_info` - so the graph looks fine and behaves wrongly,
    which is the hardest shape of this problem to recognise from anywhere else.
    """
    found: list[str] = []
    for line in lines:
        start = line.text.find(EXTENSION_FAILURE)
        if start < 0:
            continue
        rest = line.text[start + len(EXTENSION_FAILURE) :].split()
        if rest and rest[0] not in found:
            found.append(rest[0])
    return found


def filter_lines(
    lines: Iterable[LogLine],
    level: str = "",
    search: str = "",
    regex: bool = False,
) -> list[LogLine]:
    """Narrow a log by severity and by text.

    `level` is a *minimum*: "WARNING" keeps ERROR and CRITICAL too. Untagged lines are
    dropped by it, which is a deliberate trade - most untagged output is a third-party
    pack printing progress, and keeping it would make the filter useless. Nothing
    important is lost that way because `notable_lines` reads the unfiltered log.
    """
    wanted = list(lines)
    if level:
        floor = _level_rank(level)
        wanted = [line for line in wanted if line.level and _level_rank(line.level) >= floor]
    if search:
        if regex:
            pattern = re.compile(search, re.IGNORECASE)
            wanted = [line for line in wanted if pattern.search(line.text)]
        else:
            needle = search.lower()
            wanted = [line for line in wanted if needle in line.text.lower()]
    return wanted


def _level_rank(level: str) -> int:
    try:
        return LEVELS.index(level.upper())
    except ValueError as exc:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}, got {level!r}") from exc


def count_levels(lines: Iterable[LogLine]) -> dict[str, int]:
    """How many lines came in at each level, most severe first. Untagged are not counted."""
    counts: dict[str, int] = {}
    for line in lines:
        if line.level:
            counts[line.level] = counts.get(line.level, 0) + 1
    return {level: counts[level] for level in reversed(LEVELS) if level in counts}


def notable_lines(lines: Iterable[LogLine]) -> list[LogLine]:
    """The lines worth reading even when the caller asked for something else.

    Anything at ERROR or above, plus the specific strings a failed import produces -
    which are logged at INFO and WARNING and would otherwise need to be known about in
    advance to be found.
    """
    return [line for line in lines if _is_notable(line)]


def _is_notable(line: LogLine) -> bool:
    if line.level in ("ERROR", "CRITICAL"):
        return True
    return any(needle in line.text for needle in NOTABLE)
