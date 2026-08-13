"""Tests for finding a node type among a few thousand of them.

The question worth getting right is not "which names contain this word" - it is
"what can I wire in here", and on a real install those have very different
answers. So most of what follows is about slot types, about the aliases ComfyUI
already ships, and about which flags are noise and which are not.
"""

from __future__ import annotations

from typing import Any

from comfyui_mcp.graph import DESCRIPTION_SHOWN, find_node_types, summarise_schema


def entry(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "input": {"required": {}},
        "output": [],
        "output_name": [],
        "display_name": "",
        "description": "",
        "category": "testing",
        "python_module": "nodes",
        "search_aliases": [],
    }
    base.update(over)
    return base


#: Small stand-in for /object_info, in the shapes ComfyUI really emits.
SCHEMAS: dict[str, Any] = {
    "VAEDecode": entry(
        input={"required": {"samples": ["LATENT"], "vae": ["VAE"]}},
        output=["IMAGE"],
        output_name=["IMAGE"],
        display_name="VAE Decode",
        category="latent",
        search_aliases=["decode", "latent to image", "render latent"],
    ),
    "ImageUpscaleWithModel": entry(
        input={"required": {"upscale_model": ["UPSCALE_MODEL"], "image": ["IMAGE"]}},
        output=["IMAGE"],
        output_name=["IMAGE"],
        display_name="Upscale Image (using Model)",
        category="image/upscaling",
    ),
    "CRTAutoLatentUpscaler": entry(
        input={"required": {"samples": ["LATENT"]}},
        output=["LATENT"],
        output_name=["LATENT"],
        category="latent/upscaling",
        python_module="custom_nodes.crt-nodes",
    ),
    "LatentResize": entry(
        input={"required": {"samples": ["LATENT"]}},
        output=["LATENT"],
        output_name=["LATENT"],
        category="latent/upscale",  # the only place the word appears for this one
    ),
    "DiffusionModelLoaderKJ": entry(
        input={"required": {"model_name": [["a.safetensors", "b.safetensors"]]}},
        output=["MODEL"],
        output_name=["MODEL"],
        category="KJNodes",
        python_module="custom_nodes.comfyui-kjnodes",
        experimental=True,
    ),
    "CheckpointLoader": entry(
        input={"required": {"config_name": [["v1.yaml"]]}},
        output=["MODEL", "CLIP", "VAE"],
        output_name=["MODEL", "CLIP", "VAE"],
        display_name="Load Checkpoint With Config",
        category="advanced/loaders",
        deprecated=True,
    ),
    "KlingImageNode": entry(
        input={"required": {"prompt": ["STRING"]}},
        output=["IMAGE"],
        output_name=["IMAGE"],
        category="api node/image",
        python_module="comfy_api_nodes.nodes_kling",
        api_node=True,
    ),
    "Reroute": entry(
        input={"required": {"": ["*"]}},
        output=["*"],
        output_name=["*"],
        category="utils",
    ),
    "LoraLoader": entry(
        input={
            "required": {
                "model": ["MODEL", {"tooltip": "The diffusion model the LoRA will be applied to."}],
                "clip": ["CLIP"],
                "lora_name": [["one.safetensors", "two.safetensors", "three.safetensors"]],
                "strength_model": ["FLOAT", {"default": 1.0, "min": -100.0, "max": 100.0}],
            },
            "optional": {"extra": ["CONDITIONING"]},
        },
        output=["MODEL", "CLIP"],
        output_name=["MODEL", "CLIP"],
        description="LoRAs are used to modify diffusion and CLIP models.",
        category="loaders",
    ),
}


def names(result: dict[str, Any]) -> list[str]:
    return [item["class_type"] for item in result["results"]]


def test_filtering_by_output_type_finds_what_the_name_never_would() -> None:
    # Nothing about VAEDecode contains the word "latent", which is exactly the
    # case a substring search cannot answer.
    found = names(find_node_types(SCHEMAS, input_type="LATENT", output_type="IMAGE"))

    assert "VAEDecode" in found
    assert "CRTAutoLatentUpscaler" not in found  # takes a LATENT but returns one


def test_input_and_output_filters_are_anded_not_ored() -> None:
    both = names(find_node_types(SCHEMAS, input_type="IMAGE", output_type="IMAGE"))

    assert "ImageUpscaleWithModel" in both
    assert "VAEDecode" not in both


