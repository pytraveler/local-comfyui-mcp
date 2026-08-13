"""Tests for /object_info schema parsing and schema-driven typing.

The fixtures below are trimmed copies of real /object_info entries from a live
ComfyUI 0.28.3, including both combo encodings it emits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfyui_mcp import graph as G

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"

SCHEMAS: G.Schemas = {
    "KSampler": {
        "input": {
            "required": {
                "model": ["MODEL", {"tooltip": "The model used for denoising."}],
                "seed": ["INT", {"default": 0, "min": 0, "max": 18446744073709551615}],
                "steps": ["INT", {"default": 20, "min": 1, "max": 10000}],
                "cfg": ["FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.1}],
                "sampler_name": [["euler", "heun", "dpmpp_2m"]],
                "scheduler": [["simple", "karras", "normal"]],
                "denoise": ["FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0}],
            }
        }
    },
    "easy seed": {"input": {"required": {"seed": ["INT", {"default": 0, "min": 0, "max": 1125899906842624}]}}},
    "PrimitiveInt": {"input": {"required": {"value": ["INT", {"default": 0, "min": -(2**31), "max": 2**31}]}}},
    "PrimitiveFloat": {"input": {"required": {"value": ["FLOAT", {"default": 0.0}]}}},
    "PrimitiveBoolean": {"input": {"required": {"value": ["BOOLEAN", {}]}}},
    "easy positive": {"input": {"required": {"positive": ["STRING", {"multiline": True}]}}},
    "ResolutionSelector": {
        "input": {
            "required": {
                "aspect_ratio": [
                    "COMBO",
                    {"default": "1:1 (Square)", "options": ["1:1 (Square)", "16:9 (Widescreen)", "3:2 (Photo)"]},
                ],
                "megapixels": ["FLOAT", {"default": 1.0, "min": 0.1, "max": 16.0}],
                "multiple": ["INT", {"default": 8, "min": 1, "max": 64}],
            }
        }
    },
    "ImageScaleBy": {
        "input": {
            "required": {
                "upscale_method": [["nearest-exact", "bilinear", "lanczos"]],
                "scale_by": ["FLOAT", {"default": 1.0, "min": 0.01, "max": 8.0}],
            }
        }
    },
    "LoadImage": {"input": {"required": {"image": [["a.png", "b.png"], {"image_upload": True}]}}},
}


@pytest.fixture
def krea2() -> G.Graph:
    return json.loads((WORKFLOWS / "image_krea2.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "entry,expected_kind",
    [
        (["INT", {}], "int"),
        (["FLOAT", {}], "float"),
        (["BOOLEAN", {}], "bool"),
        (["STRING", {"multiline": True}], "string"),
        (["MODEL", {}], "link"),
        (["CONDITIONING", {}], "link"),
        (["IMAGE"], "link"),
    ],
)
def test_scalar_and_link_kinds(entry, expected_kind):
    assert G.parse_input_spec(entry).kind == expected_kind


def test_both_combo_encodings_yield_options():
    """ComfyUI mixes the old inline-list form and the newer COMBO form."""
    old = G.parse_input_spec([["euler", "heun"]])
    new = G.parse_input_spec(["COMBO", {"options": ["euler", "heun"]}])
    assert old.kind == new.kind == "combo"
    assert old.options == new.options == ("euler", "heun")


def test_combo_with_trailing_config():
    spec = G.parse_input_spec([["a.png", "b.png"], {"image_upload": True}])
    assert spec.options == ("a.png", "b.png")


def test_ranges_are_read():
    spec = G.parse_input_spec(["INT", {"min": 1, "max": 10000}])
    assert (spec.minimum, spec.maximum) == (1, 10000)


def test_malformed_entries_degrade_to_link():
    for junk in (None, [], {}, "INT", [123]):
        assert G.parse_input_spec(junk).kind == "link"


def test_schema_index_merges_required_and_optional():
    index = G.SchemaIndex({"N": {"input": {"required": {"a": ["INT", {}]}, "optional": {"b": ["FLOAT", {}]}}}})
    specs = index.for_class("N")
    assert specs["a"].kind == "int" and specs["b"].kind == "float"


def test_empty_index_is_falsy_and_returns_nothing():
    index = G.SchemaIndex(None)
    assert not index
    assert index.spec({"1": {"class_type": "KSampler", "inputs": {}}}, "1", "steps") is None


def test_schema_corrects_a_type_the_tables_guessed(krea2):
    """cfg resolves to a PrimitiveFloat holding `1`, which reads as int from JSON."""
    without = {p.name: p for p in G.discover_params(krea2)}
    with_schema = {p.name: p for p in G.discover_params(krea2, SCHEMAS)}
    assert without["cfg"].type == with_schema["cfg"].type == "float"
    # multiple is declared INT by the node; the table never described it at all
    assert with_schema["multiple"].type == "int"


def test_options_are_attached_to_combo_params(krea2):
    found = {p.name: p for p in G.discover_params(krea2, SCHEMAS)}
    assert found["aspect_ratio"].extra["options"] == [
        "1:1 (Square)",
        "16:9 (Widescreen)",
        "3:2 (Photo)",
    ]
    assert "16:9 (Widescreen)" in found["aspect_ratio"].to_dict()["options"]


def test_ranges_are_attached(krea2):
    found = {p.name: p for p in G.discover_params(krea2, SCHEMAS)}
    assert found["megapixels"].to_dict()["min"] == 0.1


def test_consumer_range_tightens_the_primitive_range(krea2):
    """steps lands on a PrimitiveInt (+/-2**31) but is read by a KSampler (1..10000).

    ComfyUI validates only literals sitting on the node, so a linked value out of
    the sampler's range reaches the sampler unchecked.
    """
    found = {p.name: p for p in G.discover_params(krea2, SCHEMAS)}
    assert found["steps"].to_dict()["max"] == 10000
    assert found["steps"].to_dict()["min"] == 1
    with pytest.raises(G.ParamError, match="above the maximum 10000"):
        G.apply_params(krea2, {"steps": 99999}, SCHEMAS)


def test_consumer_range_applies_to_raw_paths(krea2):
    with pytest.raises(G.ParamError, match="above the maximum 10000"):
        G.apply_params(krea2, {"30:3.steps": 99999}, SCHEMAS)


def test_tightening_keeps_the_stricter_bound_from_either_side():
    loose = G.InputSpec("int", minimum=0, maximum=10_000)
    strict = G.InputSpec("int", minimum=1, maximum=100)
    assert loose.tightened_by(strict) == G.InputSpec("int", minimum=1, maximum=100)
    assert strict.tightened_by(loose) == G.InputSpec("int", minimum=1, maximum=100)


def test_tightening_ignores_a_link_consumer():
    spec = G.InputSpec("int", minimum=1, maximum=100)
    assert spec.tightened_by(G.InputSpec("link")) == spec
    assert spec.tightened_by(None) == spec


def test_long_option_lists_are_capped():
    graph = {"1": {"class_type": "N", "inputs": {"pick": "a0"}}}
    options = [f"a{i}" for i in range(50)]
    schemas = {"N": {"input": {"required": {"pick": ["COMBO", {"options": options}]}}}}
    spec = G.SchemaIndex(schemas).for_class("N")["pick"]
    assert len(spec.options) == 50  # validation still sees them all
    G.LOADER_FIELDS["N"] = [("pick", "pick", "combo")]
    try:
        param = {p.name: p for p in G.discover_params(graph, schemas)}["pick"]
        assert len(param.to_dict()["options"]) == G.OPTIONS_SHOWN
        assert param.to_dict()["options_total"] == 50
    finally:
        del G.LOADER_FIELDS["N"]


def test_unlisted_combo_value_is_noted_but_not_rejected(krea2):
    """The declared list is not authoritative - LoadImage accepts 'sub/name.png'
    although it only advertises the top level of the input folder."""
    changes = G.apply_params(krea2, {"aspect_ratio": "banana"}, SCHEMAS)
    assert krea2["49"]["inputs"]["aspect_ratio"] == "banana"
    assert any("not among the declared options" in c for c in changes)


def test_valid_combo_value_passes_without_a_note(krea2):
    changes = G.apply_params(krea2, {"aspect_ratio": "16:9 (Widescreen)"}, SCHEMAS)
    assert krea2["49"]["inputs"]["aspect_ratio"] == "16:9 (Widescreen)"
    assert not any("declared options" in c for c in changes)


def test_subfolder_image_path_is_allowed(krea2):
    """The regression that motivated this: a real, working value must not be refused."""
    graph = {"143": {"class_type": "LoadImage", "inputs": {"image": "example.png"}}}
    schemas = {"LoadImage": SCHEMAS["LoadImage"]}
    changes = G.apply_params(graph, {"143.image": "mcp/photo.png"}, schemas)
    assert graph["143"]["inputs"]["image"] == "mcp/photo.png"
    assert any("ComfyUI will decide" in c for c in changes)


def test_out_of_range_values_are_caught(krea2):
    with pytest.raises(G.ParamError, match="above the maximum"):
        G.apply_params(krea2, {"megapixels": 999}, SCHEMAS)
    with pytest.raises(G.ParamError, match="below the minimum"):
        G.apply_params(krea2, {"megapixels": 0.0}, SCHEMAS)


def test_notes_apply_to_raw_paths_too(krea2):
    changes = G.apply_params(krea2, {"30:3.sampler_name": "nope"}, SCHEMAS)
    assert any("not among the declared options" in c for c in changes)


def test_seed_limit_is_per_node(krea2):
    """KSampler declares 2**64 but the seed lands on 'easy seed', capped at 2**50."""
    index = G.SchemaIndex(SCHEMAS)
    target = G.resolve_setter(krea2, "30:3", "seed")
    assert index.spec(krea2, *target).maximum == 1125899906842624


def test_without_schemas_nothing_is_checked(krea2):
    """Offline behaviour must stay usable: ComfyUI still reports the real error."""
    changes = G.apply_params(krea2, {"aspect_ratio": "banana", "steps": 99999})
    assert krea2["49"]["inputs"]["aspect_ratio"] == "banana"
    assert krea2["30:54"]["inputs"]["value"] == 99999
    assert not any("declared options" in c for c in changes)
