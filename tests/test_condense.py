"""Tests for fitting a live workflow into an answer.

The problem is not that the report is untidy, it is that a real graph is bigger
than a reply may be: 140 nodes measured here came to 81.6 KB, and the wiring
alone is 32 KB of that. No amount of trimming fixes a graph that size, so there
are two axes - how much is said about each node, and which nodes are spoken
about at all - and the rule tying them together is that nothing is ever quietly
left out.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from comfyui_mcp.graph import DETAIL_LEVELS, condense_workspace


def node(node_id: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": node_id,
        "type": "KSampler",
        "title": "KSampler",
        "mode": "always",
        "pos": [10, 20],
        "size": [270, 270],
        "inputs": [],
        "outputs": [],
        "collapsed": False,
        "pinned": False,
        "registered": True,
        "backend": True,
    }
    base.update(over)
    return base


def link(name: str, source: str | None = None, **over: Any) -> dict[str, Any]:
    slot: dict[str, Any] = {"name": name, "type": "LATENT", "widget": False, "required": False}
    slot["from"] = {"node": source, "slot": 0} if source else None
    slot.update(over)
    return slot


def report(*nodes: dict[str, Any], **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "format": "summary",
        "scope": "root",
        "breadcrumb": "root",
        "node_count": len(nodes),
        "nodes": list(nodes),
        "groups": [],
        "selected": {"nodes": [], "groups": []},
        "issues": [],
    }
    base.update(over)
    return base


def ids(result: dict[str, Any]) -> list[str]:
    return [item["id"] for item in result["nodes"]]


def test_an_outline_counts_links_instead_of_listing_them():
    graph = report(
        node("1", inputs=[link("samples", "9"), link("vae")], outputs=[{"name": "LATENT", "type": "LATENT", "links": 3}])
    )
    only_node = condense_workspace(graph, detail="outline")["nodes"][0]
    assert only_node["in"] == 1  # one of the two is connected
    assert only_node["out"] == 3
    assert "inputs" not in only_node and "outputs" not in only_node


def test_an_outline_drops_geometry_but_links_keeps_it():
    graph = report(node("1"))
    assert "pos" not in condense_workspace(graph, detail="outline")["nodes"][0]
    assert condense_workspace(graph, detail="links")["nodes"][0]["pos"] == [10, 20]


def test_widget_values_belong_to_full_alone():
    graph = report(node("1", widgets={"steps": 30}))
    assert "widgets" not in condense_workspace(graph, detail="links")["nodes"][0]
    assert condense_workspace(graph, detail="full")["nodes"][0]["widgets"] == {"steps": 30}


def test_properties_travel_with_the_widgets():
    # Same kind of fact - a node's settings rather than its wiring - and for some
    # nodes the whole configuration is there, so the widget values alone mislead.
    graph = report(node("1", properties={"text_for_toggles": "первое;второе"}))
    assert "properties" not in condense_workspace(graph, detail="links")["nodes"][0]
    assert condense_workspace(graph, detail="full")["nodes"][0]["properties"] == {
        "text_for_toggles": "первое;второе"
    }


def test_widget_labels_travel_with_the_values_they_sit_beside():
    # The row is drawn by the widget, so this is the only text on it that a
    # translation can reach - the slot behind a converted widget draws nothing.
    graph = report(node("1", widgets={"force_rate": 0}, widget_labels={"force_rate": "частота"}))
    assert "widget_labels" not in condense_workspace(graph, detail="links")["nodes"][0]
    assert condense_workspace(graph, detail="full")["nodes"][0]["widget_labels"] == {
        "force_rate": "частота"
    }


def test_a_node_with_no_properties_says_nothing_about_them():
    assert "properties" not in condense_workspace(report(node("1")), detail="full")["nodes"][0]


def test_the_level_that_was_used_is_reported():
    assert condense_workspace(report(node("1")), detail="links")["detail"] == "links"


def test_an_unknown_detail_level_is_refused():
    with pytest.raises(ValueError, match="detail must be one of"):
        condense_workspace(report(node("1")), detail="everything")


def test_the_dull_value_of_a_flag_is_left_out():
    # `registered: true` on 140 nodes is two kilobytes of nothing.
    only_node = condense_workspace(report(node("1")), detail="full")["nodes"][0]
    for key in ("mode", "collapsed", "pinned", "registered", "backend"):
        assert key not in only_node


@pytest.mark.parametrize(
    "field,value",
    [("mode", "muted"), ("collapsed", True), ("pinned", True), ("registered", False), ("backend", False)],
)
def test_a_flag_worth_knowing_survives(field, value):
    graph = report(node("1", **{field: value}))
    assert condense_workspace(graph, detail="outline")["nodes"][0][field] == value


def test_a_node_that_could_not_be_read_says_so_at_every_level():
    # The graphs worth reporting on are the broken ones; this must never be the
    # field that trimming removes.
    graph = report(node("1", describe_error="getInputLink threw"))
    for level in DETAIL_LEVELS:
        assert condense_workspace(graph, detail=level)["nodes"][0]["describe_error"]


def test_a_title_that_only_repeats_the_type_is_dropped():
    assert "title" not in condense_workspace(report(node("1")), detail="outline")["nodes"][0]


def test_a_title_the_author_wrote_is_kept():
    graph = report(node("1", title="Sampler shift (Clean model)"))
    assert condense_workspace(graph, detail="outline")["nodes"][0]["title"] == "Sampler shift (Clean model)"


def test_the_text_a_slot_actually_draws_survives_the_trimming():
    # litegraph renders `label ?? localized_name ?? name`, so a slot can say
    # something on screen that its name does not. Trimming that away leaves a
    # reader concluding the canvas holds no wording it has not already seen.
    graph = report(node("1", inputs=[link("text", label="文本", localized_name="Text")]))
    slot = condense_workspace(graph, detail="links")["nodes"][0]["inputs"][0]
    assert slot["label"] == "文本"
    assert slot["localized_name"] == "Text"


def test_a_slot_whose_label_is_just_its_name_says_nothing_extra():
    graph = report(node("1", inputs=[link("samples")]))
    assert condense_workspace(graph, detail="links")["nodes"][0]["inputs"][0] == {
        "name": "samples",
        "type": "LATENT",
    }


def test_an_input_reports_its_falses_by_leaving_them_out():
    graph = report(node("1", inputs=[link("vae"), link("samples", "9", required=True, widget=True)]))
    slots = condense_workspace(graph, detail="links")["nodes"][0]["inputs"]
    assert slots[0] == {"name": "vae", "type": "LATENT"}
    assert slots[1]["required"] is True and slots[1]["widget"] is True
    assert slots[1]["from"] == {"node": "9", "slot": 0}


def test_an_unconnected_output_still_occupies_its_slot():
    # A slot's position in this list *is* its slot number, and `from.slot` points
    # into it. Dropping the empty ones saves a few hundred bytes and silently
    # renumbers everything after them.
    graph = report(
        node(
            "1",
            outputs=[
                {"name": "MODEL", "type": "MODEL", "links": 0},
                {"name": "CLIP", "type": "CLIP", "links": 2},
            ],
        )
    )
    outputs = condense_workspace(graph, detail="links")["nodes"][0]["outputs"]
    assert [slot["name"] for slot in outputs] == ["MODEL", "CLIP"]


def test_an_unconnected_input_also_keeps_its_place():
    graph = report(node("1", inputs=[link("vae"), link("samples", "9")]))
    slots = condense_workspace(graph, detail="links")["nodes"][0]["inputs"]
    assert [slot["name"] for slot in slots] == ["vae", "samples"]


BRANCH = report(
    node("1", outputs=[{"name": "LATENT", "type": "LATENT", "links": 1}]),
    node("2", inputs=[link("samples", "1")], outputs=[{"name": "LATENT", "type": "LATENT", "links": 1}]),
    node("3", inputs=[link("samples", "2")]),
)


def test_only_reports_the_nodes_asked_for_and_how_many_there_were():
    result = condense_workspace(BRANCH, detail="full", only=["2"])
    assert ids(result) == ["2"]
    assert result["node_count"] == 1
    assert result["of_nodes"] == 3


def test_only_names_an_id_that_is_not_there():
    # Silently returning two of three would read as "the third does not exist".
    result = condense_workspace(BRANCH, detail="outline", only=["2", "404"])
    assert result["not_found"] == ["404"]


def test_a_subset_can_be_walked_downstream_as_well_as_up():
    # An input names where it came from, so upstream is free; the report carries
    # no downstream at all, which would make a subset a dead end.
    result = condense_workspace(BRANCH, detail="outline", only=["2"])
    assert result["nodes"][0]["feeds"] == ["3"]


def test_a_node_nothing_reads_from_carries_no_feeds():
    result = condense_workspace(BRANCH, detail="outline", only=["3"])
    assert "feeds" not in result["nodes"][0]


def test_the_whole_graph_is_never_given_feeds():
    # It would repeat every link the report already contains, backwards.
    result = condense_workspace(BRANCH, detail="outline")
    assert all("feeds" not in item for item in result["nodes"])


FAT = report(*[node(str(n), widgets={"text": "x" * 200}, inputs=[link("s", "1")]) for n in range(40)])


def test_a_report_that_fits_is_left_alone():
    result = condense_workspace(report(node("1")), detail="full", max_chars=100_000)
    assert "reduced" not in result


def test_a_report_over_budget_steps_down_and_says_so():
    result = condense_workspace(FAT, detail="full", max_chars=4000)
    assert result["detail"] != "full"
    assert any("detail dropped" in line for line in result["reduced"])
    assert len(json.dumps(result, ensure_ascii=False)) <= 4000


def test_stepping_down_stops_at_the_first_level_that_fits():
    # Going straight to outline would throw away wiring that would have fitted.
    result = condense_workspace(FAT, detail="full", max_chars=12_000)
    assert result["detail"] == "links"


def test_an_outline_too_large_to_send_drops_nodes_rather_than_lying():
    result = condense_workspace(FAT, detail="outline", max_chars=600)
    assert len(result["nodes"]) < 40
    assert any("were left out" in line for line in result["reduced"])


def test_being_reduced_always_says_what_to_do_next():
    result = condense_workspace(FAT, detail="full", max_chars=4000)
    assert any("only=" in line for line in result["reduced"])


def test_a_subset_is_not_cut_down_for_being_asked_at_full_detail():
    # The point of `only` is that a handful of nodes fits where the graph does not.
    result = condense_workspace(FAT, detail="full", only=["1", "2"], max_chars=100_000)
    assert result["detail"] == "full"
    assert "reduced" not in result
    assert result["nodes"][0]["widgets"] == {"text": "x" * 200}
