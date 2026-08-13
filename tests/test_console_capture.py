"""Run the browser half's own checks, when there is a node to run them with.

The capture section of `mcp_bridge.js` is the only JavaScript here that can be
tested at all - everything else in that file needs a live litegraph graph. It is
also the part where a fault is silent: a formatter that drops an error still logs
*something*, and nobody notices until the log is the only evidence left.

Skipped rather than failed when node is absent. This project's only hard
dependency is uv, and it should stay that way; a red line meaning "you do not
have node" would be worse than no line at all.

Skipped for the same reason on a built checkout. A release ships
`web/mcp_bridge.js` alone - no `src/`, no build scripts - and the capture section
is found in the source by a comment marker that minification deletes, so there is
nothing to lift out. The script says so itself by exiting 3; whether a source is
present is its question to answer, not a second copy of the path here.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from comfyui_mcp.config import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts" / "check_console.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_the_console_capture_passes_its_own_checks():
    result = subprocess.run(
        [shutil.which("node") or "node", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode == 3:
        pytest.skip(result.stdout.strip() or "no bridge source in this checkout")
    assert result.returncode == 0, result.stdout + result.stderr
