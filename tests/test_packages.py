"""Checkpoints on disk, and reading what is in custom_nodes.

Everything here runs against a `tmp_path` tree rather than a real install: a checkpoint
is text files, and the reason it is text files is exactly so this can be tested without
286 packages and a GPU.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from comfyui_mcp import config as C
from comfyui_mcp import env as E
from comfyui_mcp import packages as P


@pytest.fixture
def cfg(monkeypatch, tmp_path: Path) -> C.Config:
    """A config pointing at an empty ComfyUI-shaped tree."""
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(C, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "Comfy"
    (root / "ComfyUI" / "custom_nodes").mkdir(parents=True)
    monkeypatch.setenv("COMFYUI_ROOT", str(root))
    return C.load_config()


PINS = E.parse_freeze("torch==2.11.0+cu130\npillow==11.0.0\npip==25.0\n")
PACKS = [E.Pack(name="rgthree-comfy", registry_id="rgthree-comfy", version="1.0.2")]


def test_a_checkpoint_lands_beside_the_install_it_describes(cfg):
    """Not inside this checkout: it describes that install, should travel with it, and a
    `git clone` of this repository elsewhere must not look like a machine with none."""
    assert P.checkpoints_dir(cfg) == cfg.comfy_root / "mcp_checkpoints"


def test_the_directory_can_be_moved(monkeypatch, cfg, tmp_path):
    monkeypatch.setenv("COMFYUI_CHECKPOINT_DIR", str(tmp_path / "elsewhere"))
    assert P.checkpoints_dir(C.load_config()) == tmp_path / "elsewhere"


def test_writing_one_records_the_packages_the_packs_and_the_index(cfg):
    checkpoint = P.write(cfg, PINS, PACKS, note="before installing x", comfyui_version="0.3.70")

    assert checkpoint.packages_file.read_text(encoding="utf-8").splitlines() == [
        "pillow==11.0.0",
        "pip==25.0",
        "torch==2.11.0+cu130",
    ]
    assert checkpoint.manifest["torch_index"] == "https://download.pytorch.org/whl/cu130"
    assert checkpoint.manifest["unrestorable"] == ["torch==2.11.0+cu130"]
    assert checkpoint.manifest["packages_count"] == 3
    assert checkpoint.note == "before installing x"
    assert checkpoint.packs()[0]["registry_id"] == "rgthree-comfy"


def test_the_name_carries_the_comfyui_version(cfg):
    assert P.write(cfg, PINS, [], comfyui_version="0.3.70").name.endswith("__v0.3.70")


def test_a_version_with_a_slash_does_not_become_a_subdirectory(cfg):
    """The version comes from `/system_stats`, which reports whatever the install says -
    a branch name reaches it on a checkout built from git."""
    checkpoint = P.write(cfg, PINS, [], comfyui_version="feature/x y")
    assert checkpoint.path.parent == P.checkpoints_dir(cfg)


def test_a_failed_write_leaves_no_half_checkpoint(cfg, monkeypatch):
    """A directory holding a manifest and nothing else would list as a checkpoint and
    fail at restore, which is the worst moment to find out."""
    monkeypatch.setattr(P.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(ValueError("no")))
    with pytest.raises(ValueError):
        P.write(cfg, PINS, PACKS)
    assert P.load_all(cfg) == []


def test_they_list_newest_first(cfg):
    first = P.write(cfg, PINS, [], note="one")
    second_path = first.path.parent / "2099-01-01_00-00-00__vX"
    first.path.rename(second_path)
    P.write(cfg, PINS, [], note="two")
    assert P.load_all(cfg)[0].name == "2099-01-01_00-00-00__vX"


def test_a_damaged_checkpoint_is_skipped_rather_than_raised_on(cfg):
    """One unreadable folder must not make the list unreadable - the listing is what a
    caller uses to find the good one."""
    good = P.write(cfg, PINS, [])
    broken = good.path.parent / "2099-01-01_00-00-00__vX"
    broken.mkdir()
    (broken / P.MANIFEST).write_text("{not json", encoding="utf-8")
    assert [c.name for c in P.load_all(cfg)] == [good.name]


def test_a_folder_with_no_manifest_is_not_a_checkpoint(cfg):
    (P.checkpoints_dir(cfg) / "notes").mkdir(parents=True)
    assert P.load_all(cfg) == []


def test_finding_one_accepts_a_prefix(cfg):
    checkpoint = P.write(cfg, PINS, [], comfyui_version="0.3.70")
    assert P.find(cfg, checkpoint.name).name == checkpoint.name
    assert P.find(cfg, checkpoint.name[:10]).name == checkpoint.name


def test_an_ambiguous_prefix_is_refused_rather_than_guessed(cfg):
    """Restoring the wrong one of two checkpoints is not something the reply would make
    obvious, so the guess is not made."""
    base = P.write(cfg, PINS, []).path.parent
    for stamp in ("2099-01-01_00-00-00__vA", "2099-01-01_00-00-01__vB"):
        target = base / stamp
        target.mkdir()
        (target / P.MANIFEST).write_text("{}", encoding="utf-8")
    with pytest.raises(P.PackageError, match="matches 2"):
        P.find(cfg, "2099")


def test_an_unknown_name_lists_what_there_is(cfg):
    existing = P.write(cfg, PINS, [])
    with pytest.raises(P.PackageError) as exc:
        P.find(cfg, "nonsense")
    assert existing.name in str(exc.value)


def test_asking_with_no_checkpoints_at_all_says_how_to_make_one(cfg):
    with pytest.raises(P.PackageError, match="create_checkpoint"):
        P.find(cfg, "anything")


def test_pruning_keeps_the_newest(cfg):
    base = P.write(cfg, PINS, []).path.parent
    for stamp in ("2099-01-01__vA", "2098-01-01__vB", "2097-01-01__vC"):
        target = base / stamp
        target.mkdir()
        (target / P.MANIFEST).write_text("{}", encoding="utf-8")
    gone = P.prune(cfg, keep=2)
    remaining = [c.name for c in P.load_all(cfg)]
    assert remaining == ["2099-01-01__vA", "2098-01-01__vB"]
    assert "2097-01-01__vC" in gone


def test_keeping_zero_prunes_nothing(cfg):
    """0 is "no limit" rather than "delete everything" - the other reading turns a
    mistyped setting into the loss of every checkpoint at the moment one is taken."""
    P.write(cfg, PINS, [])
    assert P.prune(cfg, keep=0) == []
    assert len(P.load_all(cfg)) == 1


def test_deleting_removes_the_folder(cfg):
    checkpoint = P.write(cfg, PINS, [])
    P.delete(checkpoint)
    assert not checkpoint.path.exists()
    assert P.load_all(cfg) == []


def test_a_checkpoint_says_what_it_does_not_hold(cfg):
    """The listing has to state it: somebody restoring one after deleting a model needs
    to find out here rather than afterwards."""
    checkpoint = P.write(cfg, PINS, [])
    assert "models" in checkpoint.manifest["not_backed_up"]


def make_pack(root: Path, name: str, *, pyproject: str = "", git: dict | None = None) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "__init__.py").write_text("", encoding="utf-8")
    if pyproject:
        (path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    if git is not None:
        gitdir = path / ".git"
        gitdir.mkdir()
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (gitdir / "refs" / "heads").mkdir(parents=True)
        (gitdir / "refs" / "heads" / "main").write_text(git["commit"] + "\n", encoding="utf-8")
        (gitdir / "config").write_text(
            '[remote "origin"]\n\turl = %s\n' % git["remote"], encoding="utf-8"
        )
    return path


PYPROJECT = """
[project]
name = "comfyui-kjnodes"
version = "1.4.9"
dependencies = ["matplotlib"]
[project.urls]
Repository = "https://github.com/kijai/ComfyUI-KJNodes"
[tool.comfy]
PublisherId = "kijai"
"""


def test_a_pack_is_read_with_its_own_metadata_and_its_commit(cfg):
    nodes = cfg.comfy_dir / "custom_nodes"
    make_pack(
        nodes,
        "ComfyUI-KJNodes",
        pyproject=PYPROJECT,
        git={"commit": "a" * 40, "remote": "https://github.com/kijai/ComfyUI-KJNodes.git"},
    )
    (pack,) = P.read_packs(cfg)
    assert pack.name == "ComfyUI-KJNodes"
    assert pack.registry_id == "comfyui-kjnodes"
    assert pack.version == "1.4.9"
    assert pack.commit == "a" * 12
    assert pack.enabled is True


def test_a_pack_with_no_metadata_is_still_a_pack(cfg):
    """Plenty of older packs are a bare `__init__.py` in a git clone. Skipping them would
    make the checkpoint's record of custom_nodes quietly incomplete."""
    make_pack(cfg.comfy_dir / "custom_nodes", "old-pack")
    (pack,) = P.read_packs(cfg)
    assert (pack.name, pack.registry_id, pack.commit) == ("old-pack", "", "")


