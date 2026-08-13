"""Discovery against a SamplerCustomAdvanced graph.

krea2 and the upscaler both put seed, cfg and conditioning on the sampler node
itself. This one does not: SamplerCustomAdvanced links out to a RandomNoise for the
seed and a guider for the prompt and cfg, which is the shape every recent flux-style
workflow uses. Scanning the sampler alone finds neither.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfyui_mcp import graph as G

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
IDEOGRAM = WORKFLOWS / "image_ideogram4_t2i_int8_default.json"

pytestmark = pytest.mark.skipif(not IDEOGRAM.exists(), reason="ideogram4 workflow not present")


@pytest.fixture
def ideogram() -> G.Graph:
    return json.loads(IDEOGRAM.read_text(encoding="utf-8"))


def params_by_name(workflow: G.Graph, schemas: G.Schemas | None = None) -> dict[str, G.Param]:
    return {p.name: p for p in G.discover_params(workflow, schemas)}


def test_seed_comes_from_the_linked_noise_node(ideogram):
    seed = params_by_name(ideogram)["seed"]
    assert (seed.node_id, seed.input) == ("98:18", "noise_seed")


def test_prompt_and_cfg_come_from_the_guider(ideogram):
    found = params_by_name(ideogram)
    assert (found["prompt"].node_id, found["prompt"].input) == ("98:24", "text")
    assert (found["cfg"].node_id, found["cfg"].input) == ("98:155", "cfg")


def test_the_unconditional_branch_is_not_a_negative_prompt(ideogram):
    """The guider's negative input is a ConditioningZeroOut of the positive one."""
    assert "negative_prompt" not in params_by_name(ideogram)


def test_prompt_holds_a_json_caption(ideogram):
    """What makes this workflow need a guide: the prompt is structured, not prose."""
    caption = json.loads(params_by_name(ideogram)["prompt"].value)
    assert set(caption) == {"aspect_ratio", "high_level_description", "compositional_deconstruction"}


def test_applying_the_prompt_by_name_matches_the_raw_path(ideogram):
    by_name, by_path = G.clone(ideogram), G.clone(ideogram)
    G.apply_params(by_name, {"prompt": '{"aspect_ratio":"1:1"}'})
    G.apply_params(by_path, {"98:24.text": '{"aspect_ratio":"1:1"}'})
    assert by_name == by_path


def test_seed_range_is_the_noise_node_own(ideogram):
    """RandomNoise takes the full 2**64, unlike the 'easy seed' krea2 uses."""
    schemas = {"RandomNoise": {"input": {"required": {"noise_seed": ["INT", {"max": 2**64 - 1}]}}}}
    assert params_by_name(ideogram, schemas)["seed"].spec.maximum == 2**64 - 1


def test_scheduler_side_of_the_sampler_is_scanned_too():
    """The generic case: BasicScheduler holds the steps SamplerCustomAdvanced lacks."""
    graph = {
        "1": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["2", 0], "guider": ["3", 0], "sampler": ["4", 0], "sigmas": ["5", 0],
            },
        },
        "2": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}},
        "3": {"class_type": "CFGGuider", "inputs": {"positive": ["6", 0], "cfg": 3.5}},
        "4": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "5": {"class_type": "BasicScheduler", "inputs": {"scheduler": "simple", "steps": 20, "denoise": 1.0}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a fox"}},
    }
    found = params_by_name(graph)
    assert found["seed"].value == 42
    assert found["steps"].value == 20
    assert found["cfg"].value == 3.5
    assert found["prompt"].value == "a fox"
    assert found["scheduler"].value == "simple"