def test_a_wildcard_slot_matches_any_type_asked_for() -> None:
    # litegraph's own rule: `isValidConnection` treats '*' as anything, so a
    # Reroute really can sit between any two nodes. The search has to agree with
    # the validator that will judge the link.
    assert "Reroute" in names(find_node_types(SCHEMAS, input_type="LATENT"))
    assert "Reroute" in names(find_node_types(SCHEMAS, output_type="CONDITIONING"))


def test_a_wildcard_node_ranks_below_every_node_that_names_the_type() -> None:
    # True and uninformative: a Reroute carries an IMAGE and is never the answer
    # to "what gives me an IMAGE".
    assert names(find_node_types(SCHEMAS, output_type="IMAGE"))[-1] == "Reroute"


def test_a_type_only_wildcards_match_says_so_by_returning_only_wildcards() -> None:
    assert names(find_node_types(SCHEMAS, output_type="NOISE")) == ["Reroute"]


def test_an_optional_input_still_counts_as_something_that_can_be_wired() -> None:
    assert "LoraLoader" in names(find_node_types(SCHEMAS, input_type="CONDITIONING"))


def test_the_aliases_comfyui_ships_are_searched() -> None:
    found = find_node_types(SCHEMAS, search="latent to image")

    assert names(found) == ["VAEDecode"]
    assert found["results"][0]["matched_on"] == "alias"


def test_a_name_match_outranks_a_category_match() -> None:
    found = names(find_node_types(SCHEMAS, search="upscale"))

    assert found[-1] == "LatentResize"  # only its category says 'upscaling'
    assert set(found[:2]) == {"ImageUpscaleWithModel", "CRTAutoLatentUpscaler"}


def test_an_exact_name_comes_first_however_the_alphabet_falls() -> None:
    found = names(find_node_types(SCHEMAS, search="reroute"))

    assert found[0] == "Reroute"


def test_searching_the_description_is_a_last_resort_not_a_first_one() -> None:
    found = find_node_types(SCHEMAS, search="diffusion")
    ranked = names(found)

    assert ranked.index("DiffusionModelLoaderKJ") < ranked.index("LoraLoader")
    assert found["results"][ranked.index("LoraLoader")]["matched_on"] == "description"


def test_search_and_type_filters_narrow_each_other() -> None:
    assert names(find_node_types(SCHEMAS, search="upscale", output_type="IMAGE")) == ["ImageUpscaleWithModel"]


def test_the_pack_a_node_came_from_is_a_filter_and_reads_as_a_name() -> None:
    found = find_node_types(SCHEMAS, pack="kjnodes")

    assert names(found) == ["DiffusionModelLoaderKJ"]
    assert found["results"][0]["pack"] == "comfyui-kjnodes"  # not 'custom_nodes.comfyui-kjnodes'


def test_category_filters_on_the_whole_path() -> None:
    assert set(names(find_node_types(SCHEMAS, category="upscal"))) == {
        "ImageUpscaleWithModel",
        "CRTAutoLatentUpscaler",
        "LatentResize",
    }


def test_experimental_nodes_are_shown_because_experimental_means_new() -> None:
    # Half of this install's experimental nodes are kijai's, and they include
    # loaders in daily use. Hiding them would hide the good half.
    found = find_node_types(SCHEMAS, search="loader")

    assert "DiffusionModelLoaderKJ" in names(found)


def test_an_experimental_node_says_so_rather_than_hiding_it() -> None:
    found = find_node_types(SCHEMAS, pack="kjnodes")

    assert found["results"][0]["flags"] == ["experimental"]


def test_deprecated_nodes_are_left_out_because_something_replaced_them() -> None:
    found = find_node_types(SCHEMAS, output_type="VAE")

    assert "CheckpointLoader" not in names(found)
    assert found["hidden"] == {"deprecated": 1}


def test_what_was_hidden_is_counted_rather_than_silently_dropped() -> None:
    found = find_node_types(SCHEMAS, output_type="IMAGE")

    assert "KlingImageNode" not in names(found)
    assert found["hidden"] == {"api_node": 1}
    assert found["matched"] == len(found["results"])


def test_deprecated_and_paid_nodes_come_back_when_asked_for() -> None:
    assert "CheckpointLoader" in names(find_node_types(SCHEMAS, output_type="VAE", include_deprecated=True))
    assert "KlingImageNode" in names(find_node_types(SCHEMAS, output_type="IMAGE", include_api=True))


