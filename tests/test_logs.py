"""Tests for reading ComfyUI's console buffer.

Everything here is about the gap between what ComfyUI stores and what a caller can
read. It stores *writes* - a whole traceback in one, half a line in another - and
wraps its level tags in ANSI colour. None of that is legible as-is, and the strings
below are the shapes the real buffer holds, taken off a running install rather than
invented.

The constraint that matters most is the same one `diagnose` has: a healthy log must
produce nothing notable. A tool that cries import-failure on a clean startup sends
someone to fix what is not broken.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from comfyui_mcp import server as S
from comfyui_mcp.bridge import WorkspaceError
from comfyui_mcp.client import ComfyClient, ComfyError
from comfyui_mcp.logs import (
    COMFY_LOG_CAPACITY,
    LogLine,
    count_levels,
    failed_extensions,
    filter_lines,
    from_entries,
    notable_lines,
    to_lines,
)

# Real tags: ColoredFormatter writes bold + colour + "[LEVEL]" + reset + " ".
INFO = "\x1b[32m[INFO]\x1b[0m "
WARNING = "\x1b[1m\x1b[33m[WARNING]\x1b[0m "
ERROR = "\x1b[1m\x1b[31m[ERROR]\x1b[0m "

T0 = "2026-07-31T23:37:23.021311"
T1 = "2026-07-31T23:37:24.500000"


def write(m: str, t: str = T0) -> dict[str, Any]:
    return {"t": t, "m": m}


def texts(lines: Any) -> list[str]:
    return [line.text for line in lines]


def test_one_write_holding_several_lines_becomes_several_lines():
    # A logging record arrives whole, newlines and all.
    lines = to_lines([write(f"{INFO}first\nsecond\nthird\n")])
    assert texts(lines) == ["first", "second", "third"]


def test_a_line_split_across_writes_is_joined():
    # print(..., end="") and anything writing a prefix before its value.
    lines = to_lines([write("Loading "), write("model.safetensors\n", t=T1)])
    assert texts(lines) == ["Loading model.safetensors"]


def test_a_joined_line_carries_the_time_the_first_fragment_arrived():
    # When something started saying it, not when it got round to the newline.
    lines = to_lines([write("Loading "), write("model.safetensors\n", t=T1)])
    assert lines[0].t == T0


def test_text_left_unterminated_at_the_end_is_still_reported():
    # The last thing printed before a hang has no newline yet, and is the line
    # a caller most wants to see.
    lines = to_lines([write(f"{INFO}done\n"), write("Loading checkpoint", t=T1)])
    assert texts(lines) == ["done", "Loading checkpoint"]


def test_colour_codes_are_stripped():
    lines = to_lines([write(f"{INFO}\x1b[36mAdding extra search path\x1b[0m\n")])
    assert texts(lines) == ["Adding extra search path"]


def test_a_trailing_carriage_return_does_not_survive():
    lines = to_lines([write(f"{INFO}progress\r\n")])
    assert texts(lines) == ["progress"]


def test_entries_that_are_not_writes_are_ignored():
    lines = to_lines([None, {}, {"t": T0}, {"t": T0, "m": ""}, {"m": 42}, write(f"{INFO}real\n")])
    assert texts(lines) == ["real"]


def test_the_level_tag_becomes_a_field_and_leaves_the_text():
    lines = to_lines([write(f"{INFO}Adding extra search path checkpoints\n")])
    assert lines[0].level == "INFO"
    assert lines[0].text == "Adding extra search path checkpoints"


def test_a_traceback_in_one_write_inherits_the_level_above_it():
    # One logging record, so the traceback belongs to the ERROR that opened it -
    # and a caller filtering on ERROR is asking for exactly those lines.
    lines = to_lines(
        [
            write(
                f"{ERROR}Exception in callback _ProactorBasePipeTransport._call_connection_lost()\n"
                "handle: <Handle _ProactorBasePipeTransport._call_connection_lost()>\n"
                "Traceback (most recent call last):\n"
            )
        ]
    )
    assert [line.level for line in lines] == ["ERROR", "ERROR", "ERROR"]


def test_a_later_untagged_write_does_not_inherit_anything():
    # Nothing connects a separate write to the record before it.
    lines = to_lines([write(f"{ERROR}it broke\n"), write("FETCH DATA from: ...\n", t=T1)])
    assert [line.level for line in lines] == ["ERROR", ""]


def test_output_with_no_tag_at_all_is_kept():
    # ComfyUI-Manager prints directly, and a dying import reaches stderr raw.
    lines = to_lines([write("FETCH DATA from: https://api.comfy.org/nodes\n")])
    assert texts(lines) == ["FETCH DATA from: https://api.comfy.org/nodes"]
    assert lines[0].level == ""


def test_a_formatted_line_leads_with_the_clock():
    lines = to_lines([write(f"{WARNING}onnx is missing\n")])
    assert lines[0].format() == "23:37:23 [WARNING] onnx is missing"


HEALTHY = to_lines(
    [
        write(f"{INFO}Adding extra search path checkpoints V:/models/checkpoints\n"),
        write(f"{INFO}Total VRAM 32607 MB, total RAM 65414 MB\n"),
        write(f"{WARNING}onnx is missing\n", t=T1),
        write("FETCH DATA from: https://api.comfy.org/nodes\n", t=T1),
    ]
)


def test_level_is_a_floor_not_an_exact_match():
    assert texts(filter_lines(HEALTHY, level="WARNING")) == ["onnx is missing"]
    assert len(filter_lines(HEALTHY, level="INFO")) == 3


def test_untagged_lines_are_dropped_by_a_level_filter():
    kept = filter_lines(HEALTHY, level="DEBUG")
    assert "FETCH DATA from: https://api.comfy.org/nodes" not in texts(kept)


def test_search_ignores_case():
    assert texts(filter_lines(HEALTHY, search="ONNX")) == ["onnx is missing"]


def test_search_can_be_a_regular_expression():
    found = filter_lines(HEALTHY, search=r"\d+ MB", regex=True)
    assert texts(found) == ["Total VRAM 32607 MB, total RAM 65414 MB"]


def test_search_and_level_narrow_together():
    assert filter_lines(HEALTHY, level="WARNING", search="VRAM") == []


def test_an_unknown_level_name_is_refused():
    # Silently returning everything would read as "nothing was logged at that level".
    with pytest.raises(ValueError, match="level must be one of"):
        filter_lines(HEALTHY, level="LOUD")


def test_a_healthy_log_has_nothing_notable():
    assert notable_lines(HEALTHY) == []


def test_a_failed_import_is_notable_even_though_it_is_logged_at_info():
    # nodes.py writes its import timing table at INFO and marks failures inline,
    # so severity alone would never surface the one line that matters.
    lines = to_lines(
        [
            write(f"{INFO}  0.3 seconds: V:/custom_nodes/rgthree-comfy\n"),
            write(f"{INFO}  0.0 seconds (IMPORT FAILED): V:/custom_nodes/broken_pack\n"),
        ]
    )
    assert texts(notable_lines(lines)) == ["  0.0 seconds (IMPORT FAILED): V:/custom_nodes/broken_pack"]


def test_a_missing_package_is_notable():
    # The real one, off this install: a pack losing half its nodes to one import.
    lines = to_lines(
        [write(f"{WARNING}WanVideoWrapper WARNING: FantasyPortrait nodes not available: No module named 'onnx'\n")]
    )
    assert len(notable_lines(lines)) == 1


def test_a_node_that_could_not_be_imported_at_all_is_notable():
    lines = to_lines([write(f"{WARNING}Cannot import V:/custom_nodes/foo module for custom nodes: bar\n")])
    assert len(notable_lines(lines)) == 1


def test_errors_are_notable_without_matching_any_phrase():
    lines = to_lines([write(f"{ERROR}something went wrong in a way nobody predicted\n")])
    assert len(notable_lines(lines)) == 1


def test_counts_are_by_level_and_ignore_untagged():
    assert count_levels(HEALTHY) == {"WARNING": 1, "INFO": 2}


def test_counts_come_back_most_severe_first():
    # So a caller reading the first key learns the worst thing that happened.
    counts = count_levels(to_lines([write(f"{INFO}a\n"), write(f"{ERROR}b\n"), write(f"{WARNING}c\n")]))
    assert list(counts) == ["ERROR", "WARNING", "INFO"]


def test_the_capacity_is_the_one_ComfyUI_hardcodes():
    # app/logger.py: setup_logger(capacity=300), and main.py passes no capacity.
    # If this ever changes upstream the buffer-full note stops being true.
    assert COMFY_LOG_CAPACITY == 300


def as_comfy(monkeypatch: pytest.MonkeyPatch, entries: list[Any], has_route: bool = True) -> None:
    """A ComfyUI answering /system_stats and serving `entries` as its log buffer."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {}, "devices": []})
        if request.url.path == "/internal/logs/raw":
            if not has_route:
                return httpx.Response(404, text="Not Found")
            return httpx.Response(200, json={"entries": entries, "size": {"cols": 120, "rows": 30}})
        return httpx.Response(404, text="Not Found")

    comfy = ComfyClient(S.CFG)
    comfy._http = httpx.AsyncClient(base_url=S.CFG.base_url, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(S, "CLIENT", comfy)


STARTUP = [
    write(f"{INFO}Total VRAM 32607 MB\n"),
    write(f"{INFO}  0.0 seconds (IMPORT FAILED): V:/custom_nodes/broken_pack\n"),
    write(f"{WARNING}onnx is missing\n", t=T1),
    write("FETCH DATA from: https://api.comfy.org/nodes\n", t=T1),
]


def test_the_tool_returns_a_tail_and_says_what_it_left_out(monkeypatch):
    as_comfy(monkeypatch, STARTUP)
    result = asyncio.run(S.get_comfy_log(lines=2))
    assert result["returned"] == 2
    assert result["total_lines"] == 4
    assert result["counts"] == {"WARNING": 1, "INFO": 2}


def test_asking_for_no_limit_returns_everything(monkeypatch):
    as_comfy(monkeypatch, STARTUP)
    assert asyncio.run(S.get_comfy_log(lines=0))["returned"] == 4


def test_notable_survives_a_search_that_matched_nothing(monkeypatch):
    # The whole point: "I found no mention of onnx" must not read as "all is well"
    # when the buffer is sitting on a failed import.
    as_comfy(monkeypatch, STARTUP)
    result = asyncio.run(S.get_comfy_log(search="nothing matches this"))
    assert result["matched"] == 0
    assert any("IMPORT FAILED" in line for line in result["notable"])


def test_a_notable_line_already_in_the_tail_is_not_repeated(monkeypatch):
    # Printing the same traceback twice in one answer is pure noise. The count
    # still goes out, so nothing is hidden by leaving the text out.
    as_comfy(monkeypatch, STARTUP)
    result = asyncio.run(S.get_comfy_log(lines=0))
    assert result["notable_total"] == 1
    assert "notable" not in result


def test_a_full_buffer_says_that_older_lines_are_gone(monkeypatch):
    as_comfy(monkeypatch, [write(f"{INFO}line {n}\n") for n in range(COMFY_LOG_CAPACITY)])
    assert "buffer_full" in asyncio.run(S.get_comfy_log(lines=1))


def test_a_buffer_with_room_left_makes_no_such_claim(monkeypatch):
    as_comfy(monkeypatch, STARTUP)
    assert "buffer_full" not in asyncio.run(S.get_comfy_log())


def test_an_unknown_level_names_the_ones_that_exist(monkeypatch):
    as_comfy(monkeypatch, STARTUP)
    with pytest.raises(ComfyError, match="level must be one of"):
        asyncio.run(S.get_comfy_log(level="LOUD"))


def test_a_broken_regular_expression_says_so_rather_than_failing_obscurely(monkeypatch):
    as_comfy(monkeypatch, STARTUP)
    with pytest.raises(ComfyError, match="not a valid regular expression"):
        asyncio.run(S.get_comfy_log(search="[unclosed", regex=True))


def test_a_comfyui_without_the_route_names_what_is_missing(monkeypatch):
    # An install predating the log buffer. Saying "404" would send nobody anywhere.
    as_comfy(monkeypatch, STARTUP, has_route=False)
    with pytest.raises(ComfyError, match="/internal/logs/raw"):
        asyncio.run(S.get_comfy_log())


def console(level: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"t": T0, "level": level, "text": text, **extra}


LOAD_FAILURE = console(
    "ERROR",
    "Error loading extension /extensions/WhatDreamsCost-ComfyUI/multi_image_loader.js "
    "TypeError: app.registerExtension is not a function",
)


def test_console_entries_need_no_reassembly():
    # The page records one entry per console call, so unlike ComfyUI's stdout
    # buffer there is nothing to stitch back together.
    lines = from_entries([console("INFO", "loaded"), console("WARNING", "slow")])
    assert [(line.level, line.text) for line in lines] == [("INFO", "loaded"), ("WARNING", "slow")]


def test_a_stack_trace_stays_inside_one_entry():
    # One console.error is one event. Counting a ten-frame stack as ten errors
    # would misreport how much is wrong.
    lines = from_entries([console("ERROR", "Error: boom\n    at foo\n    at bar")])
    assert len(lines) == 1
    assert count_levels(lines) == {"ERROR": 1}


def test_a_source_tag_survives_into_the_text():
    # Otherwise an uncaught exception is indistinguishable from something that
    # merely called console.error, and only one of those is a real fault.
    lines = from_entries([console("ERROR", "Error: boom", source="uncaught")])
    assert lines[0].text == "uncaught: Error: boom"


def test_a_level_the_page_invented_is_treated_as_untagged():
    lines = from_entries([console("SHOUT", "who knows")])
    assert lines[0].level == ""


def test_console_entries_without_text_are_ignored():
    assert from_entries([None, {}, console("INFO", ""), {"text": 42}, console("INFO", "real")]) == [
        LogLine(t=T0, level="INFO", text="real")
    ]


def test_failed_extensions_names_the_file_that_did_not_load():
    found = failed_extensions(from_entries([LOAD_FAILURE]))
    assert found == ["/extensions/WhatDreamsCost-ComfyUI/multi_image_loader.js"]


def test_failed_extensions_does_not_repeat_a_file():
    found = failed_extensions(from_entries([LOAD_FAILURE, LOAD_FAILURE]))
    assert len(found) == 1


def test_a_clean_console_names_no_failed_extensions():
    assert failed_extensions(from_entries([console("WARNING", "deprecated API")])) == []


class FakeBridge:
    """Stands in for a connected tab. Records what was asked of it."""

    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.payload = payload or {}
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def call(self, method, params=None, timeout=None, client_id=""):
        self.calls.append((method, client_id))
        if self.error is not None:
            raise self.error
        return {"client_id": "tab-1", "result": self.payload}


def as_tab(monkeypatch: pytest.MonkeyPatch, **payload: Any) -> FakeBridge:
    fake = FakeBridge({"since": T0, "blind_ms": 1200, "capacity": {"entries": 400}, **payload})
    monkeypatch.setattr(S, "BRIDGE", fake)
    return fake


def test_the_browser_tool_reports_problems_whatever_the_filter_asked_for(monkeypatch):
    # Same rule as the Python log: a search that found nothing must not read as
    # "the console is clean" while an extension is lying broken in the buffer.
    as_tab(monkeypatch, entries=[console("INFO", "loaded")], problems=[LOAD_FAILURE])
    result = asyncio.run(S.get_console_log(search="nothing matches this"))
    assert result["matched"] == 0
    assert result["problems"]
    assert result["failed_extensions"] == ["/extensions/WhatDreamsCost-ComfyUI/multi_image_loader.js"]


def test_a_problem_the_answer_already_lists_is_counted_not_repeated(monkeypatch):
    as_tab(monkeypatch, entries=[LOAD_FAILURE], problems=[LOAD_FAILURE])
    result = asyncio.run(S.get_console_log())
    assert result["problems_total"] == 1
    assert "problems" not in result
    # The derived answer is still there - it is the reason to call this at all.
    assert result["failed_extensions"]


def test_the_browser_tool_passes_the_chosen_tab_through(monkeypatch):
    fake = as_tab(monkeypatch, entries=[console("INFO", "loaded")])
    asyncio.run(S.get_console_log(client_id="tab-2"))
    assert fake.calls == [("get_console_log", "tab-2")]


def test_the_blind_window_is_reported_when_nothing_looks_wrong(monkeypatch):
    # A clean console is the one answer this caveat can turn into a wrong conclusion.
    as_tab(monkeypatch, entries=[console("INFO", "loaded")])
    result = asyncio.run(S.get_console_log())
    assert result["blind_ms"] == 1200
    assert "blind_note" in result


def test_the_blind_note_is_left_out_once_there_is_something_to_look_at(monkeypatch):
    as_tab(monkeypatch, entries=[console("INFO", "loaded")], problems=[LOAD_FAILURE])
    assert "blind_note" not in asyncio.run(S.get_console_log())


def test_eviction_is_reported_and_says_where_the_warnings_went(monkeypatch):
    as_tab(monkeypatch, entries=[console("INFO", "loaded")], problems=[LOAD_FAILURE], dropped=57)
    assert "57 older entries" in asyncio.run(S.get_console_log())["dropped"]


def test_a_stale_tab_is_told_to_reload_rather_than_restart_comfyui(monkeypatch):
    # The JS is served from disk on every page load, so a reload is the whole fix.
    # Sending someone to restart ComfyUI would cost minutes and change nothing.
    monkeypatch.setattr(S, "BRIDGE", FakeBridge(error=WorkspaceError("unknown method 'get_console_log'")))
    with pytest.raises(ComfyError, match="reloading the ComfyUI tab"):
        asyncio.run(S.get_console_log())


def test_a_real_workspace_failure_is_not_disguised_as_a_stale_tab(monkeypatch):
    monkeypatch.setattr(S, "BRIDGE", FakeBridge(error=WorkspaceError("the tab caught fire")))
    with pytest.raises(WorkspaceError, match="caught fire"):
        asyncio.run(S.get_console_log())


def test_both_logs_refuse_an_unknown_level_the_same_way(monkeypatch):
    as_tab(monkeypatch, entries=[console("INFO", "loaded")])
    with pytest.raises(ComfyError, match="level must be one of"):
        asyncio.run(S.get_console_log(level="LOUD"))
