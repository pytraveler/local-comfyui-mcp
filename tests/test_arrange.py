"""Tests for laying a workspace graph out.

`arrange` is pure, so the geometry is checked here rather than by looking at a
browser. What is being tested is mostly *order*: which column a node lands in and
which row within it. Exact coordinates are only asserted where the number itself
carries a decision - the gap between columns, where the block starts.
"""

from __future__ import annotations

from typing import Any

from comfyui_mcp.graph import (
    COLLAPSED_NODE_HEIGHT,
    LAYOUT_SPACING_X,
    LAYOUT_SPACING_Y,
    arrange,
)


def node(
    node_id: str,
    *,
    pos: tuple[float, float] = (0, 0),
    size: tuple[float, float] = (200, 100),
    feeds_from: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "AnyNode",
        "pos": list(pos),
        "size": list(size),
        "inputs": [
            {"name": f"in{i}", "type": "*", "widget": False, "required": True, "from": {"node": origin, "slot": 0}}
            for i, origin in enumerate(feeds_from or [])
        ],
        "outputs": [],
    }


def placed(result: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, list[float]]:
    """Where every node ends up - the answer merged over the ones that did not move."""
    out = {str(n["id"]): [float(n["pos"][0]), float(n["pos"][1])] for n in nodes}
    out.update({k: [float(v[0]), float(v[1])] for k, v in result["positions"].items()})
    return out


