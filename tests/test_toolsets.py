"""Tests for the switch that narrows which tools the server offers.

Two constraints hold the module together, and they pull in opposite directions.
A spec must mean what a person reading it thinks it means - which is why a
leading `-download` is "everything but that" while a leading `workflows` is
"only that", and why a tool name beats a group name whatever the order. And a
healthy install must produce no warnings at all: the dependency notes exist to
catch a combination that leaves a tool unable to report on itself, and a note
that fires on the default configuration would train everyone to ignore all of
them.
"""

from __future__ import annotations

import pytest

from comfyui_mcp import toolsets as T


def tools(*rows: tuple[str, str, bool]) -> list[T.Tool]:
    return [T.Tool(name=n, group=g, risk="reads", enabled=e) for n, g, e in rows]


def enabled(spec: str, *rows: tuple[str, str]) -> set[str]:
    selection = T.parse(spec)
    return {name for name, group in rows if selection.allows(name, group)}


SOME = (("download_model", "download"), ("run_workflow", "run"), ("list_models", "workflows"))


def test_an_empty_spec_offers_everything():
    assert enabled("", *SOME) == {"download_model", "run_workflow", "list_models"}


def test_a_leading_minus_means_everything_but_that():
    assert enabled("-download", *SOME) == {"run_workflow", "list_models"}


def test_a_leading_name_means_only_that():
    assert enabled("workflows", *SOME) == {"list_models"}


def test_several_groups_can_be_removed_at_once():
    assert enabled("-download,-run", *SOME) == {"list_models"}


def test_all_is_the_explicit_form_of_the_default():
    assert enabled("all", *SOME) == {"download_model", "run_workflow", "list_models"}


def test_all_after_a_narrowing_starts_again():
    assert enabled("workflows,all", *SOME) == {"download_model", "run_workflow", "list_models"}


def test_minus_all_leaves_nothing_but_what_is_always_on():
    assert enabled("-all", *SOME) == set()
    assert T.parse("-all").allows("comfy_status", "status") is True


def test_a_tool_name_beats_its_group_when_it_comes_second():
    assert enabled("download,-download_model", ("download_model", "download")) == set()


def test_a_tool_name_beats_its_group_when_it_comes_first():
    # Order must not matter, or `workspace,-remove_workspace_nodes` and its
    # reverse would mean different things and nobody could remember which.
    assert enabled("-download_model,download", ("download_model", "download")) == set()


def test_one_tool_can_be_kept_out_of_a_group_that_is_off():
    got = enabled("-workspace,get_workspace_graph", ("get_workspace_graph", "workspace"),
                  ("save_workspace", "workspace"))
    assert got == {"get_workspace_graph"}


def test_repeating_a_name_lets_the_last_word_win():
    assert enabled("-download,download", ("download_model", "download")) == {"download_model"}


def test_blanks_and_stray_separators_are_ignored():
    assert enabled(" workflows , , ", *SOME) == {"list_models"}


def test_the_status_group_cannot_be_switched_off():
    # It is where a caller learns that the rest was switched off and how to put
    # it back; a server that cannot say that looks like one that never had them.
    for spec in ("-status", "-comfy_status", "-all", "workflows"):
        assert T.parse(spec).allows("comfy_status", "status") is True


def test_a_spec_that_changes_nothing_says_so():
    assert T.parse("").narrowed is False
    assert T.parse("all").narrowed is False
    assert T.parse("-download").narrowed is True


def test_an_unknown_group_is_refused_at_registration():
    with pytest.raises(ValueError, match="unknown tool group"):
        T.record("some_tool", "nonesuch", "reads", True)


def test_an_unknown_risk_class_is_refused_at_registration():
    with pytest.raises(ValueError, match="unknown risk class"):
        T.record("some_tool", "run", "catastrophic", True)


def test_a_typo_in_the_spec_is_reported():
    # It silently leaves a group switched on, and nothing else would mention it.
    registry = tools(("run_workflow", "run", True))
    assert T.unknown(T.parse("-run,-downlaod"), registry) == ["downlaod"]


def test_real_names_are_not_reported_as_typos():
    registry = tools(("run_workflow", "run", True))
    assert T.unknown(T.parse("-run,-run_workflow"), registry) == []


def test_a_run_with_no_way_to_watch_it_is_flagged():
    registry = tools(("run_workflow", "run", True), ("get_progress", "run", False))
    assert any("get_progress" in note for note in T.warnings(registry))


def test_a_run_that_can_be_watched_is_quiet():
    registry = tools(("run_workflow", "run", True), ("get_progress", "run", True))
    assert T.warnings(registry) == []


def test_a_name_the_registry_never_had_is_not_a_missing_one():
    # "switched off" and "this server does not have it" are different statements,
    # and only the first is something a settings file could have caused.
    registry = tools(("run_workflow", "run", True))
    assert T.warnings(registry) == []


