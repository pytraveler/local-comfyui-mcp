"""Turning a launch script into a command, and what is refused before it runs.

Everything else in `process.py` needs a real ComfyUI, but these two decisions are
pure: which interpreter an extension implies, and whether the path stays inside
the install.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

from comfyui_mcp.config import load_config
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
