"""Node packs: naming one, telling two apart, and moving one out of the way.

The registry fixtures below are a real `api.comfy.org` response, trimmed only by
deleting whole entries - the field spellings, the empty strings and the nesting are
verbatim, because every one of them is a thing that can change under this code without
a test noticing.

The toggle tests run against a `tmp_path` tree. That is possible at all because
enabling and disabling a pack is one `rename` inside one directory, which is the same
fact that makes it the safest tool in the concept.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from comfyui_mcp import config as C
from comfyui_mcp import env as E
from comfyui_mcp import packages as P

KJNODES = {
    "author": "",
    "banner_url": "",
    "category": "",
    "created_at": "2024-06-19T09:05:46.890086Z",
    "description": "Various quality of life -nodes for ComfyUI, mostly just visual stuff.",
    "downloads": 4350625,
    "github_stars": 3244,
    "icon": "https://avatars.githubusercontent.com/u/40791699",
    "id": "comfyui-kjnodes",
    "latest_version": {
        "changelog": "",
        "createdAt": "2026-08-07T22:05:04.258043Z",
        "dependencies": ["pillow>=10.3.0", "color-matcher", "matplotlib"],
        "deprecated": False,
        "downloadUrl": "",
        "id": "e05199fa-bbd3-44d9-9896-498c33ad5290",
        "node_id": "comfyui-kjnodes",
        "status": "NodeVersionStatusActive",
        "supported_accelerators": [],
        "supported_os": [],
        "tags": [],
        "version": "1.5.0",
    },
    "license": '{"file": "LICENSE"}',
    "name": "ComfyUI-KJNodes",
    "publisher": {
        "createdAt": "2024-06-09T11:11:54.62154Z",
        "description": "",
        "id": "kijai",
        "logo": "",
        "members": [],
        "name": "Kijai",
        "status": "PublisherStatusActive",
    },
    "rating": 0,
    "repository": "https://github.com/kijai/ComfyUI-KJNodes",
    "search_ranking": 5,
    "status": "NodeStatusActive",
    "status_detail": "",
    "supported_os": [],
    "tags": [],
}

SEARCH = {"limit": 3, "nodes": [KJNODES], "page": 2, "total": 135, "totalPages": 45}

SCHEMAS = {
    "ImageResizeKJ": {"python_module": "custom_nodes.comfyui-kjnodes"},
    "GetImageSizeAndCount": {"python_module": "custom_nodes.comfyui-kjnodes"},
    "KSampler": {"python_module": "nodes"},
    "MaskComposite": {"python_module": "comfy_extras.nodes_mask"},
    "RES4LYF_Sampler": {"python_module": "custom_nodes.RES4LYF"},
}


@pytest.fixture
def cfg(monkeypatch, tmp_path: Path) -> C.Config:
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(C, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "Comfy"
    (root / "ComfyUI" / "custom_nodes").mkdir(parents=True)
    monkeypatch.setenv("COMFYUI_ROOT", str(root))
    return C.load_config()


def nodes_dir(cfg: C.Config) -> Path:
    return cfg.comfy_dir / "custom_nodes"


def make_pack(cfg: C.Config, name: str, *, disabled: bool = False, old_style: bool = False) -> Path:
    root = nodes_dir(cfg)
    if disabled:
        path = root / f"{name}{E.DISABLED_SUFFIX}" if old_style else root / E.DISABLED_DIR / name
    else:
        path = root / name
    path.mkdir(parents=True)
    (path / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n", encoding="utf-8")
    return path


def test_a_listing_keeps_the_fields_that_decide_something():
    listing = E.parse_listing(KJNODES)
    assert listing is not None
    assert listing.id == "comfyui-kjnodes"
    assert listing.name == "ComfyUI-KJNodes"
    assert listing.version == "1.5.0"
    assert listing.repo == "https://github.com/kijai/ComfyUI-KJNodes"
    assert listing.publisher == "Kijai"
    assert listing.dependencies == ["pillow>=10.3.0", "color-matcher", "matplotlib"]


def test_the_dependencies_are_readable_before_anything_is_cloned():
    """The whole reason search comes first: this list is what `plan_packages` takes, and
    getting it costs one HTTP request against somebody else's database."""
    assert E.parse_listing(KJNODES).dependencies == [
        "pillow>=10.3.0",
        "color-matcher",
        "matplotlib",
    ]


