"""Tests for environment-variable configuration and .env resolution."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from comfyui_mcp import config as C


@pytest.fixture
def isolated(monkeypatch, tmp_path: Path):
    """A clean environment with no .env anywhere the loader would look."""
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(C, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_env(path: Path, body: str) -> Path:
    env = path / ".env"
    env.write_text(body, encoding="utf-8")
    return env


def test_no_env_file_uses_defaults(isolated):
    cfg = C.load_config()
    assert cfg.env_file is None
    assert cfg.base_url == "http://127.0.0.1:8188"
    assert cfg.launch_script == "run_nvidia_gpu.bat"
    assert cfg.workflows_dir == isolated / "workflows"


def test_env_file_in_project_root_is_found(isolated):
    write_env(isolated, "COMFYUI_PORT=9001\nCOMFYUI_ROOT=D:\\Comfy\n")
    cfg = C.load_config()
    assert cfg.env_file == isolated / ".env"
    assert cfg.port == 9001
    assert cfg.comfy_root == Path("D:\\Comfy")


def test_explicit_env_file_overrides_discovery(isolated, tmp_path):
    write_env(isolated, "COMFYUI_PORT=9001\n")
    other = tmp_path / "custom.env"
    other.write_text("COMFYUI_PORT=9002\n", encoding="utf-8")
    os.environ["COMFYUI_MCP_ENV_FILE"] = str(other)
    cfg = C.load_config()
    assert cfg.env_file == other
    assert cfg.port == 9002


def test_missing_explicit_env_file_is_an_error(isolated, tmp_path):
    os.environ["COMFYUI_MCP_ENV_FILE"] = str(tmp_path / "nope.env")
    with pytest.raises(C.ConfigError, match="missing file"):
        C.load_config()


def test_empty_explicit_env_file_falls_back_to_discovery(isolated):
    write_env(isolated, "COMFYUI_PORT=9001\n")
    os.environ["COMFYUI_MCP_ENV_FILE"] = ""
    assert C.load_config().port == 9001


def test_process_environment_wins_over_env_file(isolated):
    write_env(isolated, "COMFYUI_PORT=9001\nCOMFYUI_HOST=1.2.3.4\n")
    os.environ["COMFYUI_PORT"] = "9999"
    cfg = C.load_config()
    assert cfg.port == 9999
    assert cfg.host == "1.2.3.4"  # not overridden, still from .env


def test_empty_value_falls_back_to_default(isolated):
    write_env(isolated, "COMFYUI_RUN_TIMEOUT=\n")
    assert C.load_config().run_timeout == 900


def test_non_numeric_value_is_reported_with_the_variable_name(isolated):
    os.environ["COMFYUI_PORT"] = "not-a-number"
    with pytest.raises(C.ConfigError, match="COMFYUI_PORT must be a number"):
        C.load_config()


def test_all_numeric_settings_are_overridable(isolated):
    write_env(
        isolated,
        "\n".join(
            [
                "COMFYUI_REQUEST_TIMEOUT=1",
                "COMFYUI_OBJECT_INFO_TIMEOUT=2",
                "COMFYUI_STARTUP_TIMEOUT=3",
                "COMFYUI_RUN_TIMEOUT=4",
                "COMFYUI_POLL_INTERVAL=5",
                "COMFYUI_STARTUP_POLL_INTERVAL=6",
                "COMFYUI_STOP_GRACE=7",
                "COMFYUI_WS_RECV_TIMEOUT=8",
                "COMFYUI_WS_PING_INTERVAL=9",
                "COMFYUI_FALLBACK_SEED_MAX=10",
                "COMFYUI_PREVIEW_MAX_EDGE=11",
                "COMFYUI_NODE_LIST_LIMIT=12",
                "COMFYUI_MODEL_LIST_LIMIT=13",
            ]
        ),
    )
    cfg = C.load_config()
    assert (cfg.request_timeout, cfg.object_info_timeout, cfg.startup_timeout) == (1, 2, 3)
    assert (cfg.run_timeout, cfg.poll_interval, cfg.startup_poll_interval) == (4, 5, 6)
    assert (cfg.stop_grace, cfg.ws_recv_timeout, cfg.ws_ping_interval) == (7, 8, 9)
    assert (cfg.fallback_seed_max, cfg.preview_max_edge) == (10, 11)
    assert (cfg.node_list_limit, cfg.model_list_limit) == (12, 13)


def documented_in(name: str) -> set[str]:
    text = (C.PROJECT_ROOT / name).read_text(encoding="utf-8")
    return set(re.findall(r"^(COMFYUI_[A-Z_]+)=", text, re.M))


def variables_read() -> set[str]:
    source = (C.PROJECT_ROOT / "src" / "comfyui_mcp" / "config.py").read_text(encoding="utf-8")
    return set(re.findall(r'"(COMFYUI_[A-Z_]+)"', source)) - {"COMFYUI_MCP_ENV_FILE"}


@pytest.mark.parametrize("template", [".env.example", ".env.example.ru"])
def test_example_file_documents_every_variable(template: str):
    """Every template must stay in sync with what load_config actually reads.

    Both of them, because a translated template is copied into somebody's own
    .env: a setting missing from one language is a setting that person never
    learns exists.
    """
    missing = variables_read() - documented_in(template)
    assert missing == set(), f"undocumented in {template}: {missing}"


def test_the_two_templates_describe_the_same_settings():
    # The prose differs by design; the set of keys must not, or the translation
    # has quietly become a different file.
    assert documented_in(".env.example") == documented_in(".env.example.ru")


def test_the_translated_template_is_what_a_russian_install_is_seeded_from():
    assert C.example_env_file("ru").name == ".env.example.ru"
    # No template of its own falls back to the canonical English one rather than
    # to nothing - the name the documentation refers to.
    assert C.example_env_file("de").name == ".env.example"
    assert C.example_env_file("").name == ".env.example"


def test_derived_paths_hang_off_comfy_root(isolated):
    # The root is spelled with this platform's separator rather than a literal
    # "D:\Comfy": a backslash is an ordinary character in a POSIX path, so the
    # hardcoded Windows form made this assert that one component equals two.
    root = Path("D:\\Comfy") if os.name == "nt" else Path("/opt/Comfy")
    write_env(isolated, f"COMFYUI_ROOT={root}\n")
    cfg = C.load_config()
    assert cfg.comfy_dir == root / "ComfyUI"
    assert cfg.models_dir == root / "ComfyUI" / "models"
    assert cfg.media_dir("temp") == root / "ComfyUI" / "temp"
    assert cfg.media_dir("nonsense") == root / "ComfyUI" / "output"
    assert cfg.workflows_dir in cfg.readable_roots


def test_ws_url_tracks_host_and_port(isolated):
    write_env(isolated, "COMFYUI_HOST=10.0.0.5\nCOMFYUI_PORT=7000\n")
    assert C.load_config().ws_url == "ws://10.0.0.5:7000/ws"


SAMPLE = "# --- tools ---\n# COMFYUI_TOOLS=all\n\nCOMFYUI_PORT=8188\n"


def test_an_existing_setting_is_replaced_in_place():
    out = C.set_in_env_text("COMFYUI_PORT=8188\nCOMFYUI_HOST=x\n", "COMFYUI_PORT", "9000")
    assert out == "COMFYUI_PORT=9000\nCOMFYUI_HOST=x\n"


def test_everything_around_the_setting_survives():
    out = C.set_in_env_text(SAMPLE, "COMFYUI_PORT", "9000")
    assert "# --- tools ---" in out and "# COMFYUI_TOOLS=all" in out


def test_a_new_setting_lands_under_the_comment_that_documents_it():
    # Which is where a person reading the file will look for it.
    out = C.set_in_env_text(SAMPLE, "COMFYUI_TOOLS", "-download")
    lines = out.splitlines()
    assert lines[lines.index("# COMFYUI_TOOLS=all") + 1] == "COMFYUI_TOOLS=-download"


def test_a_setting_with_nowhere_obvious_to_go_is_appended():
    out = C.set_in_env_text("COMFYUI_PORT=8188\n", "COMFYUI_TOOLS", "all")
    assert out.splitlines()[-1] == "COMFYUI_TOOLS=all"


def test_the_last_active_line_wins_when_a_key_repeats():
    # dotenv reads the last one, so that is the one that has to change.
    out = C.set_in_env_text("COMFYUI_PORT=1\nCOMFYUI_PORT=2\n", "COMFYUI_PORT", "3")
    assert out == "COMFYUI_PORT=1\nCOMFYUI_PORT=3\n"


def test_a_commented_line_is_not_mistaken_for_an_active_one():
    out = C.set_in_env_text("# COMFYUI_PORT=8188\n", "COMFYUI_PORT", "9000")
    assert "# COMFYUI_PORT=8188" in out and "COMFYUI_PORT=9000" in out


def test_the_files_own_line_endings_are_kept():
    out = C.set_in_env_text("COMFYUI_PORT=8188\r\nCOMFYUI_HOST=x\r\n", "COMFYUI_PORT", "9000")
    assert "\n" not in out.replace("\r\n", "")


def test_a_value_that_would_be_read_back_wrong_is_quoted():
    assert C.set_in_env_text("", "K", "a # b").strip() == 'K="a # b"'


def test_a_written_setting_is_read_back_by_the_loader(isolated):
    write_env(isolated, "COMFYUI_PORT=8188\n")
    env = isolated / ".env"
    env.write_text(C.set_in_env_text(env.read_text(), "COMFYUI_TOOLS", "-download"), encoding="utf-8")
    assert C.load_config().tools == "-download"
