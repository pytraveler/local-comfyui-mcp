"""The install half: what is sent to ComfyUI-Manager, and how its answer is read.

Offline by construction. Manager's queue is HTTP plus one broadcast WebSocket frame,
and every decision made around it - what the body must contain, whether the security
level permits anything, whether our item succeeded, whether a plan may be applied while
ComfyUI is running - is a pure function so it can be tested without a 286-package
environment and a GPU. `manager.py` keeps only the sockets.
"""

from __future__ import annotations

import asyncio

import pytest

from comfyui_mcp import env as E
from comfyui_mcp import manager as M


def test_the_install_body_carries_the_three_keys_manager_subscripts():
    """Manager indexes these directly, so a missing one is a 500 rather than a 400."""
    item = E.install_item("comfyui-kjnodes", "1.5.0")
    for required in ("version", "channel", "mode"):
        assert required in item, required


def test_post_install_is_skipped_and_that_is_the_whole_point():
    """The seam: Manager fetches and places, this server decides about packages.

    Without it Manager pip-installs requirements.txt line by line with nothing watching
    torch, and runs the pack's own install.py.
    """
    assert E.install_item("x", "1.0")["skip_post_install"] is True


def test_the_two_version_keys_agree():
    """`version` is only tested against "unknown"; `selected_version` is the real choice.

    Sending different values in the two is how a request means one thing to the guard
    and another to the installer.
    """
    item = E.install_item("x", "2.1.0")
    assert item["version"] == item["selected_version"] == "2.1.0"


def test_no_version_means_latest():
    assert E.install_item("x", "")["selected_version"] == "latest"
    assert E.install_item("x", "   ")["selected_version"] == "latest"


def test_unknown_is_refused_because_it_is_a_different_operation():
    """"unknown" selects Manager's git-URL path, which it rates high-risk."""
    with pytest.raises(ValueError):
        E.install_item("x", "unknown")


def test_the_ui_id_defaults_to_the_pack_and_can_be_named():
    assert E.install_item("kj", "1.0")["ui_id"] == "kj"
    assert E.install_item("kj", "1.0", ui_id="mine")["ui_id"] == "mine"


@pytest.mark.parametrize("level", ["weak", "normal", "normal-", "NORMAL", " weak "])
def test_the_levels_that_permit_installing_say_nothing(level):
    assert E.security_refusal(level) == ""


def test_strong_is_refused_and_the_refusal_names_the_setting():
    said = E.security_refusal("strong")
    assert "security_level" in said
    assert "middle or below" in said


def test_an_unreadable_level_is_not_treated_as_a_refusal():
    """The file is read for the message; Manager's HTTP status is the authority.

    Guessing meanly here would refuse installs that would have worked, over a config
    file that `--user-directory` may simply have moved.
    """
    assert E.security_refusal("") == ""


def test_a_success_is_the_literal_string_manager_returns():
    ok, message = E.queue_outcome({"nodepack_result": {"kj": "success"}}, "kj")
    assert ok is True
    assert message == "success"


def test_a_failure_carries_managers_own_words():
    event = {"nodepack_result": {"kj": "Installation failed:\nkj@1.0"}}
    ok, message = E.queue_outcome(event, "kj")
    assert ok is False
    assert "Installation failed" in message


def test_an_item_missing_from_the_result_is_not_a_success():
    """Somebody queueing in the browser at the same time looks exactly like this."""
    ok, message = E.queue_outcome({"nodepack_result": {"other": "success"}}, "kj")
    assert ok is False
    assert "other" in message


def test_a_done_event_with_no_result_map_is_not_a_success():
    ok, _ = E.queue_outcome({"total_count": 1, "done_count": 1}, "kj")
    assert ok is False


def test_an_idle_queue_is_not_busy():
    idle = {"total_count": 0, "done_count": 0, "in_progress_count": 0, "is_processing": False}
    assert E.queue_is_busy(idle) is False


def test_a_running_worker_is_busy():
    assert E.queue_is_busy({"is_processing": True, "total_count": 0, "done_count": 0}) is True


def test_work_waiting_with_no_worker_yet_is_busy():
    """`/queue/install` accepted without `/queue/start` is exactly this state."""
    assert E.queue_is_busy({"is_processing": False, "total_count": 2, "done_count": 0}) is True


