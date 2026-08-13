"""Tests for the graph resolution and parameter patching engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfyui_mcp import graph as G

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


@pytest.fixture
def krea2() -> G.Graph:
    return json.loads((WORKFLOWS / "image_krea2.json").read_text(encoding="utf-8"))


def params_by_name(workflow: G.Graph) -> dict[str, G.Param]:
    return {p.name: p for p in G.discover_params(workflow)}


# --- link resolution ---------------------------------------------------------
def test_resolves_through_switch_to_primitive(krea2):
    """KSampler.steps -> switch 30:59 (selector true) -> 30:54 PrimitiveInt."""
    assert G.resolve_setter(krea2, "30:3", "steps") == ("30:54", "value")


def test_resolves_untaken_switch_branch_when_selector_flips(krea2):
    krea2["30:61"]["inputs"]["value"] = False
    assert G.resolve_setter(krea2, "30:3", "steps") == ("30:56", "value")


def test_resolves_prompt_through_passthrough_and_switch(krea2):
    """CLIPTextEncode.text -> PreviewAny -> switch -> PrimitiveStringMultiline -> easy positive."""
    assert G.resolve_setter(krea2, "30:6", "text") == ("97", "positive")


def test_literal_input_resolves_to_itself(krea2):
    assert G.resolve_setter(krea2, "30:3", "sampler_name") == ("30:3", "sampler_name")


def test_unresolvable_when_source_is_computed(krea2):
    # width comes out of ResolutionSelector, which computes rather than stores.
    assert G.resolve_setter(krea2, "30:5", "width") is None


def test_missing_node_or_input(krea2):
    assert G.resolve_setter(krea2, "nope", "steps") is None
    assert G.resolve_setter(krea2, "30:3", "nope") is None


# --- unknown node packs ------------------------------------------------------
def unknown_pack_graph() -> G.Graph:
    """A sampler fed by a primitive from a pack with no table entry."""
    return {
        "1": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 1, "steps": ["2", 0], "cfg": 8.0,
                "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
            },
        },
        "2": {"class_type": "SomeNewPack_IntValue", "inputs": {"value": 20}},
    }


def test_unknown_primitive_is_inferred_structurally():
    """A pack we have never seen should not need a table entry to be usable."""
    graph = unknown_pack_graph()
    assert G.resolve_setter(graph, "1", "steps") == ("2", "value")
    assert "steps" in {p.name for p in G.discover_params(graph)}


def test_unknown_primitive_is_writable_by_name_and_by_path():
    for key in ("steps", "1.steps", "2.value"):
        graph = unknown_pack_graph()
        G.apply_params(graph, {key: 30})
        assert graph["2"]["inputs"]["value"] == 30, key


def test_inference_picks_the_conventional_key_when_several_literals():
    graph = unknown_pack_graph()
    graph["2"]["inputs"] = {"value": 20, "label": "steps", "tooltip": "x"}
    assert G.resolve_setter(graph, "1", "steps") == ("2", "value")


def test_inference_declines_when_ambiguous():
    graph = unknown_pack_graph()
    graph["2"]["inputs"] = {"alpha": 1, "beta": 2}
    assert G.resolve_setter(graph, "1", "steps") is None


def test_inference_declines_for_computed_nodes():
    """A node with incoming links produces a value; it does not hold one."""
    graph = unknown_pack_graph()
    graph["2"] = {"class_type": "SomeMath", "inputs": {"a": ["3", 0], "b": 2}}
    graph["3"] = {"class_type": "SomeNewPack_IntValue", "inputs": {"value": 5}}
    assert G.resolve_setter(graph, "1", "steps") is None


def test_table_wins_over_inference(krea2):
    """easy seed carries extra literals; the table names the right one."""
    assert G.resolve_setter(krea2, "30:3", "seed") == ("51", "seed")


def test_unreachable_value_error_names_the_source_node(krea2):
    with pytest.raises(G.ParamError) as exc:
        G.apply_params(krea2, {"30:5.width": 512})
    message = str(exc.value)
    assert "node 49" in message and "ResolutionSelector" in message


def test_resolution_terminates_on_cycle():
    cyclic: G.Graph = {
        "1": {"class_type": "PrimitiveInt", "inputs": {"value": ["2", 0]}},
        "2": {"class_type": "PrimitiveInt", "inputs": {"value": ["1", 0]}},
    }
    assert G.resolve_setter(cyclic, "1", "value") is None


# --- discovery ---------------------------------------------------------------
def test_discovers_core_params(krea2):
    found = params_by_name(krea2)
    assert found["prompt"].node_id == "97"
    assert found["seed"].node_id == "51"
    assert (found["steps"].node_id, found["steps"].value) == ("30:54", 8)
    assert found["cfg"].node_id == "30:55"
    assert found["model"].value == "krea2_turbo_bf16.safetensors"


def test_no_negative_prompt_when_conditioning_is_zeroed(krea2):
    """The negative branch is a ConditioningZeroOut, so there is no real negative."""
    assert "negative_prompt" not in params_by_name(krea2)


def test_boolean_toggles_named_from_titles(krea2):
    found = params_by_name(krea2)
    assert found["enable_lora"].value is False
    assert found["is_turbo_model"].value is True
    assert found["second_pass"].value is False
    assert found["refine_prompt"].value is False


def test_shared_primitive_exposed_once(krea2):
    """Both samplers read seed from node 51; it should appear under one name."""
    targets = [(p.node_id, p.input) for p in G.discover_params(krea2)]
    assert len(targets) == len(set(targets))
    assert sum(1 for t in targets if t == ("51", "seed")) == 1


def test_second_sampler_keeps_its_own_distinct_fields(krea2):
    found = params_by_name(krea2)
    assert found["steps@30:63"].value == 4
    assert found["denoise@30:63"].value == 0.4


def test_outputs_and_models(krea2):
    assert [o["node_id"] for o in G.output_nodes(krea2)] == ["62", "68"]
    files = {m["file"] for m in G.required_models(krea2)}
    assert "krea2_turbo_bf16.safetensors" in files
    assert "qwen_image_vae.safetensors" in files


def test_slugify():
    assert G.slugify("Boolean (Enable LoRA?)") == "enable_lora"
    assert G.slugify("Is turbo model?") == "is_turbo_model"
    assert G.slugify("second pass") == "second_pass"


# --- application -------------------------------------------------------------
def test_apply_writes_through_to_the_primitive(krea2):
    G.apply_params(krea2, {"prompt": "a red fox", "steps": 12, "seed": 42})
    assert krea2["97"]["inputs"]["positive"] == "a red fox"
    assert krea2["30:54"]["inputs"]["value"] == 12
    assert krea2["51"]["inputs"]["seed"] == 42


def test_apply_coerces_types(krea2):
    G.apply_params(krea2, {"steps": "16", "cfg": 2, "enable_lora": "true"})
    assert krea2["30:54"]["inputs"]["value"] == 16
    assert isinstance(krea2["30:55"]["inputs"]["value"], float)
    assert krea2["30:23"]["inputs"]["value"] is True


def test_apply_rejects_bad_types(krea2):
    with pytest.raises(G.ParamError, match="integer"):
        G.apply_params(krea2, {"steps": "many"})


def test_apply_raw_path(krea2):
    G.apply_params(krea2, {"30:3.sampler_name": "dpmpp_2m"})
    assert krea2["30:3"]["inputs"]["sampler_name"] == "dpmpp_2m"


def test_raw_path_follows_links_to_the_literal(krea2):
    G.apply_params(krea2, {"30:3.steps": 20})
    assert krea2["30:54"]["inputs"]["value"] == 20


def test_apply_unknown_param(krea2):
    with pytest.raises(G.ParamError, match="unknown parameter"):
        G.apply_params(krea2, {"nonsense": 1})


def test_unknown_param_suggests_the_near_match(krea2):
    """An alphabetical list truncates away the obvious candidate; lead with it instead."""
    with pytest.raises(G.ParamError) as exc:
        G.apply_params(krea2, {"steps_count": 8})
    assert "Did you mean: steps" in str(exc.value)


def test_unknown_param_lists_every_name_for_a_normal_workflow(krea2):
    with pytest.raises(G.ParamError) as exc:
        G.apply_params(krea2, {"zzz": 1})
    message = str(exc.value)
    assert "seed" in message and "steps" in message and "vae" in message


def test_toggle_changes_which_branch_resolves(krea2):
    """Flipping is_turbo_model must redirect `steps` to the other primitive."""
    G.apply_params(krea2, {"is_turbo_model": False})
    G.apply_params(krea2, {"steps": 30})
    assert krea2["30:56"]["inputs"]["value"] == 30
    assert krea2["30:54"]["inputs"]["value"] == 8  # turbo branch untouched


def test_apply_loras_by_substring(krea2):
    G.apply_params(krea2, {"loras": [{"lora": "neondrip", "strength": 0.7}]})
    slots = krea2["77"]["inputs"]
    assert slots["lora_13"]["on"] is True
    assert slots["lora_13"]["strength"] == 0.7
    assert all(v["on"] is False for k, v in slots.items() if k.startswith("lora_") and k != "lora_13")


def test_apply_loras_accepts_plain_names(krea2):
    G.apply_params(krea2, {"loras": ["retroanime"]})
    assert krea2["77"]["inputs"]["lora_15"]["on"] is True


def test_apply_loras_empty_list_disables_all(krea2):
    krea2["77"]["inputs"]["lora_1"]["on"] = True
    G.apply_params(krea2, {"loras": []})
    assert all(v["on"] is False for k, v in krea2["77"]["inputs"].items() if k.startswith("lora_"))


def test_apply_loras_unknown_name(krea2):
    with pytest.raises(G.ParamError, match="no lora slot"):
        G.apply_params(krea2, {"loras": ["does-not-exist"]})


def test_apply_loras_ambiguous_name(krea2):
    with pytest.raises(G.ParamError, match="several slots"):
        G.apply_params(krea2, {"loras": ["krea2"]})


# --- submission helpers ------------------------------------------------------
def test_force_save_images_converts_previews(krea2):
    converted = G.force_save_images(krea2, "run")
    assert set(converted) == {"62", "68"}
    assert krea2["62"]["class_type"] == "SaveImage"
    assert krea2["62"]["inputs"]["filename_prefix"] == "run"


def test_strip_meta_leaves_graph_valid(krea2):
    stripped = G.strip_meta(krea2)
    assert all("_meta" not in node for node in stripped.values())
    assert all("class_type" in node and "inputs" in node for node in stripped.values())


def test_clone_is_deep(krea2):
    copy = G.clone(krea2)
    G.apply_params(copy, {"steps": 99})
    assert krea2["30:54"]["inputs"]["value"] == 8
