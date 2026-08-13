"""Tests for diagnosing a live workspace graph.

`diagnose` is pure, so every rule is exercised here without a browser or a running
ComfyUI. That matters more than usual: the value of a diagnosis is entirely in
whether it is right, and a rule that fires on a healthy graph is worse than one
that stays quiet - it sends someone to fix what is not broken.
"""

from __future__ import annotations

from typing import Any

import pytest

from comfyui_mcp.graph import diagnose


def node(node_id: str, node_type: str = "KSampler", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": node_id,
        "type": node_type,
        "title": None,
        "mode": "always",
        "registered": True,
        "inputs": [],
        "outputs": [],
        "widgets": {},
    }
    base.update(over)
    return base


def link_in(name: str, type_: str, origin: str, slot: int = 0, required: bool = True) -> dict[str, Any]:
    return {"name": name, "type": type_, "widget": False, "required": required, "from": {"node": origin, "slot": slot}}


def empty_in(name: str, type_: str, required: bool = True, widget: bool = False) -> dict[str, Any]:
    return {"name": name, "type": type_, "widget": widget, "required": required, "from": None}


def out(name: str, type_: str) -> dict[str, Any]:
    return {"name": name, "type": type_, "links": 1}


SCHEMA = {
    "KSampler": {"input": {"required": {"steps": ["INT", {"min": 1, "max": 10000}], "model": ["MODEL"]}}},
    "EmptyLatentImage": {"input": {"required": {"width": ["INT", {"min": 16, "max": 16384}]}}},
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["a.safetensors", "b.safetensors"]]}}},
    "VAELoader": {"input": {"required": {"vae_name": [["x.safetensors"]]}}},
    "AnyNode": {"input": {}},
}


def kinds(issues: list[dict[str, Any]]) -> list[str]:
    return [issue["kind"] for issue in issues]


def test_a_healthy_graph_produces_nothing():
    """The rule that matters most: silence when there is nothing to say."""
    nodes = [
        node("1", "CheckpointLoaderSimple", outputs=[out("MODEL", "MODEL")], widgets={"ckpt_name": "a.safetensors"}),
        node("2", "KSampler", inputs=[link_in("model", "MODEL", "1")], widgets={"steps": 20}),
    ]
    assert diagnose(nodes, SCHEMA) == []


def test_a_widget_input_with_no_link_is_not_a_hole():
    """A converted widget keeps its literal, unlike an empty MODEL slot."""
    nodes = [node("2", "KSampler", inputs=[empty_in("steps", "INT", widget=True)], widgets={"steps": 20})]
    assert diagnose(nodes, SCHEMA) == []


@pytest.mark.parametrize("wildcard", ["*", "", "None"])
def test_wildcard_slots_match_anything(wildcard):
    """ComfyUI's `*` slots are everywhere; treating them as mismatches would drown the report."""
    nodes = [
        node("1", "AnyNode", outputs=[out("out", wildcard)]),
        node("2", "KSampler", inputs=[link_in("model", "MODEL", "1")]),
    ]
    assert kinds(diagnose(nodes, None)) == []


def test_a_node_type_that_is_not_installed_is_the_first_thing_said():
    nodes = [node("7", "SomePack_Fancy", registered=False)]
    found = diagnose(nodes, None)
    assert kinds(found) == ["missing_node_type"]
    assert found[0]["severity"] == "error"
    assert "install" in found[0]["fix"]


def test_an_uninstallable_node_is_not_also_reported_for_its_slots():
    """Its slots are unknown, so every other rule would be guessing."""
    nodes = [node("7", "SomePack_Fancy", registered=False, inputs=[empty_in("image", "IMAGE")], mode="muted")]
    assert kinds(diagnose(nodes, None)) == ["missing_node_type"]


def test_a_required_input_with_nothing_plugged_in():
    nodes = [node("2", "KSampler", inputs=[empty_in("model", "MODEL")])]
    found = diagnose(nodes, SCHEMA)
    assert kinds(found) == ["unconnected_input"]
    assert "2.model" in found[0]["fix"]


def test_an_optional_input_left_empty_is_not_reported():
    nodes = [node("2", "KSampler", inputs=[empty_in("mask", "MASK", required=False)])]
    assert diagnose(nodes, SCHEMA) == []


def test_a_link_from_a_node_that_is_not_there():
    nodes = [node("2", "KSampler", inputs=[link_in("model", "MODEL", "99")])]
    assert kinds(diagnose(nodes, SCHEMA)) == ["dangling_link"]