@pytest.mark.parametrize("style", ["subdirectory", "suffix"])
def test_both_of_the_managers_disabled_spellings_are_recognised(cfg, style):
    """A pack this server calls installed while ComfyUI-Manager's own UI calls it
    disabled is a disagreement the user experiences as a node that is there and does not
    work. Manager 3.40 writes the subdirectory form and still reads the older suffix."""
    nodes = cfg.comfy_dir / "custom_nodes"
    if style == "subdirectory":
        make_pack(nodes / E.DISABLED_DIR, "sleeping-pack")
    else:
        make_pack(nodes, "sleeping-pack.disabled")

    (pack,) = P.read_packs(cfg)
    assert pack.name == "sleeping-pack"
    assert pack.enabled is False


def test_a_disabled_pack_is_the_reason_the_disk_is_read_at_all(cfg):
    """It is absent from `/object_info` exactly as a pack that was never installed is,
    so nothing ComfyUI reports can tell them apart."""
    nodes = cfg.comfy_dir / "custom_nodes"
    make_pack(nodes, "awake")
    make_pack(nodes / E.DISABLED_DIR, "asleep")
    assert {p.name: p.enabled for p in P.read_packs(cfg)} == {"awake": True, "asleep": False}


def test_the_bookkeeping_entries_are_not_packs(cfg):
    nodes = cfg.comfy_dir / "custom_nodes"
    (nodes / "__pycache__").mkdir()
    (nodes / ".git").mkdir()
    make_pack(nodes, "real-pack")
    assert [p.name for p in P.read_packs(cfg)] == ["real-pack"]


