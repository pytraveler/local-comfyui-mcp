"""Tests for workflow files on disk and their sidecar instruction files."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from comfyui_mcp import store
from comfyui_mcp.config import load_config

MINIMAL = {"1": {"class_type": "KSampler", "inputs": {"steps": 20}}}


@pytest.fixture
def cfg(tmp_path: Path):
    """A config whose workflows directory is empty and disposable."""
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    exports = tmp_path / "exports"
    exports.mkdir()
    return dataclasses.replace(load_config(), workflows_dir=workflows, export_dir=exports)


def write(cfg, name: str, graph=MINIMAL) -> Path:
    path = cfg.workflows_dir / f"{name}.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path


def write_guide(cfg, name: str, body: str = "# how to prompt this\n") -> Path:
    path = cfg.workflows_dir / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_listing_flags_the_workflows_that_have_a_guide(cfg):
    write(cfg, "plain")
    write(cfg, "documented")
    write_guide(cfg, "documented")
    listed = {w["name"]: w for w in store.list_workflows(cfg)}
    assert "guide" not in listed["plain"]
    assert listed["documented"]["guide"] == "documented.md"


def test_guide_is_reported_even_for_an_unreadable_workflow(cfg):
    (cfg.workflows_dir / "broken.json").write_text("{oops", encoding="utf-8")
    write_guide(cfg, "broken")
    entry = store.list_workflows(cfg)[0]
    assert entry["guide"] == "broken.md"
    assert "unreadable" in entry["error"]


def test_a_lone_markdown_file_is_not_a_workflow(cfg):
    write_guide(cfg, "orphan")
    assert store.list_workflows(cfg) == []


def test_loads_the_guide_next_to_the_workflow(cfg):
    write(cfg, "ideogram")
    write_guide(cfg, "ideogram", "emit JSON, not prose")
    text, path = store.load_guide(cfg, "ideogram")
    assert text == "emit JSON, not prose"
    assert path == cfg.workflows_dir / "ideogram.md"


def test_guide_name_accepts_the_json_suffix_too(cfg):
    write(cfg, "ideogram")
    write_guide(cfg, "ideogram")
    assert store.load_guide(cfg, "ideogram.json")[1].name == "ideogram.md"


def test_guide_path_is_none_rather_than_an_error_when_absent(cfg):
    write(cfg, "plain")
    assert store.guide_path(cfg, "plain") is None
    assert store.guide_path(cfg, "no-such-workflow") is None


def test_missing_guide_names_the_workflows_that_have_one(cfg):
    write(cfg, "plain")
    write(cfg, "documented")
    write_guide(cfg, "documented")
    with pytest.raises(store.WorkflowError, match="documented"):
        store.load_guide(cfg, "plain")


def test_unknown_workflow_fails_before_looking_for_a_guide(cfg):
    write(cfg, "plain")
    with pytest.raises(store.WorkflowError, match="not found"):
        store.load_guide(cfg, "nope")


def test_guide_lookup_cannot_escape_the_workflows_directory(cfg):
    (cfg.workflows_dir.parent / "secret.md").write_text("private", encoding="utf-8")
    (cfg.workflows_dir.parent / "secret.json").write_text("{}", encoding="utf-8")
    with pytest.raises(store.WorkflowError):
        store.load_guide(cfg, "../secret")


def test_shipped_ideogram_workflow_has_a_guide():
    cfg = load_config()
    text, path = store.load_guide(cfg, "image_ideogram4_t2i_int8_default")
    assert path.is_file()
    # The guide is the same magic-prompt template the graph carries in its
    # 'System Prompt' node, so it must keep the placeholders that template uses.
    assert "{{original_prompt}}" in text


UI_GRAPH = {"last_node_id": 3, "nodes": [{"id": 1}, {"id": 2}], "links": [], "groups": []}


def test_a_ui_export_lands_outside_the_workflows_directory(cfg):
    # list_workflows walks workflows/ recursively and calls anything that is not
    # API format an error - rightly, since nothing there could be run.
    path = store.save_export(cfg, "canvas", UI_GRAPH)
    assert path.parent == cfg.export_dir
    assert store.list_workflows(cfg) == []


def test_an_export_gets_a_json_extension_if_it_was_not_given_one(cfg):
    assert store.save_export(cfg, "canvas", UI_GRAPH).name == "canvas.json"


def test_a_dotted_name_keeps_everything_after_its_first_dot(cfg):
    assert (
        store.save_export(cfg, "bench_0.8mp_15s", UI_GRAPH).name
        == "bench_0.8mp_15s.json"
    )


def test_a_dotted_name_round_trips_through_resolution(cfg):
    saved = store.save_workflow(cfg, "bench_0.8mp_15s", MINIMAL)
    assert saved.name == "bench_0.8mp_15s.json"
    assert store.resolve_path(cfg, "bench_0.8mp_15s") == saved
    assert store.resolve_path(cfg, "bench_0.8mp_15s.json") == saved
    assert [w["name"] for w in store.list_workflows(cfg)] == ["bench_0.8mp_15s"]


def test_a_dotted_export_is_found_by_name(cfg):
    saved = store.save_export(cfg, "canvas_v1.2", UI_GRAPH)
    assert store.resolve_graph_file(cfg, "canvas_v1.2") == saved


def test_a_dotted_workflow_finds_its_guide(cfg):
    store.save_workflow(cfg, "bench_0.8mp_15s", MINIMAL)
    (cfg.workflows_dir / "bench_0.8mp_15s.md").write_text("how to fill it in", encoding="utf-8")
    text, path = store.load_guide(cfg, "bench_0.8mp_15s")
    assert text == "how to fill it in"
    assert path.name == "bench_0.8mp_15s.md"


def test_an_export_does_not_replace_a_file_by_accident(cfg):
    store.save_export(cfg, "canvas", UI_GRAPH)
    with pytest.raises(store.WorkflowError, match="already exists"):
        store.save_export(cfg, "canvas", UI_GRAPH)
    store.save_export(cfg, "canvas", UI_GRAPH, overwrite=True)


def test_an_export_cannot_escape_its_directory(cfg):
    with pytest.raises(store.WorkflowError, match="escapes"):
        store.save_export(cfg, "../../somewhere-else", UI_GRAPH)


def test_a_workflow_still_cannot_escape_its_own(cfg):
    # The two now share one writer, so this is worth keeping pinned.
    with pytest.raises(store.WorkflowError, match="escapes"):
        store.save_workflow(cfg, "../escape", MINIMAL)


def test_an_export_round_trips_as_json(cfg):
    path = store.save_export(cfg, "canvas", UI_GRAPH)
    assert json.loads(path.read_text(encoding="utf-8")) == UI_GRAPH


def test_a_workflow_is_found_and_read_as_api_format(cfg):
    write(cfg, "sampler")
    data, path, format = store.load_graph_file(cfg, "sampler")
    assert (data, format) == (MINIMAL, "api")
    assert path == cfg.workflows_dir / "sampler.json"


def test_an_export_is_found_and_read_as_ui_format(cfg):
    store.save_export(cfg, "canvas", UI_GRAPH)
    data, path, format = store.load_graph_file(cfg, "canvas")
    assert (data, format) == (UI_GRAPH, "ui")
    assert path.parent == cfg.export_dir


def test_the_workflows_directory_wins_a_tie(cfg):
    # The curated, runnable one beats a snapshot that happens to share its name.
    write(cfg, "shared")
    store.save_export(cfg, "shared", UI_GRAPH)
    assert store.load_graph_file(cfg, "shared")[2] == "api"


def test_an_unknown_name_lists_what_both_directories_hold(cfg):
    write(cfg, "sampler")
    store.save_export(cfg, "canvas", UI_GRAPH)
    with pytest.raises(store.WorkflowError) as exc:
        store.load_graph_file(cfg, "nope")
    assert "sampler" in str(exc.value) and "canvas" in str(exc.value)


def test_reading_cannot_escape_either_directory(cfg):
    (cfg.workflows_dir.parent / "secret.json").write_text("{}", encoding="utf-8")
    with pytest.raises(store.WorkflowError, match="escapes"):
        store.load_graph_file(cfg, "../secret")


def test_json_that_is_not_a_workflow_is_named_rather_than_loaded(cfg):
    (cfg.workflows_dir / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(store.WorkflowError, match="not a workflow"):
        store.load_graph_file(cfg, "list")


def test_unreadable_json_says_so(cfg):
    (cfg.workflows_dir / "broken.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(store.WorkflowError, match="not valid JSON"):
        store.load_graph_file(cfg, "broken")


def test_an_absolute_path_is_taken_as_given(cfg, tmp_path):
    outside = tmp_path / "elsewhere.json"
    outside.write_text(json.dumps(UI_GRAPH), encoding="utf-8")
    assert store.load_graph_file(cfg, str(outside))[1] == outside
