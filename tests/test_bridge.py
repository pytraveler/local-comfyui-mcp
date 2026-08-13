"""Tests for the workspace bridge client.

The bridge can be absent in three ways that look identical from the outside - the
node is not installed, no tab is open, or the tab failed the call - and a caller
does something different about each. So most of what is worth testing here is that
the three stay distinguishable and that each error says what to do next.

Everything runs offline against a mocked transport; nothing needs ComfyUI.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from comfyui_mcp import server as S
from comfyui_mcp.bridge import PROTOCOL, BridgeClient, WorkspaceError, WorkspaceUnavailable
from comfyui_mcp.client import ComfyClient, ComfyError
from comfyui_mcp.config import load_config
from comfyui_mcp.process import ProcessError
from comfyui_mcp.store import WorkflowError

Handler = Callable[[httpx.Request], httpx.Response]


def run(coro):
    return asyncio.run(coro)


def bridge(handler: Handler, **overrides: Any) -> BridgeClient:
    cfg = dataclasses.replace(load_config(), **overrides)
    comfy = ComfyClient(cfg)
    comfy._http = httpx.AsyncClient(base_url=cfg.base_url, transport=httpx.MockTransport(handler))
    return BridgeClient(cfg, comfy)


def json_response(status: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=payload)


def error_response(status: int, code: str, message: str, **extra: Any) -> httpx.Response:
    return json_response(status, {"error": {"code": code, "message": message, **extra}})


def routed(**by_path: Handler) -> Handler:
    """Dispatch on the path's last segment, 404 for anything unrouted."""

    def handler(request: httpx.Request) -> httpx.Response:
        leaf = request.url.path.rsplit("/", 1)[-1]
        route = by_path.get(leaf)
        return route(request) if route else httpx.Response(404, text="Not Found")

    return handler


ALIVE = {"system_stats": lambda r: json_response(200, {"system": {}, "devices": []})}

#: run_workspace resolves the tab before it can register a mirror, so its tests
#: need /clients answered as well as /call.
ONE_TAB = {
    "clients": lambda r: json_response(
        200,
        {
            "protocol": PROTOCOL,
            "preferred": "tab-1",
            "clients": [{"client_id": "tab-1", "frontend": "1.47.10", "methods": ["queue_prompt"]}],
        },
    )
}
MIRROR_OK: Handler = lambda r: json_response(200, {"ok": True})  # noqa: E731


def one_client(sid: str = "tab-1", protocol: int = PROTOCOL) -> Handler:
    return routed(
        **ALIVE,
        clients=lambda r: json_response(
            200,
            {
                "protocol": protocol,
                "preferred": sid,
                "clients": [{"client_id": sid, "frontend": "1.47.10", "methods": ["ping"], "focused": True}],
            },
        ),
    )


def test_missing_node_is_told_apart_from_a_missing_tab():
    """A 404 means the routes are not there at all - that is a setup step, not a retry."""
    client = bridge(routed(**ALIVE))
    with pytest.raises(WorkspaceUnavailable, match="not installed"):
        run(client.clients())


def test_install_error_names_both_ends_of_the_copy():
    client = bridge(routed(**ALIVE))
    with pytest.raises(WorkspaceUnavailable) as exc:
        run(client.call("ping"))
    message = str(exc.value)
    assert "comfy_node" in message and "custom_nodes" in message


def test_probe_reports_a_missing_node_without_raising():
    """workspace_status has to describe every failure; raising tells the caller less."""
    state = run(bridge(routed(**ALIVE)).probe())
    assert state["available"] is False
    assert state["reason"] == "bridge_missing"


def test_probe_reports_comfyui_being_down_before_anything_else():
    down = routed(system_stats=lambda r: httpx.Response(500))
    state = run(bridge(down).probe())
    assert (state["available"], state["reason"]) == (False, "comfyui_down")
    assert "comfy_start" in state["hint"]


def test_no_workspace_points_at_the_browser():
    handler = routed(
        **ALIVE,
        call=lambda r: error_response(409, "no_workspace", "No ComfyUI tab is connected."),
    )
    with pytest.raises(WorkspaceUnavailable, match="reload the page"):
        run(bridge(handler).call("get_graph"))


def test_no_workspace_says_the_rest_of_the_server_still_works():
    handler = routed(**ALIVE, call=lambda r: error_response(409, "no_workspace", "nope"))
    with pytest.raises(WorkspaceUnavailable, match="HTTP API"):
        run(bridge(handler).call("get_graph"))


def test_probe_separates_an_installed_node_from_a_connected_tab():
    empty = routed(**ALIVE, clients=lambda r: json_response(200, {"protocol": PROTOCOL, "preferred": None, "clients": []}))
    state = run(bridge(empty).probe())
    assert state == {
        "available": False,
        "node_installed": True,
        "protocol": PROTOCOL,
        "preferred_client": None,
        "clients": [],
        "reason": "no_workspace",
        "hint": state["hint"],
    }
    assert "browser" in state["hint"]


def test_probe_is_available_once_a_tab_answers():
    state = run(bridge(one_client()).probe())
    assert state["available"] is True
    assert state["preferred_client"] == "tab-1"


def test_protocol_mismatch_is_reported_rather_than_guessed_at():
    state = run(bridge(one_client(protocol=PROTOCOL + 1)).probe())
    assert "protocol_warning" in state
    assert "reinstall" in state["protocol_warning"]


def test_call_returns_the_whole_envelope():
    """Which tab answered is part of the answer once several can be open."""
    handler = routed(
        **ALIVE,
        call=lambda r: json_response(200, {"client_id": "tab-1", "result": {"node_count": 3}}),
    )
    reply = run(bridge(handler).call("get_graph"))
    assert reply == {"client_id": "tab-1", "result": {"node_count": 3}}


def test_call_forwards_params_and_the_bridge_timeout():
    seen: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return json_response(200, {"client_id": "tab-1", "result": None})

    run(bridge(routed(**ALIVE, call=capture), bridge_timeout=42).call("get_graph", {"format": "ui"}))
    assert seen == {"method": "get_graph", "params": {"format": "ui"}, "timeout": 42}


def test_explicit_client_id_is_forwarded_only_when_given():
    seen: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return json_response(200, {"client_id": "tab-2", "result": None})

    client = bridge(routed(**ALIVE, call=capture))
    run(client.call("ping"))
    run(client.call("ping", client_id="tab-2"))
    assert "client_id" not in seen[0]
    assert seen[1]["client_id"] == "tab-2"


def test_http_deadline_outlasts_the_bridge_deadline():
    """The node's own timeout produces a useful message; httpx giving up first loses it."""
    seen: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(request.extensions.get("timeout") or {})
        return json_response(200, {"client_id": "tab-1", "result": None})

    run(bridge(routed(**ALIVE, call=capture), bridge_timeout=20).call("ping"))
    assert seen["read"] > 20


def test_a_handler_that_threw_keeps_its_browser_detail():
    handler = routed(
        **ALIVE,
        call=lambda r: error_response(
            502, "workspace_error", "get_graph failed in the browser: no graph", detail="at describeGraph"
        ),
    )
    with pytest.raises(WorkspaceError) as exc:
        run(bridge(handler).call("get_graph"))
    assert "no graph" in str(exc.value)
    assert "at describeGraph" in str(exc.value)