def test_a_tool_that_is_itself_off_raises_nothing():
    registry = tools(("run_workflow", "run", False), ("get_progress", "run", False))
    assert T.warnings(registry) == []


def test_one_surviving_alternative_is_enough():
    # run_workflow points at get_result *and* show_image; either one answers.
    registry = tools(
        ("run_workflow", "run", True),
        ("get_progress", "run", True),
        ("get_result", "run", True),
        ("show_image", "workflows", False),
    )
    assert T.warnings(registry) == []


def test_editing_a_graph_that_cannot_be_read_is_flagged():
    registry = tools(("set_workspace_values", "edit", True), ("get_workspace_graph", "workspace", False))
    assert any("get_workspace_graph" in note for note in T.warnings(registry))


def test_reading_without_editing_is_fine():
    registry = tools(("get_workspace_graph", "workspace", True))
    assert T.warnings(registry) == []


def test_a_whole_install_composes_to_all():
    registry = tools(("a", "run", True), ("b", "download", True))
    assert T.compose(["a", "b"], registry) == "all"


def test_a_whole_group_composes_to_its_name():
    registry = tools(("a", "run", True), ("b", "run", True), ("c", "download", True))
    assert T.compose(["a", "b"], registry) == "run"


def test_part_of_a_group_composes_to_tool_names():
    registry = tools(("a", "run", True), ("b", "run", True))
    assert T.compose(["a"], registry) == "a"


def test_nothing_chosen_composes_to_minus_all():
    registry = tools(("a", "run", True))
    assert T.compose([], registry) == "-all"


def test_the_always_on_group_is_never_written_out():
    # It cannot be switched off, so naming it would only be noise in the file.
    registry = tools(("comfy_status", "status", True), ("a", "run", True))
    assert T.compose(["comfy_status"], registry) == "-all"


@pytest.mark.parametrize("chosen", [["a"], ["a", "b"], ["a", "b", "c"], ["c"], []])
def test_a_composed_spec_selects_what_it_was_composed_from(chosen):
    registry = tools(("a", "run", True), ("b", "run", True), ("c", "download", True))
    selection = T.parse(T.compose(chosen, registry))
    back = {t.name for t in registry if selection.allows(t.name, t.group)}
    assert back == set(chosen)


def real() -> list[T.Tool]:
    from comfyui_mcp import server  # noqa: F401 - importing is what fills the registry

    return list(T.REGISTRY)


def test_every_tool_the_server_defines_is_in_the_registry():
    assert len(real()) >= 44


def test_every_group_still_has_tools_in_it():
    # A group left behind after a rename would show as an empty box in the window.
    used = {entry.group for entry in real()}
    assert {g.name for g in T.GROUPS} == used


def test_a_full_install_produces_no_warnings():
    # The constraint `diagnose` has: a rule that fires on a healthy configuration
    # sends someone to fix what is not broken, and teaches them to skip the rest.
    everything = [T.Tool(t.name, t.group, t.risk, True, t.summary) for t in real()]
    assert T.warnings(everything) == []


def test_every_tool_carries_a_summary_taken_from_its_docstring():
    assert all(entry.summary for entry in real())


def test_the_catalogue_lists_the_groups_in_their_declared_order():
    catalogue = T.catalogue(real())
    assert [g["name"] for g in catalogue["groups"]] == [g.name for g in T.GROUPS]


def test_the_catalogue_accounts_for_every_tool():
    catalogue = T.catalogue(real())
    listed = [t["name"] for g in catalogue["groups"] for t in g["tools"]]
    assert sorted(listed) == sorted(entry.name for entry in real())


def test_every_group_says_what_it_can_cost():
    # The whole point of the window is the sentence beside the checkbox; a group
    # without one is a checkbox nobody can make an informed decision about.
    assert all(group.warning for group in T.GROUPS)


def test_the_text_listing_covers_every_group_and_tool():
    from comfyui_mcp import configure

    text = configure.as_text(T.catalogue(real()))
    assert all(group.name in text for group in T.GROUPS)
    assert all(entry.name in text for entry in real())


def test_saving_writes_a_spec_the_loader_reads_back(tmp_path, monkeypatch):
    import os

    from comfyui_mcp import config as C
    from comfyui_mcp import configure

    env = tmp_path / ".env"
    env.write_text("COMFYUI_PORT=8188\n", encoding="utf-8")
    keep = {entry.name for entry in real() if entry.group in ("status", "workflows")}

    spec = configure.save(keep, env)
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(C, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)

    assert C.load_config().tools == spec
    selection = T.parse(spec)
    assert {t.name for t in real() if selection.allows(t.name, t.group)} == keep
