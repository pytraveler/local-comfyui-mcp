r"""Prove the bridge transport works, without a browser.

The bridge has two halves that fail in different places: the transport (routes,
WebSocket push, reply correlation) and the handlers that run inside the page. Only
the second needs a real ComfyUI tab. This impersonates the first - a WebSocket that
registers itself and answers `mcp_bridge.call` - so the transport can be checked on
its own, and so a transport bug is never mistaken for a JavaScript one.

    .\uv.exe run python scripts\check_bridge.py            # full round trip
    .\uv.exe run python scripts\check_bridge.py --tab-only # stay connected as a tab

`--tab-only` leaves a fake tab registered so the real MCP tools can be pointed at
it: workspace_status will list it, and get_workspace_graph will return the canned
graph below. Handy for exercising the MCP side when no browser is around.

Needs a running ComfyUI with the node installed (see install_node.ps1).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

import httpx
import websockets

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from comfyui_mcp.bridge import PROTOCOL  # noqa: E402
from comfyui_mcp.config import load_config  # noqa: E402

CFG = load_config()

CANNED_GRAPH = {
    "format": "summary",
    "scope": "root",
    "node_count": 1,
    "link_count": 0,
    "nodes": [{"id": "1", "type": "FakeNode", "title": "fake tab", "mode": "always"}],
    "groups": [],
    "issues": [],
}


def say(ok: bool, text: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {text}")


def skip(text: str) -> None:
    print(f"  skip  {text}")


async def fake_tab(ready: asyncio.Event, stop: asyncio.Event, client_id: str) -> None:
    """A WebSocket that registers as a bridge client and answers calls."""
    url = f"{CFG.ws_url}?clientId={client_id}"
    async with websockets.connect(url, max_size=None) as ws:
        async with httpx.AsyncClient(base_url=CFG.base_url, timeout=10) as http:
            resp = await http.post(
                "/mcp_bridge/hello",
                json={
                    "client_id": client_id,
                    "protocol": PROTOCOL,
                    "focused": True,
                    "frontend": "fake-tab",
                    "methods": ["ping", "get_graph"],
                },
            )
            resp.raise_for_status()
            ready.set()

            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if isinstance(raw, (bytes, bytearray)):
                    continue
                message = json.loads(raw)
                if message.get("type") != "mcp_bridge.call":
                    continue

                call = message["data"]
                method, params = call.get("method"), call.get("params") or {}
                if method == "ping":
                    body = {"id": call["id"], "result": {"pong": True, "frontend": "fake-tab"}}
                elif method == "get_graph":
                    body = {"id": call["id"], "result": {**CANNED_GRAPH, "scope": params.get("scope", "root")}}
                elif method == "boom":  
                    body = {"id": call["id"], "error": {"message": "boom", "detail": "at fakeTab"}}
                else:
                    body = {"id": call["id"], "error": {"message": f"unknown method '{method}'"}}
                await http.post("/mcp_bridge/reply", json=body)


async def call(http: httpx.AsyncClient, method: str, **params: Any) -> httpx.Response:
    return await http.post("/mcp_bridge/call", json={"method": method, "params": params, "timeout": 10})


async def check() -> int:
    failures = 0

    async with httpx.AsyncClient(base_url=CFG.base_url, timeout=20) as http:
        print(f"\nComfyUI at {CFG.base_url}")
        try:
            resp = await http.get("/mcp_bridge/clients")
        except httpx.HTTPError as exc:
            say(False, f"ComfyUI is not answering: {exc}")
            return 1
        if resp.status_code == 404:
            say(False, "the bridge node is not installed - run install_node.ps1 and restart ComfyUI")
            return 1
        say(True, f"node installed, protocol {resp.json().get('protocol')}")

        real_tabs = len(resp.json().get("clients") or [])

        print("\nwithout a tab")
        if real_tabs:
            skip(f"{real_tabs} real tab(s) connected; close them to check the refusal path")
        else:
            resp = await call(http, "ping")
            no_tab = resp.status_code == 409 and (resp.json().get("error") or {}).get("code") == "no_workspace"
            say(no_tab, f"call refused with no_workspace (got {resp.status_code})")
            failures += not no_tab

        client_id = f"fake-{uuid.uuid4().hex[:8]}"
        ready, stop = asyncio.Event(), asyncio.Event()
        tab = asyncio.create_task(fake_tab(ready, stop, client_id))
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)

            print("\nwith a tab")
            listed = (await http.get("/mcp_bridge/clients")).json()
            found = listed.get("preferred") == client_id
            say(found, f"the tab is listed and preferred ({listed.get('preferred')})")
            failures += not found

            resp = await call(http, "ping")
            pinged = resp.status_code == 200 and resp.json()["result"]["pong"] is True
            say(pinged, f"ping round trip (got {resp.status_code})")
            failures += not pinged

            resp = await call(http, "get_graph", scope="active")
            payload = resp.json() if resp.status_code == 200 else {}
            routed = payload.get("client_id") == client_id
            say(routed, "the reply names the tab that answered")
            failures += not routed
            passed = (payload.get("result") or {}).get("scope") == "active"
            say(passed, "params reach the handler")
            failures += not passed

            resp = await call(http, "boom")
            failed_well = resp.status_code == 502 and (resp.json().get("error") or {}).get("code") == "workspace_error"
            say(failed_well, f"a throwing handler becomes workspace_error (got {resp.status_code})")
            failures += not failed_well

            resp = await call(http, "no_such_method")
            unknown = resp.status_code == 502
            say(unknown, f"an unknown method is reported (got {resp.status_code})")
            failures += not unknown

            resp = await http.post(
                "/mcp_bridge/mirror", json={"source": client_id, "target": "check-bridge", "ttl_s": 5}
            )
            installed = resp.status_code == 200 and resp.json().get("ok") is True
            say(installed, f"send_sync could be wrapped for progress mirroring (got {resp.status_code})")
            failures += not installed
            await http.post("/mcp_bridge/mirror", json={"source": client_id, "target": None})
        finally:
            stop.set()
            await asyncio.wait_for(tab, timeout=5)

        print("\nafter the tab closes")
        if real_tabs:
            skip("a real tab is still connected, so calls are meant to keep working")
        else:
            for _ in range(20):  
                resp = await call(http, "ping")
                if resp.status_code == 409:
                    break
                await asyncio.sleep(0.25)
            gone = resp.status_code == 409
            say(gone, f"a closed tab is pruned and calls refuse again (got {resp.status_code})")
            failures += not gone

    print(f"\n{'all checks passed' if not failures else f'{failures} check(s) failed'}\n")
    return 1 if failures else 0


async def tab_only() -> int:
    client_id = f"fake-{uuid.uuid4().hex[:8]}"
    ready, stop = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(fake_tab(ready, stop, client_id))
    await asyncio.wait_for(ready.wait(), timeout=10)
    print(f"fake tab registered as {client_id}; Ctrl+C to disconnect")
    try:
        await task
    except KeyboardInterrupt:
        stop.set()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tab-only", action="store_true", help="register as a tab and stay connected")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(tab_only() if args.tab_only else check()))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
