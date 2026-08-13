"""Tests for the window that says where ComfyUI is.

Everything the window decides is here; the Tk half only draws it. The one piece
worth reading twice is `root_candidates`, which turns the model directories a
running ComfyUI reports into the portable root - because the alternative, asking
the filesystem, is the thing this project refuses to do everywhere else, and
because `argv[0]` from `/system_stats` turned out to be relative and therefore
useless on its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comfyui_mcp import configure_comfy as CC


def portable(tmp_path: Path, script: str = "run_nvidia_gpu.bat") -> Path:
    """A plausible portable build: <root>\\ComfyUI\\ plus a launch script."""
    root = tmp_path / "Comfyui_portable"
    (root / "ComfyUI").mkdir(parents=True)
    if script:
        (root / script).write_text("@echo off\n", encoding="utf-8")
    return root


def values(**over: str) -> dict[str, str]:
    base = {field.key: "" for field in CC.FIELDS}
    base.update(over)
    return base


def test_only_real_assignments_are_read():
    text = "# comment\n\nCOMFYUI_PORT=8188\nnot a setting\nCOMFYUI_HOST = 10.0.0.5 \n"
    assert CC.read_env(text) == {"COMFYUI_PORT": "8188", "COMFYUI_HOST": "10.0.0.5"}


def test_a_commented_setting_is_not_a_value():
    assert CC.read_env("# COMFYUI_PORT=9999\n") == {}


def test_quotes_around_a_value_are_stripped():
    assert CC.read_env('COMFYUI_ROOT="D:\\Comfy"\n')["COMFYUI_ROOT"] == "D:\\Comfy"


def test_a_value_containing_an_equals_sign_survives():
    assert CC.read_env("COMFYUI_DOWNLOAD_TOKEN=hf_a=b=c\n")["COMFYUI_DOWNLOAD_TOKEN"] == "hf_a=b=c"


def test_a_complete_and_correct_setup_is_silent(tmp_path: Path):
    # The constraint every check in this project carries: a healthy answer is silent.
    root = portable(tmp_path)
    assert CC.check(values(COMFYUI_ROOT=str(root), COMFYUI_LAUNCH_SCRIPT="run_nvidia_gpu.bat")) == []


def test_a_missing_root_is_an_error():
    problems = CC.check(values())
    assert [p.key for p in problems] == ["COMFYUI_ROOT"]
    assert problems[0].severity == "error"


def test_a_root_that_is_not_there_says_so(tmp_path: Path):
    problems = CC.check(values(COMFYUI_ROOT=str(tmp_path / "nope")))
    assert "no such folder" in problems[0].message.en
    assert "нет такой папки" in problems[0].message.ru


def test_pointing_one_level_too_deep_is_named_as_such(tmp_path: Path):
    # The common mistake: COMFYUI_ROOT is the folder *containing* ComfyUI\, and
    # picking ComfyUI\ itself looks right in a directory chooser.
    root = portable(tmp_path)
    problems = CC.check(values(COMFYUI_ROOT=str(root / "ComfyUI")))
    assert problems[0].severity == "error"
    assert "one level below" in problems[0].message.en
    assert "уровнем ниже" in problems[0].message.ru


def test_a_launch_script_that_is_not_there_is_only_a_note(tmp_path: Path):
    # comfy_start is all it is for, and plenty of people start ComfyUI themselves.
    root = portable(tmp_path, script="")
    problems = CC.check(values(COMFYUI_ROOT=str(root), COMFYUI_LAUNCH_SCRIPT="run_cpu.bat"))
    assert [(p.key, p.severity) for p in problems] == [("COMFYUI_LAUNCH_SCRIPT", "note")]


@pytest.mark.parametrize("port", ["0", "70000", "8188x", "-1"])
def test_a_port_that_is_not_a_port_is_an_error(tmp_path: Path, port: str):
    problems = CC.check(values(COMFYUI_ROOT=str(portable(tmp_path)), COMFYUI_PORT=port))
    assert [p.key for p in problems] == ["COMFYUI_PORT"]


def test_an_empty_port_is_fine_because_it_means_the_default(tmp_path: Path):
    assert CC.check(values(COMFYUI_ROOT=str(portable(tmp_path)), COMFYUI_PORT="")) == []


def test_a_folder_that_does_not_exist_yet_is_only_a_note(tmp_path: Path):
    problems = CC.check(
        values(COMFYUI_ROOT=str(portable(tmp_path)), COMFYUI_EXPORT_DIR=str(tmp_path / "later"))
    )
    assert [(p.key, p.severity) for p in problems] == [("COMFYUI_EXPORT_DIR", "note")]


def test_errors_are_reported_before_notes(tmp_path: Path):
    problems = CC.check(values(COMFYUI_PORT="nope", COMFYUI_EXPORT_DIR=str(tmp_path / "later")))
    assert [p.severity for p in problems] == ["error", "error", "note"]


def test_the_launch_scripts_offered_are_the_ones_that_are_there(tmp_path: Path):
    root = portable(tmp_path, script="")
    for name in ("run_cpu.bat", "run_nvidia_gpu.bat", "readme.txt"):
        (root / name).write_text("", encoding="utf-8")
    assert CC.launch_scripts(str(root)) == ["run_nvidia_gpu.bat", "run_cpu.bat"]


def test_a_root_that_is_not_a_folder_offers_nothing():
    assert CC.launch_scripts("") == []
    assert CC.launch_scripts("Z:\\nowhere") == []


def where(*parts: str) -> str:
    return str(Path(Path.cwd().anchor, *parts))


REAL = [
    where("Programs", "Comfy", "modelsArchive", "models", "checkpoints"),
    where("Programs", "Comfy", "Comfyui_portable", "ComfyUI", "models", "checkpoints"),
    where("Programs", "Comfy", "Comfyui_portable", "ComfyUI", "output", "checkpoints"),
    where("Programs", "Comfy", "Comfyui_portable", "ComfyUI", "models", "loras"),
]


def test_the_root_is_derived_from_the_model_paths():
    assert CC.root_candidates(REAL)[0] == where("Programs", "Comfy", "Comfyui_portable")


def test_a_directory_mapped_in_from_elsewhere_drops_out_by_itself():
    # extra_model_paths.yaml routinely maps a models folder in from somewhere else.
    # It has no ComfyUI segment, so it needs no rule of its own to be excluded.
    assert where("Programs", "Comfy", "modelsArchive") not in CC.root_candidates(REAL)


def test_the_most_cited_root_comes_first():
    mixed = [
        where("one", "ComfyUI", "models", "vae"),
        where("two", "ComfyUI", "models", "vae"),
        where("two", "ComfyUI", "models", "loras"),
    ]
    assert CC.root_candidates(mixed)[0] == where("two")


def test_nothing_recognisable_yields_nothing():
    assert CC.root_candidates([where("models", "vae"), ""]) == []


def test_the_rightmost_segment_is_the_one_that_counts():
    # A path can carry the name twice. The models hang off the *last* one, so
    # scanning from the right is what puts the root next to them rather than
    # somewhere above.
    paths = [where("a", "ComfyUI", "models", "ComfyUI", "vae")]
    assert CC.root_candidates(paths)[0] == where("a", "ComfyUI", "models")


def test_saving_writes_every_field_and_keeps_the_comments(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("# пояснение\nCOMFYUI_PORT=8188\n", encoding="utf-8")
    root = portable(tmp_path)

    CC.save(values(COMFYUI_ROOT=str(root), COMFYUI_PORT="9000"), env)
    written = CC.read_env(env.read_text(encoding="utf-8"))

    assert "# пояснение" in env.read_text(encoding="utf-8")
    assert written["COMFYUI_ROOT"] == str(root)
    assert written["COMFYUI_PORT"] == "9000"


def test_a_saved_setup_is_what_the_loader_then_reads(tmp_path: Path, monkeypatch):
    import os

    from comfyui_mcp import config as C

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    root = portable(tmp_path)
    CC.save(values(COMFYUI_ROOT=str(root), COMFYUI_PORT="9000", COMFYUI_HOST="10.0.0.5"), env)

    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(C, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = C.load_config()

    assert cfg.comfy_root == root
    assert cfg.base_url == "http://10.0.0.5:9000"


def test_a_field_left_blank_falls_back_to_the_default(tmp_path: Path, monkeypatch):
    # The window writes every key, blank included, and .env.example promises that
    # an empty value means the default. `_str` used to return "" instead, which
    # would have left the server connecting to no host at all.
    import os

    from comfyui_mcp import config as C

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    CC.save(values(COMFYUI_ROOT=str(portable(tmp_path))), env)

    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(C, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = C.load_config()

    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8188
    assert cfg.launch_script == "run_nvidia_gpu.bat"


def test_the_text_listing_never_shows_the_token(tmp_path: Path):
    text = CC.as_text(values(COMFYUI_DOWNLOAD_TOKEN="hf_secret"), tmp_path / ".env")
    assert "hf_secret" not in text
    assert "COMFYUI_DOWNLOAD_TOKEN" in text


def test_the_text_listing_names_the_problems(tmp_path: Path):
    text = CC.as_text(values(COMFYUI_PORT="nope"), tmp_path / ".env")
    assert "ERROR" in text and "COMFYUI_PORT" in text


def test_the_text_listing_speaks_the_language_it_was_asked_for(tmp_path: Path):
    text = CC.as_text(values(COMFYUI_PORT="nope"), tmp_path / ".env", "ru")
    assert "ОШИБКА" in text and "COMFYUI_PORT" in text