def test_a_single_file_node_counts_and_says_so(cfg):
    """`websocket_image_save.py` ships with ComfyUI and sits loose in custom_nodes."""
    nodes = cfg.comfy_dir / "custom_nodes"
    (nodes / "websocket_image_save.py").write_text("", encoding="utf-8")
    (nodes / "example_node.py.example").write_text("", encoding="utf-8")
    (pack,) = P.read_packs(cfg)
    assert pack.name == "websocket_image_save.py" and pack.files is False


def test_no_custom_nodes_folder_is_an_empty_list_not_an_error(cfg):
    """A checkpoint taken on an install being set up must still be a checkpoint."""
    (cfg.comfy_dir / "custom_nodes").rmdir()
    assert P.read_packs(cfg) == []


def test_a_commit_is_read_from_packed_refs_too(cfg):
    """A freshly cloned repository has its branch in `packed-refs` and no loose ref file,
    which is the state most node packs are actually in."""
    nodes = cfg.comfy_dir / "custom_nodes"
    path = make_pack(nodes, "packed")
    gitdir = path / ".git"
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (gitdir / "packed-refs").write_text("# pack-refs\n%s refs/heads/main\n" % ("b" * 40), encoding="utf-8")
    (pack,) = P.read_packs(cfg)
    assert pack.commit == "b" * 12


def test_a_detached_head_still_reports_a_commit(cfg):
    nodes = cfg.comfy_dir / "custom_nodes"
    path = make_pack(nodes, "detached")
    gitdir = path / ".git"
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("c" * 40 + "\n", encoding="utf-8")
    assert P.read_packs(cfg)[0].commit == "c" * 12


def test_reading_a_pack_never_needs_git_on_the_path(cfg, monkeypatch):
    """A portable ComfyUI unpacked from a zip has no repository and no git either, and a
    routine checkpoint must not turn into an error about a missing executable."""
    monkeypatch.setattr(P.shutil, "which", lambda *_: None)
    make_pack(cfg.comfy_dir / "custom_nodes", "no-git")
    assert P.read_packs(cfg)[0].commit == ""


def test_an_explicit_python_wins(monkeypatch, cfg, tmp_path):
    exe = tmp_path / "mine" / "python.exe"
    exe.parent.mkdir()
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("COMFYUI_PYTHON", str(exe))
    found, how = P.find_python(C.load_config())
    assert found == exe and how == "COMFYUI_PYTHON"


def test_an_explicit_python_that_is_not_there_is_named(monkeypatch, cfg, tmp_path):
    monkeypatch.setenv("COMFYUI_PYTHON", str(tmp_path / "gone.exe"))
    with pytest.raises(P.PackageError, match="COMFYUI_PYTHON"):
        P.find_python(C.load_config())


def test_no_interpreter_anywhere_says_where_it_looked(cfg):
    with pytest.raises(P.PackageError) as exc:
        P.find_python(cfg)
    assert "python_embeded" in str(exc.value) and "COMFYUI_PYTHON" in str(exc.value)


