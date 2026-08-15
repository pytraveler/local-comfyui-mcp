"""MCP server exposing a local ComfyUI instance as tools."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import Context, Image, MCPServer

import httpx

from . import __version__
from . import download as D
from . import graph as G
from . import i18n
from . import logs as L
from . import store
from . import toolsets as T
from .bridge import BridgeClient, WorkspaceError, WorkspaceUnavailable
from .client import ComfyClient, ComfyError, MediaRef, RunEvent, collect_media
from .config import Config, load_config
from .process import ComfyProcess, ProcessError, open_in_browser

log = logging.getLogger("comfyui_mcp")

CFG: Config = load_config()
CLIENT = ComfyClient(CFG)
PROCESS = ComfyProcess(CFG)
BRIDGE = BridgeClient(CFG, CLIENT)

mcp = MCPServer(
    "comfyui",
    version=__version__,
    instructions=(
        "Drives a local ComfyUI instance using API-format workflow files.\n"
        "Typical flow: comfy_status -> list_workflows -> describe_workflow -> run_workflow.\n"
        "describe_workflow reports which parameters a workflow accepts and where each one "
        "lands in the graph; pass those names to run_workflow. Any input can also be set "
        "directly with a raw '<node_id>.<input>' key.\n"
        "Some workflows need their input in a shape the graph cannot express - Ideogram 4 "
        "takes a JSON caption rather than a prose prompt. Those ship an instruction file: "
        "list_workflows and describe_workflow report it as `guide`, and get_workflow_guide "
        "returns it. Read it before run_workflow whenever it is there.\n"
        "run_workflow returns file paths, not image data - call show_image on a path to look "
        "at a result.\n"
        "Generation takes minutes and is meant to. For anything slow, submit with wait=False "
        "and poll get_progress(prompt_id), which reports step, percent and how long the job "
        "has been silent. Long silences are normal - loading a model emits no steps at all - "
        "so slowness is not evidence of a hang, and interrupting to start over throws away "
        "finished work and reloads the models. Only act on a stall that get_progress shows.\n"
        "get_comfy_log reads ComfyUI's own console, which is the only place a failed "
        "custom node says anything. A node whose import died is simply absent from "
        "/object_info, and absent looks exactly like never installed - so when a node "
        "type cannot be found or a pack behaves as though half of it is missing, read "
        "the log before concluding anything.\n"
        "The workspace tools are a second, optional half: they read, edit and run the "
        "workflow the user has open in the browser, including unsaved edits, which the HTTP "
        "API cannot see at all. They need the bridge node installed and a ComfyUI tab open, "
        "and say which of the two is missing when they fail - that is a setup problem, never "
        "a reason to retry. Everything else keeps working without them. Reach for them when "
        "the question is about what is on screen; reach for describe_workflow and "
        "run_workflow when it is about a file. Editing and running are separate calls: "
        "set_workspace_values reports what it changed and is one Ctrl+Z in the browser, then "
        "run_workspace queues what is on screen.\n"
        "get_workspace_graph reports `selected`, the nodes the user has clicked on - "
        "when a request says \"this node\" or \"these\", read that before guessing from "
        "a title. set_workspace_selection highlights nodes back, which is the clearest "
        "way to ask \"these four?\" before an edit.\n"
        "A workflow that will not run for want of a model usually says which ones it needs, "
        "on the loader itself: get_workspace_graph(detail='full') reports each node's "
        "`properties.models` as [{name, url, directory}], where `directory` is the folder "
        "name to pass straight to download_model. Graphs without it usually carry a 'Model "
        "Links' note instead, whose text comes back the same way. Compare either with "
        "list_models(folder), then download_model(url, folder) for what is missing - it "
        "puts the file where ComfyUI "
        "actually looks, which is often not ComfyUI/models. These are gigabytes, so call it "
        "with dry_run=True first to see the size, leave wait=False, and poll "
        "get_download_progress; an interrupted transfer resumes on the next identical call.\n"
        "screenshot_workspace photographs the canvas, which is the only way to answer "
        "what the workflow looks like - after moving nodes around, look rather than "
        "assume. It sees the layout and not the content: prompts and image previews are "
        "HTML over the canvas and are in no screenshot, so read values with "
        "get_workspace_graph."
    ),
)

SELECTION = T.parse(CFG.tools)

LANG = i18n.resolve(CFG.lang)


def tool(group: str, risk: str, **options: Any) -> Callable[[Callable], Callable]:
    """Register an MCP tool, unless `COMFYUI_TOOLS` switches its group off.

    Used instead of `mcp.tool()` so that a tool's group and risk class are stated
    where the tool is, rather than in a table beside it that the next tool would
    forget to join. `toolsets.py` explains the rest; two things belong here:

    *A disabled tool is not registered at all*, so its schema never reaches the
    model - which is where the context saving comes from, and is most of the point.
    It stays an ordinary function, so the tests still call it.

    *That would make the capability invisible*, and an answer of "I cannot do that"
    is a lie when the truth is "somebody turned it off". `comfy_status` is never
    disabled and reports what is missing, which is the whole reason its group
    carries `always`.
    """

    def register(fn: Callable) -> Callable:
        enabled = SELECTION.allows(fn.__name__, group)
        summary = (fn.__doc__ or "").strip().split("\n", 1)[0]
        T.record(fn.__name__, group, risk, enabled, summary)
        return mcp.tool(**options)(fn) if enabled else fn

    return register


_SCHEMA_CACHE: dict[str, Any] = {}

_ALL_SCHEMAS: dict[str, Any] = {}

_MODEL_DIRS: dict[str, dict[str, list[str]]] = {}


PROGRESS_KEEP = 8
PROGRESS_LOG_EVERY = 5.0
BAR_WIDTH = 24


@dataclass
class RunProgress:
    """What one run has done so far."""

    workflow: str
    started: float  # time.monotonic()
    prompt_id: str = ""
    state: str = "running"  # running | done | error | cancelled
    node: str = ""
    node_title: str = ""
    step: int = 0
    steps: int = 0
    updated: float = 0.0
    finished: float | None = None
    error: str = ""
    outputs: list[dict[str, Any]] = field(default_factory=list)
    _rate_from: float = 0.0
    _rate_step: int = 0
    _logged: float = 0.0

    def apply(self, event: RunEvent, titles: dict[str, str]) -> None:
        now = time.monotonic()
        if event.node != self.node:
            self.node, self.node_title = event.node, titles.get(event.node, "")
            self.step, self.steps = 0, 0
            self._rate_from, self._rate_step = 0.0, 0
        if event.counted:
            if not self._rate_from or event.step < self.step:
                self._rate_from, self._rate_step = now, event.step
            self.step, self.steps = event.step, event.steps
        self.updated = now

    @property
    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    @property
    def eta(self) -> float | None:
        """Seconds left in the current counter, or None when there is nothing to go on."""
        done = self.step - self._rate_step
        if self.state != "running" or not self.steps or done <= 0:
            return None
        per_step = (time.monotonic() - self._rate_from) / done
        return round(per_step * (self.steps - self.step), 1)

    @property
    def label(self) -> str:
        if self.state != "running":
            return f"{self.state} after {self.elapsed:.0f}s"
        where = f"{self.node_title or 'node'} ({self.node})" if self.node else "starting"
        if self.steps:
            return f"step {self.step}/{self.steps} - {where}"
        return f"{where} - working"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "prompt_id": self.prompt_id,
            "workflow": self.workflow,
            "state": self.state,
            "elapsed_s": round(self.elapsed, 1),
            "node": self.node,
            "node_title": self.node_title,
            "status": self.label,
        }
        if self.steps:
            out["step"] = self.step
            out["steps"] = self.steps
            out["percent"] = round(100 * self.step / self.steps)
            out["bar"] = _bar(self.step / self.steps)
        if self.state == "running":
            out["silent_for_s"] = round(time.monotonic() - (self.updated or self.started), 1)
            eta = self.eta
            if eta is not None:
                out["eta_s"] = eta
        if self.outputs:
            out["outputs"] = self.outputs
        if self.error:
            out["error"] = self.error
        return out


def _bar(fraction: float, width: int = BAR_WIDTH) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "#" * filled + "-" * (width - filled)


_PROGRESS: dict[str, RunProgress] = {}
_WATCHERS: set[asyncio.Task[Any]] = set()


def _remember(record: RunProgress) -> None:
    _PROGRESS[record.prompt_id] = record
    if len(_PROGRESS) <= PROGRESS_KEEP:
        return
    finished = sorted(
        (r for r in _PROGRESS.values() if r.state != "running"),
        key=lambda r: r.finished or r.started,
    )
    for old in finished[: len(_PROGRESS) - PROGRESS_KEEP]:
        _PROGRESS.pop(old.prompt_id, None)


def _latest() -> RunProgress | None:
    return max(_PROGRESS.values(), key=lambda r: r.started, default=None)


def _titles_of(workflow: G.Graph) -> dict[str, str]:
    """Node id -> the author's label, for progress lines that name something."""
    return {nid: (node.get("_meta") or {}).get("title", "") for nid, node in workflow.items()}


def _progress_hook(record: RunProgress, titles: dict[str, str], ctx: Context | None):
    async def hook(event: RunEvent) -> None:
        record.apply(event, titles)
        if time.monotonic() - record._logged >= PROGRESS_LOG_EVERY:
            record._logged = time.monotonic()
            log.info("%s: %s (%.0fs)", record.workflow, record.label, record.elapsed)
        if ctx is not None:
            await ctx.report_progress(record.step, record.steps or None, record.label)

    return hook


def _settle(record: RunProgress, task: asyncio.Task[Any]) -> None:
    """Record how a watched run ended, whichever way it ended."""
    record.finished = time.monotonic()
    if task.cancelled():
        record.state = "cancelled"
        return
    exc = task.exception()
    if exc is not None:
        record.state, record.error = "error", str(exc)
        return
    _, history = task.result()
    record.state = "done"
    record.outputs = [ref.to_dict(CFG) for ref in collect_media(history)]


def _on_submitted(record: RunProgress) -> Callable[[str], None]:
    def remember(prompt_id: str) -> None:
        record.prompt_id = prompt_id
        _remember(record)

    return remember


async def _watch_in_background(
    payload: G.Graph | None, titles: dict[str, str], record: RunProgress, timeout: float,
    skipped: list[str], submit_with: Callable[[], Any] | None = None,
) -> None:
    """Submit, return as soon as ComfyUI accepts the job, and keep watching it.

    The listener task outlives this call on purpose: dropping it would leave
    get_progress with nothing to report for exactly the runs that most need it.
    """
    accepted: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    remember = _on_submitted(record)

    def on_accepted(prompt_id: str) -> None:
        remember(prompt_id)
        if not accepted.done():
            accepted.set_result(prompt_id)

    task = asyncio.create_task(
        CLIENT.execute(
            payload, timeout=timeout, on_progress=_progress_hook(record, titles, None),
            on_submitted=on_accepted, on_node_errors=skipped.extend, submit_with=submit_with,
        )
    )
    _WATCHERS.add(task)
    task.add_done_callback(lambda t: (_settle(record, t), _WATCHERS.discard(t)))
    await asyncio.wait({accepted, task}, return_when=asyncio.FIRST_COMPLETED)
    if not accepted.done():
        await task  


