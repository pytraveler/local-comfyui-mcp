"""Tests for live progress tracking.

A caller that cannot see a run advancing assumes it has hung and interrupts it, which
costs more than waiting. These cover the two halves of the fix: turning ComfyUI's
WebSocket chatter into events, and keeping a readable record of them.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from comfyui_mcp import server as S
from comfyui_mcp.client import ComfyClient, ComfyError, RunEvent
from comfyui_mcp.config import load_config

PROMPT_ID = "abc-123"


def event(node: str, step: int = 0, steps: int = 0) -> RunEvent:
    return RunEvent(node=node, step=step, steps=steps)


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock the test drives by hand."""
    now = {"t": 1000.0}
    monkeypatch.setattr(S.time, "monotonic", lambda: now["t"])
    return now


@pytest.fixture
def record(clock) -> S.RunProgress:
    return S.RunProgress(workflow="wf", started=clock["t"], prompt_id=PROMPT_ID)


@pytest.fixture(autouse=True)
def clean_registry():
    S._PROGRESS.clear()
    yield
    S._PROGRESS.clear()


def test_uncounted_node_reports_work_without_a_bar(record):
    record.apply(event("98:190"), {"98:190": "Load Diffusion Model INT8"})
    out = record.to_dict()
    assert "bar" not in out and "percent" not in out
    assert out["node_title"] == "Load Diffusion Model INT8"
    assert "working" in out["status"]


def test_counted_steps_produce_a_bar(record):
    record.apply(event("98:12", 12, 48), {})
    out = record.to_dict()
    assert out["percent"] == 25
    assert out["bar"] == "#" * 6 + "-" * 18
    assert out["status"] == "step 12/48 - node (98:12)"


def test_silence_is_measured_from_the_last_event(record, clock):
    record.apply(event("98:12", 1, 48), {})
    clock["t"] += 7
    assert record.to_dict()["silent_for_s"] == 7.0


def test_eta_ignores_the_time_spent_before_the_first_step(record, clock):
    """Two minutes loading a model must not be averaged into the per-step rate."""
    clock["t"] += 120  # model loading, no steps
    record.apply(event("98:12", 1, 41), {})
    clock["t"] += 10  # ten seconds, ten steps
    record.apply(event("98:12", 11, 41), {})
    assert record.eta == 30.0  # 1 s/step over the 30 remaining, not 13 s/step


def test_a_new_node_restarts_the_counter(record, clock):
    record.apply(event("98:12", 48, 48), {})
    record.apply(event("98:13"), {"98:13": "VAE Decode"})
    assert (record.step, record.steps) == (0, 0)
    assert record.eta is None
    assert "bar" not in record.to_dict()


def test_a_second_sampler_pass_restarts_the_rate(record, clock):
    record.apply(event("s", 40, 40), {})
    clock["t"] += 5
    record.apply(event("s", 1, 40), {})  # same node, counter back to the start
    clock["t"] += 2
    record.apply(event("s", 3, 40), {})
    assert record.eta == 37.0  # 1 s/step from the restart, not from the old rate


def test_finished_runs_stop_reporting_a_stall(record, clock):
    record.apply(event("98:12", 48, 48), {})
    record.state, record.finished = "done", clock["t"]
    clock["t"] += 600
    out = record.to_dict()
    assert out["elapsed_s"] == 0.0
    assert "silent_for_s" not in out and "eta_s" not in out


def test_a_finished_run_does_not_still_call_itself_working(record, clock):
    record.apply(event("158", 0, 0), {"158": "Save Image"})
    clock["t"] += 36
    record.state, record.finished = "done", clock["t"]
    assert record.to_dict()["status"] == "done after 36s"


def test_eviction_drops_the_oldest_finished_run_and_keeps_the_live_one(clock):
    for i in range(S.PROGRESS_KEEP + 2):
        old = S.RunProgress(workflow=f"w{i}", started=clock["t"] + i, prompt_id=f"p{i}")
        old.state, old.finished = "done", clock["t"] + i
        S._remember(old)
    live = S.RunProgress(workflow="live", started=clock["t"] + 99, prompt_id="live")
    S._remember(live)
    assert len(S._PROGRESS) <= S.PROGRESS_KEEP
    assert "live" in S._PROGRESS
    assert "p0" not in S._PROGRESS


def test_latest_is_the_most_recently_started(clock):
    for i in range(3):
        S._remember(S.RunProgress(workflow=f"w{i}", started=clock["t"] + i, prompt_id=f"p{i}"))
    assert S._latest().prompt_id == "p2"


