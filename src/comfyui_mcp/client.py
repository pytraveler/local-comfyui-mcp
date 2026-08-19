"""Async client for the ComfyUI HTTP + WebSocket API."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .config import Config

log = logging.getLogger(__name__)


def basic_auth(cfg: Config) -> httpx.BasicAuth | None:
    """Credentials for a ComfyUI behind a reverse proxy, or None for localhost.

    Both halves or nothing at all: a half-filled pair authenticates with neither,
    and `config.auth_problem` is what says so out loud.
    """
    if cfg.http_user and cfg.http_password:
        return httpx.BasicAuth(cfg.http_user, cfg.http_password)
    return None


def auth_headers(cfg: Config) -> dict[str, str]:
    """The same credentials as a header, for the WebSocket.

    httpx builds this itself from `BasicAuth`; websockets does not, so it is built
    here - and from the same pair, because two sources drift and the failure is a
    socket that 401s while every HTTP call succeeds.
    """
    if not (cfg.http_user and cfg.http_password):
        return {}
    raw = f"{cfg.http_user}:{cfg.http_password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


@dataclass(frozen=True)
class RunEvent:
    """One sign of life from a running job.

    Steps only exist while a sampler is running. The long silences are elsewhere -
    loading an 18 GB checkpoint or decoding a latent emits no step counter at all,
    and on a slow machine that is exactly when a caller starts to suspect a hang. So
    node changes are reported too, with `steps == 0` meaning "no counter for this
    one, it is simply working".
    """

    node: str
    step: int = 0
    steps: int = 0

    @property
    def counted(self) -> bool:
        return self.steps > 0


ProgressHook = Callable[[RunEvent], Awaitable[None]]


class ComfyError(RuntimeError):
    """ComfyUI rejected the request or failed while executing it."""


@dataclass
class MediaRef:
    node_id: str
    kind: str  # "images", "gifs", "audio", ...
    filename: str
    subfolder: str
    type: str  # "output" | "temp" | "input"

    def path(self, cfg: Config) -> Path:
        return cfg.media_dir(self.type) / self.subfolder / self.filename

    def to_dict(self, cfg: Config) -> dict[str, Any]:
        return {
            "path": str(self.path(cfg)),
            "filename": self.filename,
            "subfolder": self.subfolder,
            "type": self.type,
            "kind": self.kind,
            "node_id": self.node_id,
        }


class ComfyClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client_id = str(uuid.uuid4())
        self._http: httpx.AsyncClient | None = None

    async def http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self.cfg.base_url,
                timeout=self.cfg.request_timeout,
                auth=basic_auth(self.cfg),
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    # basic endpoints
    async def is_alive(self, timeout: float = 3.0) -> bool:
        try:
            client = await self.http()
            resp = await client.get("/system_stats", timeout=timeout)
            return resp.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    async def why_unreachable(self) -> str:
        """Why ComfyUI did not answer, when the answer is worth a sentence.

        A proxy that rejects the credentials replies 401, and `is_alive` reports
        that as "not running" - which sends somebody off to start a ComfyUI that
        is already up, or to open a firewall that was never shut. One extra
        request tells the two apart. Empty when there is nothing to add.

        Total, like `is_alive`: this is called to explain a failure and must not
        become one.
        """
        try:
            client = await self.http()
            resp = await client.get("/system_stats", timeout=3.0)
        except (httpx.HTTPError, OSError):
            return ""
        if resp.status_code in (401, 407):
            if basic_auth(self.cfg) is None:
                return (
                    f"ComfyUI answered {resp.status_code}: something in front of it is asking "
                    "for credentials. Set COMFYUI_USER and COMFYUI_PASSWORD."
                )
            return (
                f"ComfyUI answered {resp.status_code}: COMFYUI_USER and COMFYUI_PASSWORD were "
                "sent and rejected."
            )
        if resp.status_code != 200:
            return f"ComfyUI answered {resp.status_code} rather than 200."
        return ""

    async def system_stats(self) -> dict[str, Any]:
        client = await self.http()
        resp = await client.get("/system_stats")
        resp.raise_for_status()
        return resp.json()

    async def logs(self) -> dict[str, Any]:
        """ComfyUI's own console ring buffer.

        `/internal/logs/raw` is what the frontend's terminal panel reads: the tail of
        everything the process wrote to stdout and stderr, colour codes and all. It
        needs no browser and no bridge node - only a running ComfyUI. Entries are
        writes rather than lines; `logs.to_lines` is what makes them readable.
        """
        client = await self.http()
        resp = await client.get("/internal/logs/raw")
        if resp.status_code == 404:
            raise ComfyError(
                "this ComfyUI has no /internal/logs/raw route, so its console cannot be "
                "read remotely. The route arrived with the log buffer in app/logger.py; "
                "an install predating it can only be read from the terminal it runs in."
            )
        resp.raise_for_status()
        return resp.json()

    async def object_info(self, class_type: str | None = None) -> dict[str, Any]:
        client = await self.http()
        path = "/object_info" if class_type is None else f"/object_info/{class_type}"
        resp = await client.get(path, timeout=self.cfg.object_info_timeout)
        resp.raise_for_status()
        return resp.json()

    async def model_folders(self) -> list[str]:
        """Model folder names as ComfyUI resolves them, not as they sit on disk."""
        client = await self.http()
        resp = await client.get("/models")
        resp.raise_for_status()
        return resp.json()

    async def model_files(self, folder: str) -> list[str]:
        """Files in a model folder, honouring extra_model_paths.yaml."""
        client = await self.http()
        resp = await client.get(f"/models/{folder}")
        if resp.status_code == 404:
            raise ComfyError(f"ComfyUI has no model folder '{folder}'")
        resp.raise_for_status()
        return resp.json()

    async def model_directories(self) -> dict[str, dict[str, list[str]]]:
        """folder name -> the real directories behind it and the extensions it lists.

        `/models` names the folders; only this says where they are on disk, which
        is what a download needs. The two endpoints that answer disagree in shape
        and in age, so both are tried: `/experiment/models` also reports the
        extensions, and `/internal/folder_paths` is the older one that does not.
        """
        client = await self.http()
        resp = await client.get("/experiment/models")
        if resp.status_code == 200:
            return {
                entry["name"]: {
                    "folders": list(entry.get("folders") or []),
                    "extensions": list(entry.get("extensions") or []),
                }
                for entry in resp.json()
                if entry.get("name")
            }
        resp = await client.get("/internal/folder_paths")
        resp.raise_for_status()
        return {name: {"folders": list(paths), "extensions": []} for name, paths in resp.json().items()}

    async def queue(self) -> dict[str, Any]:
        client = await self.http()
        resp = await client.get("/queue")
        resp.raise_for_status()
        return resp.json()

    async def history(self, prompt_id: str) -> dict[str, Any] | None:
        client = await self.http()
        resp = await client.get(f"/history/{prompt_id}")
        resp.raise_for_status()
        return resp.json().get(prompt_id)

    async def interrupt(self) -> None:
        client = await self.http()
        resp = await client.post("/interrupt")
        resp.raise_for_status()

    async def free(self, unload_models: bool = True, free_memory: bool = True) -> None:
        client = await self.http()
        await client.post("/free", json={"unload_models": unload_models, "free_memory": free_memory})

    async def view(self, ref: MediaRef) -> bytes:
        client = await self.http()
        resp = await client.get(
            "/view",
            params={"filename": ref.filename, "subfolder": ref.subfolder, "type": ref.type},
        )
        resp.raise_for_status()
        return resp.content

    async def upload_image(self, path: Path, subfolder: str = "", overwrite: bool = True) -> dict[str, Any]:
        client = await self.http()
        with path.open("rb") as fh:
            files = {"image": (path.name, fh, "application/octet-stream")}
            data = {"overwrite": "true" if overwrite else "false"}
            if subfolder:
                data["subfolder"] = subfolder
            resp = await client.post("/upload/image", files=files, data=data)
        resp.raise_for_status()
        return resp.json()

    # execution 
    async def submit(
        self, graph: dict[str, Any], on_node_errors: Callable[[list[str]], None] | None = None
    ) -> str:
        """Queue a graph and return its prompt_id.

        ComfyUI validates each output node separately and runs the prompt as long as
        *one* of them is valid, answering 200 with a prompt_id plus `node_errors`
        naming the ones it skipped. Treating that as a failure would be wrong twice
        over: the run is already queued, so raising abandons a job that is going to
        occupy the GPU for minutes with nothing watching it, and a workflow whose
        author left a disconnected PreviewImage behind still produces its real output.
        Only an empty prompt_id - every output rejected - means nothing was queued.
        """
        client = await self.http()
        resp = await client.post("/prompt", json={"prompt": graph, "client_id": self.client_id})
        if resp.status_code >= 400:
            raise ComfyError(_format_prompt_error(resp))
        payload = resp.json()
        prompt_id = payload.get("prompt_id")
        node_errors = payload.get("node_errors") or {}
        if not prompt_id:
            raise ComfyError(
                f"ComfyUI queued nothing: {json.dumps(node_errors, ensure_ascii=False)}"
                if node_errors
                else f"ComfyUI returned no prompt_id: {payload}"
            )
        if node_errors:
            skipped = _format_skipped_outputs(node_errors)
            log.warning("ComfyUI skipped %d output node(s): %s", len(node_errors), "; ".join(skipped))
            if on_node_errors is not None:
                on_node_errors(skipped)
        return prompt_id

    async def execute(
        self,
        graph: dict[str, Any] | None,
        timeout: float,
        on_progress: ProgressHook | None = None,
        on_submitted: Callable[[str], None] | None = None,
        on_node_errors: Callable[[list[str]], None] | None = None,
        submit_with: Callable[[], Awaitable[str]] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Submit a graph and wait for it to finish.

        Subscribes to the WebSocket *before* submitting so no events are missed;
        falls back to polling /history when the socket is unavailable.

        `on_submitted` fires with the prompt_id the moment ComfyUI accepts the job,
        which is what lets a caller stop waiting without giving up the event stream:
        the run keeps being watched here while the caller returns.
        `on_node_errors` fires with the output nodes ComfyUI refused to run, if any.

        `submit_with` replaces the POST to /prompt with any coroutine returning a
        prompt_id - the browser queueing the graph itself, in practice. Waiting for
        a run is the same job however it was started, so only the starting differs;
        `graph` is then unused and may be None.
        """
        try:
            import websockets
        except ImportError:  # pragma: no cover - dependency is declared
            websockets = None  # type: ignore[assignment]

        if websockets is None:
            prompt_id = await self._submit(graph, on_submitted, on_node_errors, submit_with)
            return prompt_id, await self._poll_until_done(prompt_id, timeout)

        url = f"{self.cfg.ws_url}?clientId={self.client_id}"
        prompt_id = ""
        try:
            headers = auth_headers(self.cfg)
            async with websockets.connect(
                url,
                max_size=None,
                ping_interval=self.cfg.ws_ping_interval,
                **({"additional_headers": headers} if headers else {}),
            ) as ws:
                prompt_id = await self._submit(graph, on_submitted, on_node_errors, submit_with)
                history = await self._wait_on_ws(ws, prompt_id, timeout, on_progress)
                return prompt_id, history
        except ComfyError:
            raise
        except Exception as exc:  # noqa: BLE001 - socket problems degrade to polling
            log.warning("WebSocket path failed (%s); falling back to polling", exc)
            if not prompt_id:
                prompt_id = await self._submit(graph, on_submitted, on_node_errors, submit_with)
            return prompt_id, await self._poll_until_done(prompt_id, timeout)

    async def _submit(
        self,
        graph: dict[str, Any] | None,
        on_submitted: Callable[[str], None] | None,
        on_node_errors: Callable[[list[str]], None] | None = None,
        submit_with: Callable[[], Awaitable[str]] | None = None,
    ) -> str:
        if submit_with is not None:
            prompt_id = await submit_with()
        else:
            prompt_id = await self.submit(graph or {}, on_node_errors)
        if on_submitted is not None:
            on_submitted(prompt_id)
        return prompt_id

    async def _wait_on_ws(
        self,
        ws: Any,
        prompt_id: str,
        timeout: float,
        on_progress: ProgressHook | None,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        current_node = ""

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ComfyError(
                    f"timed out after {timeout:.0f}s waiting for prompt {prompt_id}. "
                    "It may still be running - check get_queue()."
                )
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=min(remaining, self.cfg.ws_recv_timeout)
                )
            except asyncio.TimeoutError:
                history = await self.history(prompt_id)
                if history and _is_complete(history):
                    return history
                continue

            if isinstance(raw, (bytes, bytearray)):
                continue  
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = message.get("type")
            data = message.get("data") or {}
            if data.get("prompt_id") not in (None, prompt_id):
                continue

            if mtype == "executing":
                node = data.get("node")
                if node is None and data.get("prompt_id") == prompt_id:
                    history = await self.history(prompt_id)
                    if history is not None:
                        return history
                elif node:
                    current_node = str(node)
                    if on_progress is not None:
                        await on_progress(RunEvent(node=current_node))
            elif mtype == "progress" and on_progress is not None:
                node = str(data.get("node") or current_node)
                step, steps = int(data.get("value", 0)), int(data.get("max", 0))
                await on_progress(RunEvent(node=node, step=step, steps=steps or 1))
            elif mtype == "execution_success":
                history = await self.history(prompt_id)
                if history is not None:
                    return history
            elif mtype == "execution_error":
                raise ComfyError(_format_execution_error(data))
            elif mtype == "execution_interrupted":
                raise ComfyError(f"execution interrupted at node {data.get('node_id')}")

    async def _poll_until_done(self, prompt_id: str, timeout: float) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            history = await self.history(prompt_id)
            if history and _is_complete(history):
                status = history.get("status") or {}
                if status.get("status_str") == "error":
                    raise ComfyError(f"execution failed: {json.dumps(status, ensure_ascii=False)[:2000]}")
                return history
            await asyncio.sleep(self.cfg.poll_interval)
        raise ComfyError(f"timed out after {timeout:.0f}s waiting for prompt {prompt_id}")


