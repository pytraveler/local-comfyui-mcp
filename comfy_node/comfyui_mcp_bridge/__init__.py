"""ComfyUI side of the MCP workspace bridge.

This package declares no nodes. It exists for the two things a custom node can do
that nothing else can: register HTTP routes on ComfyUI's own server, and ship a
JavaScript extension into the page. Together those turn the WebSocket ComfyUI
already holds open to every browser tab into an RPC channel the MCP server can
call - see `bridge.py`.

NODE_CLASS_MAPPINGS stays empty on purpose; it is present because ComfyUI treats a
module without it as a failed load and logs accordingly.
"""

from .bridge import PREFIX, PROTOCOL  # noqa: F401  (imported for its side effects: route registration)

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