def test_settle_records_how_a_watched_run_ended(record):
    async def boom():
        raise RuntimeError("node 5 exploded")

    async def main():
        task = asyncio.ensure_future(boom())
        await asyncio.gather(task, return_exceptions=True)
        S._settle(record, task)

    asyncio.run(main())
    assert record.state == "error"
    assert "exploded" in record.error


def call(tool, *args, **kwargs):
    fn = getattr(tool, "fn", tool)
    result = fn(*args, **kwargs)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


def test_get_progress_defaults_to_the_latest_run(record):
    record.apply(event("98:12", 5, 48), {})
    S._remember(record)
    out = call(S.get_progress)
    assert out["prompt_id"] == PROMPT_ID
    assert out["step"] == 5
    assert "poll again" in out["hint"]


def test_get_progress_on_an_unknown_id_says_so(monkeypatch):
    async def not_alive(timeout: float = 3.0) -> bool:
        return False

    monkeypatch.setattr(S.CLIENT, "is_alive", not_alive)
    out = call(S.get_progress, "nope")
    assert out["state"] == "unknown"


def test_get_progress_falls_back_to_history_when_untracked(monkeypatch):
    async def alive(timeout: float = 3.0) -> bool:
        return True

    async def history(prompt_id: str):
        return {"status": {"status_str": "success"}, "outputs": {}}

    monkeypatch.setattr(S.CLIENT, "is_alive", alive)
    monkeypatch.setattr(S.CLIENT, "history", history)
    out = call(S.get_progress, "ran-before-a-restart")
    assert (out["state"], out["tracked"]) == ("done", False)


