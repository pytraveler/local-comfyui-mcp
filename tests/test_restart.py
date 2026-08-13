"""Tests for restarting ComfyUI and reloading the browser tab.

The two look like one button and fix different things: a Python node pack is
imported once at startup and never re-read, while its JavaScript is served from
disk on every page load. Getting them the wrong way round costs minutes and
changes nothing, so each tool says which case it is for.

Both have the same shape of failure - the thing being restarted is also the
thing that would report success - so most of what is worth testing is that a
restart is not declared before it happened, and that a tab which never comes
back says why.

Everything runs offline against a mocked transport.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from comfyui_mcp import server as S
from comfyui_mcp.bridge import PREFIX, PROTOCOL, BridgeClient
from comfyui_mcp.client import ComfyClient, ComfyError

Handler = Callable[[httpx.Request], httpx.Response]


def run(coro):
    return asyncio.run(coro)


def json_response(status: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=payload)


def install(monkeypatch, handler: Handler) -> None:
    """Point both the HTTP client and the bridge at one mocked ComfyUI."""
    comfy = ComfyClient(S.CFG)
    comfy._http = httpx.AsyncClient(base_url=S.CFG.base_url, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(S, "CLIENT", comfy)
    monkeypatch.setattr(S, "BRIDGE", BridgeClient(S.CFG, comfy))
    S._forget_schemas()


def tab_list(tabs: list[dict[str, Any]] | None) -> dict[str, Any]:
    live = [{"client_id": "tab-1", "methods": ["reload"]}] if tabs is None else tabs
    return {
        "protocol": PROTOCOL,
        "preferred": live[0]["client_id"] if live else None,
        "clients": live,
    }


def comfyui(
    alive: list[bool] | None = None,
    queue: dict[str, Any] | None = None,
    tabs: list[dict[str, Any]] | None = None,
    restart_status: int = 200,
    seen: list[str] | None = None,
) -> Handler:
    """A ComfyUI whose liveness follows a script, the last value repeating."""
    states = list(alive if alive is not None else [True])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if seen is not None:
            seen.append(path)
        if path == "/system_stats":
            up = states.pop(0) if len(states) > 1 else states[0]
            return json_response(200, {"system": {}, "devices": []}) if up else httpx.Response(503)
        if path == "/queue":
            return json_response(200, queue or {"queue_running": [], "queue_pending": []})
        if path == f"{PREFIX}/clients":
            return json_response(200, tab_list(tabs))
        if path == f"{PREFIX}/restart":
            if restart_status != 200:
                return httpx.Response(restart_status, text="Not Found")
            return json_response(200, {"ok": True, "pid": 4242, "in_s": 0.4, "argv": ["python", "main.py"]})
        return json_response(200, {})

    return handler


class FakeProcess:
    """Stands in for a ComfyUI this server launched."""

    def __init__(self, owned: bool) -> None:
        self.owned = owned
        self.calls: list[str] = []

    async def stop(self) -> dict[str, Any]:
        self.calls.append("stop")
        return {"stopped": True, "pid": 1}

    async def start(self, client, wait: bool = True) -> dict[str, Any]:
        self.calls.append("start")
        return {"started": True, "pid": 2, "waited": wait}


@pytest.fixture(autouse=True)
def clean_caches():
    yield
    S._forget_schemas()


def test_a_comfyui_that_is_not_answering_has_nothing_to_restart(monkeypatch):
    install(monkeypatch, comfyui(alive=[False]))
    with pytest.raises(ComfyError, match="comfy_start"):
        run(S.restart_comfy())


def test_the_refusal_warns_that_a_starting_comfyui_looks_the_same(monkeypatch):
    # Measured live: the port was listening while /system_stats still timed out,
    # so "not answering" is not the same statement as "not running", and starting
    # a second instance on top of the first is the expensive way to find out.
    install(monkeypatch, comfyui(alive=[False]))
    with pytest.raises(ComfyError, match="still starting up"):
        run(S.restart_comfy())


def test_work_in_the_queue_stops_a_restart(monkeypatch):
    install(monkeypatch, comfyui(queue={"queue_running": [[1, "a"]], "queue_pending": [[2, "b"]]}))
    monkeypatch.setattr(S, "PROCESS", FakeProcess(owned=False))
    with pytest.raises(ComfyError, match="1 running and 1 queued"):
        run(S.restart_comfy())


def test_force_restarts_over_a_busy_queue_and_counts_what_was_lost(monkeypatch):
    install(
        monkeypatch,
        comfyui(alive=[True, True, False, True], queue={"queue_running": [[1, "a"]], "queue_pending": []}),
    )
    monkeypatch.setattr(S, "PROCESS", FakeProcess(owned=False))
    assert run(S.restart_comfy(force=True))["jobs_discarded"] == 1


def test_a_hand_launched_comfyui_is_re_executed_through_the_node(monkeypatch):
    # The case comfy_stop deliberately refuses, and the usual one in practice.
    seen: list[str] = []
    install(monkeypatch, comfyui(alive=[True, True, False, True], seen=seen))
    monkeypatch.setattr(S, "PROCESS", FakeProcess(owned=False))

    report = run(S.restart_comfy())

    assert "re-executed in place" in report["mechanism"]
    assert f"{PREFIX}/restart" in seen
    assert report["restart"]["pid"] == 4242


def test_a_process_this_server_started_is_relaunched_instead(monkeypatch):
    seen: list[str] = []
    install(monkeypatch, comfyui(seen=seen))
    process = FakeProcess(owned=True)
    monkeypatch.setattr(S, "PROCESS", process)

    report = run(S.restart_comfy())

    assert process.calls == ["stop", "start"]
    assert f"{PREFIX}/restart" not in seen  # the node is not asked when we own it
    assert "launch script" in report["mechanism"]


def test_without_the_node_a_hand_launched_comfyui_cannot_be_restarted(monkeypatch):
    install(monkeypatch, comfyui(restart_status=404))
    monkeypatch.setattr(S, "PROCESS", FakeProcess(owned=False))

    with pytest.raises(ComfyError, match="stop and start it yourself"):
        run(S.restart_comfy())


def test_waiting_for_it_to_come_back_starts_after_it_goes_quiet(monkeypatch):
    """It answers for a moment after agreeing, so the first probe is the old process."""
    monkeypatch.setattr(S, "_RESTART_QUIET_WAIT", 0.0)
    install(monkeypatch, comfyui(alive=[True, True, True]))  # never goes quiet
    monkeypatch.setattr(S, "PROCESS", FakeProcess(owned=False))

    report = run(S.restart_comfy())

    assert report["went_quiet_after_s"] is None
    assert report["came_back_after_s"] == 0.0


def test_a_comfyui_that_never_comes_back_says_where_to_look(monkeypatch):
    monkeypatch.setattr(S, "_RESTART_QUIET_WAIT", 0.0)
    install(monkeypatch, comfyui(alive=[True, True, False]))
    monkeypatch.setattr(S, "PROCESS", FakeProcess(owned=False))

    with pytest.raises(ComfyError, match="get_comfy_log"):
        run(S.restart_comfy(wait=0.0))


def test_a_restart_drops_the_schema_caches(monkeypatch):
    # A fresh process may expose different nodes, models and model directories.
    install(monkeypatch, comfyui(alive=[True, True, False, True]))
    monkeypatch.setattr(S, "PROCESS", FakeProcess(owned=False))
    S._SCHEMA_CACHE["KSampler"] = {"stale": True}
    S._MODEL_DIRS["vae"] = {"folders": ["/gone"], "extensions": []}

    run(S.restart_comfy())

    assert not S._SCHEMA_CACHE and not S._MODEL_DIRS


def test_a_tab_that_was_open_is_waited_for_after_the_restart(monkeypatch):
    # It reconnects and re-announces on its own `status` event, so this is a wait
    # rather than an action - but returning before it has done so would hand the
    # caller a workspace tool that fails for an unrelated-looking reason.
    install(monkeypatch, comfyui(alive=[True, True, False, True]))
    monkeypatch.setattr(S, "PROCESS", FakeProcess(owned=False))

    assert run(S.restart_comfy())["workspace_reconnected"] is True


def test_no_tab_before_the_restart_means_none_is_waited_for(monkeypatch):
    install(monkeypatch, comfyui(alive=[True, True, False, True], tabs=[]))
    monkeypatch.setattr(S, "PROCESS", FakeProcess(owned=False))

    assert "workspace_reconnected" not in run(S.restart_comfy())


def reloading(
    monkeypatch,
    result: dict[str, Any],
    tabs: list[dict[str, Any]] | None = None,
    sticky: bool = False,
) -> list[dict[str, Any]]:
    """Wire up a tab that answers `reload`, recording what it was asked.

    Once asked, the old client disappears and `tabs` takes its place - what a page
    that really went away looks like. `sticky` keeps it, which is the shape of a
    reload the browser refused, or one that threw inside the page.
    """
    asked: list[dict[str, Any]] = []
    before = [{"client_id": "tab-1", "methods": ["reload"]}]
    after = [{"client_id": "tab-2", "methods": ["reload"]}] if tabs is None else tabs
    gone = {"yet": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{PREFIX}/call":
            asked.append(json.loads(request.content))
            gone["yet"] = True
            return json_response(200, {"client_id": "tab-1", "result": result})
        if path == f"{PREFIX}/clients":
            return json_response(200, tab_list(before if sticky or not gone["yet"] else after))
        return json_response(200, {"system": {}, "devices": []})

    install(monkeypatch, handler)
    return asked


async def _nothing(client_id: str):
    return None


def test_a_reload_asks_the_tab_and_reports_the_one_that_came_back(monkeypatch):
    asked = reloading(monkeypatch, {"reloading": True, "in_ms": 250, "may_prompt": False})

    report = run(S.reload_workspace(wait=0.0))

    assert asked[0]["method"] == "reload"
    assert asked[0]["params"] == {"force": False}
    assert report["reloaded_client"] == "tab-1"
    assert report["connected"] is True
    assert report["client_id"] == "tab-2"


def test_the_canvas_is_backed_up_only_when_the_reload_is_forced(monkeypatch):
    saved: list[str] = []

    async def snapshot(client_id: str):
        saved.append(client_id)
        return Path("exports/replaced-x.json")

    monkeypatch.setattr(S, "_snapshot_canvas", snapshot)

    reloading(monkeypatch, {"reloading": True, "may_prompt": False})
    assert "backup" not in run(S.reload_workspace(wait=0.0))
    assert saved == []

    reloading(monkeypatch, {"reloading": True, "may_prompt": True})
    assert run(S.reload_workspace(wait=0.0, force=True))["backup"].endswith("replaced-x.json")
    assert saved == [""]


def test_force_is_passed_through_to_the_page(monkeypatch):
    monkeypatch.setattr(S, "_snapshot_canvas", _nothing)
    asked = reloading(monkeypatch, {"reloading": True, "may_prompt": True})

    run(S.reload_workspace(wait=0.0, force=True))

    assert asked[0]["params"] == {"force": True}


def test_a_page_that_has_not_gone_is_not_reported_as_reloaded(monkeypatch):
    """The tab stays registered while its timer runs, so "a tab is available" is
    true of the very page that was asked to leave. Found live: a throw inside the
    reload timer left the page exactly where it was and the tool announced
    success, because it had probed the tab it had just spoken to."""
    monkeypatch.setattr(S, "_snapshot_canvas", _nothing)
    reloading(monkeypatch, {"reloading": True, "may_prompt": False}, sticky=True)

    report = run(S.reload_workspace(wait=0.0, force=True))

    assert report["connected"] is False
    assert report["reason"] == "not_gone_yet"
    assert "get_console_log" in report["hint"]


def test_a_page_the_dialog_is_holding_is_pending_rather_than_failed(monkeypatch):
    # Measured live: the page went 35s after it was asked, once somebody answered
    # the confirmation. Calling that a failure at 10s was simply wrong, so the
    # wording is "not yet" and the deadline is the caller's own `wait`.
    monkeypatch.setattr(S, "_snapshot_canvas", _nothing)
    reloading(monkeypatch, {"reloading": True, "may_prompt": True}, sticky=True)

    report = run(S.reload_workspace(wait=0.0, force=True))

    assert report["reason"] == "not_gone_yet"
    assert "Leave site?" in report["hint"]
    assert "as soon as that is answered" in report["hint"]


def test_a_tab_that_left_but_has_not_returned_says_the_page_is_loading(monkeypatch):
    reloading(monkeypatch, {"reloading": True, "may_prompt": False}, tabs=[])

    report = run(S.reload_workspace(wait=0.0))

    assert report["connected"] is False
    assert report["old_tab_gone_after_s"] == 0.0
    assert "workspace_status" in report["hint"]