async def _run_to_completion(
    payload: G.Graph | None, titles: dict[str, str], record: RunProgress, timeout: float,
    skipped: list[str], ctx: Context | None, submit_with: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Submit and wait, returning the run's history."""
    try:
        _, history = await CLIENT.execute(
            payload, timeout=timeout, on_progress=_progress_hook(record, titles, ctx),
            on_submitted=_on_submitted(record), on_node_errors=skipped.extend,
            submit_with=submit_with,
        )
    except Exception as exc:
        record.state, record.finished, record.error = "error", time.monotonic(), str(exc)
        raise
    record.state, record.finished = "done", time.monotonic()
    return history


def _cached_node_count(history: dict[str, Any]) -> int:
    """How many nodes ComfyUI reused instead of running.

    An identical graph is a cache hit by design - that is exactly what makes a repeat
    run take seconds instead of minutes. It only needs reporting where a caller cannot
    see it coming: run_workspace takes no params, so two calls in a row submit
    byte-identical JSON and the second recomputes nothing at all. Unsaid, a 0.1s
    success handing back the previous run's image reads as a fresh result.
    """
    for name, data in (history.get("status") or {}).get("messages") or []:
        if name == "execution_cached":
            return len(data.get("nodes") or [])
    return 0


def _forget_schemas() -> None:
    """Drop both schema caches. Anything that invalidates one invalidates the other."""
    _SCHEMA_CACHE.clear()
    _ALL_SCHEMAS.clear()
    _MODEL_DIRS.clear()


async def _all_schemas(refresh: bool = False) -> dict[str, Any]:
    """The whole /object_info payload, fetched once.

    Searching is the only thing that needs every node type rather than the two
    dozen a workflow uses, and on this install that is 2567 entries and 4.4 MB -
    worth caching hard and worth never fetching for anything else.
    """
    if refresh:
        _forget_schemas()
    if not _ALL_SCHEMAS:
        await _require_alive()
        _ALL_SCHEMAS.update(await CLIENT.object_info())
    return _ALL_SCHEMAS


async def _ensure_schema(class_type: str, strict: bool = False) -> dict[str, Any] | None:
    """One class's /object_info entry, cached. None when there is no schema.

    `strict` re-raises a transport failure instead of folding it into None:
    for a caller that treats "no schema" as evidence (diagnose_workspace),
    a fetch that failed and a type the server lacks are different answers.
    """
    if not class_type:
        return None
    if class_type in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[class_type]
    try:
        info = await CLIENT.object_info(class_type)
    except Exception:  # noqa: BLE001 - an unreachable or unknown node just means no schema
        if strict:
            raise
        return None
    entry = info.get(class_type)
    if isinstance(entry, dict):
        _SCHEMA_CACHE[class_type] = entry
        return entry
    return None


async def _schemas_for(workflow: G.Graph, refresh: bool = False) -> G.Schemas:
    """Fetch /object_info for just the node types this workflow uses.

    Per class rather than in bulk: the full payload is ~4 MB on a large install,
    while the couple of dozen classes a workflow needs come back in milliseconds.
    Returns whatever was obtainable - an empty dict when ComfyUI is down, which
    leaves the caller on the table-driven fallback.
    """
    if refresh:
        _forget_schemas()
    wanted = {node.get("class_type") for node in workflow.values() if node.get("class_type")}
    missing = [c for c in wanted if c not in _SCHEMA_CACHE]
    if missing and await CLIENT.is_alive():
        await asyncio.gather(*(_ensure_schema(c) for c in missing), return_exceptions=True)
    return {c: _SCHEMA_CACHE[c] for c in wanted if c in _SCHEMA_CACHE}


async def _input_max(class_type: str, input_key: str) -> int:
    """The declared max of a node input.

    Seed ranges vary per node: KSampler takes 2**64, but 'easy seed' caps at 2**50.
    Falls back to COMFYUI_FALLBACK_SEED_MAX when the schema cannot be read.
    """
    entry = await _ensure_schema(class_type)
    if entry is None:
        return CFG.fallback_seed_max
    spec = G.SchemaIndex({class_type: entry}).for_class(class_type).get(input_key)
    if spec is not None and spec.maximum is not None:
        return int(spec.maximum)
    return CFG.fallback_seed_max


_LAST_MODEL_SIGNATURE: tuple[str, ...] | None = None


def _model_signature(workflow: G.Graph) -> tuple[str, ...]:
    """The set of model files a graph will load, after parameters are applied."""
    return tuple(sorted({m["file"] for m in G.required_models(workflow) if m["file"]}))


async def _free_vram_if_starved(workflow: G.Graph, enabled: bool) -> dict[str, Any] | None:
    """Unload models before a run that needs different ones - but only under pressure.

    Two conditions, and both matter. A changed model set alone is not enough: low
    free VRAM is normal and desirable, because ComfyUI keeps models resident as a
    cache and that is exactly why a repeat run takes seconds. Freeing on every
    switch would break that cache and make A/B/A alternation reload every time.
    Measured on this machine, switching between two workflows with room to spare
    costs the same either way (11.0s vs 11.0s), so the unconditional version buys
    nothing and risks the healthy case. What genuinely hurts is a switch with no
    headroom left, so that is the only case this acts on.
    """
    global _LAST_MODEL_SIGNATURE
    signature = _model_signature(workflow)
    previous, _LAST_MODEL_SIGNATURE = _LAST_MODEL_SIGNATURE, signature
    if not enabled or previous is None or previous == signature:
        return None

    free_gb, total_gb = await _vram_gb()
    if free_gb is None or total_gb is None or not total_gb:
        return None
    fraction = free_gb / total_gb
    if fraction >= CFG.free_vram_min_fraction:
        return None

    await CLIENT.free(unload_models=True, free_memory=True)
    after, _ = await _vram_gb()
    report: dict[str, Any] = {
        "reason": (
            f"different models and only {fraction:.0%} of VRAM free "
            f"(below COMFYUI_FREE_VRAM_MIN_FRACTION={CFG.free_vram_min_fraction:.0%})"
        ),
        "vram_free_gb": {"before": free_gb, "after": after},
    }
    return report


async def _vram_gb() -> tuple[float | None, float | None]:
    try:
        stats = await CLIENT.system_stats()
    except Exception:  # noqa: BLE001 - reporting only
        return (None, None)
    devices = stats.get("devices") or []
    if not devices:
        return (None, None)
    device = devices[0]
    return (
        round((device.get("vram_free") or 0) / 2**30, 2),
        round((device.get("vram_total") or 0) / 2**30, 2),
    )


async def _random_seed_for(workflow: G.Graph, key: str, discovered: dict[str, G.Param]) -> int:
    """Pick a random seed that fits the range of the node it will be written to."""
    param = discovered.get(key)
    if param is not None:
        node_id, input_key = param.node_id, param.input
    elif "." in key:
        node_id, _, input_key = key.rpartition(".")
        target = G.resolve_setter(workflow, node_id, input_key)
        if target is None:
            return random.randint(0, CFG.fallback_seed_max)
        node_id, input_key = target
    else:
        return random.randint(0, CFG.fallback_seed_max)
    class_type = (workflow.get(node_id) or {}).get("class_type", "")
    return random.randint(0, await _input_max(class_type, input_key))


async def _require_alive() -> None:
    if not await CLIENT.is_alive():
        raise ComfyError(
            f"ComfyUI is not answering on {CFG.base_url}. Call comfy_start to launch it "
            f"({CFG.comfy_root / CFG.launch_script})."
        )


@tool("status", "reads")
async def comfy_status() -> dict[str, Any]:
    """Check whether ComfyUI is running and report GPU/VRAM and queue state."""
    alive = await CLIENT.is_alive()
    info: dict[str, Any] = {
        "running": alive,
        "url": CFG.base_url,
        "comfy_root": str(CFG.comfy_root),
        "workflows_dir": str(CFG.workflows_dir),
        "env_file": str(CFG.env_file) if CFG.env_file else None,
        "started_by_mcp": PROCESS.owned,
    }
    if not CFG.comfy_root.is_dir():
        info["config_warning"] = (
            f"COMFYUI_ROOT does not exist: {CFG.comfy_root}. "
            "Copy .env.example to .env and set it."
        )
    if SELECTION.narrowed:
        off = sorted({t.group for t in T.REGISTRY if not t.enabled})
        info["tools"] = {
            "spec": CFG.tools,
            "enabled": sum(1 for t in T.REGISTRY if t.enabled),
            "of": len(T.REGISTRY),
            "groups_off": off,
            "hint": (
                "these tools are switched off in COMFYUI_TOOLS (.env), not missing. "
                "Run configure.bat to change it; the server has to be restarted afterwards."
            ),
        }
        stray = T.unknown(SELECTION)
        if stray:
            info["tools"]["unknown_in_spec"] = stray
    if not alive:
        info["hint"] = "call comfy_start to launch it"
        return info

    stats = await CLIENT.system_stats()
    system = stats.get("system") or {}
    info["comfyui_version"] = system.get("comfyui_version")
    info["python_version"] = (system.get("python_version") or "").split()[0] or None
    info["devices"] = [
        {
            "name": d.get("name"),
            "vram_total_gb": round((d.get("vram_total") or 0) / 2**30, 2),
            "vram_free_gb": round((d.get("vram_free") or 0) / 2**30, 2),
        }
        for d in (stats.get("devices") or [])
    ]
    queue = await CLIENT.queue()
    info["queue_running"] = len(queue.get("queue_running") or [])
    info["queue_pending"] = len(queue.get("queue_pending") or [])
    return info


@tool("process", "process")
async def comfy_start(wait: bool = True) -> dict[str, Any]:
    """Launch the portable ComfyUI instance.

    Args:
        wait: block until the HTTP API answers (up to COMFYUI_STARTUP_TIMEOUT).
    """
    try:
        result = await PROCESS.start(CLIENT, wait=wait)
    except ProcessError as exc:
        raise ComfyError(str(exc)) from exc
    _forget_schemas()
    return result


@tool("process", "process")
async def comfy_stop() -> dict[str, Any]:
    """Stop the ComfyUI process that this server started.

    A ComfyUI you launched yourself is not touched.
    """
    return await PROCESS.stop()


_RESTART_QUIET_WAIT = 30.0


async def _await_comfy(alive: bool, seconds: float) -> float | None:
    """Wait for ComfyUI's API to start or stop answering. Seconds taken, or None."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + max(seconds, 0.0)
    while True:
        if await CLIENT.is_alive(timeout=2.0) == alive:
            return round(loop.time() - started, 1)
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(CFG.startup_poll_interval)


@tool("process", "process")
async def restart_comfy(wait: float = CFG.startup_timeout, force: bool = False) -> dict[str, Any]:
    """Restart ComfyUI and wait for it to answer again.

    What a newly installed or edited **node** needs: `nodes.py` imports every pack
    once at startup and nothing re-reads them, so a pack added while ComfyUI runs
    is simply absent from /object_info. Changed extension *JavaScript* is the
    other case and needs only reload_workspace - a restart there is minutes spent
    on a page reload.

    Two mechanisms, picked by who owns the process. One this server started is
    stopped and relaunched through COMFYUI_LAUNCH_SCRIPT. One you started yourself
    is asked to re-exec itself, through the bridge node - which is the only way to
    reach it at all, and has the advantage of coming back on the exact command
    line and environment it already had, rather than on this server's guess at
    them. That path therefore needs the node installed.

    Schemas and model directories are dropped, since a restart is precisely what
    makes them stale. A tab that was connected reconnects on its own and is waited
    for; the workflow on its canvas survives, being the browser's, not ComfyUI's.

    Args:
        wait: seconds to wait for ComfyUI to come back.
        force: restart even with jobs running or queued, throwing that work away.
    """
    if not await CLIENT.is_alive():
        raise ComfyError(
            f"ComfyUI is not answering on {CFG.base_url}, so there is nothing to restart. "
            "Call comfy_start to launch it. (A ComfyUI still starting up does not answer "
            "either - check comfy_status before concluding it is down.)"
        )

    queue = await CLIENT.queue()
    running = len(queue.get("queue_running") or [])
    pending = len(queue.get("queue_pending") or [])
    if (running or pending) and not force:
        raise ComfyError(
            f"ComfyUI has {running} running and {pending} queued job(s); a restart throws "
            "that work away and reloads the models. Wait for it, or pass force=True."
        )

    had_tab = bool((await BRIDGE.probe()).get("available"))
    report: dict[str, Any] = {"was_running": True, "jobs_discarded": running + pending}

    if PROCESS.owned:
        report["mechanism"] = "relaunched via the launch script"
        try:
            report["stopped"] = await PROCESS.stop()
            report["started"] = await PROCESS.start(CLIENT, wait=True)
        except ProcessError as exc:
            raise ComfyError(str(exc)) from exc
    else:
        report["mechanism"] = "re-executed in place by the bridge node"
        try:
            report["restart"] = await BRIDGE.restart_comfy()
        except WorkspaceUnavailable as exc:
            raise ComfyError(
                f"{exc}\nWithout the node, a ComfyUI this server did not start cannot be "
                "restarted from here - stop and start it yourself."
            ) from exc
        except WorkspaceError as exc:
            raise ComfyError(str(exc)) from exc

        report["went_quiet_after_s"] = await _await_comfy(alive=False, seconds=_RESTART_QUIET_WAIT)
        report["came_back_after_s"] = await _await_comfy(alive=True, seconds=wait)
        if report["came_back_after_s"] is None:
            raise ComfyError(
                f"ComfyUI did not answer on {CFG.base_url} within {wait:.0f}s of restarting. "
                "It may still be importing custom nodes - call comfy_status in a moment, and "
                "get_comfy_log once it is up if a pack failed."
            )

    _forget_schemas()

    if had_tab:
        state, waited = await _await_workspace(max(wait, 0.0))
        report["workspace_reconnected"] = bool(state.get("available"))
        report["workspace_waited_s"] = waited
        if not state.get("available"):
            report["workspace_hint"] = (
                "the tab has not reconnected yet; it retries on its own, so give it a "
                "moment and call workspace_status. reload_workspace forces the issue."
            )
    return report


@tool("logs", "reads")
async def get_comfy_log(
    lines: int = CFG.log_tail_lines,
    level: str = "",
    search: str = "",
    regex: bool = False,
) -> dict[str, Any]:
    """Read ComfyUI's console - the Python side, where import and dependency failures land.

    This is the terminal ComfyUI is running in, not the browser. It is where a custom
    node says it could not import, where a missing package is named, and where a
    traceback from inside a node ends up. Nothing else in this server can see any of it:
    a node that failed to load simply does not appear in /object_info, which looks
    identical to a node that was never installed.

    Args:
        lines: how many of the most recent matching lines to return; 0 for all of them.
        level: keep only this severity and above (DEBUG/INFO/WARNING/ERROR/CRITICAL).
            Untagged output - third-party packs printing directly - is dropped by this.
        search: keep only lines containing this text, case-insensitive.
        regex: treat `search` as a regular expression instead.
    """
    await _require_alive()
    payload = await CLIENT.logs()
    entries = payload.get("entries") or []
    parsed = L.to_lines(entries)

    try:
        matched = L.filter_lines(parsed, level=level, search=search, regex=regex)
    except ValueError as exc:  
        raise ComfyError(str(exc)) from exc
    except re.error as exc:
        raise ComfyError(f"search is not a valid regular expression: {exc}") from exc

    shown = matched[-lines:] if lines > 0 else matched
    notable = L.notable_lines(parsed)

    result: dict[str, Any] = {
        "lines": [line.format() for line in shown],
        "returned": len(shown),
        "matched": len(matched),
        "total_lines": len(parsed),
        "counts": L.count_levels(parsed),
    }
    if notable:
        result["notable_total"] = len(notable)
        already = set(result["lines"])
        fresh = [text for text in (line.format() for line in notable) if text not in already]
        if fresh:
            result["notable"] = fresh[: L.NOTABLE_SHOWN]

    if len(entries) >= L.COMFY_LOG_CAPACITY:
        result["buffer_full"] = (
            f"ComfyUI keeps only the last {L.COMFY_LOG_CAPACITY} writes and the buffer is "
            "full, so older lines are already gone. A plain startup nearly fills it by "
            "itself: to see why a node failed to import, restart ComfyUI and read this "
            "before running anything else."
        )
    return result


GRAPH_FORMATS = ("summary", "ui", "api")
GRAPH_SCOPES = ("root", "active", "all")
NAVIGATION = ("root", "up")
NODE_MODES = ("always", "muted", "bypassed")
SHOT_FITS = ("graph", "view", "selected")
SHOT_FORMATS = ("png", "jpeg", "webp")
SHOT_EDGE = (256, 4096)
WORKSPACE_TOOLS = [
    "get_workspace_graph",
    "save_workspace",
    "load_workspace",
    "screenshot_workspace",
    "undo_workspace",
    "get_console_log",
    "diagnose_workspace",
    "navigate_workspace",
    "switch_workspace_tab",
    "set_workspace_selection",
    "set_workspace_values",
    "set_workspace_node_modes",
    "add_workspace_node",
    "remove_workspace_nodes",
    "set_workspace_links",
    "set_workspace_layout",
    "set_workspace_groups",
    "arrange_workspace",
    "align_workspace",
    "run_workspace",
    "reload_workspace",
]


def _check_scope(scope: str) -> None:
    if scope not in GRAPH_SCOPES:
        raise ComfyError(f"scope must be one of {', '.join(GRAPH_SCOPES)}, got {scope!r}")


WORKSPACE_OPEN_WAIT = 30.0
_WORKSPACE_POLL_INTERVAL = 0.5


async def _await_workspace(seconds: float) -> tuple[dict[str, Any], float]:
    """Poll until a tab has registered, or the deadline passes.

    Shared by everything whose caller's next move needs a working tab. A page that
    has not run its JavaScript yet fails exactly like no page at all, so returning
    before it has makes the tool a coin flip for whatever runs next.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + max(seconds, 0.0)
    while True:
        state = await BRIDGE.probe()
        if state.get("available") or loop.time() >= deadline:
            return state, round(loop.time() - started, 1)
        await asyncio.sleep(_WORKSPACE_POLL_INTERVAL)


async def _await_tab_gone(client_id: str, seconds: float) -> float | None:
    """Wait for one tab's socket to disappear. Seconds taken, or None if it never did.

    The reload equivalent of waiting for ComfyUI to go quiet before waiting for it
    to come back: the page stays registered for the length of its own timer and
    then a page load, so probing straight away finds the tab that was asked to
    reload and reports it as the tab that came back.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + max(seconds, 0.0)
    while True:
        state = await BRIDGE.probe()
        if client_id not in {c.get("client_id") for c in (state.get("clients") or [])}:
            return round(loop.time() - started, 1)
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(_WORKSPACE_POLL_INTERVAL)


@tool("workspace", "reads")
async def open_workspace(wait: float = WORKSPACE_OPEN_WAIT, force: bool = False) -> dict[str, Any]:
    """Open ComfyUI in a browser, so the workspace tools have a tab to talk to.

    The bridge needs a page open - with none, every workspace_* tool fails with
    `no_workspace` and the only fix is a human opening one. This is that fix.

    It waits for the tab to register itself rather than firing and returning,
    because the caller's next move is a workspace tool: coming back before the
    page has loaded its JavaScript would just fail again, for a reason that no
    longer has anything to do with what went wrong.

    A tab that is already connected is left alone and reported - a second one
    would work, but it becomes the preferred client and the user's own tab
    silently stops being the one that gets edited. Pass force to open anyway.

    Args:
        wait: seconds to wait for the tab to connect. 0 returns as soon as the
            browser has been handed the URL.
        force: open a tab even when one is already connected.
    """
    state = await BRIDGE.probe()
    url = CFG.base_url

    if state.get("reason") == "comfyui_down":
        raise ComfyError(f"ComfyUI is not running on {url}; call comfy_start first")

    if state.get("available") and not force:
        return {
            "url": url,
            "opened": False,
            "connected": True,
            "already_open": True,
            "clients": state.get("clients"),
            "note": "a tab is already connected; pass force=True to open another",
        }

    try:
        await asyncio.to_thread(open_in_browser, url)
    except ProcessError as exc:
        raise ComfyError(str(exc)) from exc

    result: dict[str, Any] = {"url": url, "opened": True, "already_open": False}

    if state.get("reason") == "bridge_missing":
        return {
            **result,
            "connected": False,
            "reason": "bridge_missing",
            "hint": state.get("hint"),
            "note": "the UI will open, but the workspace tools need the bridge node installed",
        }

    state, waited = await _await_workspace(wait)
    if state.get("available"):
        return {**result, "connected": True, "waited_s": waited, "clients": state.get("clients")}
    return {
        **result,
        "connected": False,
        "waited_s": waited,
        "reason": state.get("reason", "no_workspace"),
        "hint": (
            "the browser was given the URL but no tab registered in time. "
            "It may still be loading - call workspace_status in a moment."
        ),
    }


@tool("workspace", "reads")
async def workspace_status() -> dict[str, Any]:
    """Check whether the live ComfyUI workspace can be reached.

    The workspace is the workflow open in the browser. Reaching it needs two things
    beyond a running ComfyUI - the bridge node installed and a tab connected - and
    this reports which one is missing. Call it before the other workspace_* tools
    when they fail, and to find `client_id` values when several tabs are open.
    """
    state = await BRIDGE.probe()
    if state.get("available"):
        state["tools"] = WORKSPACE_TOOLS
    return state


@tool("logs", "reads")
async def get_console_log(
    lines: int = CFG.log_tail_lines,
    level: str = "",
    search: str = "",
    regex: bool = False,
    client_id: str = "",
) -> dict[str, Any]:
    """Read the browser console - the frontend half, where extension failures land.

    The companion to get_comfy_log, and it answers a different question. A node has
    two halves: a Python class ComfyUI imports, and often a JavaScript extension that
    gives it its widgets and menus. When the JavaScript half fails, the Python half
    still registers, so the node appears in /object_info and on the canvas and merely
    behaves wrongly - which is the one failure nothing else here can see.

    `failed_extensions` is the direct answer to that: the frontend catches an
    extension's import error and only console.errors it, so this is the only place it
    is recorded at all.

    Needs a connected tab, and only sees what was logged since that tab loaded - a
    reload starts the record over.

    Args:
        lines: how many of the most recent matching entries to return; 0 for all.
        level: keep only this severity and above (DEBUG/INFO/WARNING/ERROR/CRITICAL).
        search: keep only entries containing this text, case-insensitive.
        regex: treat `search` as a regular expression instead.
        client_id: which tab to ask, when several are open.
    """
    try:
        reply = await BRIDGE.call("get_console_log", client_id=client_id)
    except WorkspaceError as exc:
        if "unknown method" in str(exc):
            raise ComfyError(
                "this tab is running an mcp_bridge.js that predates console capture. The "
                "JavaScript is served fresh on every page load, so reloading the ComfyUI tab "
                "is enough - ComfyUI itself does not need restarting."
            ) from exc
        raise

    payload = reply.get("result") or {}
    parsed = L.from_entries(payload.get("entries") or [])
    problems = L.from_entries(payload.get("problems") or [])

    try:
        matched = L.filter_lines(parsed, level=level, search=search, regex=regex)
    except ValueError as exc:
        raise ComfyError(str(exc)) from exc
    except re.error as exc:
        raise ComfyError(f"search is not a valid regular expression: {exc}") from exc

    shown = matched[-lines:] if lines > 0 else matched
    result: dict[str, Any] = {
        "client_id": reply.get("client_id"),
        "lines": [line.format() for line in shown],
        "returned": len(shown),
        "matched": len(matched),
        "total_lines": len(parsed),
        "counts": L.count_levels(parsed),
        "since": payload.get("since"),
    }

    broken = L.failed_extensions(problems)
    if broken:
        result["failed_extensions"] = broken
    if problems:
        result["problems_total"] = len(problems)
        already = set(result["lines"])
        fresh = [text for text in (line.format() for line in problems) if text not in already]
        if fresh:
            result["problems"] = fresh[-L.NOTABLE_SHOWN :]

    dropped = int(payload.get("dropped") or 0)
    if dropped:
        capacity = (payload.get("capacity") or {}).get("entries")
        result["dropped"] = (
            f"{dropped} older entries have been evicted; the page keeps the last {capacity}. "
            "Warnings and errors are kept separately and are all still in `problems`."
        )

    blind_ms = int(payload.get("blind_ms") or 0)
    result["blind_ms"] = blind_ms
    if blind_ms and not problems:
        result["blind_note"] = (
            f"the page had already been running {blind_ms} ms when capture started. The "
            "frontend imports every extension at once and the bridge is one of them, so a "
            "failure logged inside that window was never recorded. Nothing here can widen it."
        )
    return result


@tool("workspace", "reads")
async def get_workspace_graph(
    format: str = "summary",
    scope: str = "root",
    detail: str = "full",
    only: list[str] | str | None = None,
    client_id: str = "",
) -> dict[str, Any]:
    """Read the workflow currently open in the browser, unsaved edits included.

    This is the one view of a workflow that no file and no HTTP endpoint can give:
    what the user is actually looking at. Use it to answer questions about the graph
    on screen; use describe_workflow for the files in the workflows directory.

    The summary reports `selected` - the nodes and groups the user has clicked on.
    Treat it as them pointing: when a request says "this one" or "these", that is
    which ones, and it beats guessing from a title. set_workspace_selection points
    back the other way.

    A large workflow does not fit in one answer at any detail that includes its
    wiring, so the report steps down a level at a time until it does and says so in
    `reduced`. When that happens the next move is `only` - outline the whole graph,
    then ask again about the handful of nodes that matter. Each node in a subset
    also carries `feeds`, the nodes reading from it, so a subset can be walked
    downstream as well as up.

    Args:
        format: "summary" for a structured report - nodes, links, groups and a list
            of issues (missing node types, muted or bypassed nodes, unconnected
            required inputs). "ui" for the raw graph as a Save would write it.
            "api" for the API-format prompt, the same JSON that Export (API) and
            run_workflow use - only the frontend can produce it, which is why it is
            available here and nowhere else in this server.
        scope: "root" for the top level of the workflow, "active" for the subgraph
            on screen, "all" to descend into every subgraph. On a workflow built
            from subgraphs "root" is a handful of boxes and everything inside them
            is invisible, so reach for "all" when the question is about the whole
            thing. Nested nodes come back with path ids - `98:12` is node 12 inside
            subgraph node 98 - the same shape the API format and progress events
            use. Only "summary" descends; "ui" and "api" already cover the lot.
        detail: how much to say about each node. "full" is everything including
            widget values; "links" drops the widgets but keeps the wiring and
            positions; "outline" is one line per node - type, title, and how many
            links go in and out. Only a ceiling: a report over
            COMFYUI_GRAPH_MAX_CHARS is reduced further whatever was asked for.
        only: report just these node ids, at the detail asked for, however big the
            graph is. Pass "selected" for whatever the user has clicked on.
        client_id: which tab to ask; defaults to the most recently focused one.
            workspace_status lists them.
    """
    if format not in GRAPH_FORMATS:
        raise ComfyError(f"format must be one of {', '.join(GRAPH_FORMATS)}, got {format!r}")
    _check_scope(scope)
    if detail not in G.DETAIL_LEVELS:
        raise ComfyError(f"detail must be one of {', '.join(G.DETAIL_LEVELS)}, got {detail!r}")

    reply = await BRIDGE.call(
        "get_graph",
        {"format": format, "scope": scope, "widgets": detail == "full"},
        client_id=client_id,
    )
    result = reply.get("result") or {}
    if format != "summary":
        return {"client_id": reply.get("client_id"), **result}

    wanted: list[str] | None
    if only is None:
        wanted = None
    elif isinstance(only, str):
        if only != "selected":
            raise ComfyError(f"only takes a list of node ids or the word 'selected', got {only!r}")
        wanted = [str(node) for node in ((result.get("selected") or {}).get("nodes") or [])]
        if not wanted:
            raise ComfyError(
                "nothing is selected in that tab, so only='selected' would report an empty "
                "graph. Ask the user to select the nodes, or pass their ids."
            )
    else:
        wanted = [str(node) for node in only]

    condensed = G.condense_workspace(result, detail=detail, only=wanted, max_chars=CFG.graph_max_chars)
    return {"client_id": reply.get("client_id"), **condensed}


@tool("workspace", "writes")
async def save_workspace(
    name: str,
    format: str = "ui",
    scope: str = "root",
    overwrite: bool = False,
    client_id: str = "",
) -> dict[str, Any]:
    """Write the workflow open in the browser to a file.

    This is how to get at a graph too large to fit in one answer: the reply is a
    path and a few numbers, and the file can then be read in slices or searched
    like any other. It is also how an unsaved canvas becomes something that
    survives the tab being closed.

    Args:
        name: file name, with or without .json.
        format: "ui" writes exactly what ComfyUI's own Save writes - positions,
            groups, titles, collapsed state - into the export directory, and it can
            be opened in ComfyUI again. "api" writes the API-format prompt into the
            workflows directory, where run_workflow and describe_workflow find it.
            The two are not interchangeable: UI format keeps the layout and cannot
            be run, API format is runnable and has no layout at all.
        scope: "root" for the whole workflow, "active" for the subgraph on screen.
        overwrite: replace the file if it already exists.
        client_id: which tab to ask; defaults to the most recently focused one.
    """
    if format not in ("ui", "api"):
        raise ComfyError(f"format must be 'ui' or 'api', got {format!r}")
    if scope not in ("root", "active"):
        raise ComfyError(f"scope must be 'root' or 'active' for a saved graph, got {scope!r}")

    reply = await BRIDGE.call("get_graph", {"format": format, "scope": scope}, client_id=client_id)
    result = reply.get("result") or {}
    data = result.get("graph") if format == "ui" else result.get("prompt")
    if not isinstance(data, dict):
        raise ComfyError(f"the tab returned no {format}-format graph to save")

    if format == "ui":
        path = store.save_export(CFG, name, data, overwrite)
        nodes = len(data.get("nodes") or [])
    else:
        path = store.save_workflow(CFG, name, data, overwrite)
        nodes = len(data)

    saved: dict[str, Any] = {
        "client_id": reply.get("client_id"),
        "path": str(path),
        "format": format,
        "scope": scope,
        "nodes": nodes,
        "size_kb": round(path.stat().st_size / 1024, 1),
    }
    saved["note"] = (
        "read it from disk rather than through a tool; it is a ComfyUI workflow file "
        "and can be opened in the browser, but it cannot be queued"
        if format == "ui"
        else f"run_workflow('{path.stem}') can run this one"
    )
    return saved


UNDO_MAX_STEPS = 50


@tool("edit", "edits")
async def undo_workspace(steps: int = 1, redo: bool = False, client_id: str = "") -> dict[str, Any]:
    """Undo the last edit in the browser, exactly as Ctrl+Z would.

    Every workspace edit is bracketed so that one call is one undo step - a batch
    of three values set together comes back in one press. This is that press,
    which is what makes a wrong edit cheap to take back rather than something to
    reconstruct by hand.

    **The history is the user's, not this server's.** Their own edits sit on the
    same stack, interleaved with ours in the order they happened, and nothing
    distinguishes them. One step back is almost always ours; several steps back is
    taking back whatever was there, theirs included. Undo one, look, undo again.

    Args:
        steps: how many to take back, 1 to 50. Pass 0 to read the depth without
            moving - the only way to tell an undo that will do something from one
            that will silently do nothing, since an empty history is not an error.
        redo: step forward instead. A redo history exists only until the next edit,
            which clears it.
        client_id: which tab; defaults to the most recently focused one.
    """
    if not 0 <= steps <= UNDO_MAX_STEPS:
        raise ComfyError(f"steps must be between 0 and {UNDO_MAX_STEPS}, got {steps}")

    reply = await BRIDGE.call("undo", {"steps": steps, "redo": redo}, client_id=client_id)
    result = reply.get("result") or {}
    undone: dict[str, Any] = {"client_id": reply.get("client_id"), **result}

    if result.get("steps") == 0:
        undone["note"] = (
            "nothing was read back, so nothing changed"
            if steps == 0
            else f"there was no {'redo' if redo else 'undo'} history left; the canvas is unchanged"
        )
    return undone


@tool("process", "process")
async def reload_workspace(
    wait: float = WORKSPACE_OPEN_WAIT, force: bool = False, client_id: str = ""
) -> dict[str, Any]:
    """Reload the ComfyUI browser tab, and wait for it to come back.

    This is the fix for exactly one thing: **changed extension JavaScript**. The
    bridge's own JS, and every node pack's, is served from disk on each page load
    and never re-read otherwise, so editing it takes a reload and nothing else -
    restarting ComfyUI would cost minutes and achieve the same thing by accident.
    Installing or upgrading a *node* is the opposite case and needs restart_comfy,
    because Python packs are imported once at startup.

    It waits for the tab to register again rather than firing and returning, for
    the reason open_workspace does: a page part-way through loading fails exactly
    like no page at all.

    **The browser can refuse.** ComfyUI asks "Leave site?" when a workflow has
    unsaved edits, and that dialog waits for a human - nothing here can dismiss
    it. So a tab with edits is refused by default; `force` reloads anyway, having
    first written the canvas to the export directory so nothing can be lost.
    Opening a second tab is the other way round the problem: a new tab loads the
    current JavaScript without disturbing this one.

    Args:
        wait: seconds to wait for the tab to reconnect. 0 returns immediately.
        force: reload even though the browser may put a confirmation on screen.
        client_id: which tab, when several are open. Defaults to the focused one.
    """
    if wait < 0:
        raise ComfyError(f"wait must not be negative, got {wait}")
    backup = await _snapshot_canvas(client_id) if force else None

    reply = await BRIDGE.call("reload", {"force": force}, client_id=client_id)
    result = reply.get("result") or {}
    report: dict[str, Any] = {"reloaded_client": reply.get("client_id"), **result}
    if backup is not None:
        report["backup"] = str(backup)

    old = reply.get("client_id") or ""
    gone = await _await_tab_gone(old, wait)
    report["old_tab_gone_after_s"] = gone
    if gone is None:
        report["connected"] = False
        report["reason"] = "not_gone_yet"
        report["hint"] = (
            f"the tab was still connected after {wait:.0f}s. "
            + (
                "The browser is most likely holding it on its 'Leave site?' "
                "confirmation, which waits for a click in the window - the reload "
                "goes through as soon as that is answered. Call workspace_status "
                "then, or reload again with a longer `wait`."
                if result.get("may_prompt")
                else "Nothing should have held it, so check get_console_log for an "
                "error thrown inside the page."
            )
        )
        return report

    state, waited = await _await_workspace(max(wait - gone, 0.0))
    report["waited_s"] = round(gone + waited, 1)
    if state.get("available"):
        report["connected"] = True
        report["client_id"] = state.get("preferred_client")
        return report

    report["connected"] = False
    report["reason"] = state.get("reason", "no_workspace")
    report["hint"] = "the page is still loading; call workspace_status in a moment."
    return report


async def _snapshot_canvas(client_id: str) -> Path | None:
    """Write the open canvas to the export directory, or None if there is nothing on it.

    What makes `load_workspace` safe to call. The frontend deactivates the change
    tracker before a load and resets it after (`beforeLoadNewGraph` /
    `afterLoadNewGraph`), so Ctrl+Z cannot reach back past the load however the
    call is bracketed - and there is no way to ask whether the canvas held unsaved
    work, since the store getter that knows is minified-internal. Making the step
    reversible is therefore the only honest answer; warning about it is not one.
    """
    reply = await BRIDGE.call("get_graph", {"format": "ui", "scope": "root"}, client_id=client_id)
    graph = (reply.get("result") or {}).get("graph")
    if not isinstance(graph, dict) or not (graph.get("nodes") or []):
        return None
    return store.save_export(CFG, f"replaced-{time.strftime('%Y%m%d-%H%M%S')}", graph, overwrite=True)


@tool("edit", "writes")
async def load_workspace(name: str, backup: bool = True, client_id: str = "") -> dict[str, Any]:
    """Open a saved workflow in the browser, replacing what is on the canvas.

    The other half of save_workspace, and the way to put a workflow *file* under
    the workspace tools: load it, edit it with them, save it back. Without this
    they can only reach whatever the user happened to have open.

    Both formats work and neither has to be named - a file in `workflows/` is API
    format, one in `exports/` is UI format, and this reads which it is. UI format
    keeps the layout it was saved with; API format has none, so ComfyUI lays it
    out itself and the result is tidy rather than familiar.

    **This is not undoable.** ComfyUI resets the undo history when a workflow is
    loaded, so Ctrl+Z will not bring the previous canvas back. That is what the
    backup is for: the canvas being replaced is written to the export directory
    first and the reply names the file.

    Args:
        name: the file, with or without .json. Looked for in the workflows
            directory first, then the export directory; an absolute path also works.
        backup: write the current canvas to the export directory before replacing
            it. Leave it on unless the canvas is known to be worth nothing.
        client_id: which tab to load into; defaults to the most recently focused one.
    """
    graph, path, format = store.load_graph_file(CFG, name)

    replaced = await _snapshot_canvas(client_id) if backup else None

    reply = await BRIDGE.call(
        "load_graph",
        {"graph": graph, "format": format, "name": path.stem},
        client_id=client_id,
    )
    result = reply.get("result") or {}
    loaded: dict[str, Any] = {
        "client_id": reply.get("client_id"),
        "path": str(path),
        "replaced": str(replaced) if replaced else None,
        **result,
    }

    notes = ["ComfyUI resets the undo history on a load, so Ctrl+Z will not undo this."]
    if replaced is None and backup:
        notes.append("The canvas was empty, so there was nothing to back up.")
    if result.get("missing_node_types"):
        dropped = result.get("dropped_nodes")
        notes.append(
            f"{dropped} nodes are not on the canvas at all - an API-format load creates "
            "nothing for a type it does not know, so the graph is now incomplete."
            if dropped
            else "Some node types are not registered; those nodes are on the canvas but will not run."
        )
        notes.append(
            "get_comfy_log says whether a pack failed to import, which is the usual cause "
            "and looks identical to never having installed it."
        )
    loaded["note"] = " ".join(notes)
    return loaded


@tool("workspace", "reads", structured_output=False)
async def screenshot_workspace(
    fit: list[str] | str = "graph",
    max_edge: int = CFG.screenshot_max_edge,
    format: str = "png",
    client_id: str = "",
) -> list[Any]:
    """Photograph the ComfyUI canvas: what the workflow looks like, not what it says.

    This answers the questions a graph dump cannot - whether the layout reads as a
    mess, which boxes overlap, where a link crosses the whole screen, what the user
    means by "that one over there". After a layout change it is the only way to
    check the result rather than assume it.

    It is a picture of the canvas, and that has a hard edge: prompts, image
    previews, markdown notes and audio players are HTML drawn *over* the canvas and
    are not in it. An empty-looking prompt box in the picture says nothing about
    the prompt - get_workspace_graph is what reads values. The report says how many
    such widgets the graph has.

    Always the graph on screen. To photograph a subgraph, navigate_workspace into
    it first; the report names which graph it is.

    Args:
        fit: what to frame. "graph" for the whole workflow, "view" for the viewport
            exactly as the user has it, "selected" for what they have clicked on, or
            a list of node ids to frame those. A whole large graph is legible only
            as a shape - for reading titles, frame a handful of nodes.
        max_edge: longest edge in pixels, 256 to 4096. Bigger reads better and costs
            more; it does not make a wide graph legible, only a small subset.
        format: "png" keeps the text crisp and is the right answer for a diagram.
            "jpeg" and "webp" are smaller and blur it.
        client_id: which tab to ask; defaults to the most recently focused one.
    """
    if isinstance(fit, str) and fit not in SHOT_FITS:
        raise ComfyError(
            f"fit must be one of {', '.join(SHOT_FITS)} or a list of node ids, got {fit!r}"
        )
    if format not in SHOT_FORMATS:
        raise ComfyError(f"format must be one of {', '.join(SHOT_FORMATS)}, got {format!r}")
    low, high = SHOT_EDGE
    if not low <= max_edge <= high:
        raise ComfyError(f"max_edge must be between {low} and {high}, got {max_edge}")

    wanted = fit if isinstance(fit, str) else [str(node) for node in fit]
    reply = await BRIDGE.call(
        "screenshot",
        {"fit": wanted, "max_edge": max_edge, "format": format},
        client_id=client_id,
    )
    result = dict(reply.get("result") or {})
    encoded = result.pop("image", "")
    if not encoded:
        raise ComfyError("that tab answered without a picture in it")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ComfyError(f"that tab sent something that is not an image: {exc}") from exc

    mime = str(result.get("mime") or "image/png")
    report: dict[str, Any] = {
        "client_id": reply.get("client_id"),
        "size_kb": round(len(data) / 1024, 1),
        **result,
    }
    notes = []
    if result.get("dom_widgets"):
        notes.append(
            f"{result['dom_widgets']} widgets here are HTML drawn over the canvas - prompt "
            "boxes, image previews, notes - and none of them is in the picture. Read their "
            "values with get_workspace_graph."
        )
    if result.get("fit") == "view":
        notes.append("This is the viewport as the user has it; anything off screen is not in it.")
    if notes:
        report["note"] = " ".join(notes)
    return [Image(data=data, format=mime.rpartition("/")[2]), report]


@tool("edit", "edits")
async def set_workspace_values(
    values: dict[str, Any] | None = None,
    properties: dict[str, Any] | None = None,
    labels: dict[str, Any] | None = None,
    scope: str = "root",
    client_id: str = "",
) -> dict[str, Any]:
    """Set widget values, node properties and on-screen labels in the open workflow.

    This edits the live canvas, exactly as if the values had been typed in. The
    change lands on the frontend's undo stack as a single step, so one Ctrl+Z in
    the browser takes back the whole call - which is the reason to pass several
    values at once rather than one per call, and why properties live here rather
    than in a tool of their own.

    Nothing is written unless every key validates, so a rejected call leaves the
    graph as it was. Numeric ranges are enforced; a combo value outside the listed
    options is reported as a note and written anyway, because a node's option list
    is not a whitelist - ComfyUI decides.

    Args:
        values: `{"<node_id>.<widget>": value}`, e.g. `{"37.megapixels": 1.5}`.
            Node ids and widget names come from get_workspace_graph.
        properties: `{"<node_id>.<property>": value}` - the second, separate set of
            settings a node carries, the ones ComfyUI edits through the Properties
            Panel on its context menu. Some nodes keep their whole configuration
            there and their widget values mean nothing without it. A separate
            argument because the two namespaces can collide: one node here has a
            `delimiter` in both. Written through the node's own setProperty, so a
            pack that rebuilds its widgets in response gets the chance to.
        labels: `{"<node_id>": {"title": ..., "inputs": {...}, "outputs": {...},
            "widgets": {...}}}` - the text drawn *on* a node rather than in it.
            This is what makes a workflow readable in another language: a graph
            written in Chinese keeps its headings in node titles, slot labels and
            widget rows, and no widget *value* reaches any of them. Addressed by
            the stable `name` from get_workspace_graph, and null or "" clears an
            override so the name shows through again - which is how a translation
            is taken back off. `widgets` is separate from `inputs` because a
            converted widget has both, and only the widget's own label changes the
            row on screen - but that one is **not saved with the workflow** and
            lasts until the page reloads, which the change log says each time.
            Keyed by node id alone, because a node has one title and several sets
            of names, and `"1.title"` would collide with a widget called `title`.
            Do not try to write `localized_name`: that one is ComfyUI's own
            translation for the current locale and is regenerated on load.
        scope: "root" for the whole workflow, "active" for the subgraph on screen.
        client_id: which tab to edit; defaults to the most recently focused one.
    """
    values = values or {}
    properties = properties or {}
    labels = labels or {}
    if not values and not properties and not labels:
        raise ComfyError(
            "nothing to set; pass values={'<node_id>.<widget>': value}, properties= or labels="
        )
    _check_scope(scope)
    malformed = [key for key in (*values, *properties) if "." not in key.strip(".")]
    if malformed:
        raise ComfyError(
            f"these keys are not <node_id>.<name> paths: {', '.join(map(repr, malformed))}. "
            "get_workspace_graph reports both parts."
        )
    dotted = [key for key in labels if "." in key]
    if dotted:
        raise ComfyError(
            f"labels is keyed by node id alone, not by a path: {', '.join(map(repr, dotted))}. "
            "Put the slot name inside, as labels={'1': {'outputs': {'STRING': '...'}}}."
        )

    reply = await BRIDGE.call(
        "set_values",
        {"values": values, "properties": properties, "labels": labels, "scope": scope},
        client_id=client_id,
    )
    result = reply.get("result") or {}
    return {"client_id": reply.get("client_id"), **result}


@tool("edit", "edits")
async def set_workspace_node_modes(
    modes: dict[str, str],
    scope: str = "root",
    client_id: str = "",
) -> dict[str, Any]:
    """Mute or bypass nodes in the workflow open in the browser.

    Muted ("never") stops a node producing anything; bypassed passes its inputs
    straight through to whatever it feeds, which is how a branch is taken out
    without unwiring it. Both are how a workflow gets narrowed to the part being
    worked on.

    Like set_workspace_values, the whole call is one undo step and nothing changes
    unless every entry is valid.

    Args:
        modes: `{"<node_id>": "always" | "muted" | "bypassed"}`.
        scope: "root" for the whole workflow, "active" for the subgraph on screen.
        client_id: which tab to edit; defaults to the most recently focused one.
    """
    if not modes:
        raise ComfyError("modes is empty; pass {'<node_id>': 'always'|'muted'|'bypassed'}")
    _check_scope(scope)
    unknown = {node: mode for node, mode in modes.items() if mode not in NODE_MODES}
    if unknown:
        raise ComfyError(
            f"mode must be one of {', '.join(NODE_MODES)}; got {unknown}"
        )

    reply = await BRIDGE.call("set_node_modes", {"modes": modes, "scope": scope}, client_id=client_id)
    result = reply.get("result") or {}
    return {"client_id": reply.get("client_id"), **result}


@tool("workspace", "reads")
async def navigate_workspace(to: str = "root", client_id: str = "") -> dict[str, Any]:
    """Move the ComfyUI canvas into a subgraph, back out one level, or to the top.

    The other workspace tools take `scope="active"`, which means whatever graph is
    on screen - so this is how to point them inside a subgraph. For reading alone
    it is usually unnecessary: get_workspace_graph(scope="all") descends without
    moving the user's view.

    This changes what the user is looking at and nothing about the workflow, so it
    is not on the undo stack - Ctrl+Z would otherwise mean two different things.

    Args:
        to: "root" for the top level, "up" for one level out, or the id of a
            subgraph node in the graph currently on screen to go into it.
        client_id: which tab to move; defaults to the most recently focused one.
    """
    if not to:
        raise ComfyError(f"to is required: {', '.join(NAVIGATION)}, or a subgraph node id")

    reply = await BRIDGE.call("navigate", {"to": str(to)}, client_id=client_id)
    result = reply.get("result") or {}
    return {"client_id": reply.get("client_id"), **result}


@tool("workspace", "reads")
async def switch_workspace_tab(
    to: str = "", force: bool = False, client_id: str = ""
) -> dict[str, Any]:
    """List the workflow tabs open in the ComfyUI window, and switch between them.

    These are the tabs along the top of ComfyUI: open workflows, exactly one of
    which is on screen. They are not browser tabs - one browser tab holds all of
    them, and `client_id` plus workspace_status is where that sense of the word
    lives.

    With no `to` it reports and moves nothing, which is also how to find out what
    is open before naming one. Every other workspace tool acts on the workflow
    that is on screen, so this is what points them at a different one.

    Switching does what clicking the tab does, and no more: the canvas is
    reloaded from that workflow's own stored state. Unsaved edits in the tab
    being left behind are not lost - they belong to that workflow, which is why
    a tab can report `modified` while a different one is on screen.

    Args:
        to: which tab - an index from a previous call, its path or its filename,
            or "next", "previous", or "recent" for the one active before this.
            Empty reports without moving. A name matching two open tabs is
            refused rather than guessed.
        force: reload the tab already on screen instead of reporting that it is
            already there.
        client_id: which browser tab to ask; defaults to the most recently focused.
    """
    reply = await BRIDGE.call(
        "tabs", {"to": str(to), "force": bool(force)}, client_id=client_id
    )
    result = reply.get("result") or {}
    return {"client_id": reply.get("client_id"), **result}


@tool("workspace", "reads")
async def set_workspace_selection(
    nodes: list[str] | None = None,
    groups: list[str] | None = None,
    add: bool = False,
    centre: bool = False,
    client_id: str = "",
) -> dict[str, Any]:
    """Highlight nodes and groups on the canvas, so the user can see which ones you mean.

    The write half of what get_workspace_graph reports as `selected`. Pointing is how
    people hand work over, and it is worth having in both directions: four node ids in
    a sentence are hard to check, while four highlighted boxes are not. Use it before
    an edit the user should agree to, and to answer "which ones?" without a list.

    Selecting nothing clears the selection. A selection is not part of the workflow, so
    this does not go on the undo stack and Ctrl+Z will not take it back.

    Args:
        nodes: node ids to select, as they appear in get_workspace_graph. They are
            local to the graph on screen - navigate_workspace first for a subgraph,
            and drop any `98:12` prefix once inside.
        groups: group ids or titles to select as well.
        add: add to what is already selected instead of replacing it.
        centre: move the view to fit the selection. Off by default, since the user
            may be looking somewhere deliberately.
        client_id: which tab; defaults to the most recently focused one.
    """
    params = {
        "nodes": [str(node) for node in (nodes or [])],
        "groups": [str(group) for group in (groups or [])],
        "add": bool(add),
        "centre": bool(centre),
    }
    reply = await BRIDGE.call("set_selection", params, client_id=client_id)
    result = reply.get("result") or {}
    return {"client_id": reply.get("client_id"), **result}


@tool("workspace", "reads")
async def diagnose_workspace(
    scope: str = "all",
    refresh_schemas: bool = False,
    client_id: str = "",
) -> dict[str, Any]:
    """Report what is wrong with the workflow open in the browser, worst first.

    Reads the live graph and checks it against ComfyUI's own node schemas: node
    types that are not installed, required inputs with nothing plugged in, links
    whose types do not match, widget values outside the declared range, and inputs
    a node no longer has. Muted and bypassed nodes are reported too - they are the
    commonest reason a workflow "does nothing" while looking fine.

    Defaults to `scope="all"` because a workflow built from subgraphs keeps almost
    everything that can break inside them; checking only the top level would pass a
    graph that cannot run.

    Each finding names the node and, where there is one, a `fix` - the other
    workspace tools are what applies it: set_workspace_links to rewire,
    set_workspace_values to bring a value into range, set_workspace_node_modes to
    un-mute, add_workspace_node to replace something missing.

    A clean report is not a promise the run will succeed: ComfyUI validates more
    at queue time, and a value can be legal and still wrong.

    Args:
        scope: "all", "root" or "active" - see get_workspace_graph.
        refresh_schemas: re-fetch /object_info first. Schemas are cached for the
            life of the process, so pass this after installing nodes or models.
        client_id: which tab to inspect; defaults to the most recently focused one.
    """
    _check_scope(scope)
    if refresh_schemas:
        _forget_schemas()

    reply = await BRIDGE.call(
        "get_graph", {"format": "summary", "scope": scope, "widgets": True}, client_id=client_id
    )
    result = reply.get("result") or {}
    nodes = result.get("nodes") or []

    schemas: dict[str, Any] = {}
    unfetched: list[str] = []
    for class_type in {str(node.get("type") or "") for node in nodes}:
        try:
            entry = await _ensure_schema(class_type, strict=True)
        except Exception:  # noqa: BLE001 - one slow fetch must not become restart advice
            unfetched.append(class_type)
            continue
        if entry is not None:
            schemas[class_type] = entry
    if schemas:
        for class_type in unfetched:
            schemas[class_type] = {"unavailable": True}

    issues = G.diagnose(nodes, schemas or None)
    counts = {level: sum(1 for i in issues if i["severity"] == level) for level in G.SEVERITIES}
    return {
        "client_id": reply.get("client_id"),
        "scope": scope,
        "breadcrumb": result.get("breadcrumb"),
        "node_count": len(nodes),
        "schemas_available": bool(schemas),
        "counts": counts,
        "issues": issues,
        "summary": (
            f"{counts['error']} error(s), {counts['warning']} warning(s), {counts['note']} note(s)"
            if issues
            else "nothing found; note that ComfyUI validates more at queue time"
        ),
        **(
            {}
            if schemas
            else {
                "note": "ComfyUI returned no node schemas, so only checks that need "
                "the graph's own shape ran. Is it running?"
            }
        ),
    }


@tool("edit", "edits")
async def add_workspace_node(
    type: str,
    title: str = "",
    pos: list[float] | None = None,
    values: dict[str, Any] | None = None,
    connect: list[dict[str, str]] | None = None,
    scope: str = "root",
    client_id: str = "",
) -> dict[str, Any]:
    """Add a node to the workflow open in the browser, wired up and configured.

    Inserting a node is one edit, so it is one call and one Ctrl+Z: the widgets
    and links go in with it. Doing it in three calls would take three presses to
    undo one intention, and a failure partway would leave a stray node behind.

    Nothing is added unless every value and link validates. Link types are checked
    against the slots before anything is written, because litegraph refuses a
    mismatched connection by doing nothing and reporting nothing.

    Args:
        type: the registered node type, e.g. "ImageScale" or "VAEEncodeTiled".
            find_node_types looks one up, by slot type when the name is not
            known; an unknown one comes back with near matches rather than a
            bare refusal.
        title: the label on the node. Defaults to the type's own.
        pos: [x, y] on the canvas. Defaults to the middle of the current view,
            so the user can see what arrived.
        values: widget values for the new node, `{"<widget>": value}` - no node
            id, since it does not have one yet.
        connect: links to make at the same time, `[{"from": ..., "to": ...}]` with
            both ends written `"<node_id>.<slot>"`. The new node is `"this"`, as
            in `{"from": "this.IMAGE", "to": "9.images"}`. A slot can be named or
            given by index.
        scope: "root" for the whole workflow, "active" for the subgraph on screen.
        client_id: which tab to edit; defaults to the most recently focused one.
    """
    if not type:
        raise ComfyError("type is required, e.g. 'ImageScale'; find_node_types looks one up")
    _check_scope(scope)

    params: dict[str, Any] = {
        "type": type,
        "title": title,
        "values": values or {},
        "connect": connect or [],
        "scope": scope,
    }
    if pos is not None:
        if len(pos) != 2:
            raise ComfyError(f"pos must be [x, y], got {pos!r}")
        params["pos"] = pos

    reply = await BRIDGE.call("add_node", params, client_id=client_id)
    result = reply.get("result") or {}
    return {"client_id": reply.get("client_id"), **result}


@tool("edit", "edits")
async def remove_workspace_nodes(
    nodes: list[str],
    scope: str = "root",
    client_id: str = "",
) -> dict[str, Any]:
    """Delete nodes from the workflow open in the browser.

    The whole batch is one undo step and nothing is removed unless every id
    exists. The response reports `links_lost` per node: deleting a node unwires
    everything attached to it, which is not part of what the caller asked for and
    cannot be seen from an id alone.

    To take a node out of the picture without losing its wiring, prefer
    set_workspace_node_modes - bypassing passes inputs straight through.

    Args:
        nodes: node ids, from get_workspace_graph.
        scope: "root" for the whole workflow, "active" for the subgraph on screen.
        client_id: which tab to edit; defaults to the most recently focused one.
    """
    if not nodes:
        raise ComfyError("nodes is empty; pass a list of node ids")
    _check_scope(scope)

    reply = await BRIDGE.call(
        "remove_nodes", {"nodes": [str(n) for n in nodes], "scope": scope}, client_id=client_id
    )
    result = reply.get("result") or {}
    return {"client_id": reply.get("client_id"), **result}


@tool("edit", "edits")
async def set_workspace_links(
    connect: list[dict[str, str]] | None = None,
    disconnect: list[str] | None = None,
    scope: str = "root",
    client_id: str = "",
) -> dict[str, Any]:
    """Wire and unwire nodes in the workflow open in the browser.

    Both lists are applied in one undo step, disconnects first, so moving a link
    from one input to another is a single Ctrl+Z rather than a half-wired graph
    between two calls.

    An input holds one link, so connecting to a taken input replaces what was
    there - the response says what under `replaced`. Type compatibility is checked
    before anything is written: litegraph refuses a mismatched connection by doing
    nothing at all, which would otherwise leave a batch half-applied in silence.

    Args:
        connect: `[{"from": "<node_id>.<output>", "to": "<node_id>.<input>"}]`.
            Slots may be named ("8.IMAGE", "9.images") or given by index ("8.0").
        disconnect: inputs to clear, `["<node_id>.<input>"]`. Only inputs - an
            output feeds many links, so "which one" would be ambiguous; clear the
            input end instead.
        scope: "root" for the whole workflow, "active" for the subgraph on screen.
        client_id: which tab to edit; defaults to the most recently focused one.
    """
    if not connect and not disconnect:
        raise ComfyError(
            "nothing to do; pass connect=[{'from': '<node_id>.<output>', 'to': "
            "'<node_id>.<input>'}] or disconnect=['<node_id>.<input>']"
        )
    _check_scope(scope)

    malformed = [ref for ref in (disconnect or []) if "." not in ref.strip(".")]
    if malformed:
        raise ComfyError(
            f"these are not <node_id>.<input> paths: {', '.join(map(repr, malformed))}. "
            "get_workspace_graph reports both parts."
        )

    reply = await BRIDGE.call(
        "set_links",
        {"connect": connect or [], "disconnect": disconnect or [], "scope": scope},
        client_id=client_id,
    )
    result = reply.get("result") or {}
    return {"client_id": reply.get("client_id"), **result}


@tool("edit", "edits")
async def set_workspace_layout(
    positions: dict[str, list[float]] | None = None,
    sizes: dict[str, list[float]] | None = None,
    collapsed: dict[str, bool] | None = None,
    refit_groups: bool = True,
    scope: str = "root",
    client_id: str = "",
) -> dict[str, Any]:
    """Move, resize, fold and unfold nodes on the canvas open in the browser.

    None of this changes what a workflow does - it runs the same however it is
    laid out - so this is purely about making a graph readable. The whole batch is
    one undo step and nothing is written unless every id exists.

    Groups follow the nodes they were holding. A group is only a rectangle, and
    what is "inside" it is whatever falls within it, so moving nodes out from
    under one would silently empty it; membership is read before anything moves
    and each affected group is refitted around those same nodes afterwards.

    arrange_workspace computes positions rather than taking them, and its answer
    can be passed straight in here.

    Args:
        positions: `{"<node_id>": [x, y]}`. Canvas coordinates, y downwards.
            A pinned node is skipped rather than moved, and reported in
            `skipped` - pinning is the author saying "not this one".
        sizes: `{"<node_id>": [width, height]}`. A size below what the node needs
            to draw its widgets is raised to that minimum and reported.
        collapsed: `{"<node_id>": true}` to fold a node down to its title bar,
            `false` to unfold it. The desired state, not a toggle, so asking for
            what a node already is does nothing. get_workspace_graph reports the
            current state per node.
        refit_groups: refit every group that held one of the changed nodes.
            Turning this off leaves the boxes where they were, which is what you
            want when moving a node deliberately out of a group.
        scope: "root" for the whole workflow, "active" for the subgraph on screen.
        client_id: which tab to edit; defaults to the most recently focused one.
    """
    if not positions and not sizes and not collapsed:
        raise ComfyError(
            "nothing to do; pass positions={'<node_id>': [x, y]}, sizes={'<node_id>': [w, h]} "
            "or collapsed={'<node_id>': True}"
        )
    _check_scope(scope)

    unclear = {k: v for k, v in (collapsed or {}).items() if not isinstance(v, bool)}
    if unclear:
        raise ComfyError(
            f"collapsed takes true or false per node, got {unclear!r}. It is the state you "
            "want, not a toggle."
        )

    for label, batch in (("positions", positions), ("sizes", sizes)):
        for node_id, pair in (batch or {}).items():
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ComfyError(f"{label}[{node_id!r}] must be a pair of numbers, got {pair!r}")

    reply = await BRIDGE.call(
        "set_layout",
        {
            "positions": positions or {},
            "sizes": sizes or {},
            "collapsed": collapsed or {},
            "refit_groups": refit_groups,
            "scope": scope,
        },
        client_id=client_id,
    )
    result = reply.get("result") or {}
    return {"client_id": reply.get("client_id"), **result}


@tool("edit", "edits")
async def set_workspace_groups(
    create: list[dict[str, Any]] | None = None,
    update: list[dict[str, Any]] | None = None,
    remove: list[str] | None = None,
    scope: str = "root",
    client_id: str = "",
) -> dict[str, Any]:
    """Create, edit and delete the group boxes on the canvas open in the browser.

    A group is a labelled rectangle drawn behind the nodes that fall inside it.
    It holds no membership of its own - which nodes are "in" it is decided by
    where the box is - so creating one around a list of nodes means fitting the
    box to them, and that is what this does.

    Deleting a group takes only the box: the nodes it framed stay exactly where
    they are. Get the current groups, with their ids and members, from
    get_workspace_graph.

    Args:
        create: `[{"title": ..., "nodes": ["3", "8"]}]` - a box fitted around
            those nodes. `"color"` takes a palette name ("green", "blue",
            "pale_blue", ...) or `#rrggbb`; `"padding"` is the gap to the nodes,
            10 by default. A group with no nodes needs an explicit
            `"bounding": [x, y, width, height]` instead.
        update: `[{"group": <id or title>, ...}]` with any of "title", "color",
            "nodes" (refit around these) or `"fit": true` (refit around whatever
            it currently holds, after the nodes inside it have moved).
        remove: groups to delete, by id or title.
        scope: "root" for the whole workflow, "active" for the subgraph on screen.
        client_id: which tab to edit; defaults to the most recently focused one.
    """
    if not create and not update and not remove:
        raise ComfyError("nothing to do; pass create, update or remove")
    _check_scope(scope)

    reply = await BRIDGE.call(
        "set_groups",
        {"create": create or [], "update": update or [], "remove": remove or [], "scope": scope},
        client_id=client_id,
    )
    result = reply.get("result") or {}
    return {"client_id": reply.get("client_id"), **result}


@tool("edit", "edits")
async def arrange_workspace(
    only: list[str] | None = None,
    spacing_x: float = G.LAYOUT_SPACING_X,
    spacing_y: float = G.LAYOUT_SPACING_Y,
    origin: list[float] | None = None,
    apply: bool = True,
    scope: str = "root",
    client_id: str = "",
) -> dict[str, Any]:
    """Lay the workflow out left to right, in the order the data flows through it.

    Each node is placed one column past everything feeding it, and the nodes in a
    column are ordered to face what they are wired to, so links run forwards and
    cross as little as possible. The block keeps the top-left corner it already
    had, so it lands where the author left it rather than at the origin.

    Reaches for the whole graph by default, which is the blunt instrument: it will
    move everything, and a group whose members end up far apart becomes a large
    box around them. Prefer `only` with one group's nodes at a time - that leaves
    the rest of the canvas untouched and keeps each block inside its own group.

    Args:
        only: node ids to arrange, leaving every other node where it is. Links to
            nodes outside the list are ignored, since they cannot place anything.
        spacing_x: gap between columns, past the widest node in the left one.
        spacing_y: gap between nodes stacked in one column.
        origin: [x, y] for the top-left of the result. Defaults to the top-left
            of what is being arranged, so nothing wanders off.
        apply: write the positions. False computes and reports them without
            touching the canvas - the same dict can then be passed to
            set_workspace_layout.
        scope: "root" for the top level, "active" for the subgraph on screen.
            Not "all": a subgraph is a canvas of its own with its own
            coordinates, so there is no one layout that covers several. Use
            navigate_workspace to go in and arrange with "active".
        client_id: which tab to arrange; defaults to the most recently focused one.
    """
    _check_scope(scope)
    if scope == "all":
        raise ComfyError(
            "scope='all' cannot be arranged: each subgraph is a canvas of its own with "
            "its own coordinates. Use navigate_workspace to enter one, then scope='active'."
        )
    if origin is not None and len(origin) != 2:
        raise ComfyError(f"origin must be [x, y], got {origin!r}")

    reply = await BRIDGE.call(
        "get_graph", {"format": "summary", "scope": scope, "widgets": False}, client_id=client_id
    )
    result = reply.get("result") or {}
    nodes = result.get("nodes") or []

    if only:
        wanted = {str(node_id) for node_id in only}
        nodes = [node for node in nodes if str(node.get("id")) in wanted]
        missing = wanted - {str(node.get("id")) for node in nodes}
        if missing:
            raise ComfyError(
                f"no such node(s) in scope={scope!r}: {', '.join(sorted(missing))}. "
                "get_workspace_graph lists the ids."
            )
    if not nodes:
        raise ComfyError(f"there are no nodes to arrange in scope={scope!r}")

    plan = G.arrange(
        nodes,
        spacing_x=spacing_x,
        spacing_y=spacing_y,
        origin=(origin[0], origin[1]) if origin else None,
    )
    report = {
        "client_id": reply.get("client_id"),
        "scope": scope,
        "breadcrumb": result.get("breadcrumb"),
        "arranged": len(nodes),
        "columns": plan["columns"],
        "moved": plan["moved"],
        "unchanged": plan["unchanged"],
        "bounds": plan["bounds"],
        "positions": plan["positions"],
    }
    if not plan["positions"]:
        return {**report, "applied": False, "note": "every node is already where this would put it"}
    if not apply:
        return {**report, "applied": False, "note": "nothing was written; pass these to set_workspace_layout"}

    written = await BRIDGE.call(
        "set_layout",
        {"positions": plan["positions"], "sizes": {}, "refit_groups": True, "scope": scope},
        client_id=client_id,
    )
    applied = written.get("result") or {}
    return {
        **report,
        "applied": True,
        "groups": applied.get("groups", []),
        "errors": applied.get("errors", []),
    }


@tool("edit", "edits")
async def align_workspace(
    nodes: list[str],
    edge: str = "",
    distribute: str = "",
    spacing: float | None = None,
    apply: bool = True,
    scope: str = "root",
    client_id: str = "",
) -> dict[str, Any]:
    """Line nodes up on a common edge, space them evenly, or both.

    Unlike arrange_workspace this never reads a link: it moves the nodes it is
    given along one axis and changes nothing else about the layout. That is the
    point - straightening a row of loaders should not rearrange the workflow
    around them. Reach for this when the graph is already laid out the way the
    author wants and only looks untidy.

    Edges account for how big each node draws, so aligning `right` lines up the
    far edges of nodes of different widths rather than their positions, and a
    collapsed node lines up by its title bar rather than by the size it reports.

    Args:
        nodes: the node ids to align - at least two. From get_workspace_graph.
        edge: "left", "right", "top", "bottom", "centre_x" or "centre_y". The
            centre forms use the middle of the whole selection.
        distribute: "x" or "y" - even out the gaps between the nodes along that
            axis. The outermost two stay where they are and the rest are shared
            out between them; gaps rather than centres, since nodes differ in
            size enough that even centres look uneven.
        spacing: an exact gap for `distribute`, in canvas units, instead of
            filling the space the nodes already span.
        edge and distribute combine only across axes: aligning tops while
            spreading horizontally is one intention, aligning lefts while
            spreading horizontally is two contradictory ones.
        apply: write the positions. False reports them without touching the
            canvas; the same dict can be passed to set_workspace_layout.
        scope: "root" for the whole workflow, "active" for the subgraph on screen.
        client_id: which tab to edit; defaults to the most recently focused one.
    """
    _check_scope(scope)
    if scope == "all":
        raise ComfyError(
            "scope='all' cannot be aligned: each subgraph is a canvas of its own with its "
            "own coordinates. Use navigate_workspace to enter one, then scope='active'."
        )

    reply = await BRIDGE.call(
        "get_graph", {"format": "summary", "scope": scope, "widgets": False}, client_id=client_id
    )
    result = reply.get("result") or {}
    wanted = [str(node_id) for node_id in nodes]
    by_id = {str(node.get("id")): node for node in result.get("nodes") or []}

    missing = [node_id for node_id in wanted if node_id not in by_id]
    if missing:
        raise ComfyError(
            f"no such node(s) in scope={scope!r}: {', '.join(missing)}. "
            "get_workspace_graph lists the ids."
        )

    try:
        plan = G.align(
            [by_id[node_id] for node_id in wanted],
            edge=edge,
            distribute=distribute,
            spacing=spacing,
        )
    except ValueError as exc:
        raise ComfyError(str(exc)) from exc

    report = {
        "client_id": reply.get("client_id"),
        "scope": scope,
        "aligned": len(wanted),
        "edge": plan["edge"],
        "distribute": plan["distribute"],
        "moved": plan["moved"],
        "unchanged": plan["unchanged"],
        "positions": plan["positions"],
    }
    if not plan["positions"]:
        return {**report, "applied": False, "note": "every node is already where this would put it"}
    if not apply:
        return {**report, "applied": False, "note": "nothing was written; pass these to set_workspace_layout"}

    written = await BRIDGE.call(
        "set_layout",
        {"positions": plan["positions"], "sizes": {}, "collapsed": {}, "refit_groups": True, "scope": scope},
        client_id=client_id,
    )
    applied = written.get("result") or {}
    return {
        **report,
        "applied": True,
        "skipped": applied.get("skipped", []),
        "groups": applied.get("groups", []),
        "errors": applied.get("errors", []),
    }


@tool("workflows", "reads")
def list_workflows() -> list[dict[str, Any]]:
    """List API-format workflow files available in the workflows directory."""
    return store.list_workflows(CFG)


@tool("workflows", "reads")
async def describe_workflow(name: str, refresh_schemas: bool = False) -> dict[str, Any]:
    """Report the parameters a workflow accepts, plus its outputs and model files.

    Each parameter lists where it is actually written in the graph. Values reached
    through primitives and switches are resolved automatically, so `steps` points at
    the primitive node feeding the sampler rather than the sampler itself.

    When ComfyUI is running, each parameter also carries its real type, allowed
    options and numeric range, taken from the node's own schema.

    Args:
        name: workflow file name, without the .json extension.
        refresh_schemas: re-read node schemas from ComfyUI. Use after installing
                         models or custom nodes, since schemas are cached.
    """
    workflow, path = store.load_workflow(CFG, name)
    schemas = await _schemas_for(workflow, refresh=refresh_schemas)
    params = G.discover_params(workflow, schemas)
    result = {
        "name": name,
        "path": str(path),
        "nodes": len(workflow),
        "parameters": [p.to_dict() for p in params],
        "outputs": G.output_nodes(workflow),
        "models": G.required_models(workflow),
        "note": (
            "Anything not listed can still be set with a raw '<node_id>.<input>' key, "
            "e.g. '30:3.sampler_name'."
        ),
    }
    guide = store.guide_path(CFG, name)
    if guide is not None:
        result["guide"] = {
            "path": str(guide),
            "hint": (
                f"this workflow ships instructions - call get_workflow_guide('{name}') "
                "and follow them when building the parameters"
            ),
        }
    if not schemas:
        result["schema_warning"] = (
            "ComfyUI is not running, so types are inferred and no option lists or "
            "ranges are available. Start it for exact values."
        )
    return result


@tool("workflows", "reads")
def get_workflow_guide(name: str) -> dict[str, Any]:
    """Read the instruction file that ships with a workflow.

    A graph says which inputs exist, never what belongs in them. Some workflows only
    work with input in a particular shape - Ideogram 4 wants a JSON caption carrying
    bounding boxes, not a prose prompt - and that convention lives in a Markdown file
    named after the workflow. Read it and follow it before calling run_workflow.

    Workflows that have one are flagged as `guide` by list_workflows and
    describe_workflow.

    Args:
        name: workflow file name, without the .json extension.
    """
    text, path = store.load_guide(CFG, name)
    return {
        "name": name,
        "guide_path": str(path),
        "chars": len(text),
        "guide": text,
    }


@tool("workflows", "reads")
async def describe_node(class_type: str, full: bool = False) -> dict[str, Any]:
    """Look up one node type: its inputs, their valid values, and what it outputs.

    Use before wiring a node in with add_workspace_node, or to check allowed combo
    values (sampler names, schedulers, model files) before passing them to
    run_workflow. find_node_types is how you get the class_type in the first place.

    Args:
        class_type: the node's class name, e.g. 'KSampler'.
        full: return the raw /object_info entry instead of the summary. Combo
              option lists are complete there and can be very large - one node on
              this install measures 199k characters - so only ask when a truncated
              option list is actually the problem.
    """
    await _require_alive()
    info = await CLIENT.object_info(class_type)
    entry = info.get(class_type) if isinstance(info, dict) else None
    if not info:
        raise ComfyError(f"unknown node type '{class_type}'; find_node_types will look one up")
    if full or not isinstance(entry, dict):
        return info
    return G.summarise_schema(entry, class_type)


@tool("workflows", "reads")
async def find_node_types(
    search: str = "",
    input_type: str = "",
    output_type: str = "",
    category: str = "",
    pack: str = "",
    include_deprecated: bool = False,
    include_api: bool = False,
    refresh: bool = False,
    limit: int = CFG.node_list_limit,
) -> dict[str, Any]:
    """Find a node type to add to a workflow, by name or by what it connects to.

    Filters are ANDed and all are optional; with none of them this lists what is
    installed. Each result carries the node's slots, so it is usually enough on its
    own - reach for describe_node when you need a widget's allowed values.

    The type filters answer the question a graph editor actually asks. "What turns
    a LATENT into an IMAGE" is input_type='LATENT', output_type='IMAGE'; searching
    for the word 'latent' would never find VAEDecode, whose name and category
    contain neither word.

    Args:
        search: case-insensitive substring, matched against the node's name, title,
                category, description, and the search aliases ComfyUI ships - which
                is why 'latent to image' finds VAEDecode.
        input_type: only nodes accepting this slot type, e.g. 'IMAGE', 'MODEL'.
        output_type: only nodes producing it. Wildcard ('*') slots match anything,
                as they do when the link is drawn, but rank below nodes that name
                the type.
        category: substring of the node's category path, e.g. 'upscal', 'loaders'.
        pack: substring of the pack it came from, e.g. 'kjnodes', 'comfy_extras'.
        include_deprecated: include nodes ComfyUI marks as superseded. Off by
                default because something replaced them. Experimental nodes are
                always included - that flag means new, not unreliable.
        include_api: include paid cloud API nodes, which need an account.
        refresh: re-fetch /object_info first. Needed after installing nodes.
        limit: maximum results. What matched beyond it is still counted.
    """
    schemas = await _all_schemas(refresh=refresh)
    return G.find_node_types(
        schemas,
        search=search,
        input_type=input_type,
        output_type=output_type,
        category=category,
        pack=pack,
        include_deprecated=include_deprecated,
        include_api=include_api,
        limit=limit,
    )


MODEL_SUFFIXES = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx"}


def _scan_models_on_disk(folder: str, search: str, limit: int) -> dict[str, Any]:
    """Fallback listing for when ComfyUI is down. Misses extra_model_paths entries."""
    root = CFG.models_dir
    if not root.is_dir():
        raise ComfyError(f"ComfyUI is not running and no models directory at {root}")
    note = (
        "ComfyUI is not running, so this is a raw disk scan of the models folder. "
        "Models mapped in via extra_model_paths.yaml are NOT included - start ComfyUI "
        "for the authoritative list."
    )
    if not folder:
        return {
            "source": "disk",
            "models_dir": str(root),
            "folders": sorted(p.name for p in root.iterdir() if p.is_dir()),
            "note": note,
        }
    target = root / folder
    if not target.is_dir():
        raise ComfyError(f"no such model folder on disk: {target}")
    needle = search.lower()
    files = [
        p.relative_to(target).as_posix()
        for p in sorted(target.rglob("*"))
        if p.is_file() and p.suffix.lower() in MODEL_SUFFIXES and (not needle or needle in p.name.lower())
    ]
    return {"source": "disk", "folder": folder, "count": len(files), "files": files[:limit], "note": note}


@tool("workflows", "reads")
async def list_models(folder: str = "", search: str = "", limit: int = CFG.model_list_limit) -> dict[str, Any]:
    """List the models ComfyUI can actually load.

    Asks ComfyUI itself, so the result honours extra_model_paths.yaml and matches the
    values a loader node will accept. A plain disk scan does not: model folders are
    routinely mapped in from elsewhere and `ComfyUI/models/` can be almost empty.

    Args:
        folder: which folder to list, e.g. 'loras', 'checkpoints', 'diffusion_models'.
                Empty lists the available folder names instead.
        search: case-insensitive substring filter on the file name.
        limit: maximum number of files to return.
    """
    if not await CLIENT.is_alive():
        return _scan_models_on_disk(folder, search, limit)

    if not folder:
        return {"source": "comfyui", "folders": sorted(await CLIENT.model_folders())}

    files = await CLIENT.model_files(folder)
    needle = search.lower()
    matched = [f for f in files if not needle or needle in f.lower()]
    result: dict[str, Any] = {
        "source": "comfyui",
        "folder": folder,
        "count": len(matched),
        "files": sorted(matched)[:limit],
    }
    if len(matched) > limit:
        result["truncated"] = f"showing {limit} of {len(matched)}; narrow with `search` or raise `limit`"
    return result


DOWNLOAD_KEEP = 8
DOWNLOAD_LOG_EVERY = 10.0


@dataclass
class DownloadProgress:
    """What one transfer has done so far."""

    job_id: str
    url: str
    folder: str
    filename: str
    destination: str
    started: float  # time.monotonic()
    state: str = "running"  # running | done | error | cancelled | skipped
    done: int = 0
    total: int | None = None
    resumed_from: int = 0
    updated: float = 0.0
    finished: float | None = None
    error: str = ""
    sha256: str = ""
    _rate_from: float = 0.0
    _rate_bytes: int = 0
    _logged: float = 0.0

    def advance(self, done: int, total: int | None) -> None:
        now = time.monotonic()
        if not self._rate_from or done < self.done:
            self._rate_from, self._rate_bytes = now, done
        self.done, self.total, self.updated = done, total or self.total, now

    @property
    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    @property
    def speed(self) -> float | None:
        """Bytes per second, or None before anything has arrived."""
        moved = self.done - self._rate_bytes
        window = time.monotonic() - self._rate_from
        return moved / window if self._rate_from and window > 0.5 and moved > 0 else None

    @property
    def eta(self) -> float | None:
        rate = self.speed
        if self.state != "running" or not self.total or not rate:
            return None
        return round(max(0.0, (self.total - self.done) / rate), 1)

    @property
    def label(self) -> str:
        if self.state != "running":
            return f"{self.state} after {self.elapsed:.0f}s"
        if self.total:
            return f"{D.human_size(self.done)} of {D.human_size(self.total)}"
        return f"{D.human_size(self.done)} so far"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "job_id": self.job_id,
            "filename": self.filename,
            "folder": self.folder,
            "state": self.state,
            "elapsed_s": round(self.elapsed, 1),
            "downloaded_bytes": self.done,
            "downloaded": D.human_size(self.done),
            "destination": self.destination,
            "status": self.label,
        }
        if self.total:
            out["size_bytes"] = self.total
            out["size"] = D.human_size(self.total)
            out["percent"] = round(100 * self.done / self.total, 1)
            out["bar"] = _bar(self.done / self.total)
        if self.resumed_from:
            out["resumed_from_bytes"] = self.resumed_from
        if self.state == "running":
            out["silent_for_s"] = round(time.monotonic() - (self.updated or self.started), 1)
            rate = self.speed
            if rate:
                out["speed"] = f"{D.human_size(rate)}/s"
            eta = self.eta
            if eta is not None:
                out["eta_s"] = eta
        if self.sha256:
            out["sha256"] = self.sha256
        if self.error:
            out["error"] = self.error
        return out


_DOWNLOADS: dict[str, DownloadProgress] = {}
_DOWNLOAD_TASKS: dict[str, asyncio.Task[Any]] = {}


def _remember_download(record: DownloadProgress) -> None:
    _DOWNLOADS[record.job_id] = record
    if len(_DOWNLOADS) <= DOWNLOAD_KEEP:
        return
    finished = sorted(
        (r for r in _DOWNLOADS.values() if r.state != "running"),
        key=lambda r: r.finished or r.started,
    )
    for old in finished[: len(_DOWNLOADS) - DOWNLOAD_KEEP]:
        _DOWNLOADS.pop(old.job_id, None)


def _find_download(job_id: str) -> DownloadProgress | None:
    """Look up by full id, then by bare file name, then by the newest."""
    if not job_id:
        return max(_DOWNLOADS.values(), key=lambda r: r.started, default=None)
    if job_id in _DOWNLOADS:
        return _DOWNLOADS[job_id]
    matched = [r for r in _DOWNLOADS.values() if r.filename == job_id]
    return matched[0] if len(matched) == 1 else None


async def _model_dirs() -> dict[str, dict[str, list[str]]]:
    if not _MODEL_DIRS:
        _MODEL_DIRS.update(await CLIENT.model_directories())
    return _MODEL_DIRS


def _outbound_client() -> httpx.AsyncClient:
    """A client for the wider internet - CLIENT's is bound to ComfyUI's base_url.

    `read` is the one timeout that matters: it bounds the wait for the next chunk,
    not the transfer, so a 5 GB file may take an hour while a dead connection is
    noticed in a minute.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=CFG.download_timeout, write=30.0, pool=30.0),
        follow_redirects=False,
    )