def test_the_empty_fields_do_not_reach_the_answer():
    """25 fields in, and `banner_url`, `category`, `tags` and both status details are
    empty on every pack measured. Reporting them is a page of nothing per hit."""
    out = E.parse_listing(KJNODES).as_dict()
    assert "banner_url" not in out and "category" not in out and "tags" not in out
    assert not any(value == "" or value == [] for value in out.values())


def test_a_listing_with_no_id_is_not_a_listing():
    assert E.parse_listing({"name": "x"}) is None
    assert E.parse_listing({"id": "   "}) is None
    assert E.parse_listing("not a dict") is None


def test_a_null_where_a_string_was_does_not_raise():
    """The registry adds and empties fields without warning, and a search that dies
    because one pack of twenty has a null is worse than one reporting a blank."""
    listing = E.parse_listing({"id": "x", "name": None, "description": None, "publisher": None})
    assert listing is not None and listing.name == "x" and listing.description == ""


def test_downloads_and_stars_survive_only_as_whole_numbers():
    assert E.parse_listing({"id": "x", "downloads": True}).downloads == 0
    assert E.parse_listing({"id": "x", "downloads": "many"}).downloads == 0
    assert E.parse_listing({"id": "x", "downloads": 12}).downloads == 12


def test_a_search_reports_where_the_page_sits_in_the_whole_result():
    """`wan` matches 135 packs. Ten of them with no total reads as the whole answer."""
    listings, paging = E.parse_search(SEARCH)
    assert [x.id for x in listings] == ["comfyui-kjnodes"]
    assert paging == {"total": 135, "page": 2, "pages": 45}


def test_an_empty_search_is_not_an_error():
    listings, paging = E.parse_search({"limit": 3, "nodes": [], "page": 1, "total": 0})
    assert listings == [] and paging["total"] == 0


def test_a_banned_pack_says_so():
    banned = dict(KJNODES, status="NodeStatusBanned")
    assert E.parse_listing(banned).banned is True
    assert E.parse_listing(KJNODES).banned is False


def test_the_registry_id_and_the_folder_name_both_find_it():
    packs = [E.Pack(name="ComfyUI-KJNodes", registry_id="comfyui-kjnodes")]
    assert E.find_packs(packs, "comfyui-kjnodes") == packs
    assert E.find_packs(packs, "ComfyUI-KJNodes") == packs
    assert E.find_packs(packs, "COMFYUI_KJNODES") == packs


def test_a_substring_is_the_fallback_and_returns_everything_it_matched():
    """Picking between two would disable the wrong pack with a reply that looks fine."""
    packs = [E.Pack(name="comfyui-kjnodes"), E.Pack(name="kjnodes-extra")]
    assert len(E.find_packs(packs, "kjnodes")) == 2


def test_an_exact_match_beats_a_substring_outright():
    packs = [E.Pack(name="crt-nodes"), E.Pack(name="crt-nodes-extra")]
    assert [p.name for p in E.find_packs(packs, "crt-nodes")] == ["crt-nodes"]


def test_a_repository_url_finds_the_pack_it_names():
    packs = [E.Pack(name="whatever", repo="https://github.com/kijai/ComfyUI-KJNodes.git")]
    assert E.find_packs(packs, "https://github.com/kijai/ComfyUI-KJNodes") == packs
    assert E.find_packs(packs, "git@github.com:kijai/ComfyUI-KJNodes.git") == packs


