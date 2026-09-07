"""ComfyUI-Manager's install queue over HTTP - the fetch half of installing a node pack.

This server does not reimplement installing. Manager already resolves a registry id to
an archive, downloads it, extracts it into `custom_nodes` and writes the `.tracking`
file that makes a later uninstall exact, and doing that a second way would be a second
set of conventions for the same folders. What it does *not* do is ask what the packages
are about to do to the environment - so the split is: Manager fetches and places, this
server decides about packages, takes the checkpoint and reports the diff.

`env.py` holds the pure half (`install_item`, `queue_outcome`, `security_refusal`), the
same split `registry.py` has with `Listing`.

**Everything here was read out of Manager 3.40's source and measured against the running
instance, because its own `openapi.yaml` is a hand-written document that disagrees with
the code exactly where it matters:**

- **The spec says the queue reports status; the code says it reports counters.** Whether
  an install *succeeded* appears in one place only - a `cm-queue-status` WebSocket event
  carrying `nodepack_result` - which `task_worker` broadcasts once and then clears in the
  next statement. After that the status route reports `{total: 0, done: 0,
  in_progress: 0, is_processing: false}`, which is byte for byte what it reports before
  anything is queued. A poller therefore cannot tell "finished" from "never started",
  let alone "failed". Hence the socket, opened before the POST for `execute()`'s reason.
- **The spec marks every field of the install body optional; the code indexes three of
  them directly.** `json_data['version']`, `['channel']` and `['mode']` are subscripted,
  so omitting one is a KeyError and a 500 rather than a 400 that says which.
- **The spec does not mention the security policy that decides whether the route answers
  at all.** Installing needs `security_level` of middle-or-below; anything Manager rates
  `high` - every git URL it does not already know, and every `nightly` - needs `weak`.

The queue is shared with ComfyUI's own Manager UI, and the results dict is keyed by
`ui_id` and cleared wholesale, so two installs at once take each other's answers. This
refuses to start into a queue that is already busy rather than racing it.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from . import env as E
from .client import ComfyClient, ComfyError, auth_headers
from .config import Config

log = logging.getLogger(__name__)

PREFIX = "/manager"

CONFIG_RELATIVE = Path("user") / "__manager" / "config.ini"


class ManagerMissing(ComfyError):
    """ComfyUI-Manager is not installed, or its routes are not registered."""


def config_path(cfg: Config) -> Path:
    return cfg.comfy_dir / CONFIG_RELATIVE


def read_settings(cfg: Config) -> dict[str, str]:
    """Manager's own config.ini, best effort, for the message rather than for the decision.

    The disk is read here for `read_packs`'s reason - no route exposes `security_level`,
    so the alternative to reading the file is discovering the policy from a bare 403 with
    the word "terminal" in it. Empty when the file is absent, moved or unreadable.
    """
    path = config_path(cfg)
    try:
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    if not parser.has_section("default"):
        return {}
    return {k: str(v) for k, v in parser.items("default")}


async def version(cfg: Config, client: ComfyClient) -> str:
    """Manager's version string, or a refusal saying it is not there.

    A 404 on this route is the one that means "not installed" - the same shape
    `bridge_missing` has, and worth the same distinct error, because installing a node
    pack and installing Manager are different jobs for whoever reads the reply.
    """
    http = await client.http()
    try:
        resp = await http.get(f"{PREFIX}/version", timeout=cfg.request_timeout)
    except httpx.HTTPError as exc:
        raise ComfyError(
            f"could not reach ComfyUI at {cfg.base_url} to ask about ComfyUI-Manager: {exc}"
        ) from exc
    if resp.status_code == 404:
        raise ManagerMissing(
            "ComfyUI-Manager is not installed in this ComfyUI, so there is no install "
            "queue to use. Everything else in the extensions group still works: reading "
            "the registry, describing what is installed, and turning a pack off. "
            "Installing needs Manager, or a person at a terminal."
        )
    resp.raise_for_status()
    return resp.text.strip()


async def queue_status(cfg: Config, client: ComfyClient) -> dict[str, Any]:
    http = await client.http()
    resp = await http.get(f"{PREFIX}/queue/status", timeout=cfg.request_timeout)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


async def probe(cfg: Config, client: ComfyClient) -> dict[str, Any]:
    """What can be known about Manager before asking it to do anything."""
    found = await version(cfg, client)
    settings = read_settings(cfg)
    level = settings.get("security_level", "")
    out: dict[str, Any] = {
        "version": found,
        "queue": await queue_status(cfg, client),
        "security_level": level or "unknown",
        "settings_file": str(config_path(cfg)) if settings else "",
    }
    refusal = E.security_refusal(level) if level else ""
    if refusal:
        out["refusal"] = refusal
    return out


async def _post(
    cfg: Config, client: ComfyClient, path: str, body: dict | None = None
) -> httpx.Response:
    http = await client.http()
    return await http.post(
        f"{PREFIX}{path}",
        json=body if body is not None else {},
        timeout=cfg.request_timeout,
    )


def refusal_text(status: int, said: str, node_id: str) -> str:
    """Manager's own words for a refusal, with the reason it does not give.

    403 and 404 are both `web.Response(status=..., text=...)` with prose meant for
    somebody looking at a terminal, and the 404 in particular says "a security error has
    occurred" for what is really "this install is rated high and your level is normal" -
    which reads as a bug rather than as a policy. Naming the policy is the difference
    between a caller changing a setting and a caller retrying forever.
    """
    first = said.strip().splitlines()[0] if said.strip() else ""
    if status == 403:
        return (
            f"ComfyUI-Manager refused to install {node_id}: {first or 'security policy'}. "
            "Installing needs its security_level to be 'middle or below' - weak, normal "
            "or normal-. This server does not work around that setting."
        )
    if status == 404:
        return (
            f"ComfyUI-Manager refused to install {node_id}: {first or 'not found'}. That "
            "status covers two different things - a pack it cannot resolve, and a pack it "
            "resolved and rated too risky for this security_level. Manager rates every git "
            "URL it does not already know, and every 'nightly' version, as high, which "
            "needs security_level 'weak'. A published pack at a released version is rated "
            "low and is the case that works."
        )
    return f"ComfyUI-Manager refused to install {node_id}: HTTP {status} {first}".strip()


async def run_install(
    cfg: Config,
    client: ComfyClient,
    node_id: str,
    version_spec: str,
    timeout: float,
) -> dict[str, Any]:
    """Queue one pack, start the queue, and wait for the event that says what happened.

    The socket is opened *before* the POST, for the reason `execute` opens it before
    submitting a prompt: the result is one broadcast frame and there is no history to
    read it back from afterwards. Unlike a prompt, there is also no id to correlate on -
    Manager keys its results by the `ui_id` we chose, and clears the map when it sends -
    so a missed frame is unrecoverable rather than merely slow.

    Returns what happened; raises only when nothing was attempted.
    """
    try:
        import websockets
    except ImportError:  # pragma: no cover - dependency is declared
        raise ComfyError(
            "the websockets package is missing, and ComfyUI-Manager reports the outcome of "
            "an install over the WebSocket and nowhere else."
        ) from None

    item = E.install_item(node_id, version_spec)
    ui_id = str(item["ui_id"])
    url = f"{cfg.ws_url}?clientId={client.client_id}"
    headers = auth_headers(cfg)
    started = time.monotonic()

    async with websockets.connect(
        url,
        max_size=None,
        ping_interval=cfg.ws_ping_interval,
        **({"additional_headers": headers} if headers else {}),
    ) as ws:
        queued = await _post(cfg, client, "/queue/install", item)
        if queued.status_code != 200:
            raise ComfyError(refusal_text(queued.status_code, queued.text, node_id))

        begun = await _post(cfg, client, "/queue/start")
        if begun.status_code not in (200, 201):
            raise ComfyError(
                f"ComfyUI-Manager accepted {node_id} into its queue but would not start "
                f"it: HTTP {begun.status_code}. The item is still queued; "
                "POST /manager/queue/reset clears it."
            )

        event = await await_done(ws, timeout)

    if event is None:
        raise ComfyError(
            f"ComfyUI-Manager did not report the queue finishing within {timeout:.0f}s. "
            "The install may still be running - its console output is in get_comfy_log, "
            "and describe_extension says whether the pack arrived. Nothing here can "
            "recover the result now: Manager broadcasts it once and clears it."
        )

    ok, message = E.queue_outcome(event, ui_id)
    return {
        "ok": ok,
        "message": message,
        "took_s": round(time.monotonic() - started, 1),
        "queue": {"total": event.get("total_count"), "done": event.get("done_count")},
    }


async def await_done(ws, timeout: float) -> dict[str, Any] | None:
    """Read frames until Manager says its queue is done, or the deadline passes.

    Binary frames are ComfyUI's image previews and are skipped without decoding, the
    same as the run watcher does. Every other event on this socket belongs to somebody
    else - a prompt executing, a progress bar - and is ignored rather than reported: a
    generation running while a pack installs is ordinary.
    """
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout=left)
        except asyncio.TimeoutError:
            return None
        if isinstance(frame, (bytes, bytearray)):
            continue
        try:
            message = json.loads(frame)
        except (TypeError, ValueError):
            continue
        if not isinstance(message, dict) or message.get("type") != "cm-queue-status":
            continue
        data = message.get("data")
        if not isinstance(data, dict):
            continue
        if data.get("status") == "done":
            return data


NODE_LIST_FILE = "custom-node-list.json"


def pack_dir(cfg: Config) -> Path | None:
    """Where ComfyUI-Manager itself lives, whichever way its folder is spelled.

    Scanned rather than assumed: it is `ComfyUI-Manager` on one machine and
    `comfyui-manager` on the next, which is the same problem `same_pack` exists for.
    """
    root = cfg.comfy_dir / "custom_nodes"
    if not root.is_dir():
        return None
    for entry in root.iterdir():
        if entry.is_dir() and E.normalise(entry.name) == E.MANAGER_PACK:
            return entry
    return None


def node_list(cfg: Config) -> tuple[set[str], set[str]]:
    """The repository URLs and pip packages Manager already knows, out of its own catalogue.

    2.5 MB of JSON on this machine, and the thing `get_risky_level` consults to decide
    whether an install is `middle`, `high` or `block`. Reading it is what lets a report
    say *why* Manager would refuse a repository before Manager is asked - the same
    reason `config.ini` is read, and with the same standing: a prediction for the
    message, never the verdict.

    Empty sets when Manager is absent or its catalogue cannot be read, which callers
    have to treat as "cannot tell" rather than as "knows nothing" - the two would
    otherwise both come out as `high`.
    """
    where = pack_dir(cfg)
    if where is None:
        return set(), set()
    path = where / NODE_LIST_FILE
    if not path.is_file():
        return set(), set()
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return set(), set()
    entries = data.get("custom_nodes") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return set(), set()

    urls: set[str] = set()
    pip: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for item in entry.get("files") or []:
            urls.add(str(item))
        for item in entry.get("pip") or []:
            pip.add(str(item))
    return urls, pip
