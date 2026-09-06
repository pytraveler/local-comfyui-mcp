"""The rules that decide whether a `pip install` is allowed to happen.

Every fixture here is text measured on a real install rather than invented, because the
one failure this module has to prevent - a plan that moves torch onto a CPU build -
looks entirely ordinary in the abstract and is only recognisable in the exact shape uv
prints it.
"""

from __future__ import annotations

import pytest

from comfyui_mcp import env as E

REAL_PLAN = """Using Python 3.13.12 environment at: V:\\Programs\\Comfy\\Comfyui_portable\\python_embeded
Resolved 13 packages in 5.26s
Would download 2 packages
Would uninstall 2 packages
Would install 2 packages
 - torch==2.11.0+cu130 (from file:///D:/a/ComfyUI/cu130_python_deps/torch-2.11.0%2Bcu130-cp313-cp313-win_amd64.whl)
 + torch==2.10.0
 - torchvision==0.26.0+cu130
 + torchvision==0.25.0
"""

NO_CHANGES = """Using Python 3.13.12 environment at: V:\\Programs\\Comfy\\Comfyui_portable\\python_embeded
Checked 1 package in 117ms
Would make no changes
"""

ADDITIVE = """Using Python 3.13.12 environment at: V:\\x\\python_embeded
Resolved 3 packages in 900ms
Would download 3 packages
Would install 3 packages
 + insightface==0.7.3
 + onnx==1.17.0
 + prettytable==3.12.0
"""

INSTALLED = E.parse_freeze(
    """
torch==2.11.0+cu130
torchvision==0.26.0+cu130
torchaudio==2.11.0+cu130
sageattention==2.2.0
triton-windows==3.7.0.post26
pillow==11.0.0
numpy==2.1.3
"""
)


def test_a_freeze_line_becomes_a_pin():
    pin = E.parse_pin("torch==2.11.0+cu130")
    assert pin is not None
    assert (pin.name, pin.version, pin.local) == ("torch", "2.11.0+cu130", "cu130")


def test_the_wheel_form_of_freeze_is_dropped_rather_than_half_understood():
    """`uv pip freeze` on a portable build emits a path from the machine that built it.

    Measured: 80 of 286 distributions here record
    `file:///D:/a/ComfyUI/cu130_python_deps/...`, a directory on a GitHub Actions
    runner. A checkpoint written that way fails on its first line, on every machine, so
    the line is refused rather than parsed into something that looks restorable.
    """
    assert E.parse_pin("aiohttp @ file:///D:/a/ComfyUI/cu130_python_deps/aiohttp.whl") is None


@pytest.mark.parametrize("junk", ["", "   ", "# a comment", "torch", "torch>=2.0"])
def test_anything_that_is_not_a_pin_is_not_a_pin(junk):
    assert E.parse_pin(junk) is None


def test_names_compare_the_way_pep_503_says():
    assert E.normalise("ComfyUI_KJNodes") == E.normalise("comfyui-kjnodes") == "comfyui-kjnodes"


def test_a_duplicate_name_keeps_the_first_reading():
    pins = E.parse_freeze("pillow==11.0.0\npillow==10.0.0\n")
    assert [p.version for p in pins] == ["11.0.0"]


def test_the_torch_index_is_read_off_the_installed_versions():
    assert E.torch_index_url(INSTALLED) == "https://download.pytorch.org/whl/cu130"


def test_a_plain_cpu_environment_names_no_extra_index():
    """Empty rather than a guess: an install with no local segment needs only PyPI, and
    pointing it at download.pytorch.org would change what it resolves for no reason."""
    assert E.torch_index_url(E.parse_freeze("torch==2.5.0\npillow==11.0.0\n")) == ""


def test_unrestorable_names_the_builds_no_index_carries():
    assert [p.name for p in E.unrestorable(INSTALLED)] == ["torch", "torchaudio", "torchvision"]


def test_packaging_tools_are_not_restored():
    pins = E.parse_freeze("pip==25.0\nsetuptools==80.0\npillow==11.0.0\n")
    assert [p.name for p in E.restorable(pins)] == ["pillow"]