@pytest.mark.parametrize("boundary", ["30:-10", "30:-20", "-10"])
def test_a_link_through_a_subgraph_boundary_is_not_dangling(boundary):
    """Subgraph input/output nodes carry reserved negative ids and are not in the
    node list. Measured on the Krea workflow: twenty of twenty-five "errors" were
    this one mistake."""
    nodes = [node("30:3", "KSampler", path=["30"], inputs=[link_in("seed", "INT", boundary)])]
    assert diagnose(nodes, SCHEMA) == []


def test_types_are_not_compared_across_a_nesting_boundary():
    """A SubgraphNode's input resolves through getInputLink to a node *inside* it,
    so the two ends describe different things. Comparing them reported the Krea
    workflow's own wiring as broken."""
    nodes = [
        node("30:65", "VAEDecode", outputs=[out("IMAGE", "IMAGE")]),
        node("30", "b0e5ca93", inputs=[link_in("value_1", "BOOLEAN", "30:65")]),
    ]
    assert diagnose(nodes, SCHEMA) == []


def test_a_link_into_a_subgraph_is_not_dangling_when_the_read_did_not_descend():
    """scope="root" reports 8 nodes but their links can name nodes inside. A subset
    of the graph must never produce findings the whole graph does not: measured,
    root reported two errors and a warning that `all` did not."""
    nodes = [node("30", "b0e5ca93", inputs=[link_in("value_1", "BOOLEAN", "30:65")])]
    assert diagnose(nodes, SCHEMA) == []


def test_types_are_still_compared_within_one_graph():
    """The narrowing must not disarm the check where it does apply."""
    nodes = [
        node("30:65", "VAELoader", outputs=[out("VAE", "VAE")]),
        node("30:3", "KSampler", inputs=[link_in("model", "MODEL", "30:65")]),
    ]
    assert kinds(diagnose(nodes, SCHEMA)) == ["type_mismatch"]


def test_a_dangling_link_at_the_same_depth_is_still_reported():
    """Nesting-aware must not mean blind: a real hole one level down still counts."""
    nodes = [node("30:3", "KSampler", inputs=[link_in("model", "MODEL", "30:99")])]
    assert kinds(diagnose(nodes, SCHEMA)) == ["dangling_link"]


def test_a_link_from_a_slot_that_does_not_exist():
    nodes = [
        node("1", "CheckpointLoaderSimple", outputs=[out("MODEL", "MODEL")]),
        node("2", "KSampler", inputs=[link_in("model", "MODEL", "1", slot=5)]),
    ]
    assert kinds(diagnose(nodes, SCHEMA)) == ["dangling_link"]


def test_a_link_whose_types_disagree_names_both_ends():
    nodes = [
        node("1", "VAELoader", outputs=[out("VAE", "VAE")]),
        node("2", "KSampler", inputs=[link_in("model", "MODEL", "1")]),
    ]
    found = diagnose(nodes, SCHEMA)
    assert kinds(found) == ["type_mismatch"]
    assert "MODEL" in found[0]["detail"] and "VAE" in found[0]["detail"]


def test_a_type_spelled_in_another_case_is_the_same_type():
    # Found on a live workflow: darkilConstantSetter declares `float` while the
    # slot reading it says `FLOAT`. litegraph lowercases both, the canvas made the
    # link, and diagnose called the working graph broken.
    nodes = [
        node("1", "PrimitiveFloat", outputs=[out("float", "float")]),
        node("2", "KSampler", inputs=[link_in("cfg", "FLOAT", "1")]),
    ]
    assert diagnose(nodes, SCHEMA) == []


def test_a_slot_accepting_several_types_matches_any_one_of_them():
    # ComfyMathExpression takes "FLOAT,INT,BOOLEAN". Comparing that against the
    # whole string rather than its members reported every input to it as wrong.
    nodes = [
        node("1", "PrimitiveFloat", outputs=[out("FLOAT", "FLOAT")]),
        node("2", "KSampler", inputs=[link_in("cfg", "FLOAT,INT,BOOLEAN", "1")]),
    ]
    assert diagnose(nodes, SCHEMA) == []


def test_a_union_that_shares_nothing_is_still_a_mismatch():
    # The narrowing must not disarm the check: overlapping is the rule, not
    # "a comma means anything goes".
    nodes = [
        node("1", "VAELoader", outputs=[out("VAE", "VAE")]),
        node("2", "KSampler", inputs=[link_in("model", "FLOAT,INT,BOOLEAN", "1")]),
    ]
    assert kinds(diagnose(nodes, SCHEMA)) == ["type_mismatch"]


def test_a_value_outside_the_declared_range():
    nodes = [node("2", "KSampler", widgets={"steps": 99999})]
    found = diagnose(nodes, SCHEMA)
    assert kinds(found) == ["value_out_of_range"]
    assert "10000" in found[0]["detail"]