def _is_complete(history: dict[str, Any]) -> bool:
    status = history.get("status") or {}
    if status.get("completed") is True:
        return True
    if status.get("status_str") in ("success", "error"):
        return True
    return bool(history.get("outputs"))


def _format_skipped_outputs(node_errors: dict[str, Any]) -> list[str]:
    """One line per output node ComfyUI validated away, saying why."""
    lines = []
    for node_id, node_error in node_errors.items():
        reasons = ", ".join(
            f"{item.get('message')} ({item.get('details')})".strip()
            for item in node_error.get("errors", [])
        )
        lines.append(f"node {node_id} ({node_error.get('class_type', '?')}): {reasons}")
    return lines


def _format_prompt_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return f"ComfyUI rejected the prompt (HTTP {resp.status_code}): {resp.text[:1000]}"
    error = payload.get("error") or {}
    parts = [f"ComfyUI rejected the prompt: {error.get('message', 'unknown error')}"]
    if error.get("details"):
        parts.append(f"details: {error['details']}")
    for node_id, node_error in (payload.get("node_errors") or {}).items():
        for item in node_error.get("errors", []):
            parts.append(
                f"node {node_id} ({node_error.get('class_type', '?')}): "
                f"{item.get('message')} {item.get('details', '')}".strip()
            )
    return "\n".join(parts)


def _format_execution_error(data: dict[str, Any]) -> str:
    parts = [
        f"node {data.get('node_id')} ({data.get('node_type')}) failed: "
        f"{data.get('exception_type')}: {data.get('exception_message')}"
    ]
    traceback = data.get("traceback")
    if isinstance(traceback, list) and traceback:
        parts.append("".join(traceback[-6:]).strip())
    return "\n".join(parts)


def collect_media(history: dict[str, Any]) -> list[MediaRef]:
    """Pull every file reference out of a history entry's outputs."""
    refs: list[MediaRef] = []
    for node_id, node_output in (history.get("outputs") or {}).items():
        if not isinstance(node_output, dict):
            continue
        for kind, items in node_output.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and "filename" in item:
                    refs.append(
                        MediaRef(
                            node_id=str(node_id),
                            kind=kind,
                            filename=item["filename"],
                            subfolder=item.get("subfolder", "") or "",
                            type=item.get("type", "output") or "output",
                        )
                    )
    return refs