def test_nothing_matches_nothing():
    assert E.find_packs([E.Pack(name="a")], "") == []
    assert E.find_packs([], "anything") == []


def test_a_disabled_copy_is_not_a_conflict():
    """Measured on this install: ComfyUI-WanVideoWrapper is present twice, enabled and
    as a disabled @nightly checkout, both under the same registry id. Counting the
    second refuses to touch the copy the user actually runs."""
    installed = [
        E.Pack(name="ComfyUI-WanVideoWrapper", registry_id="comfyui-wanvideowrapper"),
        E.Pack(
            name="ComfyUI-WanVideoWrapper@nightly",
            registry_id="comfyui-wanvideowrapper",
            enabled=False,
        ),
    ]
    candidate = E.Pack(name="x", registry_id="comfyui-wanvideowrapper")
    assert [p.name for p in E.conflicts(installed, candidate)] == ["ComfyUI-WanVideoWrapper"]


def test_the_same_pack_under_two_folder_names_is_one_conflict():
    installed = [E.Pack(name="comfyui-kjnodes", registry_id="comfyui-kjnodes")]
    listing = E.parse_listing(KJNODES)
    assert E.conflicts(installed, E.as_pack(listing)) == installed


def test_a_registry_hit_becomes_something_same_pack_can_compare():
    pack = E.as_pack(E.parse_listing(KJNODES))
    assert pack.registry_id == "comfyui-kjnodes"
    assert pack.repo.endswith("ComfyUI-KJNodes")
    assert pack.dependencies == ["pillow>=10.3.0", "color-matcher", "matplotlib"]


def test_object_info_names_the_folder_a_node_type_came_from():
    assert E.pack_of_module("custom_nodes.comfyui-kjnodes") == "comfyui-kjnodes"
    assert E.pack_of_module("custom_nodes.RES4LYF.sampling") == "RES4LYF"


def test_a_core_node_belongs_to_no_pack():
    assert E.pack_of_module("nodes") == ""
    assert E.pack_of_module("comfy_extras.nodes_mask") == ""
    assert E.pack_of_module("") == ""


def test_a_pack_is_joined_to_its_node_types_across_the_two_spellings():
    pack = E.Pack(name="ComfyUI-KJNodes")
    assert E.registered_by(SCHEMAS, pack) == ["GetImageSizeAndCount", "ImageResizeKJ"]


def test_a_pack_that_registered_nothing_is_an_empty_list_not_an_error():
    """That is the finding, not a fault: installed, enabled and registering nothing is
    an import that died, and nothing on the canvas says so."""
    assert E.registered_by(SCHEMAS, E.Pack(name="comfyui-mmaudio")) == []


def test_a_malformed_object_info_entry_is_skipped():
    assert E.registered_by({"X": "not a dict", "Y": {}}, E.Pack(name="a")) == []


def test_disabling_writes_what_manager_3_writes(tmp_path):
    """Manager reads two spellings and writes one. Writing the older form would leave a
    pack this server calls disabled that Manager's own UI shows as enabled."""
    source, target = E.toggle_target(tmp_path, E.Pack(name="rgthree-comfy"), enable=False)
    assert source == tmp_path / "rgthree-comfy"
    assert target == tmp_path / ".disabled" / "rgthree-comfy"


def test_enabling_reads_the_new_form_first(tmp_path):
    (tmp_path / ".disabled" / "rgthree-comfy").mkdir(parents=True)
    (tmp_path / "rgthree-comfy.disabled").mkdir()
    source, target = E.toggle_target(tmp_path, E.Pack(name="rgthree-comfy"), enable=True)
    assert source == tmp_path / ".disabled" / "rgthree-comfy"
    assert target == tmp_path / "rgthree-comfy"


def test_enabling_still_finds_the_old_form(tmp_path):
    """Installs made years apart are both still out there."""
    (tmp_path / "rgthree-comfy.disabled").mkdir()
    source, _ = E.toggle_target(tmp_path, E.Pack(name="rgthree-comfy"), enable=True)
    assert source == tmp_path / "rgthree-comfy.disabled"


