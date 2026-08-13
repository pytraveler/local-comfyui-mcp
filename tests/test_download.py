"""Tests for fetching a model into the folder ComfyUI will load it from.

Two things can go wrong quietly and both are expensive. A file written to a
directory ComfyUI does not read is indistinguishable from one that was never
downloaded, except that it cost several gigabytes to find out; and a transfer
that restarts from zero after every hiccup never finishes on a slow link. So
most of what is worth testing is where a file lands and what happens when the
connection does not hold.

Everything runs offline against a mocked transport; nothing is fetched.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from comfyui_mcp import download as D

Handler = Callable[[httpx.Request], httpx.Response]


def run(coro):
    return asyncio.run(coro)


def client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


URL = "https://huggingface.co/Org/Repo/resolve/main/vae/model.safetensors"
CDN = "https://us.aws.cdn.hf.co/xet-bridge-us/abc?Expires=1&Signature=2"
SUFFIXES = [".safetensors", ".ckpt", ".pt"]


# --- where a file lands -------------------------------------------------------
def test_the_first_directory_that_exists_is_chosen(tmp_path: Path):
    # ComfyUI puts the `is_default` folder first, so its order is the preference.
    first, second = tmp_path / "archive", tmp_path / "portable"
    second.mkdir()
    dest = D.choose_destination("vae", [str(first), str(second)], SUFFIXES, "model.safetensors")
    assert dest.directory == second
    assert dest.path == second / "model.safetensors"


def test_passing_over_a_missing_directory_is_reported(tmp_path: Path):
    # The one that gets passed over is usually ComfyUI's own shipped example with a
    # `# legacy location still supported` comment glued onto the path, and silently
    # using the second is exactly the kind of thing that reads as a bug later.
    broken = str(tmp_path / "clip" / "  # legacy location still supported")
    real = tmp_path / "text_encoders"
    real.mkdir()
    dest = D.choose_destination("text_encoders", [broken, str(real)], SUFFIXES, "t5.safetensors")
    assert dest.directory == real
    assert "does not exist" in dest.note


def test_the_directory_named_after_the_folder_wins_a_tie(tmp_path: Path):
    # 'diffusion_models' reaches both models/unet and models/diffusion_models, and
    # which of the two ComfyUI lists first is historical. Either loads; only one is
    # where somebody will look for the file afterwards.
    alias, named = tmp_path / "unet", tmp_path / "diffusion_models"
    alias.mkdir()
    named.mkdir()
    dest = D.choose_destination(
        "diffusion_models", [str(alias), str(named)], SUFFIXES, "model.safetensors"
    )
    assert dest.directory == named
    assert "also reaches" in dest.note


def test_the_first_directory_still_wins_when_none_is_named_after_the_folder(tmp_path: Path):
    first, second = tmp_path / "unet", tmp_path / "unet_extra"
    first.mkdir()
    second.mkdir()
    dest = D.choose_destination(
        "diffusion_models", [str(first), str(second)], SUFFIXES, "model.safetensors"
    )
    assert dest.directory == first
    assert dest.note == ""


def test_a_directory_that_exists_is_not_reported(tmp_path: Path):
    tmp_path.joinpath("vae").mkdir()
    dest = D.choose_destination("vae", [str(tmp_path / "vae")], SUFFIXES, "model.safetensors")
    assert dest.note == ""


def test_no_directory_existing_is_refused_rather_than_created(tmp_path: Path):
    # Creating the first candidate is how a model ends up in a folder named after a
    # YAML comment. Refusing costs a round trip; guessing costs the download.
    with pytest.raises(D.DownloadError, match="Create the one you want"):
        D.choose_destination("vae", [str(tmp_path / "nope")], SUFFIXES, "model.safetensors")


def test_a_directory_comfyui_does_not_register_is_refused(tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(D.DownloadError, match="never be found"):
        D.choose_destination(
            "vae", [str(tmp_path / "vae")], SUFFIXES, "model.safetensors", directory=str(elsewhere)
        )


def test_a_named_directory_from_the_list_is_honoured(tmp_path: Path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    dest = D.choose_destination(
        "vae", [str(first), str(second)], SUFFIXES, "model.safetensors", directory=str(second)
    )
    assert dest.directory == second


def test_an_extension_the_folder_does_not_list_is_refused(tmp_path: Path):
    # ComfyUI lists a folder's files by extension, so a .zip there is invisible to it.
    tmp_path.joinpath("vae").mkdir()
    with pytest.raises(D.DownloadError, match="invisible"):
        D.choose_destination("vae", [str(tmp_path / "vae")], SUFFIXES, "model.zip")


def test_a_folder_with_no_directories_at_all_is_refused():
    with pytest.raises(D.DownloadError, match="registers no directory"):
        D.choose_destination("nonesuch", [], SUFFIXES, "model.safetensors")


@pytest.mark.parametrize("name", ["/etc/model.safetensors", "C:\\models\\x.safetensors"])
def test_an_absolute_name_is_refused(tmp_path: Path, name: str):
    tmp_path.joinpath("vae").mkdir()
    with pytest.raises(D.DownloadError, match="absolute path"):
        D.choose_destination("vae", [str(tmp_path / "vae")], SUFFIXES, name)


def test_a_name_climbing_out_of_the_folder_is_refused(tmp_path: Path):
    tmp_path.joinpath("vae").mkdir()
    with pytest.raises(D.DownloadError, match="climbs out"):
        D.choose_destination("vae", [str(tmp_path / "vae")], SUFFIXES, "../../x.safetensors")


def test_a_subdirectory_in_the_name_is_allowed(tmp_path: Path):
    # ComfyUI loads 'sub/name.safetensors' happily; the option list is not a whitelist.
    tmp_path.joinpath("vae").mkdir()
    dest = D.choose_destination("vae", [str(tmp_path / "vae")], SUFFIXES, "wan/model.safetensors")
    assert dest.path == tmp_path / "vae" / "wan" / "model.safetensors"


def test_a_folder_declaring_no_extensions_accepts_anything(tmp_path: Path):
    # The older /internal/folder_paths endpoint reports no extensions at all.
    tmp_path.joinpath("vae").mkdir()
    assert D.choose_destination("vae", [str(tmp_path / "vae")], [], "model.bin").path.name == "model.bin"


def test_the_file_name_comes_from_the_url():
    assert D.filename_from_url(URL) == "model.safetensors"
    assert D.filename_from_url("https://host/a%20b.safetensors") == "a b.safetensors"


def test_a_url_ending_in_a_slash_names_no_file():
    assert D.filename_from_url("https://host/repo/") == ""


def test_a_file_larger_than_the_free_space_is_refused(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(D, "free_space", lambda _: 1024)
    with pytest.raises(D.DownloadError, match="free"):
        D.check_space(tmp_path, 10 * 1024**3)


def test_an_unknown_size_cannot_be_checked_against_the_disk(tmp_path: Path):
    D.check_space(tmp_path, None)  # no size to compare, so no refusal


# --- reading the headers before fetching --------------------------------------
def hub(request: httpx.Request) -> httpx.Response:
    """The two hops HuggingFace really answers with: a pointer, then a signed CDN link."""
    if request.url.host == "huggingface.co":
        return httpx.Response(
            302,
            headers={
                "location": CDN,
                "content-length": "1074",  # the LFS pointer, not the model
                "x-linked-size": "5207808496",
                "x-linked-etag": '"' + "a" * 64 + '"',
            },
        )
    return httpx.Response(200, headers={"content-length": "5207808496", "accept-ranges": "bytes"})


def test_the_size_comes_from_the_linked_header_not_the_pointer():
    plan = run(D.preflight(client(hub), URL))
    assert plan.size == 5207808496
    assert plan.resumable is True
    assert plan.final_url == CDN


def test_the_checksum_is_reported_though_nothing_enforces_it():
    assert run(D.preflight(client(hub), URL)).sha256 == "a" * 64


def test_the_token_reaches_the_origin_and_no_further():
    # A CDN link is pre-signed; an Authorization header on that hop is a 400, not a
    # header the server ignores. This is the one thing the transport must get right.
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("authorization")
        return hub(request)

    run(D.preflight(client(handler), URL, token="hf_secret"))
    assert seen["huggingface.co"] == "Bearer hf_secret"
    assert seen["us.aws.cdn.hf.co"] is None


def test_a_gated_file_says_where_the_token_goes():
    handler = lambda request: httpx.Response(403)  # noqa: E731
    with pytest.raises(D.DownloadError, match="COMFYUI_DOWNLOAD_TOKEN"):
        run(D.preflight(client(handler), URL))


def test_a_missing_file_points_back_at_the_note():
    handler = lambda request: httpx.Response(404)  # noqa: E731
    with pytest.raises(D.DownloadError, match="note"):
        run(D.preflight(client(handler), URL))


def test_a_redirect_that_never_lands_gives_up():
    handler = lambda request: httpx.Response(302, headers={"location": CDN})  # noqa: E731
    with pytest.raises(D.DownloadError, match="redirects"):
        run(D.preflight(client(handler), URL))


def test_a_store_refusing_head_is_asked_with_a_range_instead():
    # Plenty of stores answer HEAD with 405; a one-byte range asks the same question.
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(206, headers={"content-range": "bytes 0-0/4096"}, content=b"x")

    plan = run(D.preflight(client(handler), URL))
    assert methods == ["HEAD", "GET"]
    assert plan.size == 4096
    assert plan.resumable is True


def test_something_that_is_not_a_url_is_refused():
    with pytest.raises(D.DownloadError, match="only http"):
        run(D.preflight(client(hub), "ftp://host/model.safetensors"))


# --- the transfer -------------------------------------------------------------
BODY = b"0123456789" * 100  # 1000 bytes


def serving(body: bytes = BODY, *, ranges: bool = True) -> Handler:
    """A single-hop store that honours Range when asked to."""

    def handler(request: httpx.Request) -> httpx.Response:
        span = request.headers.get("range")
        if request.method == "HEAD":
            return httpx.Response(
                200, headers={"content-length": str(len(body)), "accept-ranges": "bytes"}
            )
        if span and ranges:
            start = int(span.split("=", 1)[1].split("-")[0])
            return httpx.Response(
                206,
                headers={"content-range": f"bytes {start}-{len(body) - 1}/{len(body)}"},
                content=body[start:],
            )
        return httpx.Response(200, headers={"content-length": str(len(body))}, content=body)

    return handler


def fetch(handler: Handler, destination: Path, **kwargs: Any) -> D.Transfer:
    return run(D.fetch(client(handler), URL, destination, **kwargs))


def test_a_download_lands_at_the_destination(tmp_path: Path):
    result = fetch(serving(), tmp_path / "model.safetensors")
    assert (tmp_path / "model.safetensors").read_bytes() == BODY
    assert result.size == len(BODY)
    assert result.attempts == 1


def test_the_destination_only_appears_once_the_file_is_whole(tmp_path: Path):
    # ComfyUI scans the folder whenever it is asked; a half-written model that is
    # already named .safetensors is one it will happily try to load.
    target = tmp_path / "model.safetensors"
    part = target.with_name("model.safetensors.part")
    seen = []

    def watch(done: int, total: int | None) -> None:
        seen.append((target.exists(), part.exists()))

    fetch(serving(), target, on_progress=watch, chunk=100)
    assert seen and not any(exists for exists, _ in seen)
    assert all(written for _, written in seen)
    assert not part.exists()


def test_an_interrupted_download_resumes_from_what_is_already_there(tmp_path: Path):
    target = tmp_path / "model.safetensors"
    target.with_name("model.safetensors.part").write_bytes(BODY[:400])
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            asked.append(request.headers.get("range"))
        return serving()(request)

    result = fetch(handler, target, size=len(BODY))
    assert asked == ["bytes=400-"]
    assert target.read_bytes() == BODY
    assert result.resumed_from == 400


def test_a_server_that_ignores_range_starts_the_file_over(tmp_path: Path):
    # Answering 200 to a Range request means the body is the whole file; appending
    # it to what is already there would produce a corrupt model of the right size.
    target = tmp_path / "model.safetensors"
    target.with_name("model.safetensors.part").write_bytes(b"junk" * 100)
    fetch(serving(ranges=False), target, size=len(BODY))
    assert target.read_bytes() == BODY


def test_a_leftover_part_at_or_past_the_full_size_is_discarded(tmp_path: Path):
    target = tmp_path / "model.safetensors"
    target.with_name("model.safetensors.part").write_bytes(b"x" * (len(BODY) + 50))
    fetch(serving(), target, size=len(BODY))
    assert target.read_bytes() == BODY


def test_a_short_file_is_refused_rather_than_moved_into_place(tmp_path: Path):
    target = tmp_path / "model.safetensors"
    with pytest.raises(D.DownloadError, match="expected"):
        fetch(serving(BODY[:10]), target, size=len(BODY))
    assert not target.exists()


def test_a_network_error_is_retried_and_the_file_still_arrives(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(D, "_pause", lambda attempt: 0.0)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("connection reset", request=request)
        return serving()(request)

    result = fetch(handler, tmp_path / "model.safetensors", retries=3)
    assert result.attempts == 2
    assert (tmp_path / "model.safetensors").read_bytes() == BODY


def test_giving_up_says_how_many_attempts_it_took(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(D, "_pause", lambda attempt: 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(D.DownloadError, match="gave up after 3 attempts"):
        fetch(handler, tmp_path / "model.safetensors", retries=3)


def test_a_refusal_is_not_retried(tmp_path: Path, monkeypatch):
    # 403 means the request is wrong, and asking again five times only wastes a minute.
    monkeypatch.setattr(D, "_pause", lambda attempt: 0.0)
    gets = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "1000"})
        gets["n"] += 1
        return httpx.Response(403)

    with pytest.raises(D.DownloadError, match="gated"):
        fetch(handler, tmp_path / "model.safetensors", retries=5)
    assert gets["n"] == 1


def test_progress_is_reported_as_the_bytes_arrive(tmp_path: Path):
    seen: list[tuple[int, int | None]] = []
    fetch(
        serving(),
        tmp_path / "model.safetensors",
        on_progress=lambda done, total: seen.append((done, total)),
        chunk=250,
    )
    assert [done for done, _ in seen] == [250, 500, 750, 1000]
    assert {total for _, total in seen} == {1000}


def test_a_subdirectory_in_the_name_is_created(tmp_path: Path):
    target = tmp_path / "vae" / "wan" / "model.safetensors"
    fetch(serving(), target)
    assert target.is_file()


def test_human_size_reads_as_a_person_would_write_it():
    assert D.human_size(5207808496) == "4.85 GB"
    assert D.human_size(3 * 1024**2) == "3.00 MB"
    assert D.human_size(512) == "512 B"


HOSTS = D.hosts_from("huggingface.co,civitai.com")


def test_the_default_list_is_a_list():
    from comfyui_mcp.config import DEFAULT_DOWNLOAD_HOSTS

    assert "huggingface.co" in D.hosts_from(DEFAULT_DOWNLOAD_HOSTS)


def test_an_allowed_host_passes():
    D.check_host("https://huggingface.co/x/resolve/main/m.safetensors", HOSTS)


def test_a_subdomain_of_an_allowed_host_passes():
    # One `huggingface.co` has to cover the hosts the Hub actually serves from.
    assert D.host_allowed("cdn-lfs.huggingface.co", HOSTS) is True


def test_a_host_merely_ending_in_the_same_letters_does_not_pass():
    # "evilhuggingface.co" endswith "huggingface.co" as a string, and must not
    # pass on that alone - the boundary is the dot.
    assert D.host_allowed("evilhuggingface.co", HOSTS) is False


def test_a_host_not_on_the_list_is_refused_by_name():
    with pytest.raises(D.DownloadError, match="example.com"):
        D.check_host("https://example.com/model.safetensors", HOSTS)


def test_the_refusal_says_where_a_url_of_unknown_origin_comes_from():
    # The point is not "add this host" - it is "was this link yours".
    with pytest.raises(D.DownloadError, match="workflow"):
        D.check_host("https://example.com/model.safetensors", HOSTS)


def test_an_empty_list_is_the_opt_out():
    D.check_host("https://example.com/model.safetensors", D.hosts_from(""))
    assert D.host_allowed("anything.at.all", ()) is True


def test_a_leading_star_is_tolerated_in_the_list():
    assert D.host_allowed("cdn.civitai.com", D.hosts_from("*.civitai.com")) is True


def test_the_host_check_is_case_insensitive():
    D.check_host("https://HuggingFace.CO/x/m.safetensors", HOSTS)


def test_something_that_is_not_a_url_is_refused():
    with pytest.raises(D.DownloadError, match="not a URL"):
        D.check_host("just some text", HOSTS)


@pytest.mark.parametrize("name", ["m.ckpt", "m.pt", "m.pth", "m.bin", "m.pkl", "M.CKPT"])
def test_a_pickle_format_is_refused(name):
    with pytest.raises(D.DownloadError, match="executes code"):
        D.check_format(name, allow_pickle=False)


@pytest.mark.parametrize("name", ["m.safetensors", "m.sft", "m.gguf", "sub/m.safetensors"])
def test_a_data_format_passes(name):
    D.check_format(name, allow_pickle=False)


def test_the_pickle_refusal_names_the_way_out():
    with pytest.raises(D.DownloadError, match="COMFYUI_DOWNLOAD_ALLOW_PICKLE"):
        D.check_format("m.ckpt", allow_pickle=False)


def test_a_pickle_passes_once_it_is_allowed():
    D.check_format("m.ckpt", allow_pickle=True)


def test_a_file_over_the_ceiling_is_refused():
    with pytest.raises(D.DownloadError, match="ceiling"):
        D.check_size(3 * 1024**3, 2 * 1024**3)


def test_a_file_under_the_ceiling_passes():
    D.check_size(1024, 2 * 1024**3)


def test_no_ceiling_is_the_default_and_means_no_ceiling():
    # The volume is protected by check_space; real models run to 50 GB, so a
    # ceiling that fires on a legitimate one costs more than it saves.
    D.check_size(500 * 1024**3, 0)


def test_an_unknown_size_cannot_be_over_the_ceiling():
    D.check_size(None, 1024)


def run(coro):
    import asyncio

    return asyncio.run(coro)


@pytest.fixture
def offline(monkeypatch, tmp_path: Path):
    """A server whose ComfyUI answers, and whose network refuses to be used."""
    from comfyui_mcp import server as S

    folder = tmp_path / "vae"
    folder.mkdir()

    async def alive() -> None:
        return None

    async def dirs() -> dict[str, dict[str, list[str]]]:
        return {"vae": {"folders": [str(folder)], "extensions": [".safetensors", ".ckpt"]}}

    def forbidden(*_a, **_k):
        raise AssertionError("a request went out before the checks had finished")

    monkeypatch.setattr(S, "_require_alive", alive)
    monkeypatch.setattr(S, "_model_dirs", dirs)
    monkeypatch.setattr(S, "_outbound_client", forbidden)
    return S


def settings(monkeypatch, server, **over):
    """Config is frozen, so a variation is a new one rather than an assignment."""
    import dataclasses

    monkeypatch.setattr(server, "CFG", dataclasses.replace(server.CFG, **over))


def test_the_tool_refuses_a_host_that_is_not_on_the_list(offline, monkeypatch):
    from comfyui_mcp.client import ComfyError

    settings(monkeypatch, offline, download_allow_hosts="huggingface.co")
    with pytest.raises(ComfyError, match="example.com"):
        run(offline.download_model("https://example.com/m.safetensors", "vae", dry_run=True))


def test_the_tool_refuses_a_pickle_before_touching_the_network(offline, monkeypatch):
    from comfyui_mcp.client import ComfyError

    settings(monkeypatch, offline, download_allow_hosts="", download_allow_pickle=False)
    with pytest.raises(ComfyError, match="executes code"):
        run(offline.download_model("https://huggingface.co/m.ckpt", "vae", dry_run=True))


def test_an_allowed_pickle_gets_as_far_as_the_network(offline, monkeypatch):
    # The mirror of the two above: with both settings relaxed, the refusals are
    # out of the way and the very next thing is the request they were guarding.
    from comfyui_mcp.client import ComfyError

    settings(monkeypatch, offline, download_allow_hosts="", download_allow_pickle=True)
    with pytest.raises(AssertionError, match="before the checks"):
        run(offline.download_model("https://example.com/m.ckpt", "vae", dry_run=True))
