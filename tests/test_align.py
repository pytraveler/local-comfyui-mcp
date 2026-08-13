"""Tests for snapping nodes to a line and spacing them evenly.

`align` never reads a link - it moves what it is given along one axis and leaves
everything else alone. So what is worth testing is that it moves along exactly
one axis, that it respects how big each node draws, and that it refuses the two
instructions that contradict each other.
"""

from __future__ import annotations

from typing import Any

import pytest

from comfyui_mcp.graph import COLLAPSED_NODE_HEIGHT, align


def node(node_id: str, pos: tuple[float, float], size: tuple[float, float] = (200, 100)) -> dict[str, Any]:
    return {"id": node_id, "type": "AnyNode", "pos": list(pos), "size": list(size)}


def placed(result: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, list[float]]:
    out = {str(n["id"]): [float(n["pos"][0]), float(n["pos"][1])] for n in nodes}
    out.update({k: [float(v[0]), float(v[1])] for k, v in result["positions"].items()})
    return out


def test_aligning_left_puts_every_node_on_the_leftmost_edge() -> None:
    nodes = [node("1", (100, 0)), node("2", (400, 200)), node("3", (250, 400))]
    where = placed(align(nodes, edge="left"), nodes)

    assert [x for x, _ in where.values()] == [100, 100, 100]


def test_aligning_right_lines_up_the_far_edges_not_the_near_ones() -> None:
    # Different widths, so equal x would be wrong and equal x+width is right.
    nodes = [node("1", (0, 0), size=(200, 100)), node("2", (0, 200), size=(500, 100))]
    where = placed(align(nodes, edge="right"), nodes)

    assert where["1"][0] + 200 == where["2"][0] + 500 == 500


def test_aligning_bottom_accounts_for_how_tall_each_node_is() -> None:
    nodes = [node("1", (0, 0), size=(200, 100)), node("2", (300, 0), size=(200, 400))]
    where = placed(align(nodes, edge="bottom"), nodes)

    assert where["1"][1] + 100 == where["2"][1] + 400 == 400


def test_centring_uses_the_middle_of_the_selection_not_the_average_node() -> None:
    # Two nodes bunched left and one far right: the average of the three centres
    # would sit left of the middle, which is not what "centre" means on a canvas.
    nodes = [node("1", (0, 0)), node("2", (10, 200)), node("3", (800, 400))]
    where = placed(align(nodes, edge="centre_x"), nodes)

    centres = {x + 100 for x, _ in where.values()}
    assert centres == {(0 + 1000) / 2}


def test_snapping_one_axis_leaves_the_other_untouched() -> None:
    nodes = [node("1", (100, 17)), node("2", (400, 933))]
    where = placed(align(nodes, edge="left"), nodes)

    assert [y for _, y in where.values()] == [17, 933]


def test_a_collapsed_node_lines_up_by_what_it_draws() -> None:
    nodes = [node("1", (0, 0), size=(200, 100)), node("2", (300, 0), size=(200, 800))]
    nodes[1]["collapsed"] = True
    where = placed(align(nodes, edge="bottom"), nodes)

    assert where["1"][1] + 100 == where["2"][1] + COLLAPSED_NODE_HEIGHT


def test_distributing_keeps_the_outermost_nodes_where_they_are() -> None:
    nodes = [node("1", (0, 0)), node("2", (150, 0)), node("3", (1000, 0))]
    where = placed(align(nodes, distribute="x"), nodes)

    assert where["1"][0] == 0
    assert where["3"][0] == 1000


def test_distributing_evens_the_gaps_rather_than_the_centres() -> None:
    # Widths differ, so equal centre-to-centre spacing would leave visibly
    # different amounts of space between the boxes.
    nodes = [
        node("1", (0, 0), size=(100, 100)),
        node("2", (200, 0), size=(400, 100)),
        node("3", (900, 0), size=(100, 100)),
    ]
    where = placed(align(nodes, distribute="x"), nodes)

    first_gap = where["2"][0] - (where["1"][0] + 100)
    second_gap = where["3"][0] - (where["2"][0] + 400)
    assert first_gap == second_gap


def test_an_explicit_spacing_packs_from_the_first_node() -> None:
    nodes = [node("1", (0, 0), size=(200, 100)), node("2", (900, 0), size=(200, 100))]
    where = placed(align(nodes, distribute="x", spacing=50), nodes)

    assert where["1"][0] == 0
    assert where["2"][0] == 250


def test_evening_out_the_gaps_never_reorders_the_nodes() -> None:
    nodes = [node("c", (900, 0)), node("a", (0, 0)), node("b", (100, 0))]
    where = placed(align(nodes, distribute="x"), nodes)

    assert where["a"][0] < where["b"][0] < where["c"][0]


def test_a_row_can_be_lined_up_and_spread_in_one_call() -> None:
    nodes = [node("1", (0, 40)), node("2", (150, 900)), node("3", (1000, 12))]
    where = placed(align(nodes, edge="top", distribute="x"), nodes)

    assert {y for _, y in where.values()} == {12}
    assert where["1"][0] < where["2"][0] < where["3"][0]


def test_snapping_and_spreading_along_the_same_axis_is_refused() -> None:
    nodes = [node("1", (0, 0)), node("2", (300, 0))]
    with pytest.raises(ValueError, match="both act on the x axis"):
        align(nodes, edge="left", distribute="x")


def test_an_edge_that_is_not_one_lists_the_ones_that_are() -> None:
    with pytest.raises(ValueError, match="centre_x"):
        align([node("1", (0, 0)), node("2", (1, 1))], edge="sideways")


def test_being_asked_for_nothing_is_refused_rather_than_returning_nothing() -> None:
    with pytest.raises(ValueError, match="nothing to do"):
        align([node("1", (0, 0)), node("2", (1, 1))])


def test_one_node_cannot_be_aligned_with_itself() -> None:
    with pytest.raises(ValueError, match="at least two"):
        align([node("1", (0, 0))], edge="left")


def test_nodes_already_on_the_line_are_left_out_of_the_answer() -> None:
    nodes = [node("1", (100, 0)), node("2", (100, 200)), node("3", (400, 400))]
    result = align(nodes, edge="left")

    assert set(result["positions"]) == {"3"}
    assert result["moved"] == 1 and result["unchanged"] == 2