def test_the_embedded_interpreter_is_preferred(cfg):
    """A portable build is `python_embeded` and nothing else; a leftover venv beside it
    is the classic way to install into the interpreter nobody runs."""
    win = P.sys.platform == "win32"
    embedded = cfg.comfy_root / "python_embeded" / ("python.exe" if win else "bin/python")
    embedded.parent.mkdir(parents=True, exist_ok=True)
    embedded.write_text("", encoding="utf-8")
    venv = cfg.comfy_root / "venv" / ("Scripts" if win else "bin") / ("python.exe" if win else "python")
    venv.parent.mkdir(parents=True, exist_ok=True)
    venv.write_text("", encoding="utf-8")

    found, _ = P.find_python(cfg)
    assert found == embedded
    assert len(P.python_candidates(cfg)) == 2


def test_missing_uv_says_which_command_produces_one(monkeypatch, cfg):
    """Never a download: a tool call that quietly pulls an executable off the internet to
    satisfy itself is the shape of thing this whole concept exists to stop."""
    monkeypatch.setattr(P.shutil, "which", lambda *_: None)
    monkeypatch.setattr(P, "PROJECT_ROOT", cfg.comfy_root / "not-a-checkout")
    with pytest.raises(P.PackageError) as exc:
        P.find_uv(cfg)
    assert "install.bat" in str(exc.value) and "COMFYUI_UV" in str(exc.value)


def test_the_child_environment_forces_utf8_and_silences_the_progress_bar(monkeypatch):
    """A Russian Windows console is cp866 and cannot encode half of what uv prints, and
    the output here is parsed rather than shown."""
    monkeypatch.setattr(os, "environ", {"PATH": "x"})
    child = P.child_env()
    assert child["PYTHONUTF8"] == "1"
    assert child["PYTHONIOENCODING"] == "utf-8"
    assert child["UV_NO_PROGRESS"] == "1"


AUDIT = {
    "dependencies": [
        {"name": "pillow", "version": "11.0.0", "vulns": []},
        {
            "name": "aiohttp",
            "version": "3.13.5",
            "vulns": [
                {"id": "PYSEC-2026-237", "fix_versions": ["3.13.6"]},
                {"id": "PYSEC-2026-2104", "fix_versions": ["3.13.6"]},
            ],
        },
        {"name": "torch", "version": "2.11.0+cu130", "vulns": [{"id": "X", "fix_versions": ["2.12"]}]},
        {"name": "js2py", "version": "0.74", "vulns": [{"id": "PYSEC-2026-1476", "fix_versions": []}]},
        {
            "name": "gitpython",
            "version": "3.1.50",
            "vulns": [
                {"id": "PYSEC-2026-3783", "fix_versions": ["3.1.51"]},
                {"id": "PYSEC-2026-3784", "fix_versions": ["3.1.51"]},
                {"id": "PYSEC-2026-3783", "fix_versions": ["3.1.51"]},
                {"id": "PYSEC-2026-3784", "fix_versions": ["3.1.51"]},
            ],
        },
    ]
}


def test_the_audit_keeps_only_the_affected_packages():
    report = P.summarise_audit(AUDIT, PINS)
    assert report["packages_affected"] == 4
    assert "pillow" not in [row["name"] for row in report["vulnerable"]]


def test_the_worst_comes_first():
    assert P.summarise_audit(AUDIT, PINS)["vulnerable"][0]["name"] == "aiohttp"


def test_a_finding_on_a_pinned_package_says_it_cannot_be_moved():
    """Otherwise the obvious next call is an upgrade that this server refuses, and the
    caller learns that one round trip later than it needed to."""
    row = next(r for r in P.summarise_audit(AUDIT, PINS)["vulnerable"] if r["name"] == "torch")
    assert row["pinned"] is True
    assert "CUDA" in row["note"]


def test_a_vulnerability_with_no_published_fix_says_so():
    row = next(r for r in P.summarise_audit(AUDIT, PINS)["vulnerable"] if r["name"] == "js2py")
    assert row["fixed_in"] == []
    assert "no fixed version" in row["note"]


def test_a_clean_environment_produces_an_empty_list_not_a_missing_key():
    report = P.summarise_audit({"dependencies": [{"name": "pillow", "version": "1", "vulns": []}]}, PINS)
    assert report == {"vulnerable": [], "packages_audited": len(PINS), "packages_affected": 0}


