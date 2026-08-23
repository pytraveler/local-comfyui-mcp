"""Turning a launch script into a command, what is refused, and how it is stopped.

Most of `process.py` needs a real ComfyUI, but the decisions are pure: which
interpreter an extension implies, whether the path stays inside the install, and
the ask-then-force escalation `stop` walks. The last is exercised against a fake
process, because the property worth pinning - that a ComfyUI we did not start is
never signalled - must hold on a machine with no ComfyUI on it at all.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from comfyui_mcp.config import load_config
from comfyui_mcp import process as process_mod
from comfyui_mcp.process import ComfyProcess, ProcessError


def rooted(tmp_path: Path, script: str) -> ComfyProcess:
    return ComfyProcess(dataclasses.replace(load_config(), comfy_root=tmp_path, launch_script=script))


def test_a_powershell_launcher_is_run_by_powershell(tmp_path: Path):
    command = ComfyProcess._command(tmp_path / "run_multigpu.ps1")
    assert command[0] == "powershell.exe"
    assert "-File" in command
    assert command[-1].endswith("run_multigpu.ps1")


def test_the_policy_is_bypassed_for_this_process_and_nothing_else(tmp_path: Path):
    """The alternative is telling somebody to loosen a machine-wide policy."""
    command = ComfyProcess._command(tmp_path / "run.ps1")
    assert command[command.index("-ExecutionPolicy") + 1] == "Bypass"
    assert "-NoProfile" in command


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe is the Windows branch")
def test_a_batch_file_still_goes_through_cmd(tmp_path: Path):
    assert ComfyProcess._command(tmp_path / "run.bat")[:2] == ["cmd.exe", "/c"]


def test_a_script_outside_the_root_is_refused(tmp_path: Path):
    outside = tmp_path.parent / "elsewhere.bat"
    outside.write_text("", encoding="utf-8")
    with pytest.raises(ProcessError, match="outside"):
        rooted(tmp_path, str(outside))._launch_path()


def test_traversal_out_of_the_root_is_refused_too(tmp_path: Path):
    with pytest.raises(ProcessError, match="outside"):
        rooted(tmp_path, "../elsewhere.bat")._launch_path()


def test_a_script_inside_the_root_resolves(tmp_path: Path):
    (tmp_path / "run_nvidia_gpu.bat").write_text("", encoding="utf-8")
    assert rooted(tmp_path, "run_nvidia_gpu.bat")._launch_path().name == "run_nvidia_gpu.bat"


def test_a_missing_script_says_which_one(tmp_path: Path):
    with pytest.raises(ProcessError, match="not found"):
        rooted(tmp_path, "run_nvidia_gpu.bat")._launch_path()


class FakeProc:
    """A Popen stand-in that exits only when told to."""

    def __init__(self, pid: int = 4321, exits_on_ask: bool = True) -> None:
        self.pid = pid
        self.exits_on_ask = exits_on_ask
        self._dead = False

    def poll(self):
        return 0 if self._dead else None

    def wait(self):
        while not self._dead:
            time.sleep(0.01)
        return 0


def armed(tmp_path: Path, proc: FakeProc, grace: float = 0.05) -> ComfyProcess:
    p = ComfyProcess(dataclasses.replace(load_config(), comfy_root=tmp_path, stop_grace=grace))
    p._proc = proc
    return p


@pytest.fixture
def signals(monkeypatch):
    """Record what stop() would have sent, and let the test decide the outcome."""
    sent: list[tuple[str, int]] = []

    async def ask(self, pid):
        sent.append(("ask", pid))
        if self._proc.exits_on_ask:
            self._proc._dead = True
        return self._proc.exits_on_ask

    async def force(self, pid):
        sent.append(("force", pid))
        self._proc._dead = True

    monkeypatch.setattr(ComfyProcess, "_ask_to_stop", ask)
    monkeypatch.setattr(ComfyProcess, "_force_stop", force)
    return sent


def test_a_comfyui_we_did_not_start_is_never_signalled(tmp_path: Path, signals):
    result = asyncio.run(ComfyProcess(dataclasses.replace(load_config(), comfy_root=tmp_path)).stop())
    assert result["stopped"] is False
    assert signals == []


def test_stopping_asks_before_it_forces(tmp_path: Path, signals):
    result = asyncio.run(armed(tmp_path, FakeProc()).stop())
    assert signals == [("ask", 4321)]
    assert result["stopped"] is True and result["forced"] is False


def test_a_process_that_ignores_the_ask_is_forced(tmp_path: Path, signals):
    result = asyncio.run(armed(tmp_path, FakeProc(exits_on_ask=False)).stop())
    assert signals == [("ask", 4321), ("force", 4321)]
    assert result["forced"] is True


def test_stopping_twice_does_nothing_the_second_time(tmp_path: Path, signals):
    p = armed(tmp_path, FakeProc())
    assert asyncio.run(p.stop())["stopped"] is True
    assert asyncio.run(p.stop())["stopped"] is False
    assert signals == [("ask", 4321)]


def test_taskkill_walks_the_tree_because_python_is_the_grandchild(tmp_path: Path):
    assert ComfyProcess._taskkill_command(99, force=False) == ["taskkill", "/PID", "99", "/T"]
    assert ComfyProcess._taskkill_command(99, force=True)[-1] == "/F"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals the process group")
def test_a_dead_process_group_is_not_an_error(tmp_path: Path):
    """The script finishing between the check and the signal is an ordinary race."""
    assert ComfyProcess._signal_group(2**22, signal.SIGTERM) is False


@pytest.fixture(autouse=True)
def never_the_real_lock(tmp_path, monkeypatch):
    """Keep every test off the machine's own ownership record.

    `stop()` clears the record, and the helpers below build a ComfyProcess from the
    real config - so without this the suite deletes the lock file belonging to a
    ComfyUI actually running on the developer's machine, and the next server start
    fails to adopt it. Caught exactly that way.
    """
    monkeypatch.setattr(ComfyProcess, "_lock_path", lambda self: tmp_path / "owned.lock")


@pytest.fixture
def lock(tmp_path, monkeypatch):
    """The ownership record, plus a fake identity probe whose answers the test sets."""
    world: dict[int, str] = {}
    monkeypatch.setattr(process_mod, "process_started_at", lambda pid: world.get(pid))
    return tmp_path / "owned.lock", world


def fresh(tmp_path: Path) -> ComfyProcess:
    return ComfyProcess(dataclasses.replace(load_config(), comfy_root=tmp_path))


def test_a_recorded_process_is_taken_back(tmp_path: Path, lock):
    path, world = lock
    world[777] = "started-at-noon"
    path.write_text(json.dumps({"pid": 777, "started_at": "started-at-noon"}), encoding="utf-8")

    p = fresh(tmp_path)
    assert p.adopt() == {"adopted": True, "pid": 777}
    assert p.owned is True and p.pid == 777


def test_a_reused_pid_is_refused_and_the_record_dropped(tmp_path: Path, lock):
    path, world = lock
    world[777] = "a-completely-different-process"
    path.write_text(json.dumps({"pid": 777, "started_at": "started-at-noon"}), encoding="utf-8")

    p = fresh(tmp_path)
    assert p.adopt()["adopted"] is False
    assert p.owned is False and p.pid is None
    assert not path.exists()


def test_a_process_that_has_since_exited_is_refused(tmp_path: Path, lock):
    path, _ = lock
    path.write_text(json.dumps({"pid": 777, "started_at": "started-at-noon"}), encoding="utf-8")
    p = fresh(tmp_path)
    assert p.adopt()["adopted"] is False
    assert not path.exists()


def test_no_record_is_not_an_error(tmp_path: Path, lock):
    p = fresh(tmp_path)
    assert p.adopt()["adopted"] is False
    assert p.owned is False


def test_an_unreadable_record_is_dropped_rather_than_retried(tmp_path: Path, lock):
    path, _ = lock
    path.write_text("{ this is not json", encoding="utf-8")
    assert fresh(tmp_path).adopt()["adopted"] is False
    assert not path.exists()


def test_stopping_an_adopted_process_signals_it_and_clears_the_record(tmp_path: Path, lock, signals):
    path, world = lock
    world[777] = "started-at-noon"
    path.write_text(json.dumps({"pid": 777, "started_at": "started-at-noon"}), encoding="utf-8")

    p = fresh(tmp_path)
    p.adopt()

    async def ask(self, pid):
        signals.append(("ask", pid))
        world.pop(pid, None)          # it honoured the ask and exited
        return True

    p._ask_to_stop = ask.__get__(p)
    assert asyncio.run(p.stop()) == {"stopped": True, "pid": 777, "forced": False}
    assert not path.exists()
    assert p.owned is False


def test_adoption_never_reaches_a_comfyui_we_did_not_start(tmp_path: Path, lock, signals):
    """No record means no ownership, whatever is answering the port."""
    p = fresh(tmp_path)
    p.adopt()
    assert asyncio.run(p.stop())["stopped"] is False
    assert signals == []


def test_an_exited_process_is_not_reported_as_running():
    """Found live: comfy_status said started_by_mcp while ComfyUI was already gone.

    A process object outlives the process while any handle to it survives - and
    `Popen` holds one - so "can I open it and read its start time" kept answering
    for a corpse. Ownership is built on this, so the probe has to mean *running*.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert process_mod.process_started_at(proc.pid) is not None
        proc.kill()
        proc.wait()
        assert process_mod.process_started_at(proc.pid) is None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_a_pid_that_never_existed_has_no_token():
    assert process_mod.process_started_at(4194300) is None