def test_a_refusal_arrives_as_its_message_and_nothing_else():
    # The tab sends no detail for a condition it recognised and wrote a sentence
    # about. Four frames of our own dispatcher would only bury that sentence -
    # and the criterion is whether the message is the whole answer, which is why
    # the test above still gets its trace.
    refusal = "set_selection failed in the browser: no node '999' in the graph on screen"
    handler = routed(**ALIVE, call=lambda r: error_response(502, "workspace_error", refusal, detail=None))
    with pytest.raises(WorkspaceError) as exc:
        run(bridge(handler).call("set_selection"))
    assert str(exc.value) == refusal


def test_a_silent_tab_is_a_workspace_error_not_an_unavailable_one():
    """It answered the socket once, so this is a fault to look at, not a setup step."""
    handler = routed(**ALIVE, call=lambda r: error_response(504, "timeout", "the workspace did not answer"))
    with pytest.raises(WorkspaceError, match="did not answer"):
        run(bridge(handler).call("get_graph"))


def test_unknown_client_sends_the_caller_to_workspace_status():
    handler = routed(**ALIVE, call=lambda r: error_response(409, "unknown_client", "client_id x is not connected"))
    with pytest.raises(WorkspaceUnavailable, match="workspace_status"):
        run(bridge(handler).call("ping", client_id="x"))


