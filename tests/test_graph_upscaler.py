"""Discovery against a second, differently-shaped workflow.

The krea2 graph is text-to-image with heavy switch indirection; this one is an
img2img upscaler with a real negative prompt, an image entry point and repeated
node types. Between them they cover the shapes discovery has to handle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfyui_mcp import graph as G

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
UPSCALER = WORKFLOWS / "Fluxmania V IMG2IMG Upscaler 2.json"

pytestmark = pytest.mark.skipif(not UPSCALER.exists(), reason="upscaler workflow not present")


@pytest.fixture
def upscaler() -> G.Graph:
    return json.loads(UPSCALER.read_text(encoding="utf-8"))


def params_by_name(workflow: G.Graph) -> dict[str, G.Param]:
    return {p.name: p for p in G.discover_params(workflow)}


def test_load_image_is_the_img2img_entry_point(upscaler):
    """Without this, feeding an image in needs a raw '143.image' path."""
    image = params_by_name(upscaler)["image"]
    assert (image.node_id, image.input) == ("143", "image")
    assert image.type == "combo"


def test_real_negative_prompt_is_discovered(upscaler):
    """Unlike krea2, this graph has a genuine negative encoder rather than a zero-out."""
    found = params_by_name(upscaler)
    assert found["prompt"].node_id == "6"
    assert found["negative_prompt"].node_id == "7"


def test_upscaler_knobs(upscaler):
    found = params_by_name(upscaler)
    assert found["upscale_model"].value == "4x_foolhardy_Remacri.pth"
    assert found["max_size"].value == 1280
    assert found["guidance"].value == 3.5
    assert found["denoise"].value == 0.75


def test_repeated_node_types_get_distinct_names(upscaler):
    """Two ImageScaleBy nodes must both be reachable."""
    found = params_by_name(upscaler)
    assert found["scale_by"].node_id == "102"
    assert found["scale_by@148"].node_id == "148"
    assert found["scale_by"].type == "float"


def test_dual_clip_loader_gguf_variant(upscaler):
    found = params_by_name(upscaler)
    assert found["clip1"].value == "t5xxl_fp16.safetensors"
    assert found["clip2"].value == "clip_l.safetensors"


def test_applying_the_image_by_name_matches_the_raw_path(upscaler):
    by_name = G.clone(upscaler)
    by_path = G.clone(upscaler)
    G.apply_params(by_name, {"image": "mcp/photo.png"})
    G.apply_params(by_path, {"143.image": "mcp/photo.png"})
    assert by_name["143"]["inputs"]["image"] == "mcp/photo.png"
    assert by_name == by_path


def test_scale_by_stays_a_float(upscaler):
    G.apply_params(upscaler, {"scale_by": 1})
    assert isinstance(upscaler["102"]["inputs"]["scale_by"], float)


def test_outputs_include_both_preview_stages(upscaler):
    assert [o["node_id"] for o in G.output_nodes(upscaler)] == ["161", "162"]
