"""Request/reply bridge between the MCP server and an open ComfyUI browser tab.

ComfyUI's HTTP API reaches the backend and stops there. The workflow a user is
actually editing does not live in the backend: it lives in litegraph, in the page,
and the only copy of its unsaved state is in that tab's memory. Nothing outside the
browser can read it, let alone change it.

This module borrows the WebSocket ComfyUI already keeps open to every client and
turns it into a request/reply channel:

    POST /mcp_bridge/call  ->  send_sync("mcp_bridge.call", ..., sid)  ->  the tab
    the tab  ->  POST /mcp_bridge/reply  ->  the pending future resolves  ->  response

Three things are deliberate.

*The tab is authoritative and optional.* With no tab registered every call fails
with `no_workspace` rather than falling back to the workflow file on disk. A stale
answer about a live canvas is worse than no answer, because the caller cannot tell
the difference.

*Liveness is not tracked here.* `PromptServer.sockets` already drops a socket when
its tab closes, so membership in it is the truth and no heartbeat of our own can
be more current than that. The registry below only records which of those sockets
proved they are running our JavaScript.

*Replies are correlated by id, not by order.* A tab may answer a cheap call while a
slow one is still running, and a call that timed out may still answer afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from typing import Any

from aiohttp import web
from server import PromptServer

log = logging.getLogger("mcp_bridge")

PREFIX = "/mcp_bridge"
CALL_EVENT = "mcp_bridge.call"

PROTOCOL = 1

DEFAULT_TIMEOUT = 20.0
MAX_TIMEOUT = 300.0

TOKEN = os.environ.get("COMFYUI_MCP_BRIDGE_TOKEN", "")
TOKEN_HEADER = "X-MCP-Bridge-Token"

routes = PromptServer.instance.routes


class Clients:
    """Sockets that have announced themselves as running the bridge extension."""

    def __init__(self) -> None:
        self._seen: dict[str, dict[str, Any]] = {}

    def announce(self, sid: str, info: dict[str, Any]) -> dict[str, Any]:
        entry = self._seen.setdefault(sid, {"first_seen": time.time()})
        entry.update({k: v for k, v in info.items() if v is not None})
        entry["last_seen"] = time.time()
        if info.get("focused"):
            entry["focused_at"] = time.time()
        return entry

    def live(self) -> dict[str, dict[str, Any]]:
        """Announced clients whose WebSocket is still open, dead ones pruned."""
        sockets = PromptServer.instance.sockets
        for sid in [sid for sid in self._seen if sid not in sockets]:
            self._seen.pop(sid, None)
        return dict(self._seen)

    def pick(self, sid: str | None = None) -> str | None:
        """Which tab a call goes to.

        With several tabs open there is no single "the workspace", so the most
        recently focused one wins - that is the one whose canvas the user is
        looking at. An explicit sid overrides the guess and is an error if dead,
        because silently retargeting a call at another tab would edit a workflow
        the caller never asked about.
        """
        live = self.live()
        if sid:
            if sid not in live:
                raise KeyError(sid)
            return sid
        if not live:
            return None
        return max(live, key=lambda s: (live[s].get("focused_at", 0.0), live[s]["last_seen"]))


CLIENTS = Clients()

PENDING: dict[str, asyncio.Future] = {}

MIRRORS: dict[str, tuple[str, float]] = {}

MIRROR_MAX_S = 3600.0

_ORIGINAL_SEND_SYNC = None


def _install_mirror() -> bool:
    """Copy one client's event stream to a second socket.

    ComfyUI addresses every execution message to whoever submitted the job -
    `send_sync(..., server.client_id)` throughout execution.py, and
    comfy_execution/progress.py spells out why: "Include client_id to ensure
    message is only sent to the initiating client". Exactly one socket can watch a
    run.

    That is a problem only because both halves need to. The tab has to own the run
    or its canvas reports someone else's job; the MCP server has to see the events
    or it cannot answer "is it still working". So the tab queues, and this hands
    the MCP server a copy.

    `send_sync` is three lines and takes (event, data, sid), which is what makes
    wrapping it the cheap option - the mirror never has to know what an event
    means. Failure is silent and total: no wrapper, no mirror, everything else
    keeps working.
    """
    global _ORIGINAL_SEND_SYNC
    if _ORIGINAL_SEND_SYNC is not None:
        return True

    server = PromptServer.instance
    original = getattr(server, "send_sync", None)
    if not callable(original):
        log.warning("mcp_bridge: no PromptServer.send_sync to wrap; progress mirroring is off")
        return False

    def send_sync(event, data, sid=None):
        original(event, data, sid)
        if not sid or not isinstance(event, str):
            return
        entry = MIRRORS.get(sid)
        if entry is None:
            return
        target, expires = entry
        if time.time() > expires:
            MIRRORS.pop(sid, None)
        elif target != sid:
            original(event, data, target)

    server.send_sync = send_sync
    _ORIGINAL_SEND_SYNC = original
    log.info("mcp_bridge: progress mirroring installed")
    return True


def _error(status: int, code: str, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message, **extra}}, status=status)


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any malformed body is the same answer
        return {}
    return payload if isinstance(payload, dict) else {}


@routes.post(f"{PREFIX}/hello")
async def hello(request: web.Request) -> web.Response:
    """A tab announcing that it is running the extension.

    Sent on load, on reconnect and on window focus. `client_id` is the sid ComfyUI
    handed the page, so it is checked against the live sockets: an id that is not
    there means the caller is not the tab it claims to be.
    """
    body = await _body(request)
    sid = body.get("client_id")
    if not isinstance(sid, str) or not sid:
        return _error(400, "bad_request", "client_id is required")
    if sid not in PromptServer.instance.sockets:
        return _error(409, "unknown_client", f"no open WebSocket for client_id {sid}")

    entry = CLIENTS.announce(
        sid,
        {
            "protocol": body.get("protocol"),
            "frontend": body.get("frontend"),
            "methods": body.get("methods"),
            "focused": bool(body.get("focused")),
        },
    )
    log.debug("mcp_bridge: client %s announced (%s)", sid, entry.get("frontend"))
    return web.json_response({"ok": True, "protocol": PROTOCOL, "client_id": sid})


@routes.get(f"{PREFIX}/clients")
async def clients(request: web.Request) -> web.Response:
    """Which tabs are reachable right now. Answering at all proves the node is installed."""
    live = CLIENTS.live()
    preferred = CLIENTS.pick()
    return web.json_response(
        {
            "protocol": PROTOCOL,
            "preferred": preferred,
            "clients": [
                {
                    "client_id": sid,
                    "frontend": entry.get("frontend"),
                    "methods": entry.get("methods") or [],
                    "focused": sid == preferred,
                    "idle_s": round(time.time() - entry["last_seen"], 1),
                }
                for sid, entry in sorted(live.items(), key=lambda kv: -kv[1]["last_seen"])
            ],
        }
    )


@routes.post(f"{PREFIX}/call")
async def call(request: web.Request) -> web.Response:
    """Run one method in the browser and return what it produced."""
    if TOKEN and request.headers.get(TOKEN_HEADER) != TOKEN:
        return _error(401, "unauthorized", f"{TOKEN_HEADER} missing or wrong")

    body = await _body(request)
    method = body.get("method")
    if not isinstance(method, str) or not method:
        return _error(400, "bad_request", "method is required")

    try:
        timeout = min(float(body.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
    except (TypeError, ValueError):
        return _error(400, "bad_request", "timeout must be a number")

    try:
        sid = CLIENTS.pick(body.get("client_id"))
    except KeyError:
        return _error(409, "unknown_client", f"client_id {body['client_id']} is not connected")
    if sid is None:
        return _error(
            409,
            "no_workspace",
            "No ComfyUI tab is connected to the bridge. Open ComfyUI in a browser "
            "and reload the page, then try again.",
        )

    req_id = uuid.uuid4().hex
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    PENDING[req_id] = future
    try:
        PromptServer.instance.send_sync(
            CALL_EVENT,
            {"id": req_id, "method": method, "params": body.get("params") or {}},
            sid,
        )
        reply = await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
        return _error(
            504,
            "timeout",
            f"the workspace did not answer '{method}' within {timeout:g}s; "
            "the tab may be busy, hidden or on an older version of the extension",
            client_id=sid,
        )
    finally:
        PENDING.pop(req_id, None)

    if isinstance(reply.get("error"), dict):
        error = reply["error"]
        return _error(
            502,
            "workspace_error",
            f"{method} failed in the browser: {error.get('message', 'unknown error')}",
            client_id=sid,
            detail=error.get("detail"),
        )
    return web.json_response({"client_id": sid, "result": reply.get("result")})


@routes.post(f"{PREFIX}/mirror")
async def mirror(request: web.Request) -> web.Response:
    """Start copying `source`'s events to `target`, or stop with no target.

    Registered before a run rather than torn down after it: the caller that would
    do the tearing down is the one thing that might not survive the run, and a
    mirror that outlives its run is harmless where a missing one is not. `ttl_s`
    bounds it either way, and re-registering refreshes it.
    """
    if TOKEN and request.headers.get(TOKEN_HEADER) != TOKEN:
        return _error(401, "unauthorized", f"{TOKEN_HEADER} missing or wrong")

    body = await _body(request)
    source = body.get("source")
    target = body.get("target")
    if not isinstance(source, str) or not source:
        return _error(400, "bad_request", "source is required")

    if not target:
        MIRRORS.pop(source, None)
        return web.json_response({"ok": True, "source": source, "target": None, "active": len(MIRRORS)})

    if not isinstance(target, str):
        return _error(400, "bad_request", "target must be a client id or null")
    if not _install_mirror():
        return _error(
            501,
            "mirror_unavailable",
            "this ComfyUI has no wrappable PromptServer.send_sync, so events cannot be "
            "copied; the run will still work but progress will not be reported",
        )
    try:
        ttl = min(float(body.get("ttl_s") or MIRROR_MAX_S), MIRROR_MAX_S)
    except (TypeError, ValueError):
        return _error(400, "bad_request", "ttl_s must be a number")

    MIRRORS[source] = (target, time.time() + ttl)
    return web.json_response(
        {"ok": True, "source": source, "target": target, "ttl_s": ttl, "active": len(MIRRORS)}
    )


RESTART_DELAY_S = 0.4


def restart_argv() -> list[str]:
    """The command line to come back as.

    Re-exec rather than relaunch, because only the running process knows how it
    was started. The launch script is one guess at that and is routinely wrong -
    flags typed by hand, a different .bat, a venv activated first - and a restart
    that quietly changes the command line is worse than no restart at all. The
    environment rides along too, which matters on this install: the launcher
    calls `vcvars64.bat` before ComfyUI, and nothing here could reproduce that.
    """
    argv = list(sys.argv)
    if "--windows-standalone-build" in argv:
        argv.remove("--windows-standalone-build")

    if argv and argv[0].endswith("__main__.py"):  
        module = os.path.basename(os.path.dirname(argv[0]))
        return [sys.executable, "-m", module, *argv[1:]]
    if sys.platform == "win32":
        return [f'"{sys.executable}"', f'"{argv[0]}"', *argv[1:]] if argv else [f'"{sys.executable}"']
    return [sys.executable, *argv]


def _exec_self(argv: list[str]) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - a closed stream must not stop the restart
            pass
    log.info("mcp_bridge: restarting ComfyUI as %s", argv)
    os.execv(sys.executable, argv)


@routes.post(f"{PREFIX}/restart")
async def restart(request: web.Request) -> web.Response:
    """Replace this ComfyUI process with a fresh one.

    Installing a custom node, or changing one, needs a restart: `nodes.py` imports
    every pack once at startup and nothing re-reads them. Doing it from inside is
    what makes it work for a ComfyUI nobody here launched - which is the usual
    case, and the one `comfy_stop` deliberately refuses to touch.

    The reply is sent first and the exec runs on a timer, because the caller has
    to learn the restart began: after `os.execv` there is no process left to
    answer with, and a caller that saw the connection drop cannot tell a restart
    from a crash.
    """
    if TOKEN and request.headers.get(TOKEN_HEADER) != TOKEN:
        return _error(401, "unauthorized", f"{TOKEN_HEADER} missing or wrong")

    argv = restart_argv()
    asyncio.get_running_loop().call_later(RESTART_DELAY_S, _exec_self, argv)
    return web.json_response(
        {"ok": True, "pid": os.getpid(), "in_s": RESTART_DELAY_S, "argv": argv}
    )


@routes.post(f"{PREFIX}/reply")
async def reply(request: web.Request) -> web.Response:
    """A tab answering a call.

    An unknown id is not an error the tab can act on - it means the caller already
    gave up - so it answers 200 and says so instead of making the page log a failure.
    """
    body = await _body(request)
    future = PENDING.get(str(body.get("id") or ""))
    if future is None:
        return web.json_response({"ok": False, "reason": "unknown_or_expired_id"})
    if not future.done():
        future.set_result(body)
    return web.json_response({"ok": True})


log.info("mcp_bridge: routes registered under %s (protocol %d)", PREFIX, PROTOCOL)