def test_an_empty_status_is_not_read_as_busy():
    assert E.queue_is_busy({}) is False


def test_an_inline_comment_is_stripped_only_after_whitespace():
    got, _ = E.parse_requirements("torch>=2.0  # needed for the encoder\n")
    assert got == ["torch>=2.0"]


def test_a_fragment_in_a_direct_reference_survives():
    """`split('#')[0]` - Manager's rule - truncates this and installs different bytes."""
    line = "pkg @ https://host/wheels/pkg-1.0-py3-none-any.whl#sha256=deadbeef"
    got, _ = E.parse_requirements(line + "\n")
    assert got == [line]


def test_a_whole_line_comment_is_dropped():
    got, _ = E.parse_requirements("# for image utils\nnumpy\n")
    assert got == ["numpy"]


def test_options_are_set_aside_rather_than_dropped():
    """`--index-url` changes where every package comes from; silence about it would lie."""
    got, skipped = E.parse_requirements("numpy\n-r base.txt\n--index-url https://x/simple\n")
    assert got == ["numpy"]
    assert skipped == ["-r base.txt", "--index-url https://x/simple"]


def test_blank_lines_and_stray_whitespace_are_nothing():
    got, skipped = E.parse_requirements("\n\n   \n#\n")
    assert got == []
    assert skipped == []


def _plan(**kw) -> E.Plan:
    return E.Plan(**kw)


def test_a_plan_that_does_nothing_disturbs_nothing():
    """The bug this function exists for: `plan_is_additive` answers False here too.

    Asking that one instead makes an install defer packages it was never going to move.
    """
    plan = _plan(no_changes=True)
    assert E.disturbs_installed(plan) is False
    assert E.plan_is_additive(plan) is False


def test_new_packages_alone_disturb_nothing():
    plan = _plan(moves=(E.Move(name="cowsay", after="6.1"),))
    assert E.disturbs_installed(plan) is False


def test_moving_an_installed_version_disturbs_it():
    plan = _plan(moves=(E.Move(name="numpy", before="1.0", after="2.0"),))
    assert E.disturbs_installed(plan) is True


def test_removing_a_package_disturbs_it():
    plan = _plan(moves=(E.Move(name="numpy", before="1.0"),))
    assert E.disturbs_installed(plan) is True


def test_the_settings_are_read_from_comfyuis_user_directory(tmp_path, monkeypatch):
    from comfyui_mcp.config import load_config

    cfg = load_config()
    target = tmp_path / "ComfyUI" / "user" / "__manager"
    target.mkdir(parents=True)
    (target / "config.ini").write_text(
        "[default]\nsecurity_level = strong\nuse_uv = True\n", encoding="utf-8"
    )
    monkeypatch.setattr(type(cfg), "comfy_dir", property(lambda self: tmp_path / "ComfyUI"))
    assert M.read_settings(cfg)["security_level"] == "strong"


def test_a_missing_settings_file_is_ordinary(tmp_path, monkeypatch):
    from comfyui_mcp.config import load_config

    cfg = load_config()
    monkeypatch.setattr(type(cfg), "comfy_dir", property(lambda self: tmp_path / "nowhere"))
    assert M.read_settings(cfg) == {}


def test_a_403_names_the_levels_that_would_work():
    said = M.refusal_text(403, "ERROR: To use this action, a security_level of...", "kj")
    assert "normal-" in said


def test_a_404_says_which_of_its_two_meanings_apply():
    """Manager answers 404 both for "no such pack" and for "too risky for this level"."""
    said = M.refusal_text(404, "A security error has occurred.", "kj")
    assert "two different things" in said
    assert "nightly" in said


def test_any_other_status_still_reports_the_status():
    assert "HTTP 500" in M.refusal_text(500, "", "kj")


class FakeSocket:
    """Frames in the order ComfyUI would send them."""

    def __init__(self, frames):
        self._frames = list(frames)

    async def recv(self):
        if not self._frames:
            await asyncio.sleep(3600)  # nothing more is coming
        return self._frames.pop(0)


def _run(coro):
    return asyncio.run(coro)