def test_a_single_file_pack_is_renamed_in_place(tmp_path):
    """There is nowhere to move a file to, which is what Manager's copy_set_active does."""
    pack = E.Pack(name="websocket_image_save.py", files=False)
    source, target = E.toggle_target(tmp_path, pack, enable=False)
    assert source == tmp_path / "websocket_image_save.py"
    assert target == tmp_path / "websocket_image_save.py.disabled"


def test_disabling_moves_the_folder_and_leaves_the_files_alone(cfg):
    make_pack(cfg, "rgthree-comfy")
    pack = [p for p in P.read_packs(cfg) if p.name == "rgthree-comfy"][0]

    source, target = P.toggle_pack(cfg, pack, enable=False)

    assert not source.exists()
    assert (target / "__init__.py").read_text(encoding="utf-8") == "NODE_CLASS_MAPPINGS = {}\n"
    assert [(p.name, p.enabled) for p in P.read_packs(cfg)] == [("rgthree-comfy", False)]


def test_enabling_puts_it_back_exactly(cfg):
    make_pack(cfg, "rgthree-comfy", disabled=True)
    pack = [p for p in P.read_packs(cfg) if p.name == "rgthree-comfy"][0]
    assert pack.enabled is False

    P.toggle_pack(cfg, pack, enable=True)
    assert [(p.name, p.enabled) for p in P.read_packs(cfg)] == [("rgthree-comfy", True)]


def test_a_round_trip_changes_nothing(cfg):
    made = make_pack(cfg, "crt-nodes")
    pack = P.read_packs(cfg)[0]
    P.toggle_pack(cfg, pack, enable=False)
    P.toggle_pack(cfg, E.Pack(name="crt-nodes", enabled=False), enable=True)
    assert (made / "__init__.py").is_file()


def test_the_old_spelling_is_enabled_too(cfg):
    make_pack(cfg, "was-node-suite-comfyui", disabled=True, old_style=True)
    pack = [p for p in P.read_packs(cfg) if p.name == "was-node-suite-comfyui"][0]
    assert pack.enabled is False

    _, target = P.toggle_pack(cfg, pack, enable=True)
    assert target == nodes_dir(cfg) / "was-node-suite-comfyui"


def test_two_copies_on_disk_are_refused_rather_than_one_destroyed(cfg):
    """Manager can produce this - install, disable, install again - and the folders in
    custom_nodes are the user's, so one of them may hold edits nothing else has."""
    make_pack(cfg, "rgthree-comfy")
    make_pack(cfg, "rgthree-comfy", disabled=True)
    pack = E.Pack(name="rgthree-comfy")

    with pytest.raises(P.PackageError, match="already exists"):
        P.toggle_pack(cfg, pack, enable=False)

    assert (nodes_dir(cfg) / "rgthree-comfy" / "__init__.py").is_file()
    assert (nodes_dir(cfg) / ".disabled" / "rgthree-comfy" / "__init__.py").is_file()


def test_a_pack_that_is_not_there_says_which_state_it_is_not_in(cfg):
    with pytest.raises(P.PackageError, match="not disabled"):
        P.toggle_pack(cfg, E.Pack(name="ghost", enabled=False), enable=True)


def test_the_disabled_directory_is_made_on_demand(cfg):
    """A portable install that has never disabled anything does not have one."""
    make_pack(cfg, "kaytool")
    assert not (nodes_dir(cfg) / E.DISABLED_DIR).exists()
    P.toggle_pack(cfg, E.Pack(name="kaytool"), enable=False)
    assert (nodes_dir(cfg) / E.DISABLED_DIR / "kaytool").is_dir()


