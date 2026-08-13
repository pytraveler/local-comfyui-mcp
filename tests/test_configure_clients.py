"""Tests for generating a client's MCP config.

The generator is checked against the three configs this repository already ships,
because those are the ones known to work: if `.mcp.json` and `opencode.jsonc` ever
change shape, the window must not keep producing the old one.

Those three are not in a release - they hold this machine's absolute paths - so the
tests that read one skip there rather than fail. The specification is simply absent,
and saying so is more use than a red line about a file nobody removed by accident.

The other half is merging into a file somebody else owns. Two rules carry it: a
file with comments is never rewritten - reserialising JSON throws every comment
away - and everything already in the file survives.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from comfyui_mcp import configure_clients as CC

REPO = Path(__file__).resolve().parents[1]


def test_the_claude_shape_splits_the_command_from_its_arguments():
    made = CC.entry(CC.CLAUDE, REPO)
    assert made["command"].endswith(("python.exe", "python"))
    assert made["args"] == ["-m", "comfyui_mcp.server"]
    assert made["env"] == {"PYTHONPATH": str(REPO / "src")}


def test_the_local_shape_puts_the_whole_command_line_in_one_array():
    made = CC.entry(CC.LOCAL, REPO)
    assert made["type"] == "local"
    assert made["command"][1:] == ["-m", "comfyui_mcp.server"]
    assert made["enabled"] is True


def test_the_local_shape_says_environment_and_never_env():
    # OpenCode's McpLocalConfig sets "additionalProperties": false, so a stray
    # `env` is a schema error rather than a key it quietly ignores.
    made = CC.entry(CC.LOCAL, REPO)
    assert "environment" in made and "env" not in made


def test_the_claude_shape_has_no_timeout_because_nothing_reads_it():
    assert "timeout" not in CC.entry(CC.CLAUDE, REPO)


def test_the_cursor_shape_is_the_claude_one_plus_a_type():
    # Cursor's documentation calls `type` required in its table of fields and
    # leaves it out of its own example. Sending it is the safe side of that:
    # an extra key is harmless where it is optional, a missing one is not.
    cursor = CC.entry(CC.CURSOR, REPO)
    assert cursor["type"] == "stdio"
    assert {k: v for k, v in cursor.items() if k != "type"} == CC.entry(CC.CLAUDE, REPO)


def test_only_cursor_is_sent_the_stdio_type():
    # Nothing says the other clients on the same entry form tolerate an extra key.
    typed = [c.name for c in CC.CLIENTS if CC.entry(c.shape, REPO).get("type") == "stdio"]
    assert typed == ["cursor"]


def test_cursor_and_claude_share_a_container():
    assert CC.root_path(CC.CURSOR) == CC.root_path(CC.CLAUDE) == ("mcpServers",)


def test_an_unknown_shape_has_no_container_either():
    with pytest.raises(ValueError, match="unknown shape"):
        CC.root_path("something-else")


def test_there_are_only_two_entry_forms():
    forms = {CC.shape_of(name).array_command for name in CC.SHAPES}
    assert forms == {True, False}


@pytest.mark.parametrize(
    "shape,root",
    [
        (CC.CLAUDE, ("mcpServers",)),
        (CC.CURSOR, ("mcpServers",)),
        (CC.LOCAL, ("mcp",)),
        (CC.OPENCLAW, ("mcp", "servers")),
        (CC.HERMES, ("mcp_servers",)),
        (CC.CODEX, ("mcp_servers",)),
    ],
)
def test_each_shape_knows_where_its_entry_sits(shape, root):
    assert CC.root_path(shape) == root


def test_openclaws_container_is_nested_two_deep():
    doc = CC.document(CC.OPENCLAW, REPO)
    assert list(doc) == ["mcp"] and "comfyui" in doc["mcp"]["servers"]


def test_nesting_builds_the_path_it_is_given():
    assert CC.nest(("a", "b"), 1) == {"a": {"b": 1}}
    assert CC.nest(("a",), 1) == {"a": 1}


def test_yaml_quotes_paths_so_a_backslash_stays_a_backslash():
    # A double-quoted YAML scalar honours escapes, and every path here is a
    # Windows one: "C:\Users" would come back mangled. Single quotes do not.
    out = CC.render(CC.document(CC.HERMES, Path("C:\\Comfy")), CC.YAML)
    assert "'C:\\Comfy\\.venv" in out or "'C:\\Comfy/.venv" in out
    assert '"C:' not in out


def test_yaml_puts_the_arguments_on_their_own_lines():
    out = CC.render(CC.document(CC.HERMES, REPO), CC.YAML)
    assert "mcp_servers:" in out
    assert "\n      - '-m'" in out


def test_toml_writes_env_as_its_own_table():
    # Which is how the Codex documentation's own example shows it.
    out = CC.render(CC.document(CC.CODEX, REPO), CC.TOML)
    assert "[mcp_servers.comfyui]" in out
    assert "[mcp_servers.comfyui.env]" in out
    assert "PYTHONPATH = " in out


def test_toml_is_read_back_by_the_standard_library():
    # The emitter is hand-written, so something else has to agree it is TOML.
    import tomllib

    out = CC.render(CC.document(CC.CODEX, REPO), CC.TOML)
    back = tomllib.loads(out)["mcp_servers"]["comfyui"]
    assert back["args"] == ["-m", "comfyui_mcp.server"]
    assert back["command"].endswith(("python.exe", "python"))
    assert back["env"]["PYTHONPATH"].endswith("src")


def test_a_windows_path_survives_the_toml_round_trip():
    import tomllib

    out = CC.render(CC.document(CC.CODEX, Path("C:\\Comfy")), CC.TOML)
    assert tomllib.loads(out)["mcp_servers"]["comfyui"]["command"].startswith("C:\\Comfy")


def test_an_unknown_format_is_refused():
    with pytest.raises(ValueError, match="unknown format"):
        CC.render({}, "ini")


def test_the_timeout_is_derived_from_the_run_timeout():
    # Written down, the two drift apart; computed, the client can never give up
    # while ComfyUI is still generating - and the queue would keep going anyway.
    assert CC.timeout_ms(900) == 960_000
    assert CC.entry(CC.LOCAL, REPO, run_timeout_s=1800)["timeout"] == 1_860_000


def test_an_unknown_shape_is_refused():
    with pytest.raises(ValueError, match="unknown shape"):
        CC.entry("something-else", REPO)


def test_each_shape_knows_its_root_key():
    assert CC.root_path(CC.CLAUDE) == ("mcpServers",)
    assert CC.root_path(CC.LOCAL) == ("mcp",)


def needs(name: str):
    return pytest.mark.skipif(
        not (REPO / name).is_file(),
        reason=f"released checkout: {name} is not shipped, it holds absolute paths",
    )


def committed(name: str) -> dict:
    return json.loads(CC.strip_comments((REPO / name).read_text(encoding="utf-8")))


@needs(".mcp.json")
def test_the_generated_claude_config_matches_the_committed_one():
    theirs = committed(".mcp.json")["mcpServers"]["comfyui"]
    ours = CC.entry(CC.CLAUDE, REPO)
    assert sorted(theirs) == sorted(ours)
    assert theirs["args"] == ours["args"]
    assert sorted(theirs["env"]) == sorted(ours["env"])


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("opencode.jsonc", marks=needs("opencode.jsonc")),
        pytest.param("kilo.jsonc", marks=needs("kilo.jsonc")),
    ],
)
def test_the_generated_local_config_matches_the_committed_ones(name):
    theirs = committed(name)["mcp"]["comfyui"]
    ours = CC.entry(CC.LOCAL, REPO)
    assert sorted(theirs) == sorted(ours)
    assert theirs["type"] == ours["type"]
    assert theirs["command"][1:] == ours["command"][1:]
    assert theirs["timeout"] == ours["timeout"]


def test_every_client_names_a_shape_that_exists():
    assert all(client.shape in CC.SHAPES for client in CC.CLIENTS)


@needs("kilo.jsonc")
def test_kilos_global_file_wants_the_other_shape_from_its_project_file():
    # Not a typo and not a guess: the mcp_settings.json Kilo's extension creates
    # for itself contains {"mcpServers": {}}, while the kilo.jsonc committed here
    # - a project-level config - is mcp / type: local. Defaulting the global
    # target to the project form would produce a file the extension ignores.
    assert CC.BY_NAME["kilo"].shape == CC.CLAUDE
    assert committed("kilo.jsonc").keys() == {"$schema", "mcp"}
    assert "mcpServers" in CC.BY_NAME["kilo"].note.en
    assert "mcpServers" in CC.BY_NAME["kilo"].note.ru


@needs("kilo.jsonc")
def test_a_url_inside_a_string_is_not_a_comment():
    # "$schema": "https://opencode.ai/config.json" contains // and is not one.
    assert CC.has_comments((REPO / "kilo.jsonc").read_text(encoding="utf-8")) is False


@needs("opencode.jsonc")
def test_a_real_comment_is_found():
    assert CC.has_comments((REPO / "opencode.jsonc").read_text(encoding="utf-8")) is True


def test_both_comment_styles_count():
    assert CC.has_comments('{"a": 1} // note') is True
    assert CC.has_comments('{"a": /* note */ 1}') is True


@needs("opencode.jsonc")
def test_stripping_comments_leaves_valid_json():
    text = (REPO / "opencode.jsonc").read_text(encoding="utf-8")
    assert json.loads(CC.strip_comments(text))["mcp"]["comfyui"]["type"] == "local"


def test_merging_into_an_empty_file_writes_the_whole_document():
    out = json.loads(CC.merge("", CC.CLAUDE, {"command": "python"}))
    assert out == {"mcpServers": {"comfyui": {"command": "python"}}}


def test_another_server_in_the_same_file_survives():
    existing = json.dumps({"mcpServers": {"other": {"command": "x"}}})
    out = json.loads(CC.merge(existing, CC.CLAUDE, {"command": "python"}))
    assert set(out["mcpServers"]) == {"other", "comfyui"}


def test_unrelated_top_level_settings_survive():
    existing = json.dumps({"theme": "dark", "mcp": {}})
    out = json.loads(CC.merge(existing, CC.LOCAL, {"type": "local"}))
    assert out["theme"] == "dark"


def test_an_earlier_version_of_our_own_entry_is_replaced_not_duplicated():
    existing = json.dumps({"mcpServers": {"comfyui": {"command": "old"}}})
    out = json.loads(CC.merge(existing, CC.CLAUDE, {"command": "new"}))
    assert out["mcpServers"]["comfyui"] == {"command": "new"}


def test_a_nested_container_is_created_on_the_way_down():
    out = json.loads(CC.merge("{}", CC.OPENCLAW, {"command": "python"}))
    assert out["mcp"]["servers"]["comfyui"] == {"command": "python"}


def test_a_nested_container_that_already_exists_keeps_its_neighbours():
    existing = json.dumps({"mcp": {"servers": {"other": {}}, "enabled": True}})
    out = json.loads(CC.merge(existing, CC.OPENCLAW, {"command": "python"}))
    assert set(out["mcp"]["servers"]) == {"other", "comfyui"}
    assert out["mcp"]["enabled"] is True


@pytest.mark.parametrize("shape", [CC.HERMES, CC.CODEX])
def test_a_format_that_is_only_written_is_never_merged(shape):
    # Reading YAML or TOML back through an emitter this narrow would drop
    # whatever else lives in the file - and for both of these, that is most of it.
    with pytest.raises(CC.MergeRefused, match="only generated here"):
        CC.merge("anything: at all\n", shape, {})


def test_a_commented_file_is_refused_rather_than_rewritten():
    # Reserialising would drop every comment; a repo whose own configs carry
    # explanations is not one to do that to somebody else's file.
    with pytest.raises(CC.MergeRefused, match="carries comments"):
        CC.merge('{ // почему именно так\n "mcpServers": {} }', CC.CLAUDE, {})


def test_a_file_that_is_not_json_is_refused_with_the_line_number():
    with pytest.raises(CC.MergeRefused, match="line"):
        CC.merge("{not json", CC.CLAUDE, {})


def test_a_root_key_holding_something_other_than_an_object_is_refused():
    with pytest.raises(CC.MergeRefused, match="not an object"):
        CC.merge(json.dumps({"mcpServers": []}), CC.CLAUDE, {})


def test_a_file_whose_top_level_is_a_list_is_refused():
    with pytest.raises(CC.MergeRefused, match="not an object"):
        CC.merge("[]", CC.CLAUDE, {})


def test_writing_creates_the_file_and_any_missing_folders(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "mcp.json"
    CC.write(target, CC.CLAUDE, CC.entry(CC.CLAUDE, REPO))
    assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["comfyui"]["args"]


def test_writing_over_an_existing_file_keeps_a_copy(tmp_path: Path):
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")

    report = CC.write(target, CC.CLAUDE, {"command": "python"})

    assert ".bak" in report.en and ".bak" in report.ru
    assert "other" in json.loads((tmp_path / "mcp.json.bak").read_text(encoding="utf-8"))["mcpServers"]
    assert set(json.loads(target.read_text(encoding="utf-8"))["mcpServers"]) == {"other", "comfyui"}


def test_a_commented_file_is_left_alone_entirely(tmp_path: Path):
    target = tmp_path / "opencode.jsonc"
    before = '{\n  // объяснение\n  "mcp": {}\n}\n'
    target.write_text(before, encoding="utf-8")

    with pytest.raises(CC.MergeRefused):
        CC.write(target, CC.LOCAL, {"type": "local"})

    assert target.read_text(encoding="utf-8") == before
    assert not (tmp_path / "opencode.jsonc.bak").exists()


def test_a_tilde_is_expanded():
    assert CC.expand("~/.lmstudio/mcp.json").startswith(str(Path.home()))


@pytest.mark.skipif(os.name != "nt", reason="expandvars only understands %VAR% on Windows")
def test_a_windows_variable_is_expanded(monkeypatch):
    # Kilo's default path is the one that needs this. On POSIX the same call
    # leaves %APPDATA% alone, which is right - there is no such variable there,
    # and Kilo keeps its settings somewhere else anyway.
    monkeypatch.setenv("APPDATA", "D:\\Roaming")
    assert CC.expand("%APPDATA%/Code/User").startswith("D:\\Roaming")


def test_a_client_with_no_config_file_expands_to_nothing():
    assert CC.expand("") == ""
    assert CC.BY_NAME["cherry"].location == ""


def test_writing_a_new_yaml_file_is_allowed_because_nothing_is_lost(tmp_path: Path):
    target = tmp_path / "config.yaml"
    CC.write(target, CC.HERMES, CC.entry(CC.HERMES, REPO))
    assert target.read_text(encoding="utf-8").startswith("mcp_servers:")


def test_writing_over_an_existing_yaml_file_is_not(tmp_path: Path):
    target = tmp_path / "config.yaml"
    target.write_text("model: hermes\n", encoding="utf-8")
    with pytest.raises(CC.MergeRefused):
        CC.write(target, CC.HERMES, CC.entry(CC.HERMES, REPO))
    assert target.read_text(encoding="utf-8") == "model: hermes\n"


def test_a_new_toml_file_is_valid_toml(tmp_path: Path):
    import tomllib

    target = tmp_path / "config.toml"
    CC.write(target, CC.CODEX, CC.entry(CC.CODEX, REPO))
    assert "comfyui" in tomllib.loads(target.read_text(encoding="utf-8"))["mcp_servers"]


@pytest.mark.parametrize("title", ["Harness", "Ollama", "vLLM"])
def test_a_server_mistaken_for_a_client_is_named_rather_than_left_out(title):
    # Somebody who came looking would otherwise read the silence as an oversight.
    assert title in [name for name, _ in CC.NOT_CLIENTS]
    assert title in CC.as_text()


def test_nothing_is_both_a_client_and_not_one():
    listed = {c.title for c in CC.CLIENTS}
    assert all(title not in listed for title, _ in CC.NOT_CLIENTS)


def test_the_listing_says_what_to_do_with_an_inference_engine_instead():
    # The useful half of the refusal: the engine is reached through a frontend,
    # and the frontend's own config is the one to generate.
    assert "OpenAI-compatible provider" in CC.as_text()
    assert "OpenAI-совместимого провайдера" in CC.as_text(lang="ru")


def test_llama_cpp_is_not_in_that_list_because_it_grew_a_client():
    assert "llama.cpp" not in [title for title, _ in CC.NOT_CLIENTS]
    assert CC.BY_NAME["llamacpp"] in CC.CLIENTS


def test_the_listing_covers_every_client():
    text = CC.as_text()
    assert all(client.title in text for client in CC.CLIENTS)


def test_llama_cpp_is_a_client_like_the_rest():
    # llama-server has its own MCP client and asks for "Cursor-compatible format",
    # which is the claude shape. Its config has no fixed home - the path is handed
    # to the process - so the default is a file in this checkout.
    llama = CC.BY_NAME["llamacpp"]
    assert llama.shape == CC.CLAUDE
    assert llama.location.endswith(".json")
    assert CC.LLAMA_CPP_FLAG in llama.note.en and CC.LLAMA_CPP_FLAG in llama.note.ru


def test_the_flag_llama_cpp_is_told_the_path_with_is_spelled_out_once():
    # Plural "servers", singular "config". Getting it wrong costs an evening, and
    # it appears in two places, so it is a constant rather than two strings.
    assert CC.LLAMA_CPP_FLAG == "--mcp-servers-config"
    assert CC.LLAMA_CPP_FLAG in CC.FOOTER_NOTE.en and CC.LLAMA_CPP_FLAG in CC.FOOTER_NOTE.ru


def test_every_client_explains_itself():
    assert all(client.note for client in CC.CLIENTS)
