"""The Comfy Registry over HTTP: searching for a node pack, and reading one entry.

Impure by definition - the same side of the line `client.py` is on, with the pure
half (`Listing`, `parse_listing`, `parse_search`) in `env.py` where it can be tested
against a recorded payload rather than against whoever is deploying api.comfy.org
today.

**This is the only part of the extension concept that can answer before anything is
cloned**, which is why it comes first. A registry entry carries the pack's own
`latest_version.dependencies`, so the question "what would installing this move" is
answerable while the pack is still a row in somebody else's database - `plan_packages`
takes exactly that list. Cloning a repository to find out what it wants is the thing
this ordering exists to avoid.

Two things about the API decided the shape, both measured rather than read:

- **It needs no key and no account.** `GET /nodes/search?search=&limit=&page=` and
  `GET /nodes/{id}` both answer 200 to an anonymous request. So there is no credential
  to store and none of `download_model`'s token machinery applies.
- **A miss and an error are different HTTP results.** A search with no hits is 200 with
  `total: 0`; an unknown id is 404 with `{error, message}`. Reporting the second as an
  empty result would read as "no such pack" when the truth may be a typo in a URL or a
  registry that moved, so `fetch` says which it got.

`COMFYUI_REGISTRY_URL` exists for a mirror or a proxy, not as an off switch - the
switch is the `extensions` tool group, and it is the one that means the same thing to
every client.
"""

from __future__ import annotations

import httpx

from . import env as E
from .config import Config
from .client import ComfyError

SEARCH_LIMIT = 100


def base_url(cfg: Config) -> str:
    return (cfg.registry_url or E.REGISTRY_URL).rstrip("/")


async def _get(cfg: Config, path: str, params: dict) -> httpx.Response:
    """One anonymous GET, with a network failure worded as a network failure.

    The distinction `_resolve_failure` draws for uv, drawn here for the same reason: a
    registry that cannot be reached says nothing whatever about the pack being asked
    for, and reporting it as "not found" sends somebody to check a spelling that was
    right all along.
    """
    url = f"{base_url(cfg)}{path}"
    try:
        async with httpx.AsyncClient(timeout=cfg.registry_timeout) as client:
            return await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise ComfyError(
            f"could not reach the Comfy Registry at {url}: {exc}. That is a network "
            "problem rather than an answer about the pack - the registry is a separate "
            "host from ComfyUI and from PyPI, so this can fail while both of those work. "
            "COMFYUI_REGISTRY_URL points it somewhere else."
        ) from exc


async def search(cfg: Config, query: str, limit: int, page: int = 1) -> tuple[list[E.Listing], dict]:
    """Packs matching `query`, and where the page sits in the whole result."""
    response = await _get(
        cfg,
        "/nodes/search",
        {"search": query, "limit": max(1, min(limit, SEARCH_LIMIT)), "page": max(1, page)},
    )
    if response.status_code != 200:
        raise ComfyError(
            f"the Comfy Registry answered {response.status_code} to a search for {query!r}. "
            "Nothing is wrong with the query on this side; that is the registry's own state."
        )
    return E.parse_search(_json(response))


async def fetch(cfg: Config, node_id: str) -> E.Listing | None:
    """One pack by its registry id, or None when the registry does not know it.

    None is a real answer here and not a failure: plenty of installed packs were
    cloned straight from GitHub and were never published, so "the registry has never
    heard of this" is the ordinary case for a third of what is on this machine.
    """
    response = await _get(cfg, f"/nodes/{node_id}", {})
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise ComfyError(
            f"the Comfy Registry answered {response.status_code} for {node_id!r}."
        )
    return E.parse_listing(_json(response))


def _json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