def test_the_measured_plan_reads_as_two_moves():
    plan = E.parse_plan(REAL_PLAN)
    assert plan.resolved == 13
    assert plan.environment.endswith("python_embeded")
    assert plan.no_changes is False
    assert {(m.name, m.before, m.after) for m in plan.changes} == {
        ("torch", "2.11.0+cu130", "2.10.0"),
        ("torchvision", "0.26.0+cu130", "0.25.0"),
    }


def test_the_provenance_tail_is_dropped():
    """`(from file:///D:/a/...)` is uv saying where the *installed* one came from, which
    on a portable build is always the build runner and says nothing about the move."""
    plan = E.parse_plan(REAL_PLAN)
    torch = next(m for m in plan.moves if m.name == "torch")
    assert torch.before == "2.11.0+cu130"


def test_nothing_to_do_is_its_own_answer_not_an_empty_list():
    """"Nothing to do" and "could not tell" must not look alike: one means the caller can
    proceed, the other means the plan was not read."""
    plan = E.parse_plan(NO_CHANGES)
    assert plan.no_changes is True and plan.moves == ()
    assert E.parse_plan("some unrelated output").no_changes is False


def test_an_additive_plan_is_recognised_as_such():
    plan = E.parse_plan(ADDITIVE)
    assert E.plan_is_additive(plan) is True
    assert [m.name for m in plan.installs] == ["insightface", "onnx", "prettytable"]


def test_a_plan_with_a_move_is_not_additive():
    assert E.plan_is_additive(E.parse_plan(REAL_PLAN)) is False


def test_an_empty_plan_is_not_additive_either():
    """`plan_is_additive` answers "only adds", and a plan that does nothing adds
    nothing - reporting it as additive would put a reassuring word on an empty answer."""
    assert E.plan_is_additive(E.parse_plan(NO_CHANGES)) is False


def test_the_measured_plan_is_refused_and_names_torch():
    risks = E.plan_risks(E.parse_plan(REAL_PLAN), INSTALLED)
    assert {r.name for r in risks} == {"torch", "torchvision"}
    assert all(r.rule == "pinned" for r in risks)
    assert "2.11.0+cu130" in " ".join(r.detail for r in risks)


def test_a_healthy_plan_produces_nothing():
    """The constraint `diagnose` has, applied to a guard rather than a report: a rule
    that fires on an ordinary install trains everybody to pass whatever flag turns it
    off, and then it is not there for the plan that mattered."""
    assert E.plan_risks(E.parse_plan(ADDITIVE), INSTALLED) == []
    assert E.plan_risks(E.parse_plan(NO_CHANGES), INSTALLED) == []


def test_losing_a_cuda_build_is_caught_even_outside_the_pinned_family():
    plan = E.parse_plan(
        " - onnxruntime-gpu==1.26.0+cu130\n + onnxruntime-gpu==1.26.0\n"
    )
    risks = E.plan_risks(plan, INSTALLED)
    assert [r.rule for r in risks] == ["loses_cuda"]


def test_a_bare_removal_is_data_loss_and_says_so():
    risks = E.plan_risks(E.parse_plan(" - prettytable==3.12.0\n"), INSTALLED)
    assert [(r.name, r.rule) for r in risks] == [("prettytable", "removed")]


def test_an_extra_protected_name_is_honoured():
    plan = E.parse_plan(" - numpy==2.1.3\n + numpy==1.26.0\n")
    assert E.plan_risks(plan, INSTALLED) == []
    assert [r.rule for r in E.plan_risks(plan, INSTALLED, protect=frozenset({"numpy"}))] == ["pinned"]


def test_an_install_is_never_a_risk_even_for_a_pinned_name():
    """A pinned package that is not installed yet is not being moved - refusing there
    would block the one case where installing torch is the right answer."""
    assert E.plan_risks(E.parse_plan(" + xformers==0.0.28\n"), INSTALLED) == []


def test_a_sync_that_removes_a_local_build_is_a_loss():
    plan = E.parse_plan(" - torch==2.11.0+cu130\n - prettytable==3.12.0\n")
    assert [p.name for p in E.removal_losses(plan, INSTALLED)] == ["torch"]


