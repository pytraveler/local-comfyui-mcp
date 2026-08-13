"""Discovery against a WanVideo image-to-video graph.

The three image workflows all hand their sampler a `latent_image` and a pair of
conditioning links. This one hands `WanVideoSamplerv2` a single `text_embeds` link
carrying both prompts, an `image_embeds` link carrying the frame count, and a
`scheduler` node carrying steps and shift - and runs three samplers in sequence off
one shared seed. Between the four, discovery sees every shape it has to handle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfyui_mcp import graph as G

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
VIDEO = WORKFLOWS / "video_wan2_2_14B_i2v_3_samplers_kj.json"

pytestmark = pytest.mark.skipif(not VIDEO.exists(), reason="video workflow not present")


@pytest.fixture
def video() -> G.Graph:
    return json.loads(VIDEO.read_text(encoding="utf-8"))


def params_by_name(workflow: G.Graph) -> dict[str, G.Param]:
    return {p.name: p for p in G.discover_params(workflow)}


def test_both_prompts_come_off_one_text_embeds_link(video):
    """WanVideoTextEncode holds the pair; there is no positive/negative to walk."""
    found = params_by_name(video)
    assert (found["prompt"].node_id, found["prompt"].input) == ("201", "value")
    assert (found["negative_prompt"].node_id, found["negative_prompt"].input) == ("202", "value")


def test_steps_come_from_the_scheduler_node(video):
    steps = params_by_name(video)["steps"]
    assert (steps.node_id, steps.input, steps.value) == ("194", "value", 14)


def test_frame_count_comes_through_the_image_embeds_switch(video):
    """322 is an Any Switch; stopping there would hide the encoder behind it."""
    frames = params_by_name(video)["num_frames"]
    assert (frames.node_id, frames.input, frames.value) == ("193", "value", 81)


def test_computed_geometry_is_not_offered_as_a_knob(video):
    """width/height are outputs of the crop node, so there is nothing to write."""
    found = params_by_name(video)
    assert "width" not in found and "height" not in found


def test_three_samplers_share_one_seed_node(video):
    """Both HIGH samplers read node 220, so the literal is exposed once."""
    found = params_by_name(video)
    assert (found["seed"].node_id, found["seed"].input) == ("220", "seed")
    # The LOW sampler was authored with its own literal, so it stays distinct.
    assert (found["seed@224"].node_id, found["seed@224"].input) == ("224", "seed")


def test_shift_is_named_once_per_distinct_target(video):
    found = params_by_name(video)
    assert found["shift"].node_id == "195"
    assert found["shift@313"].node_id == "313"


def test_node_title_disambiguates_the_suffixed_names(video):
    """`shift` vs `shift@313` says nothing on its own; the author's titles do."""
    found = params_by_name(video)
    assert found["shift"].to_dict()["node_title"] == "Sampler shift (Clean model)"
    assert found["shift@313"].to_dict()["node_title"] == "Sampler shift"


def test_wan_loaders_are_named(video):
    found = params_by_name(video)
    assert found["model"].value == "Wan2_2-I2V-A14B-HIGH_bf16.safetensors"
    assert found["model@164"].to_dict()["node_title"] == "LOW noise"
    assert found["vae"].value == "Wan2_1_VAE_fp32.safetensors"
    assert found["text_encoder"].value == "nsfw_wan_umt5-xxl_bf16.safetensors"


def test_video_specific_knobs(video):
    found = params_by_name(video)
    assert found["image"].node_id == "188"
    assert found["max_side_length"].value == 640
    assert found["frame_rate"].value == 16


def test_flat_lora_slots_are_read_like_a_power_lora_loader(video):
    """WanVideoLoraSelectMulti stores lora_N/strength_N pairs, not a dict per slot."""
    loras = params_by_name(video)["loras@167"]
    assert loras.type == "loras"
    assert loras.to_dict()["node_title"] == "LOW LoRAs"
    available = {e["lora"].rsplit("\\", 1)[-1]: e for e in loras.extra["available"]}
    # A slot reading "none" is empty, not a lora named "none".
    assert "none" not in available
    assert "W25_Realistic_I2V_LOW_v2.safetensors" in available
    # This graph ships every slot at strength 0, which is a slot doing nothing.
    assert available["W25_Realistic_I2V_LOW_v2.safetensors"]["on"] is False
    assert loras.value == []


def test_a_loader_with_no_lora_in_any_slot_is_not_offered():
    """A loader dragged in but never filled would promise a knob that does nothing."""
    empty = {
        "1": {"class_type": "WanVideoLoraSelectMulti", "inputs": {"lora_0": "none", "strength_0": 1.0}},
        "2": {"class_type": "Power Lora Loader (rgthree)", "inputs": {"➕ Add Lora": ""}},
    }
    assert [p for p in G.discover_params(empty) if p.type == "loras"] == []


def test_enabling_a_flat_lora_sets_its_strength_and_zeroes_the_rest(video):
    G.apply_params(video, {"loras@166": [{"lora": "W25_Realistic_I2V_HIGH", "strength": 0.7}]})
    strengths = {k: v for k, v in video["166"]["inputs"].items() if k.startswith("strength_")}
    assert strengths == {
        "strength_0": 0.0, "strength_1": 0.7, "strength_2": 0.0, "strength_3": 0.0, "strength_4": 0
    }


def test_disabling_a_flat_lora_keeps_the_name_it_matched_on(video):
    """Writing "none" would erase what the next call has to match against."""
    G.apply_params(video, {"loras@167": [{"lora": "W25_Realistic_I2V_LOW", "strength": 0.5}]})
    G.apply_params(video, {"loras@167": []})
    assert video["167"]["inputs"]["lora_1"].endswith("W25_Realistic_I2V_LOW_v2.safetensors")
    assert video["167"]["inputs"]["strength_1"] == 0.0


def test_a_bare_lora_name_defaults_to_full_strength(video):
    """A flat slot has no on/off flag, so an enabled one needs a strength."""
    G.apply_params(video, {"loras": ["PusaV1_lora_HIGH"]})
    assert video["165"]["inputs"]["strength_2"] == 1.0


def test_required_models_finds_the_wrapper_loaders(video):
    """The WanVideo loaders call their input `model`/`model_name`, not `unet_name`."""
    files = {m["file"] for m in G.required_models(video)}
    assert "Wan2_1_VAE_fp32.safetensors" in files
    assert "wan2.2_i2v_A14b_low_noise_lightx2v_4step.safetensors" in files
    assert "nsfw_wan_umt5-xxl_bf16.safetensors" in files
    # Every lora slot ships at strength 0, so nothing loads one.
    assert not any("W25_Realistic" in f for f in files)


def test_required_models_picks_up_a_lora_once_it_is_enabled(video):
    G.apply_params(video, {"loras@167": ["W25_Realistic_I2V_LOW"]})
    assert any("W25_Realistic_I2V_LOW" in m["file"] for m in G.required_models(video))


def test_the_video_node_is_an_output(video):
    assert "284" in {o["node_id"] for o in G.output_nodes(video)}


def test_applying_by_name_matches_the_raw_path(video):
    by_name, by_path = G.clone(video), G.clone(video)
    G.apply_params(by_name, {"num_frames": 49})
    G.apply_params(by_path, {"320.num_frames": 49})
    assert by_name["193"]["inputs"]["value"] == 49
    assert by_name == by_path