def test_a_result_carries_enough_wiring_to_skip_a_second_lookup() -> None:
    found = find_node_types(SCHEMAS, search="LoraLoader")["results"][0]

    assert found["inputs"] == ["MODEL", "CLIP", "CONDITIONING?"]  # optional marked, widgets elsewhere
    assert found["widgets"] == ["lora_name", "strength_model"]
    assert found["outputs"] == ["MODEL", "CLIP"]


def test_a_long_description_is_cut_rather_than_carried_whole() -> None:
    schemas = {"Verbose": entry(description="word " * 200)}
    found = find_node_types(schemas, search="verbose")["results"][0]

    assert len(found["about"]) == DESCRIPTION_SHOWN + 3


def test_the_limit_bounds_the_results_but_not_the_count() -> None:
    found = find_node_types(SCHEMAS, limit=2)

    assert found["shown"] == 2
    assert found["matched"] > 2
    assert found["installed"] == len(SCHEMAS)


def test_a_combo_keeps_a_sample_of_its_options_and_the_true_count() -> None:
    schemas = {"Loader": entry(input={"required": {"name": [[f"m{i}.safetensors" for i in range(71)]]}})}
    described = summarise_schema(schemas["Loader"], "Loader", options_shown=3)
    combo = described["inputs"][0]

    assert combo["options"] == ["m0.safetensors", "m1.safetensors", "m2.safetensors"]
    assert combo["options_total"] == 71


def test_a_short_option_list_is_not_reported_as_truncated() -> None:
    described = summarise_schema(SCHEMAS["LoraLoader"], "LoraLoader")
    combo = next(i for i in described["inputs"] if i["name"] == "lora_name")

    assert len(combo["options"]) == 3
    assert "options_total" not in combo


def test_one_option_list_repeated_across_slots_is_written_out_once() -> None:
    # A multi-slot loader carries the same model list per slot: 50 of them on
    # `easy loraSwitcher`, which was 25 200 of its 31 460 characters.
    models = [f"m{i}.safetensors" for i in range(72)]
    schemas = {"Switcher": entry(input={"required": {f"lora_{i}_name": [list(models)] for i in range(1, 4)}})}
    described = summarise_schema(schemas["Switcher"], "Switcher")

    assert described["inputs"][0]["options_total"] == 72
    assert [i.get("same_options_as") for i in described["inputs"][1:]] == ["lora_1_name", "lora_1_name"]
    assert all("options" not in i for i in described["inputs"][1:])


def test_lists_that_only_look_alike_are_not_folded_together() -> None:
    schemas = {
        "Two": entry(input={"required": {"a": [["x", "y"]], "b": [["x", "z"]]}}),
    }
    described = summarise_schema(schemas["Two"], "Two")

    assert described["inputs"][1]["options"] == ["x", "z"]
    assert "same_options_as" not in described["inputs"][1]


def test_the_newer_combo_encoding_is_read_the_same_way() -> None:
    schemas = {"Sampler": entry(input={"required": {"name": ["COMBO", {"options": ["euler", "heun"]}]}})}
    described = summarise_schema(schemas["Sampler"], "Sampler")

    assert described["inputs"][0]["options"] == ["euler", "heun"]


def test_a_link_input_reports_its_type_not_a_literal_kind() -> None:
    described = summarise_schema(SCHEMAS["LoraLoader"], "LoraLoader")
    model = described["inputs"][0]

    assert model == {
        "name": "model",
        "type": "MODEL",
        "about": "The diffusion model the LoRA will be applied to.",
    }


def test_defaults_and_ranges_survive_the_summary() -> None:
    described = summarise_schema(SCHEMAS["LoraLoader"], "LoraLoader")
    strength = next(i for i in described["inputs"] if i["name"] == "strength_model")

    assert (strength["type"], strength["default"], strength["min"], strength["max"]) == ("float", 1.0, -100.0, 100.0)


def test_an_optional_input_is_marked_and_kept_in_place() -> None:
    described = summarise_schema(SCHEMAS["LoraLoader"], "LoraLoader")

    assert described["inputs"][-1] == {"name": "extra", "optional": True, "type": "CONDITIONING"}


def test_outputs_carry_the_authors_labels_beside_the_types() -> None:
    described = summarise_schema(SCHEMAS["VAEDecode"], "VAEDecode")

    assert described["outputs"] == [{"name": "IMAGE", "type": "IMAGE"}]
    assert described["aliases"] == ["decode", "latent to image", "render latent"]