async def _transfer(record: DownloadProgress, plan: D.Preflight, destination: Path) -> None:
    """Run one download to completion, recording it as it goes."""

    def on_progress(done: int, total: int | None) -> None:
        record.advance(done, total)
        if time.monotonic() - record._logged >= DOWNLOAD_LOG_EVERY:
            record._logged = time.monotonic()
            log.info("%s: %s (%.0fs)", record.filename, record.label, record.elapsed)

    try:
        async with _outbound_client() as http:
            result = await D.fetch(
                http,
                plan.url,
                destination,
                token=CFG.download_token,
                size=plan.size,
                retries=CFG.download_retries,
                on_progress=on_progress,
            )
        record.state = "done"
        record.done, record.total = result.size, result.size
        record.resumed_from = result.resumed_from
        record.sha256 = result.sha256 or record.sha256
        _forget_schemas()
        log.info("%s: downloaded %s to %s", record.filename, D.human_size(result.size), destination)
    except asyncio.CancelledError:
        record.state = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 - the record is the only place this is reported
        record.state = "error"
        record.error = str(exc)
        log.warning("%s: download failed: %s", record.filename, exc)
    finally:
        record.finished = time.monotonic()
        _DOWNLOAD_TASKS.pop(record.job_id, None)


@tool("download", "writes")
async def download_model(
    url: str,
    folder: str,
    filename: str = "",
    directory: str = "",
    wait: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Download a model file into the folder ComfyUI will actually load it from.

    Meant for the models a workflow declares on its own loaders: at detail="full"
    get_workspace_graph reports each node's `properties.models` as
    [{name, url, directory}], and `directory` is this tool's `folder`. Graphs
    without that property usually ship a "Model Links" note saying the same thing
    in prose. Either way, check what is already there with list_models(folder)
    first and fetch only the rest.

    Where the file lands is decided by ComfyUI, not by this server: the directory
    list honours extra_model_paths.yaml, so it is routinely on another drive
    entirely, and a model written anywhere else is invisible however right the
    bytes are. The reply always names the directory it chose.

    Interrupted transfers resume: the bytes go to a `.part` file beside the target
    and re-issuing the same call continues from where it stopped, so a failure
    costs the remainder rather than the whole file.

    Args:
        url: direct link to the file, as written in the note.
        folder: ComfyUI model folder, e.g. 'vae' or 'diffusion_models'.
                list_models() with no arguments lists the valid names.
        filename: name to save as. Defaults to the last segment of the URL.
                  A 'subdir/name.safetensors' is allowed; ComfyUI loads those.
        directory: which of the folder's registered directories to use. Defaults
                   to the first one that exists, which is what ComfyUI's own
                   `is_default` ordering intends.
        wait: wait for the download to finish. False returns immediately and the
              transfer keeps running, so get_download_progress reports it - prefer
              that for anything large, since a multi-gigabyte file outlasts most
              tool-call deadlines.
        overwrite: fetch again even though the file is already there.
        dry_run: report size, checksum and destination without fetching anything.
                 Worth doing first: a link in a note is often several gigabytes.
    """
    await _require_alive()
    name = (filename or D.filename_from_url(url)).strip()
    if not name:
        raise ComfyError(f"cannot tell a file name from {url!r} - pass `filename`")
    try:
        D.check_host(url, D.hosts_from(CFG.download_allow_hosts))
        D.check_format(name, CFG.download_allow_pickle)
    except D.DownloadError as exc:
        raise ComfyError(str(exc)) from exc

    known = await _model_dirs()
    entry = known.get(folder)
    if entry is None:
        raise ComfyError(
            f"ComfyUI has no model folder '{folder}'. Known: {', '.join(sorted(known))}"
        )
    try:
        dest = D.choose_destination(folder, entry["folders"], entry["extensions"], name, directory)
    except D.DownloadError as exc:
        raise ComfyError(str(exc)) from exc

    job_id = f"{folder}/{name}"
    if dest.path.is_file() and not overwrite:
        return {
            "job_id": job_id,
            "state": "skipped",
            "already_present": True,
            **dest.to_dict(),
            "size": D.human_size(dest.path.stat().st_size),
            "hint": "pass overwrite=True to fetch it again",
        }
    if job_id in _DOWNLOAD_TASKS:
        return {**(_DOWNLOADS[job_id].to_dict()), "hint": "already downloading; poll get_download_progress"}

    try:
        async with _outbound_client() as http:
            plan = await D.preflight(http, url, CFG.download_token)
        D.check_size(plan.size, int(CFG.download_max_gb * 2**30))
        D.check_space(dest.directory, plan.size)
    except D.DownloadError as exc:
        raise ComfyError(str(exc)) from exc

    part = dest.path.with_name(dest.path.name + ".part")
    resume = part.stat().st_size if part.is_file() else 0
    report: dict[str, Any] = {"job_id": job_id, **dest.to_dict(), **plan.to_dict()}
    if resume:
        report["resuming_from"] = D.human_size(resume)
    if dry_run:
        report["state"] = "planned"
        report["free_space"] = D.human_size(D.free_space(dest.directory))
        report["hint"] = "nothing was fetched; call again with dry_run=False"
        return report

    record = DownloadProgress(
        job_id=job_id,
        url=url,
        folder=folder,
        filename=name,
        destination=str(dest.path),
        started=time.monotonic(),
        total=plan.size,
        done=resume,
        resumed_from=resume,
        sha256=plan.sha256,
    )
    _remember_download(record)

    if wait:
        await _transfer(record, plan, dest.path)
        result = record.to_dict()
        if record.state == "error":
            raise ComfyError(record.error)
        return {**report, **result}

    task = asyncio.create_task(_transfer(record, plan, dest.path))
    _DOWNLOAD_TASKS[job_id] = task
    _WATCHERS.add(task)
    task.add_done_callback(_WATCHERS.discard)
    return {
        **report,
        "state": "running",
        "hint": f"downloading in the background; poll get_download_progress({job_id!r})",
    }


@tool("download", "reads")
async def get_download_progress(job_id: str = "") -> dict[str, Any]:
    """Report how far a download has got: bytes, percent, speed and an ETA.

    `silent_for_s` is the telling number, as it is for a run: a slow link keeps it
    small while a dead one lets it grow. A stalled transfer does not need
    cancelling - the retry logic resumes by itself - so act only on a silence that
    outlasts COMFYUI_DOWNLOAD_TIMEOUT several times over.

    Args:
        job_id: '<folder>/<filename>', or just the file name, or empty for the
                most recent download.
    """
    record = _find_download(job_id)
    if record is None:
        known = ", ".join(sorted(_DOWNLOADS)) or "(none)"
        return {
            "job_id": job_id,
            "state": "unknown",
            "hint": f"no download recorded under that id. Tracked: {known}.",
        }
    result = record.to_dict()
    if record.state == "running":
        result["hint"] = "still downloading - poll again"
    elif record.state == "done":
        result["hint"] = "finished; the file is in place and ComfyUI will list it"
    elif record.state in ("error", "cancelled"):
        result["hint"] = "call download_model again with the same arguments to resume"
    return result


@tool("download", "runs")
async def cancel_download(job_id: str = "") -> dict[str, Any]:
    """Stop a download that is still running.

    What has arrived stays in the `.part` file, so calling download_model again
    with the same arguments continues rather than starting over. For a transfer
    that is merely slow this is counter-productive.

    Args:
        job_id: '<folder>/<filename>', or just the file name, or empty for the
                most recent download.
    """
    record = _find_download(job_id)
    if record is None:
        return {"cancelled": False, "hint": f"no download recorded under {job_id!r}"}
    task = _DOWNLOAD_TASKS.get(record.job_id)
    if task is None or task.done():
        return {"cancelled": False, "job_id": record.job_id, "state": record.state}
    task.cancel()
    return {
        "cancelled": True,
        "job_id": record.job_id,
        "kept_bytes": record.done,
        "hint": "call download_model again with the same arguments to resume",
    }


@tool("run", "runs")
async def run_workflow(
    name: str,
    params: dict[str, Any] | None = None,
    wait: bool = True,
    timeout: float = CFG.run_timeout,
    save_outputs: bool = True,
    free_on_switch: bool | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Run a workflow and return the paths of the files it produced.

    Call describe_workflow first to see which parameter names a workflow accepts.
    Pass seed=-1 to randomise the seed.

    Args:
        name: workflow file name, without the .json extension.
        params: parameter overrides, e.g. {"prompt": "a red fox", "seed": -1, "steps": 8}.
                Raw '<node_id>.<input>' keys are accepted for anything not discovered.
        wait: wait for the run to finish. When False, returns the prompt_id immediately
              and the run keeps being watched in the background, so get_progress(prompt_id)
              reports its steps as they happen. Prefer this for anything slow: waiting
              blind is what makes a caller mistake a working generation for a hung one.
        timeout: seconds to wait before giving up on a running job.
        save_outputs: convert PreviewImage nodes to SaveImage so results are written to
                      output/ instead of the temp folder that ComfyUI clears on restart.
        free_on_switch: unload models from VRAM before this run when it needs a different
                        set than the previous one AND free VRAM is already below
                        COMFYUI_FREE_VRAM_MIN_FRACTION. Defaults to COMFYUI_FREE_ON_SWITCH.
                        Low free VRAM is normal on its own - ComfyUI keeps models cached -
                        so this deliberately does nothing when there is headroom.
    """
    await _require_alive()
    workflow, path = store.load_workflow(CFG, name)
    workflow = G.clone(workflow)

    schemas = await _schemas_for(workflow)
    requested = dict(params or {})
    discovered = {p.name: p for p in G.discover_params(workflow, schemas)}
    for key, value in list(requested.items()):
        if "seed" in key.rpartition(".")[2].split("@")[0] and value in (-1, "random", "-1"):
            requested[key] = await _random_seed_for(workflow, key, discovered)

    try:
        changes = G.apply_params(workflow, requested, schemas, discovered=discovered)
    except G.ParamError as exc:
        raise ComfyError(str(exc)) from exc

    converted = G.force_save_images(workflow, filename_prefix=Path(name).stem) if save_outputs else []
    payload = G.strip_meta(workflow)

    switch = CFG.free_on_switch if free_on_switch is None else free_on_switch
    freed = await _free_vram_if_starved(workflow, switch)

    record = RunProgress(workflow=name, started=time.monotonic())
    skipped: list[str] = []

    titles = _titles_of(workflow)
    if not wait:
        await _watch_in_background(payload, titles, record, timeout, skipped)
        return {
            "prompt_id": record.prompt_id,
            "workflow": name,
            "waited": False,
            "applied": changes,
            "freed_vram": freed,
            "skipped_outputs": skipped or None,
            "hint": f"call get_progress('{record.prompt_id}') to watch it, get_result when done",
        }

    history = await _run_to_completion(payload, titles, record, timeout, skipped, ctx)
    return {
        "prompt_id": record.prompt_id,
        "workflow": name,
        "source": str(path),
        "duration_s": round(record.elapsed, 1),
        "applied": changes or ["(workflow defaults)"],
        "freed_vram": freed,
        "converted_preview_nodes": converted,
        "skipped_outputs": skipped or None,
        "outputs": [ref.to_dict(CFG) for ref in collect_media(history)],
        "hint": "call show_image(path) to view a result",
    }


@tool("run", "runs")
async def run_workspace(
    wait: bool = True,
    timeout: float = CFG.run_timeout,
    client_id: str = "",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Press Queue Prompt in the browser and watch the run from here.

    The tab queues its own canvas, unsaved edits and all, so everything reacts the
    way it does when the button is clicked by hand: nodes light up, progress bars
    fill, sampler previews appear, and the result lands in the node that produced
    it. Nothing is written to the workflows directory. Use this when the user is
    working on a graph in front of them; use run_workflow for a file.

    It has to be the tab that queues. ComfyUI addresses execution events to whoever
    submitted the job, and the frontend only tracks jobs it queued itself, so a
    graph submitted from here leaves the canvas reporting someone else's run. The
    events are copied back to this server so progress is still reported; if that
    copy cannot be set up the run still happens and `progress_mirrored` says it did
    not, which is the one case where get_progress goes quiet.

    Two consequences of it being the real button. Widget callbacks run, so a seed
    set to `randomize` advances on its own - no two runs are alike unless the
    canvas says so. And the graph is not rewritten on the way out: a PreviewImage
    stays a PreviewImage and its result lands in temp/, which ComfyUI clears on
    restart. Change the node itself with set_workspace_values to keep it.

    There is no `params` argument on purpose: set_workspace_values makes the edit,
    reports what changed from and to, and leaves it as one Ctrl+Z. Folding that into
    a run would hide an edit to the user's canvas inside a call that reads as
    read-only, and spend GPU minutes before anyone had seen the change.

    Muted and bypassed nodes are dropped when the graph is converted, so a muted
    SaveImage produces no output at all. get_workspace_graph lists both under
    `issues`, which is worth a look when a run finishes with nothing to show.

    Args:
        wait: wait for the run to finish. When False, returns the prompt_id
            immediately and keeps watching in the background, so
            get_progress(prompt_id) reports steps as they happen. Prefer this for
            anything slow - waiting blind is what makes a caller mistake a working
            generation for a hung one.
        timeout: seconds to wait before giving up on a running job.
        client_id: which tab to run; defaults to the most recently focused one.
    """
    await _require_alive()

    listing = await BRIDGE.clients()
    tab = client_id or listing.get("preferred")
    if not tab:
        raise WorkspaceUnavailable(
            "No ComfyUI tab is connected, so there is no canvas to run. Call open_workspace, "
            f"or open {CFG.base_url} in a browser, or call run_workflow for a file."
        )
    for entry in listing.get("clients") or []:
        methods = entry.get("methods")
        if entry.get("client_id") == tab and methods and "queue_prompt" not in methods:
            raise WorkspaceUnavailable(
                f"The tab {tab} is running an older version of the bridge extension, which "
                "cannot queue. Reload the ComfyUI page (Ctrl+Shift+R) and try again."
            )
    mirrored = await BRIDGE.mirror(tab, CLIENT.client_id, ttl_s=timeout + 60)

    record = RunProgress(workflow="workspace", started=time.monotonic())
    skipped: list[str] = []
    queued: dict[str, Any] = {}

    titles: dict[str, str] = {}

    async def queue_in_browser() -> str:
        reply = await BRIDGE.call("queue_prompt", {}, client_id=tab)
        queued.update(reply.get("result") or {})
        titles.update(queued.get("titles") or {})
        skipped.extend(queued.get("node_errors") or [])
        return str(queued["prompt_id"])

    common: dict[str, Any] = {"client_id": tab, "workflow": "(open in the browser)"}

    if not wait:
        await _watch_in_background(None, titles, record, timeout, skipped, queue_in_browser)
        return {
            **common,
            "prompt_id": record.prompt_id,
            "node_count": queued.get("node_count"),
            "progress_mirrored": bool(mirrored.get("ok")),
            "waited": False,
            "skipped_outputs": skipped or None,
            "hint": f"call get_progress('{record.prompt_id}') to watch it, get_result when done",
        }

    history = await _run_to_completion(None, titles, record, timeout, skipped, ctx, queue_in_browser)
    node_count = queued.get("node_count") or 0
    cached = _cached_node_count(history)
    return {
        **common,
        "prompt_id": record.prompt_id,
        "node_count": node_count,
        "duration_s": round(record.elapsed, 1),
        "cached_nodes": cached,
        "progress_mirrored": bool(mirrored.get("ok")),
        "skipped_outputs": skipped or None,
        "outputs": [ref.to_dict(CFG) for ref in collect_media(history)],
        "hint": (
            "nothing was recomputed - the canvas is identical to the previous run, so these "
            "are its outputs. Change something (a new seed, or a widget set to randomize) "
            "to get a new result."
            if cached >= node_count > 0
            else "call show_image(path) to view a result"
        ),
    }


@tool("run", "reads")
async def get_result(prompt_id: str) -> dict[str, Any]:
    """Fetch the outputs of a previously submitted prompt."""
    await _require_alive()
    history = await CLIENT.history(prompt_id)
    if history is None:
        return {"prompt_id": prompt_id, "finished": False, "hint": "still queued or running"}
    status = history.get("status") or {}
    return {
        "prompt_id": prompt_id,
        "finished": True,
        "status": status.get("status_str"),
        "outputs": [ref.to_dict(CFG) for ref in collect_media(history)],
    }


@tool("run", "reads")
async def get_progress(prompt_id: str = "") -> dict[str, Any]:
    """Report how far a run has got: step, percent, elapsed time and an ETA.

    A generation takes minutes, and nothing about a long silence distinguishes real
    work from a hang - so check here instead of guessing. `silent_for_s` is the
    telling number: it stays small while the job advances, and only a large and
    growing one means something is actually wrong. Loading a model produces no steps
    for a minute or more, which is normal and shows up as `working`.

    Interrupting and re-running costs more than waiting: the models are already
    resident and a re-run pays for them again.

    Args:
        prompt_id: which run to report on. Defaults to the most recent one.
    """
    record = _PROGRESS.get(prompt_id) if prompt_id else _latest()
    if record is not None:
        result = record.to_dict()
        if record.state == "running":
            result["hint"] = "still working - poll again rather than interrupting"
        elif record.state == "done":
            result["hint"] = "finished; call show_image(path) to view an output"
        return result

    if prompt_id and await CLIENT.is_alive():
        history = await CLIENT.history(prompt_id)
        if history is not None:
            return {
                "prompt_id": prompt_id,
                "state": "done",
                "tracked": False,
                "status": (history.get("status") or {}).get("status_str"),
                "outputs": [ref.to_dict(CFG) for ref in collect_media(history)],
            }
    known = ", ".join(sorted(_PROGRESS)) or "(none)"
    return {
        "prompt_id": prompt_id,
        "state": "unknown",
        "tracked": False,
        "hint": f"no run recorded under that id. Tracked runs: {known}. Try get_queue.",
    }


@tool("run", "reads")
async def get_queue() -> dict[str, Any]:
    """Show what ComfyUI is currently running and what is queued behind it.

    Running items carry their progress when this server started them; get_progress
    gives the same detail for one run.
    """
    await _require_alive()
    queue = await CLIENT.queue()

    def summarise(items: list[Any], running: bool = False) -> list[dict[str, Any]]:
        out = []
        for item in items:
            if isinstance(item, list) and len(item) >= 2:
                entry = {"number": item[0], "prompt_id": item[1]}
                record = _PROGRESS.get(str(item[1])) if running else None
                if record is not None:
                    entry["progress"] = record.to_dict()
                out.append(entry)
        return out

    return {
        "running": summarise(queue.get("queue_running") or [], running=True),
        "pending": summarise(queue.get("queue_pending") or []),
    }


@tool("run", "runs")
async def interrupt() -> dict[str, Any]:
    """Interrupt the job ComfyUI is currently executing.

    For a run that is merely slow this is counter-productive - the steps already
    computed are lost and the models get reloaded. Check get_progress first.
    """
    await _require_alive()
    await CLIENT.interrupt()
    return {"interrupted": True}


@tool("run", "runs")
async def free_memory(unload_models: bool = True) -> dict[str, Any]:
    """Ask ComfyUI to unload models and free VRAM."""
    await _require_alive()
    await CLIENT.free(unload_models=unload_models)
    return {"freed": True, "unloaded_models": unload_models}


@tool("workflows", "reads")
def show_image(path: str, max_edge: int = CFG.preview_max_edge) -> Image:
    """Return a generated image so it can be viewed, downscaled to keep it small.

    Args:
        path: a path from run_workflow's outputs.
        max_edge: longest edge in pixels after downscaling.
    """
    target = Path(path).expanduser()
    roots = [r.resolve() for r in CFG.readable_roots]
    resolved = target.resolve()
    if not any(str(resolved).startswith(str(root)) for root in roots):
        raise ComfyError(
            f"refusing to read {resolved}: outside the ComfyUI output/temp/input folders"
        )
    if not resolved.is_file():
        raise ComfyError(f"file not found: {resolved}")

    data = resolved.read_bytes()
    try:
        from PIL import Image as PILImage
    except ImportError:
        return Image(data=data, format=resolved.suffix.lstrip(".").lower() or "png")

    with PILImage.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        if max(img.size) > max_edge:
            ratio = max_edge / max(img.size)
            img = img.resize((round(img.width * ratio), round(img.height * ratio)), PILImage.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
    return Image(data=buffer.getvalue(), format="png")


@tool("run", "writes")
async def upload_input_image(path: str, subfolder: str = "") -> dict[str, Any]:
    """Upload a local image into ComfyUI's input folder so workflows can load it.

    Returns the name to pass to a LoadImage node.
    """
    await _require_alive()
    source = Path(path).expanduser()
    if not source.is_file():
        raise ComfyError(f"file not found: {source}")
    result = await CLIENT.upload_image(source, subfolder=subfolder)
    _forget_schemas()
    name = result.get("name", source.name)
    if result.get("subfolder"):
        name = f"{result['subfolder']}/{name}"
    return {"uploaded": True, "load_image_name": name, "raw": result}


def main() -> None:
    if "--list-tools" in sys.argv:
        payload = T.catalogue(lang=LANG)
        listing = json.dumps(payload, ensure_ascii=False, indent=2)
        if not i18n.can_encode(listing, sys.stdout.encoding):
            listing = json.dumps(payload, ensure_ascii=True, indent=2)
        print(listing)
        return

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for stray in T.unknown(SELECTION):
        log.warning("COMFYUI_TOOLS names %r, which is neither a group nor a tool", stray)
    for note in T.warnings(lang=LANG):
        log.warning("%s", note)
    try:
        mcp.run()
    finally:
        try:
            asyncio.run(CLIENT.aclose())
        except RuntimeError:
            pass


if __name__ == "__main__":
    main()