def test_the_checkpoint_serialises_to_something_a_reply_can_carry(cfg):
    checkpoint = P.write(cfg, PINS, PACKS, note="n", comfyui_version="0.3.70")
    payload = checkpoint.as_dict()
    assert json.dumps(payload)
    assert payload["packages"] == 3 and payload["custom_nodes"] == 1


def test_an_unreachable_index_is_not_reported_as_a_bad_requirement():
    """Measured live, with pypi.org blocked. The two call for opposite next steps, and a
    plan that failed on the network says nothing about whether the install was safe."""
    message = P._resolve_failure(
        "error: Request failed after 3 retries in 49.6s\n"
        "  Caused by: Failed to fetch: `https://pypi.org/simple/insightface/`\n"
        "  Caused by: operation timed out"
    )
    assert "could not reach a package index" in message
    assert "not a verdict on the requirements" in message


def test_a_real_resolution_failure_still_reads_as_one():
    message = P._resolve_failure(
        "error: Distribution `nope==9.9` can't be installed because it doesn't have a "
        "source distribution or wheel for the current platform"
    )
    assert message.startswith("uv could not resolve that:")


def test_a_version_that_cannot_be_a_folder_name_still_becomes_one(cfg):
    """`/system_stats` reports whatever the install says it is, and on Windows a `:` or a
    backslash in a folder name is unwritable rather than merely ugly."""
    checkpoint = P.write(cfg, PINS, [], comfyui_version=r"feature/x:y\z 1")
    assert checkpoint.path.parent == P.checkpoints_dir(cfg)
    assert checkpoint.path.is_dir()
    assert P.find(cfg, checkpoint.name).name == checkpoint.name


def test_two_versions_differing_only_in_punctuation_do_not_collide():
    assert P.new_name("v1.2/3") != P.new_name("v1.2.3")


def test_an_alias_reported_twice_is_one_finding():
    """OSV reports a vulnerability once per alias - `gitpython` arrived with two ids
    listed twice each. The count is what the rows are sorted by, so inflating it moves
    the wrong package to the top of the list a caller reads first."""
    row = next(r for r in P.summarise_audit(AUDIT, PINS)["vulnerable"] if r["name"] == "gitpython")
    assert row["count"] == 2
    assert row["ids"] == ["PYSEC-2026-3783", "PYSEC-2026-3784"]


def test_the_worst_is_still_first_once_duplicates_are_gone():
    """aiohttp has two real findings and gitpython two duplicated pairs; before the
    dedupe the second sorted above the first."""
    names = [r["name"] for r in P.summarise_audit(AUDIT, PINS)["vulnerable"]]
    assert names.index("aiohttp") < names.index("gitpython")


def test_uv_uses_pypi_when_nothing_is_configured(cfg):
    """The case almost everybody is in has to stay byte-for-byte what it was: no index
    argument at all, so uv's own default applies."""
    uv = P.Uv(Path("uv.exe"), Path("python.exe"), 60)
    argv = uv._base("install")
    assert "--default-index" not in argv


def test_a_configured_index_reaches_every_uv_call(monkeypatch, tmp_path):
    """Measured need: pypi.org's TLS handshake takes 10.2 s on the machine this was
    written on, which is past uv's connect deadline, while a mirror answers in 0.4 s.
    Without this there is no way to tell the server about one."""
    uv = P.Uv(Path("uv.exe"), Path("python.exe"), 60, default_index="https://mirror/simple/")
    for verb in ("install", "list", "sync"):
        argv = [str(a) for a in uv._base(verb)]
        assert argv[argv.index("--default-index") + 1] == "https://mirror/simple/"


def test_the_index_is_stripped_of_stray_whitespace():
    """A settings window writes every key it edits, and a pasted URL carries a newline."""
    uv = P.Uv(Path("uv.exe"), Path("python.exe"), 60, default_index="  https://m/  ")
    assert uv.default_index == "https://m/"
    blank = P.Uv(Path("uv.exe"), Path("python.exe"), 60, default_index="   ")
    assert "--default-index" not in blank._base("install")


def test_the_two_indexes_are_different_questions():
    """`--default-index` is where everything comes from; `--extra-index-url` is the
    torch one, derived from what is installed. Both can be in play at once."""
    uv = P.Uv(
        Path("uv.exe"),
        Path("python.exe"),
        60,
        index_url="https://download.pytorch.org/whl/cu130",
        default_index="https://mirror/simple/",
    )
    argv = [str(a) for a in uv._base("install")] + uv._index()
    assert "--default-index" in argv and "--extra-index-url" in argv
    assert argv[argv.index("--extra-index-url") + 1] == "https://download.pytorch.org/whl/cu130"