def test_a_non_json_failure_still_produces_a_message():
    handler = routed(**ALIVE, call=lambda r: httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(WorkspaceError, match="Internal Server Error"):
        run(bridge(handler).call("ping"))


def test_token_is_sent_only_when_one_is_configured():
    seen: list[str | None] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-MCP-Bridge-Token"))
        return json_response(200, {"client_id": "tab-1", "result": None})

    run(bridge(routed(**ALIVE, call=capture)).call("ping"))
    run(bridge(routed(**ALIVE, call=capture), bridge_token="s3cret").call("ping"))
    assert seen == [None, "s3cret"]


def test_a_rejected_token_says_which_variable_to_set():
    handler = routed(**ALIVE, call=lambda r: error_response(401, "unauthorized", "X-MCP-Bridge-Token missing"))
    with pytest.raises(WorkspaceUnavailable, match="COMFYUI_MCP_BRIDGE_TOKEN"):
        run(bridge(handler, bridge_token="wrong").call("ping"))


def test_an_empty_edit_is_refused():
    with pytest.raises(ComfyError, match="nothing to set"):
        run(S.set_workspace_values({}))
    with pytest.raises(ComfyError, match="modes is empty"):
        run(S.set_workspace_node_modes({}))


@pytest.mark.parametrize("key", ["37", "", ".", "megapixels"])
def test_keys_that_are_not_node_dot_name_paths_are_named(key):
    with pytest.raises(ComfyError, match="not <node_id>.<name> paths"):
        run(S.set_workspace_values({key: 1}))


@pytest.mark.parametrize("key", ["37", "", ".", "text_for_toggles"])
def test_a_property_key_is_held_to_the_same_shape(key):
    with pytest.raises(ComfyError, match="not <node_id>.<name> paths"):
        run(S.set_workspace_values(properties={key: 1}))


def editing(monkeypatch: pytest.MonkeyPatch, seen: list[dict[str, Any]]) -> None:
    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return json_response(200, {"client_id": "tab-1", "result": {"applied": [], "errors": []}})

    as_workspace(monkeypatch, routed(**ALIVE, **ONE_TAB, call=capture))


def test_widgets_and_properties_travel_in_separate_maps(monkeypatch):
    # They are two namespaces and the names collide: darkilMultiToggles carries a
    # `delimiter` in both, so one merged map could not say which was meant.
    seen: list[dict[str, Any]] = []
    editing(monkeypatch, seen)
    run(S.set_workspace_values(values={"1.delimiter": " | "}, properties={"1.delimiter": ", "}))
    assert seen[0]["params"]["values"] == {"1.delimiter": " | "}
    assert seen[0]["params"]["properties"] == {"1.delimiter": ", "}


def test_a_properties_only_edit_still_goes_out(monkeypatch):
    seen: list[dict[str, Any]] = []
    editing(monkeypatch, seen)
    run(S.set_workspace_values(properties={"1.text_for_toggles": "a;b;c"}))
    assert seen[0]["params"] == {
        "values": {},
        "properties": {"1.text_for_toggles": "a;b;c"},
        "labels": {},
        "scope": "root",
    }


def test_labels_travel_as_a_third_namespace(monkeypatch):
    seen: list[dict[str, Any]] = []
    editing(monkeypatch, seen)
    spec = {"title": "Текст (многострочный)", "outputs": {"STRING": "Строка"}}
    run(S.set_workspace_values(labels={"1": spec}))
    assert seen[0]["params"]["labels"] == {"1": spec}
    assert seen[0]["params"]["values"] == {}


def test_labels_alone_are_enough_to_make_a_call(monkeypatch):
    editing(monkeypatch, [])
    run(S.set_workspace_values(labels={"1": {"title": "x"}}))  # must not raise


def test_a_label_keyed_by_a_path_says_where_the_slot_name_goes(monkeypatch):
    # The obvious wrong guess, since the other two maps are keyed that way.
    editing(monkeypatch, [])
    with pytest.raises(ComfyError, match="keyed by node id alone"):
        run(S.set_workspace_values(labels={"1.STRING": "Строка"}))


def test_an_empty_call_now_mentions_labels_too(monkeypatch):
    editing(monkeypatch, [])
    with pytest.raises(ComfyError, match="labels="):
        run(S.set_workspace_values())


def test_an_unknown_mode_lists_the_ones_that_exist():
    with pytest.raises(ComfyError, match="always, muted, bypassed"):
        run(S.set_workspace_node_modes({"37": "off"}))


@pytest.mark.parametrize(
    "call_tool",
    [
        lambda: S.get_workspace_graph(scope="elsewhere"),
        lambda: S.set_workspace_values({"37.x": 1}, scope="elsewhere"),
        lambda: S.set_workspace_node_modes({"37": "muted"}, scope="elsewhere"),
    ],
)
def test_every_workspace_tool_checks_scope(call_tool):
    with pytest.raises(ComfyError, match="scope must be one of"):
        run(call_tool())


def test_workspace_status_lists_the_tools_a_tab_unlocks():
    assert "set_workspace_values" in S.WORKSPACE_TOOLS
    assert set(S.WORKSPACE_TOOLS) >= {
        "get_workspace_graph",
        "diagnose_workspace",
        "navigate_workspace",
        "set_workspace_node_modes",
        "add_workspace_node",
        "remove_workspace_nodes",
        "set_workspace_links",
        "run_workspace",
    }


def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record what open_workspace hands to the browser instead of opening one."""
    urls: list[str] = []
    monkeypatch.setattr(S, "open_in_browser", urls.append)
    return urls


def clients_route(*tabs: str) -> Handler:
    payload = {
        "protocol": PROTOCOL,
        "preferred": tabs[0] if tabs else None,
        "clients": [{"client_id": t, "frontend": "1.47.10", "methods": []} for t in tabs],
    }
    return lambda r: json_response(200, payload)


def test_a_tab_that_is_already_connected_is_left_alone(monkeypatch):
    """A second tab becomes the preferred client, so the user's own stops being edited."""
    urls = opened(monkeypatch)
    as_workspace(monkeypatch, routed(**ALIVE, clients=clients_route("tab-1")))

    result = run(S.open_workspace())

    assert urls == []
    assert (result["opened"], result["connected"], result["already_open"]) == (False, True, True)


def test_force_opens_one_anyway(monkeypatch):
    urls = opened(monkeypatch)
    as_workspace(monkeypatch, routed(**ALIVE, clients=clients_route("tab-1")))

    result = run(S.open_workspace(force=True, wait=0))

    assert urls == [S.CFG.base_url]
    assert result["opened"] is True


def test_opening_waits_for_the_tab_to_register(monkeypatch):
    """Returning before the page has run its JavaScript just fails the next call."""
    urls = opened(monkeypatch)
    answers = [clients_route(), clients_route(), clients_route("tab-9")]

    def clients(request: httpx.Request) -> httpx.Response:
        return (answers.pop(0) if len(answers) > 1 else answers[0])(request)

    as_workspace(monkeypatch, routed(**ALIVE, clients=clients))
    monkeypatch.setattr(S, "_WORKSPACE_POLL_INTERVAL", 0)

    result = run(S.open_workspace(wait=5))

    assert urls == [S.CFG.base_url]
    assert result["connected"] is True
    assert result["clients"][0]["client_id"] == "tab-9"


def test_a_tab_that_never_registers_says_so_rather_than_claiming_success(monkeypatch):
    opened(monkeypatch)
    as_workspace(monkeypatch, routed(**ALIVE, clients=clients_route()))
    monkeypatch.setattr(S, "_WORKSPACE_POLL_INTERVAL", 0)

    result = run(S.open_workspace(wait=0))

    assert (result["opened"], result["connected"]) == (True, False)
    assert result["reason"] == "no_workspace"


def test_a_missing_node_opens_the_ui_but_does_not_wait_for_what_cannot_come(monkeypatch):
    """Nothing can ever register without the node, so the deadline would be burnt."""
    urls = opened(monkeypatch)
    as_workspace(monkeypatch, routed(**ALIVE))  # /clients unrouted -> 404

    result = run(S.open_workspace(wait=600))

    assert urls == [S.CFG.base_url]
    assert (result["opened"], result["connected"]) == (True, False)
    assert result["reason"] == "bridge_missing"


def test_a_dead_comfyui_is_refused_rather_than_opening_an_error_page(monkeypatch):
    opened(monkeypatch)
    as_workspace(monkeypatch, lambda r: httpx.Response(500, text="down"))

    with pytest.raises(ComfyError, match="comfy_start"):
        run(S.open_workspace())


def test_a_desktop_with_no_browser_is_an_error_not_a_silent_wait(monkeypatch):
    def refuse(url: str) -> None:
        raise ProcessError("no browser is registered")

    monkeypatch.setattr(S, "open_in_browser", refuse)
    as_workspace(monkeypatch, routed(**ALIVE, clients=clients_route()))

    with pytest.raises(ComfyError, match="no browser is registered"):
        run(S.open_workspace(wait=0))


def test_all_is_a_scope_the_reader_accepts(monkeypatch):
    """Subgraph internals are the bulk of a real workflow, so reading has to reach them."""
    seen: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return json_response(200, {"client_id": "tab-1", "result": {"nodes": []}})

    as_workspace(monkeypatch, routed(**ALIVE, call=capture))
    run(S.get_workspace_graph(scope="all"))
    assert seen["params"]["scope"] == "all"


def test_navigating_nowhere_is_refused():
    with pytest.raises(ComfyError, match="to is required"):
        run(S.navigate_workspace(""))


def test_diagnosis_defaults_to_the_whole_workflow(monkeypatch):
    """Checking only the top level would pass a graph that cannot run."""
    seen: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return json_response(200, {"client_id": "tab-1", "result": {"nodes": []}})

    as_workspace(monkeypatch, routed(**ALIVE, call=capture, object_info=lambda r: json_response(200, {})))
    result = run(S.diagnose_workspace())
    assert seen["params"]["scope"] == "all"
    assert result["counts"] == {"error": 0, "warning": 0, "note": 0}
    assert "queue time" in result["summary"]


def test_diagnosis_says_when_it_had_no_schemas(monkeypatch):
    """Half the checks need them, and a caller should not read silence as health."""
    handler = routed(
        **ALIVE,
        call=lambda r: json_response(
            200,
            {
                "client_id": "tab-1",
                "result": {"nodes": [{"id": "1", "type": "KSampler", "registered": True, "inputs": [], "outputs": []}]},
            },
        ),
    )
    as_workspace(monkeypatch, handler)
    monkeypatch.setattr(S, "_SCHEMA_CACHE", {})
    result = run(S.diagnose_workspace())
    assert result["schemas_available"] is False
    assert "no node schemas" in result["note"]


@pytest.mark.parametrize("scope", ["root", "active", "all"])
def test_diagnosis_takes_the_same_scopes_as_the_reader(scope, monkeypatch):
    as_workspace(
        monkeypatch,
        routed(**ALIVE, call=lambda r: json_response(200, {"client_id": "tab-1", "result": {"nodes": []}})),
    )
    assert run(S.diagnose_workspace(scope=scope))["scope"] == scope


def test_diagnosis_rejects_a_scope_that_is_not_one():
    with pytest.raises(ComfyError, match="scope must be one of"):
        run(S.diagnose_workspace(scope="elsewhere"))


FAT_NODE = {
    "Switcher": {
        "input": {"required": {f"lora_{i}_name": [[f"m{j}.safetensors" for j in range(72)]] for i in range(1, 51)}},
        "output": ["MODEL"],
        "output_name": ["MODEL"],
        "category": "loaders",
        "python_module": "custom_nodes.ComfyUI-Easy-Use",
    }
}


def as_comfy(monkeypatch: pytest.MonkeyPatch, catalogue: dict[str, Any], seen: list[str] | None = None) -> None:
    """Point the server at a mocked /object_info, both whole and per class.

    Measured against the running instance: an unknown class is answered 200 with
    an empty object, not a 404.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if seen is not None:
            seen.append(path)
        if path == "/object_info":
            return json_response(200, catalogue)
        if path.startswith("/object_info/"):
            wanted = path.rsplit("/", 1)[-1]
            entry = catalogue.get(wanted)
            return json_response(200, {wanted: entry} if entry else {})
        return json_response(200, {"system": {}, "devices": []})

    comfy = ComfyClient(S.CFG)
    comfy._http = httpx.AsyncClient(base_url=S.CFG.base_url, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(S, "CLIENT", comfy)
    S._forget_schemas()


def test_describing_a_node_summarises_rather_than_returning_the_raw_entry(monkeypatch):
    """The raw entry for this shape measures 199k characters on the real install."""
    as_comfy(monkeypatch, FAT_NODE)

    described = run(S.describe_node("Switcher"))

    assert described["class_type"] == "Switcher"
    assert described["inputs"][0]["options_total"] == 72
    assert len(json.dumps(described)) < len(json.dumps(FAT_NODE)) / 10


def test_the_raw_entry_is_still_reachable_when_a_truncated_list_is_the_problem(monkeypatch):
    as_comfy(monkeypatch, FAT_NODE)

    assert run(S.describe_node("Switcher", full=True)) == FAT_NODE


def test_an_unknown_node_type_says_how_to_find_a_real_one(monkeypatch):
    as_comfy(monkeypatch, FAT_NODE)

    with pytest.raises(ComfyError, match="find_node_types"):
        run(S.describe_node("NoSuchNode"))


def test_the_whole_payload_is_fetched_once_and_then_reused(monkeypatch):
    """It is 4.4 MB and several seconds on a large install."""
    seen: list[str] = []
    as_comfy(monkeypatch, FAT_NODE, seen)

    run(S.find_node_types(search="switcher"))
    run(S.find_node_types(output_type="MODEL"))
    assert seen.count("/object_info") == 1

    run(S.find_node_types(refresh=True))
    assert seen.count("/object_info") == 2


def test_a_node_type_is_required_and_the_error_says_where_to_look():
    with pytest.raises(ComfyError, match="find_node_types"):
        run(S.add_workspace_node(""))


def test_a_position_that_is_not_a_pair_is_refused():
    with pytest.raises(ComfyError, match="pos must be"):
        run(S.add_workspace_node("ImageScale", pos=[1, 2, 3]))


def test_removing_nothing_is_refused_rather_than_being_a_no_op():
    with pytest.raises(ComfyError, match="nodes is empty"):
        run(S.remove_workspace_nodes([]))


def test_a_link_call_with_neither_list_says_what_both_look_like():
    with pytest.raises(ComfyError, match="disconnect=") as exc:
        run(S.set_workspace_links())
    assert "connect=" in str(exc.value)


@pytest.mark.parametrize("ref", ["37", "", ".", "images"])
def test_disconnect_targets_that_are_not_paths_are_named(ref):
    with pytest.raises(ComfyError, match="not <node_id>.<input> paths"):
        run(S.set_workspace_links(disconnect=[ref]))


@pytest.mark.parametrize(
    "call_tool",
    [
        lambda: S.add_workspace_node("ImageScale", scope="elsewhere"),
        lambda: S.remove_workspace_nodes(["37"], scope="elsewhere"),
        lambda: S.set_workspace_links(disconnect=["37.image"], scope="elsewhere"),
    ],
)
def test_every_structural_tool_checks_scope(call_tool):
    with pytest.raises(ComfyError, match="scope must be one of"):
        run(call_tool())


def test_add_node_forwards_the_whole_edit_as_one_call(monkeypatch):
    """Widgets and links travel with the node so the insert is one undo step."""
    seen: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return json_response(200, {"client_id": "tab-1", "result": {"added": {"node": "42"}}})

    as_workspace(monkeypatch, routed(**ALIVE, call=capture))
    run(
        S.add_workspace_node(
            "ImageScale",
            title="upscale",
            values={"width": 2048},
            connect=[{"from": "8.IMAGE", "to": "this.image"}],
        )
    )
    assert seen["method"] == "add_node"
    assert seen["params"]["type"] == "ImageScale"
    assert seen["params"]["values"] == {"width": 2048}
    assert seen["params"]["connect"] == [{"from": "8.IMAGE", "to": "this.image"}]
    # Omitted rather than sent as null: the tab decides where, and "middle of the
    # view" is not something this side can compute.
    assert "pos" not in seen["params"]


def small_graph(request: httpx.Request) -> httpx.Response:
    """A graph the arranger has something to do with: 1 feeds 2, and 3 is off on its own."""
    return json_response(
        200,
        {
            "client_id": "tab-1",
            "result": {
                "nodes": [
                    {"id": "1", "type": "A", "pos": [0, 0], "size": [200, 100], "inputs": [], "outputs": []},
                    {
                        "id": "2",
                        "type": "B",
                        "pos": [0, 200],
                        "size": [200, 100],
                        "inputs": [{"name": "x", "type": "*", "from": {"node": "1", "slot": 0}}],
                        "outputs": [],
                    },
                    {"id": "3", "type": "C", "pos": [0, 2000], "size": [200, 100], "inputs": [], "outputs": []},
                ]
            },
        },
    )


def test_a_layout_call_with_no_batch_at_all_says_what_they_look_like():
    with pytest.raises(ComfyError, match="sizes=") as exc:
        run(S.set_workspace_layout())
    assert "positions=" in str(exc.value) and "collapsed=" in str(exc.value)


@pytest.mark.parametrize("value", ["true", 1, None, "toggle"])
def test_folding_takes_a_state_rather_than_a_toggle(value):
    """`collapse()` in litegraph toggles, so a blind batch would unfold what was folded."""
    with pytest.raises(ComfyError, match="not a toggle"):
        run(S.set_workspace_layout(collapsed={"77": value}))


def test_folding_travels_in_the_same_call_as_moving(monkeypatch):
    """Tidying is one intention, so folding three nodes and moving one is one Ctrl+Z."""
    seen: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return json_response(200, {"client_id": "tab-1", "result": {"moved": [], "collapsed": []}})

    as_workspace(monkeypatch, routed(**ALIVE, call=capture))
    run(S.set_workspace_layout(positions={"37": [10, 20]}, collapsed={"77": False}))
    assert seen["method"] == "set_layout"
    assert seen["params"]["collapsed"] == {"77": False}
    assert seen["params"]["positions"] == {"37": [10, 20]}


@pytest.mark.parametrize("pair", [[1, 2, 3], [1], "10,20", 5])
def test_a_position_that_is_not_a_pair_of_numbers_is_refused(pair):
    with pytest.raises(ComfyError, match="must be a pair of numbers"):
        run(S.set_workspace_layout(positions={"37": pair}))


def test_moving_nodes_asks_the_tab_to_refit_the_groups_they_were_in(monkeypatch):
    """A group is only a rectangle, so nodes leaving one silently empty it."""
    seen: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return json_response(200, {"client_id": "tab-1", "result": {"moved": []}})

    as_workspace(monkeypatch, routed(**ALIVE, call=capture))
    run(S.set_workspace_layout(positions={"37": [10, 20]}))
    assert seen["method"] == "set_layout"
    assert seen["params"]["positions"] == {"37": [10, 20]}
    assert seen["params"]["refit_groups"] is True


def test_a_group_call_with_nothing_in_it_is_refused():
    with pytest.raises(ComfyError, match="pass create, update or remove"):
        run(S.set_workspace_groups())


def test_group_edits_travel_as_one_call(monkeypatch):
    seen: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return json_response(200, {"client_id": "tab-1", "result": {"created": []}})

    as_workspace(monkeypatch, routed(**ALIVE, call=capture))
    run(S.set_workspace_groups(create=[{"title": "Sampling", "nodes": ["3"]}], remove=["Old"]))
    assert seen["method"] == "set_groups"
    assert seen["params"]["create"] == [{"title": "Sampling", "nodes": ["3"]}]
    assert seen["params"]["remove"] == ["Old"]
    assert seen["params"]["update"] == []


def test_arranging_everything_at_once_across_subgraphs_is_refused():
    """Each subgraph is its own canvas, so there is no one layout that covers several."""
    with pytest.raises(ComfyError, match="canvas of its own"):
        run(S.arrange_workspace(scope="all"))


def test_arranging_reads_then_writes(monkeypatch):
    calls: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body["method"] == "get_graph":
            return small_graph(request)
        return json_response(200, {"client_id": "tab-1", "result": {"moved": [], "groups": []}})

    as_workspace(monkeypatch, routed(**ALIVE, call=capture))
    result = run(S.arrange_workspace())

    assert [call["method"] for call in calls] == ["get_graph", "set_layout"]
    # Read without widget values: the arranger needs geometry, and a full read of
    # a real workflow is most of a megabyte of prompts and lora lists.
    assert calls[0]["params"]["widgets"] is False
    assert calls[1]["params"]["positions"] == result["positions"]
    assert result["applied"] is True and result["columns"] == 2


def test_arranging_can_report_a_plan_without_touching_the_canvas(monkeypatch):
    calls: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return small_graph(request)

    as_workspace(monkeypatch, routed(**ALIVE, call=capture))
    result = run(S.arrange_workspace(apply=False))

    assert [call["method"] for call in calls] == ["get_graph"]
    assert result["applied"] is False
    assert result["positions"] and "set_workspace_layout" in result["note"]


def test_arranging_a_node_that_is_not_there_names_it(monkeypatch):
    as_workspace(monkeypatch, routed(**ALIVE, call=small_graph))
    with pytest.raises(ComfyError, match="no such node"):
        run(S.arrange_workspace(only=["1", "404"]))


def test_arranging_a_subset_leaves_the_rest_alone(monkeypatch):
    calls: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body["method"] == "get_graph":
            return small_graph(request)
        return json_response(200, {"client_id": "tab-1", "result": {"moved": [], "groups": []}})

    as_workspace(monkeypatch, routed(**ALIVE, call=capture))
    result = run(S.arrange_workspace(only=["1", "2"]))

    assert result["arranged"] == 2
    # Node 3 was never read into the plan, so it cannot appear in the write.
    assert "3" not in calls[1]["params"]["positions"]
    assert set(calls[1]["params"]["positions"]) <= {"1", "2"}


def test_an_empty_canvas_says_so_rather_than_reporting_a_layout(monkeypatch):
    as_workspace(
        monkeypatch,
        routed(**ALIVE, call=lambda r: json_response(200, {"client_id": "tab-1", "result": {"nodes": []}})),
    )
    with pytest.raises(ComfyError, match="no nodes to arrange"):
        run(S.arrange_workspace())


def test_a_canvas_already_laid_out_is_left_alone(monkeypatch):
    """Writing the positions it already has would be an undo step that undoes nothing."""
    calls: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body["method"] == "get_graph":
            return small_graph(request)
        return json_response(200, {"client_id": "tab-1", "result": {"moved": [], "groups": []}})

    as_workspace(monkeypatch, routed(**ALIVE, call=capture))
    settled = run(S.arrange_workspace())["positions"]

    def replay(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        response = small_graph(request)
        graph = json.loads(response.content)
        for node in graph["result"]["nodes"]:
            if node["id"] in settled:
                node["pos"] = settled[node["id"]]
        return json_response(200, graph)

    calls.clear()
    as_workspace(monkeypatch, routed(**ALIVE, call=replay))
    result = run(S.arrange_workspace())
    assert [call["method"] for call in calls] == ["get_graph"]
    assert result["applied"] is False and result["moved"] == 0


def test_aligning_reads_then_writes(monkeypatch):
    calls: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body["method"] == "get_graph":
            return small_graph(request)
        return json_response(200, {"client_id": "tab-1", "result": {"moved": [], "groups": []}})

    as_workspace(monkeypatch, routed(**ALIVE, call=capture))
    # "top", not "left": the fixture already shares an x, and aligning to a line
    # everything is on writes nothing at all - which is a different test.
    result = run(S.align_workspace(["1", "2", "3"], edge="top"))

    assert [call["method"] for call in calls] == ["get_graph", "set_layout"]
    assert result["applied"] is True and result["edge"] == "top"
    assert calls[1]["params"]["positions"] == result["positions"]


def test_aligning_names_a_node_that_is_not_there(monkeypatch):
    as_workspace(monkeypatch, routed(**ALIVE, call=small_graph))
    with pytest.raises(ComfyError, match="no such node"):
        run(S.align_workspace(["1", "404"], edge="left"))


def test_contradictory_alignment_is_refused_before_anything_is_read(monkeypatch):
    """Snapping to a line and spreading along it cannot both win."""
    as_workspace(monkeypatch, routed(**ALIVE, call=small_graph))
    with pytest.raises(ComfyError, match="both act on the x axis"):
        run(S.align_workspace(["1", "2"], edge="left", distribute="x"))


def test_aligning_across_subgraphs_is_refused():
    with pytest.raises(ComfyError, match="canvas of its own"):
        run(S.align_workspace(["1", "2"], edge="left", scope="all"))


@pytest.mark.parametrize(
    "call_tool",
    [
        lambda: S.set_workspace_layout(positions={"37": [0, 0]}, scope="elsewhere"),
        lambda: S.set_workspace_groups(remove=["x"], scope="elsewhere"),
        lambda: S.arrange_workspace(scope="elsewhere"),
        lambda: S.align_workspace(["1", "2"], edge="left", scope="elsewhere"),
    ],
)
def test_every_layout_tool_checks_scope(call_tool):
    with pytest.raises(ComfyError, match="scope must be one of"):
        run(call_tool())


def as_workspace(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    """Point the server's own singletons at a mocked ComfyUI and bridge."""
    comfy = ComfyClient(S.CFG)
    comfy._http = httpx.AsyncClient(base_url=S.CFG.base_url, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(S, "CLIENT", comfy)
    monkeypatch.setattr(S, "BRIDGE", BridgeClient(S.CFG, comfy))


def saving(monkeypatch: pytest.MonkeyPatch, tmp_path, payload: dict[str, Any]):
    """A tab answering get_graph with `payload`, and disposable output directories."""
    exports, workflows = tmp_path / "exports", tmp_path / "workflows"
    exports.mkdir()
    workflows.mkdir()
    monkeypatch.setattr(S, "CFG", dataclasses.replace(S.CFG, export_dir=exports, workflows_dir=workflows))
    as_workspace(
        monkeypatch,
        routed(**ALIVE, **ONE_TAB, call=lambda r: json_response(200, {"client_id": "tab-1", "result": payload})),
    )
    return exports, workflows


UI_REPLY = {"format": "ui", "scope": "root", "graph": {"nodes": [{"id": 1}, {"id": 2}], "groups": []}}
API_REPLY = {"format": "api", "scope": "root", "prompt": {"1": {"class_type": "KSampler"}}}


def test_a_ui_save_goes_to_the_export_directory(monkeypatch, tmp_path):
    exports, _ = saving(monkeypatch, tmp_path, UI_REPLY)
    result = run(S.save_workspace("canvas"))
    assert result["path"].startswith(str(exports))
    assert result["nodes"] == 2


def test_an_api_save_goes_where_run_workflow_will_find_it(monkeypatch, tmp_path):
    _, workflows = saving(monkeypatch, tmp_path, API_REPLY)
    result = run(S.save_workspace("canvas", format="api"))
    assert result["path"].startswith(str(workflows))
    assert "run_workflow" in result["note"]


def test_a_save_refuses_a_format_that_is_neither(monkeypatch, tmp_path):
    saving(monkeypatch, tmp_path, UI_REPLY)
    with pytest.raises(ComfyError, match="must be 'ui' or 'api'"):
        run(S.save_workspace("canvas", format="summary"))


def test_a_save_refuses_scope_all(monkeypatch, tmp_path):
    # "all" means "descend into subgraphs", which only the summary does - both of
    # these formats already contain everything nested inside them.
    saving(monkeypatch, tmp_path, UI_REPLY)
    with pytest.raises(ComfyError, match="must be 'root' or 'active'"):
        run(S.save_workspace("canvas", scope="all"))


def test_a_tab_that_returned_no_graph_does_not_leave_an_empty_file(monkeypatch, tmp_path):
    exports, _ = saving(monkeypatch, tmp_path, {"format": "ui", "scope": "root"})
    with pytest.raises(ComfyError, match="no ui-format graph"):
        run(S.save_workspace("canvas"))
    assert list(exports.iterdir()) == []


def undoing(monkeypatch: pytest.MonkeyPatch, seen: list[dict[str, Any]] | None = None, **result: Any):
    def capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        return json_response(
            200,
            {
                "client_id": "tab-1",
                "result": {
                    "direction": "undo",
                    "steps": 1,
                    "requested": 1,
                    "undo_depth": 4,
                    "redo_depth": 1,
                    "node_count": 9,
                    **result,
                },
            },
        )

    as_workspace(monkeypatch, routed(**ALIVE, **ONE_TAB, call=capture))


def test_one_step_is_the_default(monkeypatch):
    # One workspace call is one undo step, so one press is the ordinary ask.
    seen: list[dict[str, Any]] = []
    undoing(monkeypatch, seen)
    result = run(S.undo_workspace())
    assert seen[0]["params"] == {"steps": 1, "redo": False}
    assert result["undo_depth"] == 4
    assert "note" not in result


def test_zero_steps_reads_the_depth_without_moving(monkeypatch):
    seen: list[dict[str, Any]] = []
    undoing(monkeypatch, seen, steps=0, requested=0)
    result = run(S.undo_workspace(steps=0))
    assert seen[0]["params"]["steps"] == 0
    assert "nothing changed" in result["note"]


def test_an_empty_history_is_reported_rather_than_treated_as_a_failure(monkeypatch):
    # updateState pops, and popping an empty queue does nothing at all - so the
    # difference between "undone" and "there was nothing" has to be said out loud.
    undoing(monkeypatch, steps=0, undo_depth=0, available_was=0)
    assert "no undo history left" in run(S.undo_workspace())["note"]


def test_redo_is_the_same_stack_the_other_way(monkeypatch):
    seen: list[dict[str, Any]] = []
    undoing(monkeypatch, seen, direction="redo")
    assert run(S.undo_workspace(redo=True))["direction"] == "redo"
    assert seen[0]["params"]["redo"] is True


@pytest.mark.parametrize("steps", [-1, 51])
def test_a_step_count_outside_the_range_is_refused(monkeypatch, steps):
    undoing(monkeypatch)
    with pytest.raises(ComfyError, match="between 0 and 50"):
        run(S.undo_workspace(steps=steps))


CANVAS = {"nodes": [{"id": 1, "type": "KSampler"}], "links": []}


def loading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    seen: list[dict[str, Any]] | None = None,
    canvas: dict[str, Any] | None = CANVAS,
    fail_on: str = "",
    **result: Any,
):
    """Disposable directories, and a tab that answers get_graph and load_graph."""
    exports, workflows = tmp_path / "exports", tmp_path / "workflows"
    exports.mkdir()
    workflows.mkdir()
    monkeypatch.setattr(S, "CFG", dataclasses.replace(S.CFG, export_dir=exports, workflows_dir=workflows))

    def capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        if body["method"] == fail_on:
            return error_response(502, "workspace_error", f"{fail_on} failed")
        if body["method"] == "get_graph":
            return json_response(200, {"client_id": "tab-1", "result": {"graph": canvas}})
        return json_response(
            200,
            {"client_id": "tab-1", "result": {"node_count": 2, "breadcrumb": "root", **result}},
        )

    as_workspace(monkeypatch, routed(**ALIVE, **ONE_TAB, call=capture))
    return exports, workflows


def test_a_ui_export_is_loaded_as_ui_format(monkeypatch, tmp_path):
    seen: list[dict[str, Any]] = []
    exports, _ = loading(monkeypatch, tmp_path, seen)
    (exports / "canvas.json").write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
    run(S.load_workspace("canvas"))
    load = [call for call in seen if call["method"] == "load_graph"][0]
    assert load["params"]["format"] == "ui"
    assert load["params"]["name"] == "canvas"


def test_a_workflow_file_is_loaded_as_api_format(monkeypatch, tmp_path):
    # Nothing names the format: the file is read for it, because the two loaders
    # are not interchangeable and the wrong one produces an empty canvas.
    seen: list[dict[str, Any]] = []
    _, workflows = loading(monkeypatch, tmp_path, seen)
    (workflows / "sampler.json").write_text(json.dumps({"1": {"class_type": "KSampler"}}), encoding="utf-8")
    run(S.load_workspace("sampler"))
    assert [c for c in seen if c["method"] == "load_graph"][0]["params"]["format"] == "api"


def test_the_canvas_is_written_out_before_it_is_replaced(monkeypatch, tmp_path):
    seen: list[dict[str, Any]] = []
    exports, workflows = loading(monkeypatch, tmp_path, seen)
    (workflows / "sampler.json").write_text(json.dumps({"1": {"class_type": "KSampler"}}), encoding="utf-8")
    result = run(S.load_workspace("sampler"))

    assert [call["method"] for call in seen] == ["get_graph", "load_graph"]
    backup = Path(result["replaced"])
    assert backup.parent == exports
    assert json.loads(backup.read_text(encoding="utf-8")) == CANVAS


def test_a_backup_that_failed_stops_the_load(monkeypatch, tmp_path):
    # The whole point of the backup is that the step is reversible. Loading anyway
    # would leave the caller believing it was.
    seen: list[dict[str, Any]] = []
    _, workflows = loading(monkeypatch, tmp_path, seen, fail_on="get_graph")
    (workflows / "sampler.json").write_text(json.dumps({"1": {"class_type": "KSampler"}}), encoding="utf-8")
    with pytest.raises(WorkspaceError):
        run(S.load_workspace("sampler"))
    assert [call["method"] for call in seen] == ["get_graph"]


def test_an_empty_canvas_leaves_no_file_behind(monkeypatch, tmp_path):
    exports, workflows = loading(monkeypatch, tmp_path, canvas={"nodes": [], "links": []})
    (workflows / "sampler.json").write_text(json.dumps({"1": {"class_type": "KSampler"}}), encoding="utf-8")
    result = run(S.load_workspace("sampler"))
    assert result["replaced"] is None
    assert list(exports.iterdir()) == []
    assert "nothing to back up" in result["note"]


def test_turning_the_backup_off_does_not_even_read_the_canvas(monkeypatch, tmp_path):
    seen: list[dict[str, Any]] = []
    _, workflows = loading(monkeypatch, tmp_path, seen)
    (workflows / "sampler.json").write_text(json.dumps({"1": {"class_type": "KSampler"}}), encoding="utf-8")
    run(S.load_workspace("sampler", backup=False))
    assert [call["method"] for call in seen] == ["load_graph"]


def test_every_load_says_that_ctrl_z_will_not_undo_it(monkeypatch, tmp_path):
    # ComfyUI resets the change tracker on load, so this is not a caveat that can
    # be engineered away - only reported.
    _, workflows = loading(monkeypatch, tmp_path)
    (workflows / "sampler.json").write_text(json.dumps({"1": {"class_type": "KSampler"}}), encoding="utf-8")
    assert "Ctrl+Z" in run(S.load_workspace("sampler"))["note"]


def test_unregistered_types_send_the_caller_to_the_import_log(monkeypatch, tmp_path):
    _, workflows = loading(monkeypatch, tmp_path, missing_node_types=["SomePack_Thing"])
    (workflows / "sampler.json").write_text(json.dumps({"1": {"class_type": "KSampler"}}), encoding="utf-8")
    result = run(S.load_workspace("sampler"))
    assert result["missing_node_types"] == ["SomePack_Thing"]
    assert "on the canvas but will not run" in result["note"]
    assert "get_comfy_log" in result["note"]


def test_nodes_an_api_load_dropped_are_said_to_be_gone_rather_than_broken(monkeypatch, tmp_path):
    # Measured: 28 nodes in the file, 24 on the canvas. loadApiJson creates nothing
    # for a type it does not know, so those four are not there to be fixed - which
    # is a different next step from a red placeholder that merely will not run.
    _, workflows = loading(
        monkeypatch, tmp_path, missing_node_types=["Combine Tiles"], dropped_nodes=4
    )
    (workflows / "sampler.json").write_text(json.dumps({"1": {"class_type": "KSampler"}}), encoding="utf-8")
    note = run(S.load_workspace("sampler"))["note"]
    assert "4 nodes are not on the canvas at all" in note
    assert "will not run" not in note


def test_a_name_that_is_in_neither_directory_never_reaches_the_tab(monkeypatch, tmp_path):
    seen: list[dict[str, Any]] = []
    loading(monkeypatch, tmp_path, seen)
    with pytest.raises(WorkflowError, match="no saved graph"):
        run(S.load_workspace("nope"))
    assert seen == []


PIXELS = b"\x89PNG\r\n\x1a\n not really a png, but nothing here decodes it"


def shooting(
    monkeypatch: pytest.MonkeyPatch,
    seen: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> None:
    """A tab answering `screenshot` with a picture and the usual report."""
    payload: dict[str, Any] = {
        "image": base64.b64encode(PIXELS).decode(),
        "mime": "image/png",
        "width": 1400,
        "height": 800,
        "fit": "graph",
        "framed_items": 12,
        "in_subgraph": False,
        "breadcrumb": "root",
        "node_count": 12,
        **overrides,
    }

    def capture(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return json_response(200, {"client_id": "tab-1", "result": payload})

    as_workspace(monkeypatch, routed(**ALIVE, **ONE_TAB, call=capture))


def test_a_shot_comes_back_as_a_picture_and_a_report(monkeypatch):
    shooting(monkeypatch)
    image, report = run(S.screenshot_workspace())
    assert isinstance(image, S.Image)
    assert image.data == PIXELS
    assert image._mime_type == "image/png"
    assert report["framed_items"] == 12
    assert report["size_kb"] == round(len(PIXELS) / 1024, 1)


def test_the_picture_and_the_report_arrive_as_two_content_blocks(monkeypatch):
    # This is what `structured_output=False` on the tool buys. Left to auto-detect,
    # the list return would be turned into an output schema and the Image
    # serialised into it - which is not a thing an Image can survive.
    shooting(monkeypatch)
    result = run(S.mcp.call_tool("screenshot_workspace", {}))
    assert [block.type for block in result.content] == ["image", "text"]
    assert result.content[0].mime_type == "image/png"
    assert result.structured_content is None
    assert "framed_items" in result.content[1].text


def test_the_base64_is_not_repeated_in_the_report(monkeypatch):
    # It travels once, as the image. Left in the report as well it would arrive a
    # second time as text - the same payload, useless, and the larger half of it.
    shooting(monkeypatch)
    _, report = run(S.screenshot_workspace())
    assert "image" not in report


def test_the_format_comes_from_what_the_browser_actually_encoded(monkeypatch):
    # A canvas asked for a type it cannot encode falls back to PNG without saying
    # so, which is why the tab reads the type back out of the data URL it got.
    shooting(monkeypatch, mime="image/webp")
    image, _ = run(S.screenshot_workspace(format="webp"))
    assert image._mime_type == "image/webp"


def test_node_ids_reach_the_tab_as_strings(monkeypatch):
    seen: list[dict[str, Any]] = []
    shooting(monkeypatch, seen)
    run(S.screenshot_workspace(fit=[86, "93"]))
    assert seen[0]["params"] == {"fit": ["86", "93"], "max_edge": S.CFG.screenshot_max_edge, "format": "png"}


@pytest.mark.parametrize("fit", ["graph", "view", "selected"])
def test_the_named_framings_are_passed_through(monkeypatch, fit):
    seen: list[dict[str, Any]] = []
    shooting(monkeypatch, seen)
    run(S.screenshot_workspace(fit=fit))
    assert seen[0]["params"]["fit"] == fit


def test_a_framing_that_is_neither_a_word_nor_a_list_is_refused(monkeypatch):
    shooting(monkeypatch)
    with pytest.raises(ComfyError, match="graph, view, selected"):
        run(S.screenshot_workspace(fit="everything"))


def test_an_unencodable_format_is_refused_here_rather_than_silently_downgraded(monkeypatch):
    shooting(monkeypatch)
    with pytest.raises(ComfyError, match="png, jpeg, webp"):
        run(S.screenshot_workspace(format="tiff"))


@pytest.mark.parametrize("edge", [64, 8000])
def test_an_edge_outside_the_range_is_refused(monkeypatch, edge):
    shooting(monkeypatch)
    with pytest.raises(ComfyError, match="between 256 and 4096"):
        run(S.screenshot_workspace(max_edge=edge))


def test_dom_widgets_are_reported_as_a_note_rather_than_left_to_be_noticed(monkeypatch):
    # The one way this tool misleads: a prompt box is HTML over the canvas, so it
    # photographs empty whatever it holds.
    shooting(monkeypatch, dom_widgets=7)
    _, report = run(S.screenshot_workspace())
    assert "7 widgets" in report["note"]
    assert "get_workspace_graph" in report["note"]


def test_a_clean_graph_gets_no_note_at_all(monkeypatch):
    shooting(monkeypatch)
    _, report = run(S.screenshot_workspace())
    assert "note" not in report


def test_a_shot_of_the_viewport_says_that_it_may_not_be_all_of_it(monkeypatch):
    shooting(monkeypatch, fit="view", framed_items=None)
    _, report = run(S.screenshot_workspace(fit="view"))
    assert "off screen" in report["note"]


def test_an_answer_with_no_picture_in_it_is_an_error(monkeypatch):
    shooting(monkeypatch, image="")
    with pytest.raises(ComfyError, match="without a picture"):
        run(S.screenshot_workspace())


def test_something_that_is_not_base64_is_named_rather_than_returned(monkeypatch):
    shooting(monkeypatch, image="not base64 at all!!")
    with pytest.raises(ComfyError, match="not an image"):
        run(S.screenshot_workspace())


def selection_call(monkeypatch: pytest.MonkeyPatch, seen: list[dict[str, Any]]) -> None:
    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return json_response(200, {"client_id": "tab-1", "result": {"selected": {"nodes": [], "groups": []}}})

    as_workspace(monkeypatch, routed(**ALIVE, **ONE_TAB, call=capture))


def test_selecting_nothing_asks_for_an_empty_selection_rather_than_null(monkeypatch):
    # `for (const id of null)` throws, and a default parameter does not rescue a key
    # that is present and null - so the empty list has to be built on this side.
    seen: list[dict[str, Any]] = []
    selection_call(monkeypatch, seen)
    run(S.set_workspace_selection())
    assert seen[0]["method"] == "set_selection"
    assert seen[0]["params"] == {"nodes": [], "groups": [], "add": False, "centre": False}


def test_ids_reach_the_tab_as_strings(monkeypatch):
    # Node ids are numbers in litegraph and strings everywhere else here; the tab
    # compares them as strings, so send what it compares.
    seen: list[dict[str, Any]] = []
    selection_call(monkeypatch, seen)
    run(S.set_workspace_selection(nodes=[37, "158"], groups=["Sampling"], add=True, centre=True))
    assert seen[0]["params"] == {
        "nodes": ["37", "158"],
        "groups": ["Sampling"],
        "add": True,
        "centre": True,
    }


def test_the_selection_goes_to_the_named_tab(monkeypatch):
    seen: list[dict[str, Any]] = []
    selection_call(monkeypatch, seen)
    result = run(S.set_workspace_selection(nodes=["37"], client_id="tab-1"))
    assert seen[0]["client_id"] == "tab-1"
    assert result["client_id"] == "tab-1"
    assert result["selected"] == {"nodes": [], "groups": []}


def test_the_tab_is_asked_to_queue_rather_than_the_graph_being_submitted(monkeypatch):
    """The whole point: a job submitted from here would not be the tab's own."""
    seen: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return error_response(502, "workspace_error", "stop here")

    def refuse_to_submit(request: httpx.Request) -> httpx.Response:
        raise AssertionError("run_workspace posted a graph to /prompt itself")

    as_workspace(monkeypatch, routed(**ALIVE, **ONE_TAB, call=capture, mirror=MIRROR_OK, prompt=refuse_to_submit))
    with pytest.raises(WorkspaceError):
        run(S.run_workspace())
    assert seen[0]["method"] == "queue_prompt"
    assert seen[0]["client_id"] == "tab-1"


def test_the_mirror_is_registered_before_the_tab_queues_anything(monkeypatch):
    """Once the job is running its events already have a destination."""
    order: list[str] = []

    def note(name: str, response: httpx.Response):
        def handler(request: httpx.Request) -> httpx.Response:
            order.append(name)
            return response

        return handler

    as_workspace(
        monkeypatch,
        routed(
            **ALIVE,
            **ONE_TAB,
            mirror=note("mirror", json_response(200, {"ok": True})),
            call=note("call", error_response(502, "workspace_error", "stop here")),
        ),
    )
    with pytest.raises(WorkspaceError):
        run(S.run_workspace())
    assert order == ["mirror", "call"]


def test_the_mirror_names_this_server_as_the_target(monkeypatch):
    seen: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return json_response(200, {"ok": True})

    as_workspace(
        monkeypatch,
        routed(**ALIVE, **ONE_TAB, mirror=capture, call=lambda r: error_response(502, "workspace_error", "x")),
    )
    with pytest.raises(WorkspaceError):
        run(S.run_workspace())
    assert seen["source"] == "tab-1"
    assert seen["target"] == S.CLIENT.client_id
    assert seen["ttl_s"] > 0


def test_a_comfyui_that_cannot_mirror_still_runs(monkeypatch):
    """No progress is a degradation; refusing the run over it would be a regression."""
    as_workspace(
        monkeypatch,
        routed(
            **ALIVE,
            **ONE_TAB,
            mirror=lambda r: error_response(501, "mirror_unavailable", "no send_sync to wrap"),
            call=lambda r: error_response(502, "workspace_error", "reached the tab anyway"),
        ),
    )
    with pytest.raises(WorkspaceError, match="reached the tab anyway"):
        run(S.run_workspace())


def test_run_workspace_needs_a_tab_like_the_rest_of_them(monkeypatch):
    empty = routed(
        **ALIVE,
        clients=lambda r: json_response(200, {"protocol": PROTOCOL, "preferred": None, "clients": []}),
    )
    as_workspace(monkeypatch, empty)
    with pytest.raises(WorkspaceUnavailable, match="no canvas to run"):
        run(S.run_workspace())


def test_a_tab_on_older_javascript_is_told_to_reload(monkeypatch):
    """A page keeps the JS it loaded, so this is how an update is normally met."""
    stale = routed(
        **ALIVE,
        clients=lambda r: json_response(
            200,
            {
                "protocol": PROTOCOL,
                "preferred": "tab-1",
                "clients": [{"client_id": "tab-1", "methods": ["ping", "get_graph"]}],
            },
        ),
    )
    as_workspace(monkeypatch, stale)
    with pytest.raises(WorkspaceUnavailable, match="Ctrl\\+Shift\\+R"):
        run(S.run_workspace())


def test_a_tab_that_reports_no_methods_is_given_the_benefit_of_the_doubt():
    """Only an explicit list proves absence; an empty one is just an old node."""
    quiet = routed(
        **ALIVE,
        clients=lambda r: json_response(
            200, {"protocol": PROTOCOL, "preferred": "tab-1", "clients": [{"client_id": "tab-1"}]}
        ),
        mirror=MIRROR_OK,
        call=lambda r: error_response(502, "workspace_error", "got as far as the tab"),
    )
    with pytest.MonkeyPatch.context() as mp:
        as_workspace(mp, quiet)
        with pytest.raises(WorkspaceError, match="got as far as the tab"):
            run(S.run_workspace())


def test_run_workspace_says_comfyui_is_down_rather_than_blaming_the_tab(monkeypatch):
    as_workspace(monkeypatch, routed(system_stats=lambda r: httpx.Response(500)))
    with pytest.raises(ComfyError, match="comfy_start"):
        run(S.run_workspace())


@pytest.mark.parametrize(
    "messages, expected",
    [
        ([["execution_start", {}], ["execution_cached", {"nodes": ["1", "2", "3"]}]], 3),
        ([["execution_start", {}], ["execution_success", {}]], 0),
        ([["execution_cached", {}]], 0),  # the key ComfyUI always sends, empty
        ([], 0),
    ],
)
def test_a_run_that_recomputed_nothing_can_say_so(messages, expected):
    """Two run_workspace calls over an unchanged canvas are a guaranteed cache hit."""
    assert S._cached_node_count({"status": {"messages": messages}}) == expected


def test_a_history_with_no_status_block_reports_nothing_cached():
    assert S._cached_node_count({}) == 0


def test_a_dead_server_is_unavailable_rather_than_a_workspace_error():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(WorkspaceUnavailable, match="not answering"):
        run(bridge(refuse).call("ping"))