def test_removing_an_ordinary_package_is_not_a_loss():
    """`prettytable` comes back from PyPI on the next install; that is the difference
    the refusal is drawing, and it has to let the ordinary case through."""
    assert E.removal_losses(E.parse_plan(" - prettytable==3.12.0\n"), INSTALLED) == []


def test_a_diff_reports_the_three_kinds_separately():
    before = E.parse_freeze("a==1\nb==2\nc==3\n")
    after = E.parse_freeze("a==1\nb==9\nd==4\n")
    changed = E.diff(before, after)
    assert changed.as_dict() == {
        "added": [{"name": "d", "version": "4"}],
        "removed": [{"name": "c", "version": "3"}],
        "changed": [{"name": "b", "from": "2", "to": "9"}],
    }


def test_an_unchanged_environment_diffs_to_nothing():
    assert E.diff(INSTALLED, INSTALLED).empty is True


REPORTED = "3.13.12 (tags/v3.13.12:1cbe481, Feb  3 2026, 18:22:25) [MSC v.1944 64 bit (AMD64)]"


def test_the_same_interpreter_matches_whole():
    assert E.python_matches(REPORTED, REPORTED)
    assert E.python_mismatch(REPORTED, REPORTED) == ""


def test_a_different_python_is_refused_and_the_message_names_both():
    other = "3.12.4 (main, Jun  7 2024) [MSC v.1937 64 bit (AMD64)]"
    problem = E.python_mismatch(other, REPORTED)
    assert "3.12.4" in problem and "3.13.12" in problem
    assert "COMFYUI_PYTHON" in problem


def test_a_trimmed_report_still_matches_on_the_version_number():
    """The fallback for a ComfyUI that reports something shorter than `sys.version`.
    Weaker on purpose, and the only alternative is refusing every call on such an
    install."""
    assert E.python_matches(REPORTED, "3.13.12")


def test_an_empty_answer_never_counts_as_a_match():
    """A probe that failed must not read as agreement - that is the one way this check
    could pass while pointing at the wrong interpreter."""
    assert not E.python_matches("", REPORTED)
    assert not E.python_matches(REPORTED, "")


KJNODES = """
[project]
name = "comfyui-kjnodes"
description = "Various quality of life -nodes for ComfyUI."
version = "1.4.9"
dependencies = ["pillow>=10.3.0", "color-matcher", "matplotlib"]

[project.urls]
Repository = "https://github.com/kijai/ComfyUI-KJNodes"

[tool.comfy]
PublisherId = "kijai"
DisplayName = "ComfyUI-KJNodes"
"""


def test_a_pack_states_its_own_identity():
    meta = E.parse_pyproject(KJNODES)
    assert meta["registry_id"] == "comfyui-kjnodes"
    assert meta["version"] == "1.4.9"
    assert meta["publisher"] == "kijai"
    assert meta["repo"] == "https://github.com/kijai/ComfyUI-KJNodes"
    assert meta["dependencies"][0] == "pillow>=10.3.0"


@pytest.mark.parametrize("junk", ["", "not toml at all {{{", "[project]\nname = 3\n"])
def test_a_missing_or_broken_pyproject_is_ordinary_rather_than_an_error(junk):
    """Plenty of older packs are a bare `__init__.py` in a git clone, and they are still
    installed. A reader that raised on one would fail every checkpoint on a real machine."""
    assert isinstance(E.parse_pyproject(junk), dict)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/kijai/ComfyUI-KJNodes",
        "https://github.com/kijai/ComfyUI-KJNodes.git",
        "https://github.com/kijai/ComfyUI-KJNodes/",
        "git@github.com:kijai/ComfyUI-KJNodes.git",
    ],
)
def test_every_spelling_of_one_repository_is_one_key(url):
    assert E.repo_key(url) == "kijai/comfyui-kjnodes"