def test_the_done_event_is_returned():
    frames = ['{"type": "cm-queue-status", "data": {"status": "done", "done_count": 1}}']
    got = _run(M.await_done(FakeSocket(frames), 5))
    assert got["status"] == "done"


def test_binary_frames_are_skipped_without_decoding():
    """Those are ComfyUI's image previews - a generation running alongside is ordinary."""
    frames = [b"\x00\x01preview", '{"type": "cm-queue-status", "data": {"status": "done"}}']
    assert _run(M.await_done(FakeSocket(frames), 5)) == {"status": "done"}


def test_somebody_elses_events_are_ignored():
    frames = [
        '{"type": "progress", "data": {"value": 3, "max": 20}}',
        '{"type": "executing", "data": {"node": "12"}}',
        '{"type": "cm-queue-status", "data": {"status": "done", "total_count": 1}}',
    ]
    assert _run(M.await_done(FakeSocket(frames), 5))["total_count"] == 1


def test_an_in_progress_event_is_not_the_end():
    """Manager sends one of these per item and clears its results only on `done`."""
    frames = [
        '{"type": "cm-queue-status", "data": {"status": "in_progress", "target": "kj"}}',
        '{"type": "cm-queue-status", "data": {"status": "done", "done_count": 1}}',
    ]
    assert _run(M.await_done(FakeSocket(frames), 5))["done_count"] == 1


def test_a_frame_that_is_not_json_does_not_end_the_wait():
    frames = ["not json at all", '{"type": "cm-queue-status", "data": {"status": "done"}}']
    assert _run(M.await_done(FakeSocket(frames), 5)) == {"status": "done"}


def test_silence_becomes_none_rather_than_hanging():
    assert _run(M.await_done(FakeSocket([]), 0.1)) is None


@pytest.mark.parametrize(
    "url",
    [
        "--upload-pack=/bin/sh",
        "git@github.com:owner/repo.git",
        "file:///etc/passwd",
        "http://github.com/owner/repo",
        "/home/user/repo",
        "",
    ],
)
def test_only_https_is_cloned(url):
    """Everything else reaches something other than a fetch, or is an option to git."""
    assert E.check_clone_url(url) != ""


def test_an_https_url_is_accepted():
    assert E.check_clone_url("https://github.com/owner/repo") == ""


def test_whitespace_means_it_is_not_one_url():
    assert "whitespace" in E.check_clone_url("https://host/a b")


def test_the_folder_name_cannot_choose_where_the_clone_lands():
    assert E.repo_folder("https://host/../../etc") == "etc"
    assert E.repo_folder("https://host/owner/name.git") == "name"
    assert "/" not in E.repo_folder("https://host/a/b/c")
    assert "\\" not in E.repo_folder("https://host/a%5Cb")


def test_a_folder_name_is_never_empty():
    """A tail that is nothing but punctuation cleans down to nothing, and must not."""
    assert E.repo_folder("https://host/---") == "repository"
    assert E.repo_folder("https://host/...") == "repository"
    assert E.repo_folder("https://host/") == "host"


def test_a_repository_manager_knows_is_installable():
    known = {"https://github.com/owner/repo"}
    assert E.manager_rating(known, {"numpy"}, list(known), ["numpy"]) == E.RATING_INSTALLABLE


def test_a_repository_manager_does_not_know_is_high():
    assert E.manager_rating(set(), set(), ["https://github.com/owner/repo"], []) == (
        E.RATING_UNKNOWN_REPO
    )


def test_a_known_repository_asking_for_an_unknown_package_is_blocked():
    """`block` is refused at every security level, the weakest included."""
    known = {"https://github.com/owner/repo"}
    rating = E.manager_rating(known, {"numpy"}, list(known), ["something-nobody-has"])
    assert rating == E.RATING_UNKNOWN_PIP


def test_an_unreadable_catalogue_is_not_read_as_knowing_nothing(tmp_path, monkeypatch):
    """Empty sets mean "cannot tell"; treating them as knowledge rates everything high."""
    from comfyui_mcp.config import load_config

    cfg = load_config()
    monkeypatch.setattr(type(cfg), "comfy_dir", property(lambda self: tmp_path / "nowhere"))
    assert M.node_list(cfg) == (set(), set())