def columns_of(result: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[float, list[str]]:
    """Node ids grouped by the x they landed on, each column top to bottom."""
    by_x: dict[float, list[tuple[float, str]]] = {}
    for node_id, (x, y) in placed(result, nodes).items():
        by_x.setdefault(x, []).append((y, node_id))
    return {x: [node_id for _, node_id in sorted(rows)] for x, rows in by_x.items()}


def test_a_chain_becomes_one_node_per_column_left_to_right() -> None:
    nodes = [node("1"), node("2", feeds_from=["1"]), node("3", feeds_from=["2"])]
    result = arrange(nodes)

    assert result["columns"] == 3
    xs = [placed(result, nodes)[node_id][0] for node_id in ("1", "2", "3")]
    assert xs == sorted(xs) and len(set(xs)) == 3


def test_two_sources_feeding_one_node_share_a_column() -> None:
    nodes = [node("1"), node("2"), node("3", feeds_from=["1", "2"])]
    columns = columns_of(arrange(nodes), nodes)

    assert sorted(columns[min(columns)]) == ["1", "2"]
    assert columns[max(columns)] == ["3"]


def test_a_node_sits_past_its_furthest_source_not_its_nearest() -> None:
    # 1 -> 2 -> 3 and 1 -> 3: the long path decides, so 3 never lands on 2.
    nodes = [node("1"), node("2", feeds_from=["1"]), node("3", feeds_from=["1", "2"])]
    result = arrange(nodes)

    assert result["columns"] == 3
    where = placed(result, nodes)
    assert where["3"][0] > where["2"][0] > where["1"][0]


def test_the_gap_between_columns_is_the_spacing_past_the_widest_node() -> None:
    nodes = [node("1", size=(400, 100)), node("2", size=(200, 100)), node("3", feeds_from=["1", "2"])]
    where = placed(arrange(nodes), nodes)

    assert where["3"][0] - where["1"][0] == 400 + LAYOUT_SPACING_X


def test_nodes_stacked_in_a_column_clear_each_other_by_the_spacing() -> None:
    nodes = [node("1", size=(200, 100)), node("2", size=(200, 300)), node("3", feeds_from=["1", "2"])]
    where = placed(arrange(nodes), nodes)

    top, bottom = sorted((where["1"][1], where["2"][1]))
    heights = {where["1"][1]: 100, where["2"][1]: 300}
    assert bottom - top == heights[top] + LAYOUT_SPACING_Y


def test_a_column_is_reordered_to_face_what_feeds_it() -> None:
    # 1 and 2 each feed one of 3 and 4, but 3 and 4 start in the opposite order.
    nodes = [
        node("1", pos=(0, 0)),
        node("2", pos=(0, 200)),
        node("3", pos=(400, 200), feeds_from=["1"]),
        node("4", pos=(400, 0), feeds_from=["2"]),
    ]
    columns = columns_of(arrange(nodes), nodes)

    assert columns[min(columns)] == ["1", "2"]
    assert columns[max(columns)] == ["3", "4"]


def test_nodes_with_nothing_feeding_them_keep_the_order_the_author_had() -> None:
    nodes = [node("a", pos=(0, 500)), node("b", pos=(0, 100)), node("c", pos=(0, 300))]
    columns = columns_of(arrange(nodes), nodes)

    assert columns[min(columns)] == ["b", "c", "a"]


def test_columns_are_centred_against_each_other() -> None:
    nodes = [node("1", size=(200, 100)), node("2", size=(200, 100)), node("3", feeds_from=["1", "2"])]
    where = placed(arrange(nodes), nodes)

    column = sorted((where["1"][1], where["2"][1]))
    assert where["3"][1] + 100 / 2 == (column[0] + column[1] + 100) / 2


# --- where the block lands ---------------------------------------------------


def test_the_block_stays_where_the_author_left_it() -> None:
    nodes = [node("1", pos=(3000, 1200)), node("2", pos=(3400, 1200), feeds_from=["1"])]
    where = placed(arrange(nodes), nodes)

    assert min(x for x, _ in where.values()) == 3000
    assert min(y for _, y in where.values()) == 1200


def test_an_explicit_origin_wins_over_where_the_nodes_were() -> None:
    nodes = [node("1", pos=(3000, 1200)), node("2", pos=(3400, 1200), feeds_from=["1"])]
    where = placed(arrange(nodes, origin=(0, 0)), nodes)

    assert min(x for x, _ in where.values()) == 0
    assert min(y for _, y in where.values()) == 0


def test_nodes_already_in_place_are_left_out_of_the_answer() -> None:
    nodes = [node("1"), node("2", feeds_from=["1"])]
    once = arrange(nodes)
    for node_id, pos in once["positions"].items():
        next(n for n in nodes if n["id"] == node_id)["pos"] = pos

    twice = arrange(nodes)
    assert twice["positions"] == {}
    assert twice["moved"] == 0 and twice["unchanged"] == len(nodes)


def test_a_cycle_is_laid_out_rather_than_refused() -> None:
    nodes = [node("1", feeds_from=["3"]), node("2", feeds_from=["1"]), node("3", feeds_from=["2"])]
    result = arrange(nodes)

    assert len(placed(result, nodes)) == 3


def test_a_loop_back_into_a_node_does_not_push_it_past_its_own_consumers() -> None:
    # The Power Lora Loader shape, and the reason back edges are dropped rather
    # than relaxed over: 30 hands a model to 77, which hands it back. Left in,
    # that link made 30 a column *right* of the previews it feeds.
    nodes = [
        node("src"),
        node("30", feeds_from=["src", "77"]),
        node("77", feeds_from=["30"]),
        node("preview", feeds_from=["30"]),
    ]
    where = placed(arrange(nodes), nodes)

    assert where["30"][0] > where["src"][0]
    assert where["preview"][0] > where["30"][0]
    assert where["77"][0] > where["30"][0]


def test_links_from_outside_the_set_are_ignored() -> None:
    # Arranging a subset: node 2's source is not being arranged, so it cannot
    # decide a column, and node 2 must not vanish or land on top of node 1.
    nodes = [node("1"), node("2", feeds_from=["99"])]
    columns = columns_of(arrange(nodes), nodes)

    assert sorted(columns[min(columns)]) == ["1", "2"]


def test_a_link_across_a_subgraph_boundary_does_not_decide_a_column() -> None:
    # `30:-10` is a subgraph's own input node, and `30:5` is a level deeper than
    # `7` - neither is a node on this canvas, so neither can place one.
    nodes = [node("7", feeds_from=["30:-10"]), node("8", feeds_from=["30:5"])]
    columns = columns_of(arrange(nodes), nodes)

    assert sorted(columns[min(columns)]) == ["7", "8"]


def test_a_collapsed_node_takes_the_room_it_draws_in_not_the_room_it_reports() -> None:
    # A folded-up node keeps reporting the size it will have when unfolded, so
    # believing it leaves several hundred pixels of nothing under it.
    nodes = [
        node("1", size=(200, 600)),
        node("2", size=(200, 600)),
        node("3", feeds_from=["1", "2"]),
    ]
    nodes[0]["collapsed"] = True
    where = placed(arrange(nodes), nodes)

    assert abs(where["2"][1] - where["1"][1]) == COLLAPSED_NODE_HEIGHT + LAYOUT_SPACING_Y


def test_a_node_with_no_size_yet_still_occupies_a_row() -> None:
    nodes = [node("1", size=(0, 0)), node("2", size=(0, 0)), node("3", feeds_from=["1", "2"])]
    where = placed(arrange(nodes), nodes)

    assert where["1"][1] != where["2"][1]


def test_an_empty_graph_answers_rather_than_throwing() -> None:
    assert arrange([])["positions"] == {}