class FakeWS:
    """Replays canned ComfyUI messages, then blocks like a quiet socket."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages = [json.dumps(m) for m in messages]

    async def recv(self) -> str:
        if not self.messages:
            await asyncio.sleep(3600)
        return self.messages.pop(0)


def ws_events(messages: list[dict], monkeypatch) -> list[RunEvent]:
    client = ComfyClient(load_config())

    async def history(prompt_id: str):
        return {"status": {"status_str": "success"}, "outputs": {}}

    monkeypatch.setattr(client, "history", history)
    seen: list[RunEvent] = []

    async def hook(evt: RunEvent) -> None:
        seen.append(evt)

    asyncio.run(client._wait_on_ws(FakeWS(messages), PROMPT_ID, timeout=5, on_progress=hook))
    return seen


def test_node_changes_are_reported_even_without_steps(monkeypatch):
    """The model-loading phase emits no step counter - it must still show as work."""
    seen = ws_events(
        [
            {"type": "executing", "data": {"node": "98:190", "prompt_id": PROMPT_ID}},
            {"type": "execution_success", "data": {"prompt_id": PROMPT_ID}},
        ],
        monkeypatch,
    )
    assert seen == [RunEvent(node="98:190")]
    assert not seen[0].counted


def test_steps_are_reported_with_the_node_that_owns_them(monkeypatch):
    seen = ws_events(
        [
            {"type": "executing", "data": {"node": "37", "prompt_id": PROMPT_ID}},
            {"type": "progress", "data": {"value": 3, "max": 48, "node": "98:12", "prompt_id": PROMPT_ID}},
            {"type": "execution_success", "data": {"prompt_id": PROMPT_ID}},
        ],
        monkeypatch,
    )
    assert seen[-1] == RunEvent(node="98:12", step=3, steps=48)


def test_progress_for_another_prompt_is_ignored(monkeypatch):
    seen = ws_events(
        [
            {"type": "progress", "data": {"value": 9, "max": 20, "prompt_id": "someone-else"}},
            {"type": "execution_success", "data": {"prompt_id": PROMPT_ID}},
        ],
        monkeypatch,
    )
    assert seen == []


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload, self.status_code, self.text = payload, status_code, json.dumps(payload)

    def json(self) -> dict:
        return self.payload


def submit(payload: dict, status_code: int = 200) -> tuple[str, list[str]]:
    """Run ComfyClient.submit against a canned /prompt response."""
    client = ComfyClient(load_config())

    class FakeHTTP:
        async def post(self, url: str, **kwargs):
            return FakeResponse(payload, status_code)

    async def http():
        return FakeHTTP()

    client.http = http  # type: ignore[method-assign]
    skipped: list[str] = []
    return asyncio.run(client.submit({}, skipped.extend)), skipped


SKIPPED_OUTPUT = {
    "331": {
        "class_type": "PreviewImage",
        "errors": [{"message": "Required input is missing", "details": "images"}],
    }
}


def test_a_queued_run_is_not_abandoned_over_a_skipped_output():
    """ComfyUI answers 200 with an id when *some* output validates: it is running.

    Raising here would leave that run holding the GPU with nothing watching it.
    """
    prompt_id, skipped = submit({"prompt_id": PROMPT_ID, "node_errors": SKIPPED_OUTPUT})
    assert prompt_id == PROMPT_ID
    assert skipped == ["node 331 (PreviewImage): Required input is missing (images)"]


def test_a_clean_submission_reports_nothing_skipped():
    assert submit({"prompt_id": PROMPT_ID}) == (PROMPT_ID, [])


def test_no_prompt_id_means_nothing_was_queued():
    """Every output rejected - here the error is real and must surface."""
    with pytest.raises(ComfyError, match="queued nothing"):
        submit({"node_errors": SKIPPED_OUTPUT})


def test_an_http_error_still_raises():
    with pytest.raises(ComfyError, match="rejected the prompt"):
        submit({"error": {"message": "invalid prompt"}}, status_code=400)


@pytest.fixture(autouse=True)
def clean_downloads():
    S._DOWNLOADS.clear()
    S._DOWNLOAD_TASKS.clear()
    yield
    S._DOWNLOADS.clear()
    S._DOWNLOAD_TASKS.clear()


@pytest.fixture
def transfer(clock) -> S.DownloadProgress:
    return S.DownloadProgress(
        job_id="vae/model.safetensors",
        url="https://host/model.safetensors",
        folder="vae",
        filename="model.safetensors",
        destination="V:/models/vae/model.safetensors",
        started=clock["t"],
        total=1000,
    )


def test_a_transfer_reports_a_bar_and_a_readable_size(transfer):
    transfer.advance(250, 1000)
    out = transfer.to_dict()
    assert out["percent"] == 25.0
    assert out["bar"] == "#" * 6 + "-" * 18
    assert out["status"] == "250 B of 1000 B"


def test_a_size_the_server_never_gave_still_reports_what_arrived(clock):
    record = S.DownloadProgress(
        job_id="vae/x", url="u", folder="vae", filename="x", destination="d", started=clock["t"]
    )
    record.advance(4096, None)
    out = record.to_dict()
    assert "percent" not in out and "bar" not in out
    assert out["status"] == "4.00 KB so far"


def test_silence_is_measured_from_the_last_byte(transfer, clock):
    transfer.advance(100, 1000)
    clock["t"] += 12
    assert transfer.to_dict()["silent_for_s"] == 12.0


def test_speed_ignores_the_bytes_that_were_already_on_disk(transfer, clock):
    """A resumed file must not report its head start as instant throughput."""
    transfer.advance(400, 1000)  # first callback of this attempt, 400 already there
    clock["t"] += 2
    transfer.advance(600, 1000)  # 200 bytes in 2 seconds
    assert transfer.speed == 100.0
    assert transfer.eta == 4.0  # the 400 left at 100 B/s


def test_a_finished_transfer_stops_reporting_a_stall(transfer, clock):
    transfer.advance(1000, 1000)
    transfer.state = "done"
    transfer.finished = clock["t"]
    out = transfer.to_dict()
    assert "silent_for_s" not in out and "eta_s" not in out
    assert out["status"].startswith("done after")


def test_a_resumed_transfer_says_where_it_picked_up(transfer):
    transfer.resumed_from = 400
    assert transfer.to_dict()["resumed_from_bytes"] == 400


def test_a_job_is_found_by_its_bare_file_name(transfer):
    S._remember_download(transfer)
    assert S._find_download("model.safetensors") is transfer
    assert S._find_download("vae/model.safetensors") is transfer
    assert S._find_download("") is transfer


def test_an_ambiguous_bare_name_is_not_guessed_at(clock):
    for folder in ("vae", "loras"):
        S._remember_download(
            S.DownloadProgress(
                job_id=f"{folder}/x.safetensors",
                url="u",
                folder=folder,
                filename="x.safetensors",
                destination="d",
                started=clock["t"],
            )
        )
    assert S._find_download("x.safetensors") is None


def test_only_finished_transfers_are_forgotten(clock):
    for n in range(S.DOWNLOAD_KEEP + 3):
        record = S.DownloadProgress(
            job_id=f"vae/{n}.safetensors",
            url="u",
            folder="vae",
            filename=f"{n}.safetensors",
            destination="d",
            started=clock["t"] + n,
        )
        if n % 2:
            record.state, record.finished = "done", clock["t"] + n
        S._remember_download(record)
    assert len(S._DOWNLOADS) == S.DOWNLOAD_KEEP
    assert all(r.state == "running" for r in S._DOWNLOADS.values() if r.job_id.endswith("0.safetensors"))