def test_a_value_below_the_minimum():
    nodes = [node("3", "EmptyLatentImage", widgets={"width": 8})]
    assert kinds(diagnose(nodes, SCHEMA)) == ["value_out_of_range"]


def test_a_combo_value_off_the_list_is_a_note_not_an_error():
    """An option list is not a whitelist - LoadImage loads paths it never lists."""
    nodes = [node("1", "CheckpointLoaderSimple", widgets={"ckpt_name": "sub/dir/c.safetensors"})]
    found = diagnose(nodes, SCHEMA)
    assert kinds(found) == ["value_not_listed"]
    assert found[0]["severity"] == "note"


def test_muted_and_bypassed_are_reported_because_they_look_like_nothing_wrong():
    nodes = [node("1", "KSampler", mode="muted"), node("2", "KSampler", mode="bypassed")]
    found = diagnose(nodes, SCHEMA)
    assert sorted(kinds(found)) == ["bypassed", "muted"]
    assert all(issue["severity"] == "note" for issue in found)
    assert "set_workspace_node_modes" in found[0]["fix"]


@pytest.mark.parametrize(
    "widget",
    ["control_after_generate", "lora_1", "➕ Add Lora", "sampling_mode.top_k", "preview_text"],
)
def test_a_widget_with_no_matching_input_is_not_evidence_of_anything(widget):
    """The frontend synthesises these. Measured: a rule that flagged them produced
    43 warnings on a healthy workflow and buried the real findings among them."""
    assert diagnose([node("2", "KSampler", widgets={"steps": 20, widget: 1})], SCHEMA) == []


def test_a_node_the_browser_knows_and_the_server_does_not():
    nodes = [node("2", "FreshlyInstalled", backend=True)]
    found = diagnose(nodes, SCHEMA)
    assert kinds(found) == ["unknown_to_server"]
    assert "restart" in found[0]["fix"]


@pytest.mark.parametrize("node_type", ["MarkdownNote", "Note"])
def test_frontend_only_nodes_are_not_called_missing(node_type):
    """/object_info answers for these with an empty object. Measured, not assumed."""
    assert diagnose([node("1", node_type, backend=False)], SCHEMA) == []


def test_a_subgraph_node_is_not_called_missing_either():
    """It has nodeData, so the backend flag alone does not exclude it - but there is
    no server-side type behind a subgraph, and its contents are reported separately."""
    container = node("30", "b0e5ca93-2731", backend=True, subgraph={"name": "Krea", "id": "b0e5ca93"})
    assert diagnose([container], SCHEMA) == []


def test_a_payload_without_the_backend_flag_stays_quiet():
    """An older extension sends no flag; a false alarm costs more than a missed hint."""
    plain = node("2", "FreshlyInstalled")
    plain.pop("backend", None)
    assert diagnose([plain], SCHEMA) == []


def test_a_node_that_could_not_be_read_reports_that_and_stops():
    nodes = [node("2", "KSampler", describe_error="boom", inputs=[empty_in("model", "MODEL")])]
    assert kinds(diagnose(nodes, SCHEMA)) == ["describe_failed"]


def test_without_schemas_only_shape_checks_run():
    """describe_workflow degrades the same way; a partial answer beats none."""
    nodes = [node("2", "KSampler", inputs=[empty_in("model", "MODEL")], widgets={"steps": 99999})]
    found = diagnose(nodes, None)
    assert kinds(found) == ["unconnected_input"]


def test_widgets_are_not_called_unknown_when_there_is_nothing_to_compare_against():
    nodes = [node("2", "KSampler", widgets={"anything": 1})]
    assert diagnose(nodes, None) == []


def test_findings_come_back_worst_first():
    nodes = [
        node("1", "CheckpointLoaderSimple", mode="muted", widgets={"ckpt_name": "nope.safetensors"}),
        node("2", "KSampler", inputs=[empty_in("model", "MODEL")]),
        node("3", "FreshlyInstalled", backend=True),
    ]
    found = diagnose(nodes, SCHEMA)
    assert [issue["severity"] for issue in found] == sorted(
        [issue["severity"] for issue in found], key=["error", "warning", "note"].index
    )
    assert found[0]["kind"] == "unconnected_input"


def test_nested_nodes_keep_the_path_ids_they_arrived_with():
    """`98:12` is what the API format and the progress events call it too."""
    nodes = [
        node("98:11", "VAELoader", outputs=[out("VAE", "VAE")]),
        node("98:12", "KSampler", inputs=[link_in("model", "MODEL", "98:11")]),
    ]
    found = diagnose(nodes, SCHEMA)
    assert found[0]["node"] == "98:12"
    assert "98:11" in found[0]["detail"]
