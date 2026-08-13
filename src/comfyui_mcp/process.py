"""Start and stop the portable ComfyUI instance.

Only processes started by this server are ever stopped - a ComfyUI the user
launched by hand is left alone.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import webbrowser
from pathlib import Path

from .client import ComfyClient
from .config import Config

log = logging.getLogger(__name__)


class ProcessError(RuntimeError):
    pass


def open_in_browser(url: str) -> None:
    """Hand a URL to the desktop's default browser.

    `webbrowser.open` is the whole implementation on every platform we care
    about - on Windows it is `os.startfile`, which is what a hand-written
    fallback would have called anyway. It reports failure by returning False
    rather than raising, and False is the honest answer on a headless box where
    there is no browser to open: that has to become an error here, or the
    caller waits out its whole deadline for a tab that was never going to come.

    Blocking, so callers run it off the event loop: it shells out, and what it
    shells out to is not under our control.
    """
    try:
        launched = webbrowser.open(url, new=2)
    except Exception as exc:  # noqa: BLE001 - a browser handler can fail any way it likes
        raise ProcessError(f"could not open a browser for {url}: {exc}") from exc
    if not launched:
        raise ProcessError(f"no browser is registered to open {url}; open it by hand")
    log.info("opened %s in the default browser", url)


class ComfyProcess:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def owned(self) -> bool:
        """True when we started ComfyUI and it is still alive."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self.owned and self._proc else None

    def _launch_path(self) -> Path:
        script = self.cfg.comfy_root / self.cfg.launch_script
        if not script.exists():
            raise ProcessError(
                f"launch script not found: {script}. "
                "Set COMFYUI_ROOT / COMFYUI_LAUNCH_SCRIPT to point at your install."
            )
        return script

    async def start(self, client: ComfyClient, wait: bool = True) -> dict[str, object]:
        if await client.is_alive():
            return {"started": False, "reason": "ComfyUI is already running", "owned": self.owned}

        script = self._launch_path()
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

        log.info("launching %s", script)
        self._proc = subprocess.Popen(
            ["cmd.exe", "/c", str(script)] if sys.platform == "win32" else [str(script)],
            cwd=str(self.cfg.comfy_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        if not wait:
            return {"started": True, "pid": self._proc.pid, "waited": False}

        deadline = asyncio.get_running_loop().time() + self.cfg.startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._proc.poll() is not None:
                raise ProcessError(
                    f"ComfyUI exited immediately (code {self._proc.returncode}). "
                    f"Run {script} manually to see the error."
                )
            if await client.is_alive(timeout=self.cfg.startup_poll_interval):
                return {"started": True, "pid": self._proc.pid, "waited": True}
            await asyncio.sleep(self.cfg.startup_poll_interval)

        raise ProcessError(
            f"ComfyUI did not answer on {self.cfg.base_url} within "
            f"{self.cfg.startup_timeout:.0f}s. It may still be loading."
        )

    async def stop(self) -> dict[str, object]:
        if not self.owned or self._proc is None:
            return {"stopped": False, "reason": "no ComfyUI process owned by this server"}

        pid = self._proc.pid
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        else:
            self._proc.terminate()

        try:
            await asyncio.wait_for(asyncio.to_thread(self._proc.wait), timeout=self.cfg.stop_grace)
        except asyncio.TimeoutError:
            self._proc.kill()
        self._proc = None
        return {"stopped": True, "pid": pid}
