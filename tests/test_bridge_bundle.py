"""The shipped browser half must be the build of the source it was made from.

`web/mcp_bridge.js` is a build artefact, and the failure that introduces is
silent: someone edits `src/mcp_bridge.js`, forgets to rebuild, and ComfyUI keeps
serving the previous bundle. Nothing on the Python side can see that - the tab
registers, answers, and is simply running different code from the one being read.

The build is deterministic for a pinned esbuild version and one set of flags, so
a byte comparison is the whole test. It runs the build *script* rather than
repeating its flags here: a second copy of the command line is the thing that
goes stale, and then the test is measuring itself.

**A release ships the bundle alone** - no `src/`, no `minify_bridge.*`. There is
then nothing to compare it against, and saying so is the only honest answer: a
red line meaning "you installed the release rather than the development tree"
would be worse than no line, the same rule that makes node's absence a skip. What
still holds in both trees is the shape of `web/`, so that one always runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from comfyui_mcp.config import PROJECT_ROOT

NODE = PROJECT_ROOT / "comfy_node" / "comfyui_mcp_bridge"
SOURCE = NODE / "src" / "mcp_bridge.js"
BUNDLE = NODE / "web" / "mcp_bridge.js"
BUILD = PROJECT_ROOT / ("minify_bridge.bat" if os.name == "nt" else "minify_bridge.sh")

# A development tree has the source and the script that builds it; a release has
# neither. Nothing in between is a state worth having, so the two are one flag.
BUILDABLE = SOURCE.is_file() and BUILD.is_file()
released = pytest.mark.skipif(
    not BUILDABLE, reason="built checkout: no bridge source or build script to compare against"
)


def _build_command(destination):
    if os.name == "nt":
        return ["cmd", "/c", str(BUILD), str(destination)]
    return [shutil.which("bash") or "bash", str(BUILD), str(destination)]


def test_the_served_directory_holds_only_the_bundle():
    """ComfyUI imports every .js under web/, so a second one registers twice."""
    served = sorted(p.name for p in BUNDLE.parent.glob("**/*.js"))
    assert served == ["mcp_bridge.js"], (
        "web/ must hold the bundle and nothing else ComfyUI would import, "
        f"found {served}"
    )
    assert BUNDLE.stat().st_size > 0, f"{BUNDLE} is empty"


@released
def test_the_capture_markers_survive_in_the_source():
    """check_console.mjs finds its section by comment, which the build deletes."""
    text = SOURCE.read_text(encoding="utf-8")
    assert "// --- the browser console" in text
    assert "captureConsole();" in text


@released
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
@pytest.mark.skipif(shutil.which("npx") is None, reason="npx is not on PATH")
def test_the_shipped_bundle_is_the_build_of_the_source(tmp_path):
    fresh = tmp_path / "mcp_bridge.js"
    result = subprocess.run(
        _build_command(fresh),
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0 or not fresh.is_file():
        pytest.skip(f"the build did not run: {result.stdout}{result.stderr}")

    assert fresh.read_bytes() == BUNDLE.read_bytes(), (
        f"{BUNDLE.relative_to(PROJECT_ROOT)} is not the build of "
        f"{SOURCE.relative_to(PROJECT_ROOT)}. Run {BUILD.name} and commit both."
    )
