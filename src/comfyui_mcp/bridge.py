"""Client for the workspace bridge - the half of ComfyUI that needs a browser.

Everything else in this package talks to ComfyUI's own HTTP API, which knows about
files, models and queued jobs but nothing at all about the workflow a user has open.
That one lives in the page. `comfy_node/comfyui_mcp_bridge` adds the routes that
reach it; this module calls them.

The bridge is optional by design. It can be absent three different ways and a
caller can do something about each, so they are three different errors rather than
one "it did not work":

- the node is not installed (`bridge_missing`) - a one-time setup step;
- no tab is connected (`no_workspace`) - open ComfyUI and reload;
- the tab is there but the call failed (`workspace_error`) - a real fault.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .client import ComfyClient, ComfyError
from .config import Config

log = logging.getLogger(__name__)

PREFIX = "/mcp_bridge"
TOKEN_HEADER = "X-MCP-Bridge-Token"

PROTOCOL = 1


class WorkspaceUnavailable(ComfyError):
    """No browser workspace to talk to - not installed, or no tab open."""


class WorkspaceError(ComfyError):
    """The tab answered, and the answer was a failure."""


class BridgeClient:
    """Calls methods inside the ComfyUI page over the node's HTTP routes."""

    def __init__(self, cfg: Config, client: ComfyClient) -> None:
        self.cfg = cfg
        self._comfy = client

    @property
    def node_source(self) -> str:
        from .config import PROJECT_ROOT

        return str(PROJECT_ROOT / "comfy_node" / "comfyui_mcp_bridge")

    @property
    def node_target(self) -> str:
        return str(self.cfg.comfy_dir / "custom_nodes" / "comfyui_mcp_bridge")

    def _not_installed(self) -> WorkspaceUnavailable:
        return WorkspaceUnavailable(
            "The ComfyUI bridge node is not installed, so the workspace cannot be "
            f"reached. Install it by linking or copying\n  {self.node_source}\ninto\n"
            f"  {self.node_target}\nand restarting ComfyUI. Tools that only use the "
            "HTTP API keep working without it."
        )

    def _headers(self) -> dict[str, str]:
        return {TOKEN_HEADER: self.cfg.bridge_token} if self.cfg.bridge_token else {}

    async def clients(self) -> dict[str, Any]:
        """Which tabs are reachable. Raises if the node is not installed."""
        http = await self._comfy.http()
        try:
            resp = await http.get(f"{PREFIX}/clients", timeout=self.cfg.request_timeout)
        except httpx.HTTPError as exc:
            raise WorkspaceUnavailable(f"ComfyUI is not answering on {self.cfg.base_url}: {exc}") from exc
        if resp.status_code == 404:
            raise self._not_installed()
        resp.raise_for_status()
        return resp.json()

    async def probe(self) -> dict[str, Any]:
        """Report the bridge's state without raising - for status tools.

        Deliberately total: every failure becomes a described state, because a
        status tool that raises tells the caller less than one that says which of
        the three things is wrong.
        """
        if not await self._comfy.is_alive():
            return {
                "available": False,
                "reason": "comfyui_down",
                "hint": f"ComfyUI is not answering on {self.cfg.base_url}; call comfy_start.",
            }
        try:
            payload = await self.clients()
        except WorkspaceUnavailable as exc:
            return {"available": False, "reason": "bridge_missing", "hint": str(exc)}
        except (httpx.HTTPError, ValueError) as exc:
            return {"available": False, "reason": "unreachable", "hint": str(exc)}

        found = payload.get("clients") or []
        state: dict[str, Any] = {
            "available": bool(found),
            "node_installed": True,
            "protocol": payload.get("protocol"),
            "preferred_client": payload.get("preferred"),
            "clients": found,
        }
        if payload.get("protocol") != PROTOCOL:
            state["protocol_warning"] = (
                f"the bridge node speaks protocol {payload.get('protocol')}, this server "
                f"speaks {PROTOCOL}; reinstall the node from {self.node_source}"
            )
        if not found:
            state["reason"] = "no_workspace"
            state["hint"] = (
                f"The node is installed but no browser tab is connected. Call open_workspace, "
                f"or open {self.cfg.base_url} in a browser and reload the page."
            )
        return state

    async def restart_comfy(self) -> dict[str, Any]:
        """Ask the node to re-exec ComfyUI, and report the command line it will use.

        A plain route rather than a browser call: it needs no tab, only the node.
        The answer comes back before the exec - see the route's own reasoning -
        so a normal 200 here means the restart is under way, not that it finished.
        """
        http = await self._comfy.http()
        try:
            resp = await http.post(
                f"{PREFIX}/restart", json={}, headers=self._headers(), timeout=self.cfg.request_timeout
            )
        except httpx.HTTPError as exc:
            raise WorkspaceUnavailable(f"ComfyUI is not answering on {self.cfg.base_url}: {exc}") from exc
        if resp.status_code == 404:
            raise self._not_installed()
        if resp.status_code != 200:
            error = _error_of(resp)
            raise WorkspaceError(error.get("message") or f"HTTP {resp.status_code}")
        return resp.json()

    async def mirror(self, source: str, target: str | None, ttl_s: float | None = None) -> dict[str, Any]:
        """Ask the node to copy `source`'s event stream to `target`.

        Needed because a run belongs to one socket: the tab has to queue the job for
        its own canvas to react, which leaves this server with nothing to report
        progress from. The copy is what gives both halves the same stream.

        Never fatal. A ComfyUI whose `send_sync` cannot be wrapped still runs the
        graph; it just cannot say how far along it is, and that is worth degrading
        to rather than refusing the run.
        """
        body: dict[str, Any] = {"source": source, "target": target}
        if ttl_s is not None:
            body["ttl_s"] = ttl_s

        http = await self._comfy.http()
        try:
            resp = await http.post(
                f"{PREFIX}/mirror", json=body, headers=self._headers(), timeout=self.cfg.request_timeout
            )
        except httpx.HTTPError as exc:
            return {"ok": False, "reason": str(exc)}
        if resp.status_code == 200:
            return resp.json()
        error = _error_of(resp)
        return {"ok": False, "reason": error.get("message") or f"HTTP {resp.status_code}"}

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        client_id: str = "",
    ) -> dict[str, Any]:
        """Run one method in the browser.

        Returns the node's envelope - `{"client_id": ..., "result": ...}` - rather
        than the bare result, because with several tabs open *which* one answered
        is part of the answer, and will matter more once calls start mutating.
        """
        wait = timeout if timeout is not None else self.cfg.bridge_timeout
        body: dict[str, Any] = {"method": method, "params": params or {}, "timeout": wait}
        if client_id:
            body["client_id"] = client_id

        http = await self._comfy.http()
        try:
            resp = await http.post(
                f"{PREFIX}/call", json=body, headers=self._headers(), timeout=wait + 15
            )
        except httpx.HTTPError as exc:
            raise WorkspaceUnavailable(f"ComfyUI is not answering on {self.cfg.base_url}: {exc}") from exc

        if resp.status_code == 404:
            raise self._not_installed()
        if resp.status_code == 200:
            return resp.json()

        error = _error_of(resp)
        code = error.get("code", "")
        message = error.get("message") or resp.text[:500]

        if code == "no_workspace":
            raise WorkspaceUnavailable(
                f"No ComfyUI tab is connected, so '{method}' has nothing to read. "
                f"Call open_workspace, or open {self.cfg.base_url} in a browser and reload "
                "the page. Tools that only use the HTTP API work without it."
            )
        if code == "unknown_client":
            raise WorkspaceUnavailable(f"{message}. Call workspace_status for the tabs that are.")
        if code == "unauthorized":
            raise WorkspaceUnavailable(
                f"{message}. Set COMFYUI_BRIDGE_TOKEN to the value COMFYUI_MCP_BRIDGE_TOKEN "
                "has in ComfyUI's environment."
            )
        detail = error.get("detail")
        raise WorkspaceError("\n".join(part for part in (message, detail) if part))


def _error_of(resp: httpx.Response) -> dict[str, Any]:
    """The node's error envelope, or an empty one when the body is not ours."""
    try:
        payload = resp.json()
    except ValueError:
        return {}
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else {}