def test_the_same_pack_under_two_folder_names_is_one_pack():
    """The failure this prevents is not a folder-name collision - it is two copies of one
    pack registering the same class names, which ComfyUI resolves by whichever imported
    last."""
    a = E.Pack(name="ComfyUI-KJNodes", registry_id="comfyui-kjnodes")
    b = E.Pack(name="comfyui_kjnodes", registry_id="ComfyUI_KJNodes")
    assert E.same_pack(a, b)


def test_the_remote_decides_when_neither_has_a_registry_id():
    a = E.Pack(name="kjnodes-fork", repo="git@github.com:kijai/ComfyUI-KJNodes.git")
    b = E.Pack(name="ComfyUI-KJNodes", repo="https://github.com/kijai/ComfyUI-KJNodes")
    assert E.same_pack(a, b)


def test_two_different_packs_are_not_confused():
    a = E.Pack(name="ComfyUI-KJNodes", registry_id="comfyui-kjnodes")
    b = E.Pack(name="rgthree-comfy", registry_id="rgthree-comfy")
    assert not E.same_pack(a, b)


def test_a_disabled_pack_serialises_as_disabled():
    assert E.Pack(name="x", enabled=False).as_dict() == {"name": "x", "enabled": False}


def test_an_empty_field_is_left_out_rather_than_reported_as_empty():
    """The rule the rest of the server follows: report a value only when it is not the
    boring answer. Thirty-eight packs times four empty strings is noise."""
    assert "commit" not in E.Pack(name="x").as_dict()


def test_one_move_produces_one_risk():
    """A torch downgrade is pinned and unrestorable and arguably a removal; saying all
    three is three sentences about one fact, which reads as three problems. Measured on
    the plan above: four risks for two moves before the rules were made exclusive."""
    risks = E.plan_risks(E.parse_plan(REAL_PLAN), INSTALLED)
    assert len(risks) == len(E.parse_plan(REAL_PLAN).moves) == 2


def test_a_local_build_moving_to_another_local_build_is_still_worth_saying():
    """The case only `unrestorable` catches: not the pinned family, and the +cu tag
    survives, so nothing else fires - but if the move is wrong there is nothing to
    install back from."""
    pins = E.parse_freeze("onnxruntime-gpu==1.26.0+cu130\n")
    plan = E.parse_plan(" - onnxruntime-gpu==1.26.0+cu130\n + onnxruntime-gpu==1.25.0+cu130\n")
    assert [r.rule for r in E.plan_risks(plan, pins)] == ["unrestorable"]


RECORDED = E.parse_freeze("pillow==11.0.0\ntorch==2.11.0+cu130\n")
LIVE = E.parse_freeze(
    "pillow==11.0.0\ntorch==2.11.0+cu130\nsetuptools==81.0.0\nwheel==0.47.0\nalbucore==0.0.24\n"
)


def test_a_sync_carries_the_packaging_tools_so_it_does_not_delete_them():
    """Measured on a real checkpoint: a sync of the 282 restorable pins planned
    `- setuptools==81.0.0` and `- wheel==0.47.0`, because omitting a name from a sync
    list means uninstall it. That is how an environment loses the ability to build
    anything at install time."""
    names = {p.name for p in E.sync_list(RECORDED, LIVE)}
    assert {"setuptools", "wheel"} <= names
    assert "albucore" not in names, "a sync is still meant to remove what is not recorded"


def test_they_are_carried_at_the_version_installed_now_not_the_recorded_one():
    """"Leave them alone" means neither removed nor moved."""
    recorded = E.parse_freeze("pillow==11.0.0\nsetuptools==70.0.0\n")
    carried = {p.name: p.version for p in E.sync_list(E.restorable(recorded), LIVE)}
    assert carried["setuptools"] == "81.0.0"


def test_the_install_list_still_omits_them():
    assert "setuptools" not in {p.name for p in E.restorable(LIVE)}


def test_packaging_tools_are_not_reported_as_installed_since_the_checkpoint():
    """They are deliberately absent from the recorded list, so calling pip "installed
    since" is a claim about the user's environment that is simply untrue."""
    assert [p.name for p in E.installed_since(RECORDED, LIVE)] == ["albucore"]