def test_a_single_file_pack_toggles_without_a_directory(cfg):
    path = nodes_dir(cfg) / "websocket_image_save.py"
    path.write_text("# a pack that is one file\n", encoding="utf-8")

    pack = [p for p in P.read_packs(cfg) if p.name.startswith("websocket_image_save")][0]
    assert pack.files is False
    assert pack.name == "websocket_image_save.py", "a single-file pack is named as it sits on disk"

    _, target = P.toggle_pack(cfg, pack, enable=False)
    assert target.name == "websocket_image_save.py.disabled"
    assert not path.exists()


def test_pack_location_finds_it_in_any_of_the_three_places(cfg):
    made = make_pack(cfg, "comfyui-rmbg")
    assert P.pack_location(cfg, E.Pack(name="comfyui-rmbg")) == made

    P.toggle_pack(cfg, E.Pack(name="comfyui-rmbg"), enable=False)
    found = P.pack_location(cfg, E.Pack(name="comfyui-rmbg"))
    assert found == nodes_dir(cfg) / E.DISABLED_DIR / "comfyui-rmbg"

    assert P.pack_location(cfg, E.Pack(name="never-installed")) is None


def test_missing_custom_nodes_is_named_rather_than_crashed_on(cfg, monkeypatch):
    monkeypatch.setattr(P, "custom_nodes_dir", lambda c: c.comfy_dir / "nowhere")
    with pytest.raises(P.PackageError, match="not a directory"):
        P.toggle_pack(cfg, E.Pack(name="x"), enable=False)


def test_an_update_is_two_different_strings():
    listing = E.parse_listing(KJNODES)
    assert E.update_available(E.Pack(name="x", version="1.4.0"), listing) is True
    assert E.update_available(E.Pack(name="x", version="1.5.0"), listing) is False


def test_a_pack_with_no_version_cannot_be_compared():
    """Nine of the packs here carry no version at all, and a missing one is not an
    update - reporting it as one would put a permanent notice on a third of the list."""
    listing = E.parse_listing(KJNODES)
    assert E.update_available(E.Pack(name="x", version=""), listing) is False
    assert E.update_available(E.Pack(name="x", version="1.4.0"), E.Listing(id="y")) is False


REAL_MMAUDIO = [
    "accelerate>=0.33.0",
    "numpy<=1.26.4",
    "huggingface-hub>=0.24.5",
    "# for Image utils",
    "imagesize>=1.4.1",
    "came_pytorch",
    "# for T5XXL tokenizer (SD3/FLUX)",
    "sentencepiece>=0.2.0",
]


def test_a_comment_a_pack_left_in_its_dependency_list_is_dropped():
    """Verbatim from ComfyUI-MMAudio's own pyproject.toml on this install: a
    requirements.txt converted to TOML by something that kept the comments as entries.
    `plan_packages` is documented as taking this list, and uv fails the whole plan on a
    line that was never a requirement."""
    assert E.clean_requirements(REAL_MMAUDIO) == [
        "accelerate>=0.33.0",
        "numpy<=1.26.4",
        "huggingface-hub>=0.24.5",
        "imagesize>=1.4.1",
        "came_pytorch",
        "sentencepiece>=0.2.0",
    ]


def test_a_hash_inside_a_direct_reference_survives():
    """`#sha256=` is part of the URL, and truncating there installs different bytes."""
    line = "pkg @ https://host/pkg-1.0-py3-none-any.whl#sha256=abc123"
    assert E.clean_requirements([line]) == [line]


def test_blanks_go_too():
    assert E.clean_requirements(["", "   ", "pillow"]) == ["pillow"]


def test_both_readers_clean_the_list():
    """It arrives this way from the disk and from the registry, so neither may pass it on."""
    toml = 'dependencies = ["a>=1", "# note", "b"]\n'
    assert E.parse_pyproject("[project]\n" + toml)["dependencies"] == ["a>=1", "b"]
    listing = E.parse_listing({"id": "x", "latest_version": {"dependencies": ["# note", "c"]}})
    assert listing.dependencies == ["c"]
