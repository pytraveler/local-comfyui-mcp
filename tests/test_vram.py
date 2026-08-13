"""Tests for unloading models before a switch that would run out of VRAM.

Measured on the dev machine, switching workflows with headroom to spare costs the
same with or without the unload (11.0s vs 11.0s), so freeing is gated on real
memory pressure - ComfyUI keeps models resident on purpose and that cache is what
makes a repeat run take seconds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfyui_mcp import graph as G
from comfyui_mcp import server as S

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
TOTAL_GB = 32.0


@pytest.fixture
def krea2() -> G.Graph:
    return json.loads((WORKFLOWS / "image_krea2.json").read_text(encoding="utf-8"))


@pytest.fixture
def other(krea2) -> G.Graph:
    graph = G.clone(krea2)
    G.apply_params(graph, {"model": "other_model.safetensors"})
    return graph


@pytest.fixture
def gpu(monkeypatch):
    """Record /free calls and let each test dictate how much VRAM is free."""
    state = {"free_gb": 1.0, "calls": []}

    async def fake_free(unload_models=True, free_memory=True):
        state["calls"].append({"unload_models": unload_models, "free_memory": free_memory})
        state["free_gb"] = TOTAL_GB

    async def fake_vram():
        return (state["free_gb"], TOTAL_GB)

    monkeypatch.setattr(S.CLIENT, "free", fake_free)
    monkeypatch.setattr(S, "_vram_gb", fake_vram)
    monkeypatch.setattr(S, "_LAST_MODEL_SIGNATURE", None)
    return state


def test_signature_lists_every_model_the_graph_loads(krea2):
    signature = S._model_signature(krea2)
    assert "krea2_turbo_bf16.safetensors" in signature
    assert "qwen_image_vae.safetensors" in signature
    assert signature == tuple(sorted(set(signature))), "must be sorted and deduped"


def test_signature_follows_a_changed_model_param(krea2, other):
    assert S._model_signature(other) != S._model_signature(krea2)


def test_signature_ignores_unrelated_params(krea2):
    before = S._model_signature(krea2)
    G.apply_params(krea2, {"prompt": "anything", "steps": 12, "seed": 5})
    assert S._model_signature(krea2) == before


@pytest.mark.asyncio
async def test_first_run_never_frees(krea2, gpu):
    assert await S._free_vram_if_starved(krea2, enabled=True) is None
    assert gpu["calls"] == []


@pytest.mark.asyncio
async def test_same_models_never_free_however_tight(krea2, gpu):
    gpu["free_gb"] = 0.1
    await S._free_vram_if_starved(krea2, enabled=True)
    assert await S._free_vram_if_starved(krea2, enabled=True) is None
    assert gpu["calls"] == []


@pytest.mark.asyncio
async def test_headroom_means_no_free_even_on_a_switch(krea2, other, gpu):
    """The measured case: plenty of VRAM, so unloading would only break the cache."""
    gpu["free_gb"] = TOTAL_GB * 0.9
    await S._free_vram_if_starved(krea2, enabled=True)
    assert await S._free_vram_if_starved(other, enabled=True) is None
    assert gpu["calls"] == []


@pytest.mark.asyncio
async def test_switch_under_pressure_frees_once(krea2, other, gpu):
    gpu["free_gb"] = TOTAL_GB * 0.05
    await S._free_vram_if_starved(krea2, enabled=True)
    report = await S._free_vram_if_starved(other, enabled=True)

    assert gpu["calls"] == [{"unload_models": True, "free_memory": True}]
    assert "5% of VRAM free" in report["reason"]
    assert report["vram_free_gb"]["after"] > report["vram_free_gb"]["before"]


@pytest.mark.asyncio
async def test_threshold_boundary_is_inclusive(krea2, other, gpu):
    """Exactly at the threshold counts as enough headroom."""
    gpu["free_gb"] = TOTAL_GB * S.CFG.free_vram_min_fraction
    await S._free_vram_if_starved(krea2, enabled=True)
    assert await S._free_vram_if_starved(other, enabled=True) is None


@pytest.mark.asyncio
async def test_disabled_still_tracks_the_signature(krea2, other, gpu):
    """Turning it off must not leave a stale baseline behind."""
    gpu["free_gb"] = TOTAL_GB * 0.05
    await S._free_vram_if_starved(krea2, enabled=False)
    await S._free_vram_if_starved(other, enabled=False)
    assert gpu["calls"] == []
    assert await S._free_vram_if_starved(krea2, enabled=True) is not None


@pytest.mark.asyncio
async def test_unreadable_vram_does_not_free(krea2, other, monkeypatch, gpu):
    async def no_stats():
        return (None, None)

    monkeypatch.setattr(S, "_vram_gb", no_stats)
    await S._free_vram_if_starved(krea2, enabled=True)
    assert await S._free_vram_if_starved(other, enabled=True) is None
    assert gpu["calls"] == []
