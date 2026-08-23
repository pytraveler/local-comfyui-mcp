"""Start and stop the portable ComfyUI instance.

Only processes started by this server are ever stopped - a ComfyUI the user
launched by hand is left alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Any

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


def process_started_at(pid: int) -> str | None:
    """When `pid` started, as a token to compare against. None when it is not there.

    This is what makes a recorded pid safe to act on later. Pids are reused - on
    Windows readily, and after a reboot on every platform - so a lock file naming
    one is naming a *number*, and the number alone could by then belong to
    somebody's editor. The start time turns it into an identity: a reused pid
    carries a different one, and the mismatch is what refuses the adoption.

    The check is deliberately "does the token match", never "does the pid open".
    A process that has just exited can still be opened while any handle to it
    survives, so openability answers a different question than the one asked.

    None also means "cannot tell", and every caller must treat that as a refusal
    rather than a maybe - which is what an unrecognised platform gets.
    """
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0x0
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
        )
        if not handle:
            return None
        try:
            if kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0:
                return None
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return None
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)

    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        except (OSError, IndexError):
            return None
        if not fields:
            return None
        if fields[0] == "Z":
            return None
        return fields[19] if len(fields) > 19 else None

    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ps", "-o", "state=,lstart=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        line = out.stdout.strip()
        if not line or line.startswith("Z"):
            return None
        return line.split(None, 1)[1].strip() if " " in line else None

    return None


class ComfyProcess:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._proc: subprocess.Popen[bytes] | None = None
        self._adopted: tuple[int, str] | None = None

    @property
    def owned(self) -> bool:
        """True when this server started ComfyUI and it is still alive.

        Ownership is the gate on every destructive path here, and it is
        deliberately not "is something answering the port": a ComfyUI the user
        started by hand answers it just the same, and is not ours to touch.
        """
        if self._proc is not None:
            return self._proc.poll() is None
        if self._adopted is not None:
            pid, token = self._adopted
            return process_started_at(pid) == token
        return False

    @property
    def pid(self) -> int | None:
        if not self.owned:
            return None
        if self._proc is not None:
            return self._proc.pid
        return self._adopted[0] if self._adopted else None

    def _lock_path(self) -> Path:
        """Where the pid of a ComfyUI we started is recorded.

        Keyed by port, because that is what identifies the instance: two checkouts
        pointed at one ComfyUI should find each other's record, and two pointed at
        different ports should not.

        A plain file, managed by hand. `tempfile`'s auto-deleting kinds are exactly
        wrong for this - measured: a file opened with `delete=True` is removed when
        the process is killed, which is the one case the record exists to survive.
        """
        return Path(tempfile.gettempdir()) / f"comfyui_mcp_{self.cfg.port}.lock"

    def _write_lock(self, pid: int) -> None:
        token = process_started_at(pid)
        if token is None:
            log.debug("no start-time token for pid %s; not recording ownership", pid)
            return
        try:
            self._lock_path().write_text(
                json.dumps({"pid": pid, "started_at": token, "port": self.cfg.port}),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("could not record ownership of pid %s: %s", pid, exc)

    def _clear_lock(self) -> None:
        self._lock_path().unlink(missing_ok=True)

    def adopt(self) -> dict[str, object]:
        """Take back a ComfyUI this server started before it was restarted.

        A hard kill of this server leaves ComfyUI running with nothing driving it,
        and until now that orphan was also unreachable: ownership lived only in
        memory, so `comfy_stop` refused the very process it had started. This is
        what closes that - the record on disk is checked, and a match restores
        ownership so the user can stop it when they choose.

        It restores ownership; it does not act on it. Killing whatever is found at
        startup would be the obvious shortcut and is the wrong one twice over: a
        generation survives an editor crash and can still be running here - runs of
        twenty minutes and more are ordinary - and two MCP clients open at once
        would each shoot down the other's ComfyUI on launch.

        A record that does not verify is deleted rather than kept: it names a pid
        that is gone, or one that now belongs to somebody else, and neither is
        something to try again later.
        """
        path = self._lock_path()
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            pid, token = int(record["pid"]), str(record["started_at"])
        except FileNotFoundError:
            return {"adopted": False, "reason": "no record of a ComfyUI started by this server"}
        except (OSError, ValueError, KeyError, TypeError):
            self._clear_lock()
            return {"adopted": False, "reason": "the ownership record was unreadable"}

        if process_started_at(pid) != token:
            self._clear_lock()
            return {"adopted": False, "pid": pid, "reason": "that process is gone or is no longer ours"}

        self._adopted = (pid, token)
        log.info("adopted the ComfyUI this server started earlier (pid %s)", pid)
        return {"adopted": True, "pid": pid}

    def _launch_path(self) -> Path:
        root = self.cfg.comfy_root.resolve()
        script = (self.cfg.comfy_root / self.cfg.launch_script).resolve()
        if not script.is_relative_to(root):
            raise ProcessError(
                f"launch script {script} is outside {root}. COMFYUI_LAUNCH_SCRIPT names a "
                "file inside the ComfyUI folder; point COMFYUI_ROOT at the install that "
                "holds it rather than reaching out of it."
            )
        if not script.exists():
            raise ProcessError(
                f"launch script not found: {script}. "
                "Set COMFYUI_ROOT / COMFYUI_LAUNCH_SCRIPT to point at your install."
            )
        return script

    @staticmethod
    def _command(script: Path) -> list[str]:
        """How to start this script, which its extension decides.

        `cmd.exe /c` runs a .bat and cannot run a .ps1: Windows ships no
        association for .ps1 at all - `assoc .ps1` is empty on 11 - so cmd would
        hand it to whatever the user has registered, or to nothing. PowerShell is
        therefore invoked outright, with the flags install_node.bat already uses.

        **`-ExecutionPolicy Bypass` applies to this one process** and alters
        nothing on the machine. The alternative is telling somebody to loosen a
        machine-wide policy to start ComfyUI, which is a far worse trade than
        running the script they themselves chose in the settings window.

        `powershell.exe` rather than `pwsh`: a launcher written for a portable
        build is Windows PowerShell's, and pwsh need not be installed.
        """
        if script.suffix.lower() == ".ps1":
            return [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ]
        if sys.platform == "win32":
            return ["cmd.exe", "/c", str(script)]
        return [str(script)]

    async def start(self, client: ComfyClient, wait: bool = True) -> dict[str, object]:
        if await client.is_alive():
            return {"started": False, "reason": "ComfyUI is already running", "owned": self.owned}

        script = self._launch_path()
        extra: dict[str, Any] = {}
        if sys.platform == "win32":
            extra["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            extra["start_new_session"] = True

        log.info("launching %s", script)
        self._proc = subprocess.Popen(
            self._command(script),
            cwd=str(self.cfg.comfy_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **extra,
        )

        self._adopted = None
        self._write_lock(self._proc.pid)

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

    @staticmethod
    def _taskkill_command(pid: int, force: bool) -> list[str]:
        """taskkill's argv for the tree under `pid`.

        `/T` is the part that reaches ComfyUI. What we hold is the `cmd.exe` or
        `powershell.exe` the launch script runs in; python is its child, so killing
        the pid alone leaves python reparented, holding the port and the GPU.

        Without `/F` taskkill asks rather than insists, which is a real request a
        console app can refuse - and it says so with a non-zero exit code, which is
        what lets `stop` escalate at once instead of sitting out the whole grace
        period for a signal that was never going to be honoured.
        """
        argv = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            argv.append("/F")
        return argv

    async def _ask_to_stop(self, pid: int) -> bool:
        """Ask the tree to end. False when the ask could not be delivered at all.

        The two platforms disagree about what "ask" means, and the difference is
        the whole reason this is not one call: POSIX has SIGTERM, which every
        process can act on, while Windows has no signal and taskkill's polite form
        talks to windows - which a ComfyUI started with CREATE_NO_WINDOW does not
        have. So the Windows ask frequently fails, and a False here is ordinary
        rather than exceptional.
        """
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                *self._taskkill_command(pid, force=False),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await proc.wait() == 0
        return self._signal_group(pid, signal.SIGTERM)

    async def _force_stop(self, pid: int) -> None:
        """End the tree without asking."""
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                *self._taskkill_command(pid, force=True),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return
        self._signal_group(pid, signal.SIGKILL)

    @staticmethod
    def _signal_group(pid: int, sig: int) -> bool:
        """Signal the whole process group. False when there is nothing left to signal.

        `start_new_session` at launch is what makes the group exist and makes the pid
        its leader, so `getpgid(pid)` is the tree. A process that has already exited
        raises rather than returning anything, and that is a normal race here: the
        script may well have finished between the check and the signal.
        """
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    async def _reap(self, timeout: float) -> bool:
        """Wait for the process to exit. False on timeout.

        An adopted process is not our child, so there is no `wait` to call on it and
        no exit status to collect - only the start-time token, polled until it stops
        matching. That is the same test `owned` uses, so the two cannot disagree.
        """
        if self._proc is not None:
            try:
                await asyncio.wait_for(asyncio.to_thread(self._proc.wait), timeout=timeout)
            except asyncio.TimeoutError:
                return False
            return True

        if self._adopted is None:
            return True
        pid, token = self._adopted
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if process_started_at(pid) != token:
                return True
            await asyncio.sleep(0.1)
        return process_started_at(pid) != token

    async def stop(self) -> dict[str, object]:
        """End the ComfyUI this server started, and only that one.

        A ComfyUI the user launched by hand has no `_proc` behind it and is left
        alone - that is the rule the whole module exists to keep, and it is why the
        guard is on ownership rather than on whether anything is answering the port.

        Asked first, forced second. The previous version went straight to a hard
        kill, which costs ComfyUI its chance to finish writing whatever it had open;
        the escalation is skipped only when the ask could not be delivered, so a
        Windows console app does not buy a full grace period of waiting for nothing.
        """
        pid = self.pid
        if pid is None:
            return {"stopped": False, "reason": "no ComfyUI process owned by this server"}
        asked = await self._ask_to_stop(pid)
        forced = False
        if not asked or not await self._reap(self.cfg.stop_grace):
            forced = True
            await self._force_stop(pid)
            await self._reap(self.cfg.stop_grace)

        self._proc = None
        self._adopted = None
        self._clear_lock()
        log.info("stopped ComfyUI (pid %s, forced=%s)", pid, forced)
        return {"stopped": True, "pid": pid, "forced": forced}
