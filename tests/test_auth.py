"""Basic Auth for a ComfyUI reached through a reverse proxy.

The property that matters most is the one nobody asked for: with no credentials
set, everything is byte-for-byte what it was. Everyone on localhost is in that
case, so it is the first test here rather than an afterthought.
"""

from __future__ import annotations

import base64
import dataclasses
import json

import pytest

from comfyui_mcp import server as S
from comfyui_mcp.client import ComfyClient, auth_headers, basic_auth
from comfyui_mcp.config import auth_problem, load_config

USER, PASSWORD = "comfy", "s3cr3t"


def cfg(**over):
    return dataclasses.replace(load_config(), **over)


def run(coro):
    import asyncio

    return asyncio.run(coro)


def test_without_credentials_the_client_is_what_it_always_was():
    plain = cfg(http_user="", http_password="")
    assert basic_auth(plain) is None
    assert auth_headers(plain) == {}
    assert auth_problem(plain) == ""


def test_an_empty_value_is_the_same_as_an_absent_one():
    assert basic_auth(cfg(http_user="", http_password=PASSWORD)) is None


def test_credentials_reach_the_http_client():
    client = ComfyClient(cfg(http_user=USER, http_password=PASSWORD))
    http = run(client.http())
    try:
        assert http.auth is not None
    finally:
        run(client.aclose())


def test_the_websocket_carries_the_same_pair():
    """One source for both, or the socket 401s while every HTTP call succeeds."""
    header = auth_headers(cfg(http_user=USER, http_password=PASSWORD))["Authorization"]
    scheme, _, blob = header.partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(blob).decode() == f"{USER}:{PASSWORD}"


@pytest.mark.parametrize(
    "half,missing",
    [
        ({"http_user": USER, "http_password": ""}, "COMFYUI_PASSWORD"),
        ({"http_user": "", "http_password": PASSWORD}, "COMFYUI_USER"),
    ],
)
def test_half_a_pair_is_named_rather_than_left_to_a_401(half, missing):
    value = cfg(**half)
    assert basic_auth(value) is None
    assert missing in auth_problem(value)


def test_comfy_status_reports_that_auth_is_on_but_never_the_password(monkeypatch):
    async def dead():
        return False

    async def silent():
        return ""

    monkeypatch.setattr(S, "CFG", cfg(http_user=USER, http_password=PASSWORD))
    monkeypatch.setattr(S.CLIENT, "is_alive", dead)
    monkeypatch.setattr(S.CLIENT, "why_unreachable", silent)

    status = run(getattr(S.comfy_status, "fn", S.comfy_status)())
    assert status["auth"] == "basic"
    assert PASSWORD not in json.dumps(status)
