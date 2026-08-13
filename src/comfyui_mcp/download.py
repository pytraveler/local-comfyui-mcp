r"""Fetching a model file into the folder ComfyUI will actually look in.

A workflow's "Model Links" note names the files it needs and the folder each one
belongs in; this is the half that brings them down. Two facts shape the module.

**Where a file goes cannot be worked out from disk.** `extra_model_paths.yaml`
maps folders in from anywhere and `is_default: true` puts them *first*, so
`ComfyUI/models/vae` is routinely not the answer - measured on the install this
was written against, the first `checkpoints` folder is on another drive
entirely. The list has to come from the running ComfyUI, and a model written
anywhere else is invisible to it however correct the bytes are.

**A registered folder need not exist.** 25 of 86 were missing here, and one of
them is worse than missing: ComfyUI's own shipped `extra_model_paths.yaml`
carries

    text_encoders: |
         models/text_encoders/
         models/clip/  # legacy location still supported

and inside a `|` block scalar `#` is not a comment, so the trailing text is part
of the path. Taking `folders[0]` would create a directory named
``clip\  # legacy location still supported`` and drop a 5 GB model into it.
Hence `choose_destination` picks the first folder that **exists** and refuses
rather than inventing one - a refusal costs a round trip, a wrong directory
costs the download twice and looks like the model was never fetched.

The transfer itself is ordinary except for two things worth keeping:

- **The token never goes past the first host.** A Hub URL answers 302 with a
  pre-signed CDN link; an `Authorization` header on that hop is a 400. So
  redirects are walked by hand and credentials are attached only to the origin.
- **A signed link expires.** Every retry re-walks from the original URL rather
  than reusing the resolved one, or a resume an hour later fetches an error page.

Three things are refused before the transfer starts, and all three are refused on
a `dry_run` too - a plan that passes and a fetch that then refuses would be worse
than either. The host must be on the allow-list, because a URL routinely arrives
from somebody else's workflow rather than from the user; the format must not be a
pickle, because loading one runs code; and the size must be under the ceiling, if
one is set. See `check_host`, `check_format` and `check_size`.

Nothing here verifies a checksum. The size is the completion test; `sha256` is
reported so a caller can check if it wants one, because hashing several
gigabytes costs minutes of disk and turns a benign mismatch into a lost download.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlparse

import httpx

USER_AGENT = "comfyui-mcp (+httpx)"
MAX_HOPS = 10
CHUNK = 1 << 20  # 1 MiB - small enough for responsive progress, large enough to be cheap
BACKOFF_CAP = 30.0
REDIRECTS = (301, 302, 303, 307, 308)
RETRY_STATUS = (408, 429, 500, 502, 503, 504)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SPACE_MARGIN = 1.02


class DownloadError(Exception):
    """The transfer cannot proceed. Retrying the same call will not help."""


@dataclass(frozen=True)
class Destination:
    """The resolved answer to "where does this file go"."""

    folder: str
    directory: Path
    path: Path
    considered: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "folder": self.folder,
            "directory": str(self.directory),
            "path": str(self.path),
        }
        if self.note:
            out["note"] = self.note
        return out


def _unsafe(filename: str) -> str:
    """Why this name may not be joined onto a model folder, or ''."""
    name = filename.strip()
    if not name:
        return "the file name is empty"
    if name[0] in "/\\" or ":" in name:
        return f"{filename!r} is an absolute path; pass a name relative to the model folder"
    parts = [p for p in re.split(r"[/\\]", name) if p]
    if any(p == ".." for p in parts):
        return f"{filename!r} climbs out of the model folder"
    if not parts or parts[-1] in (".", ""):
        return f"{filename!r} does not name a file"
    return ""


def choose_destination(
    folder: str,
    folders: list[str],
    extensions: list[str],
    filename: str,
    directory: str = "",
) -> Destination:
    """Pick the directory a model should be written to.

    `folders` and `extensions` are ComfyUI's own answer for this folder name, in
    its own order - `is_default` has already put the preferred one first.
    """
    problem = _unsafe(filename)
    if problem:
        raise DownloadError(problem)

    if not folders:
        raise DownloadError(
            f"ComfyUI registers no directory for the model folder '{folder}'. "
            "Call list_models() with no arguments for the folder names it knows."
        )

    suffix = Path(filename).suffix.lower()
    allowed = {e.lower() for e in extensions}
    if allowed and suffix not in allowed:
        raise DownloadError(
            f"'{folder}' holds {', '.join(sorted(allowed))} and {filename!r} is {suffix or 'extensionless'}. "
            "ComfyUI lists files by extension, so this one would be invisible to it."
        )

    considered = list(folders)
    note = ""
    if directory:
        chosen = Path(directory)
        if not any(Path(cand) == chosen for cand in folders):
            raise DownloadError(
                f"{directory} is not one of ComfyUI's directories for '{folder}', so a model "
                f"written there would never be found. Registered: {'; '.join(folders)}"
            )
    else:
        existing = [Path(cand) for cand in folders if Path(cand).is_dir()]
        if not existing:
            raise DownloadError(
                f"none of ComfyUI's directories for '{folder}' exist yet: {'; '.join(folders)}. "
                "Create the one you want and pass it as `directory`."
            )
        named = [d for d in existing if d.name == folder]
        chosen = named[0] if named else existing[0]
        if Path(folders[0]) != chosen:
            missing_first = not Path(folders[0]).is_dir()
            note = (
                f"first registered directory does not exist, using {chosen}"
                if missing_first
                else f"'{folder}' also reaches {folders[0]}; using the directory of that name"
            )

    path = (chosen / filename).resolve()
    if not str(path).startswith(str(chosen.resolve())):
        raise DownloadError(f"{filename!r} would land outside {chosen}")
    return Destination(folder=folder, directory=chosen, path=path, considered=considered, note=note)


def free_space(directory: Path) -> int:
    """Bytes free on the volume holding `directory`, or its nearest existing parent."""
    probe = directory
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def check_space(directory: Path, needed: int | None) -> None:
    """Refuse before starting rather than filling the volume and failing at 99%."""
    if not needed:
        return
    free = free_space(directory)
    if free < needed * SPACE_MARGIN:
        raise DownloadError(
            f"{human_size(needed)} needed on {directory.anchor or directory}, {human_size(free)} free"
        )


def human_size(size: float) -> str:
    for unit, scale in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= scale:
            return f"{size / scale:.2f} {unit}"
    return f"{int(size)} B"


PICKLE_SUFFIXES = frozenset({".ckpt", ".pt", ".pt2", ".pth", ".bin", ".pkl"})


def hosts_from(spec: str) -> tuple[str, ...]:
    """Parse a comma-separated allow-list. Empty means no restriction."""
    return tuple(
        part.strip().lower().lstrip("*.")
        for part in spec.split(",")
        if part.strip()
    )


def host_allowed(host: str, allowed: Iterable[str]) -> bool:
    """Whether `host` is one of `allowed`, or a subdomain of one.

    Subdomains count so a single `huggingface.co` covers the hosts the Hub
    actually serves from; an empty list allows everything, which is the opt-out.
    """
    entries = tuple(allowed)
    if not entries:
        return True
    host = (host or "").lower()
    return any(host == entry or host.endswith("." + entry) for entry in entries)


def check_host(url: str, allowed: Iterable[str]) -> None:
    """Refuse a URL before a single request goes out.

    First check of the three, and the only one that runs before the network,
    because this is the one guarding against a URL that was never the user's idea
    - a link read out of somebody else's workflow, which reaches the caller as
    ordinary text and can say anything.
    """
    entries = tuple(allowed)
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise DownloadError(f"not a URL: {url!r}")
    if not host_allowed(host, entries):
        raise DownloadError(
            f"{host} is not in COMFYUI_DOWNLOAD_ALLOW_HOSTS ({', '.join(entries)}). "
            "Add it there if this link is one you chose yourself - a URL that came out "
            "of a workflow's note or a node property is not."
        )


def check_format(filename: str, allow_pickle: bool) -> None:
    """Refuse a format that runs code when ComfyUI loads it.

    A `.ckpt` is a zipped pickle: loading one executes what is inside it. Recent
    ComfyUI reads weights with `weights_only=True`, but plenty of custom nodes
    call `torch.load` themselves, so the mitigation is not one to rely on.
    """
    if allow_pickle:
        return
    suffix = Path(filename).suffix.lower()
    if suffix in PICKLE_SUFFIXES:
        raise DownloadError(
            f"{suffix} is a pickle format - loading the file executes code stored inside it. "
            "Prefer the .safetensors or .gguf build of the same model. If there is none and "
            "you trust the source, set COMFYUI_DOWNLOAD_ALLOW_PICKLE=true in .env."
        )


def check_size(size: int | None, max_bytes: int) -> None:
    """Refuse a file over the ceiling, when there is one. 0 means no ceiling."""
    if not max_bytes or not size:
        return
    if size > max_bytes:
        raise DownloadError(
            f"{human_size(size)} is over the {human_size(max_bytes)} ceiling set by "
            "COMFYUI_DOWNLOAD_MAX_GB. Raise it there, or set it to 0 for no ceiling."
        )


@dataclass
class Preflight:
    url: str
    final_url: str
    hosts: list[str]
    size: int | None = None
    sha256: str = ""
    filename: str = ""
    resumable: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"url": self.url, "hosts": self.hosts, "resumable": self.resumable}
        if self.size:
            out["size_bytes"] = self.size
            out["size"] = human_size(self.size)
        if self.sha256:
            out["sha256"] = self.sha256
        return out


def filename_from_url(url: str) -> str:
    """The name a browser would save the URL under."""
    tail = unquote(urlparse(url).path).rsplit("/", 1)[-1]
    return tail if not _unsafe(tail) else ""


def _headers(url: str, origin: str, token: str) -> dict[str, str]:
    head = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if token and (urlparse(url).hostname or "") == origin:
        head["Authorization"] = f"Bearer {token}"
    return head


def _sha256_of(resp: httpx.Response) -> str:
    """The file's sha256 when the server volunteers one. Reported, never enforced."""
    for key in ("x-linked-etag", "etag"):
        raw = (resp.headers.get(key) or "").strip('"W/ ')
        if HEX64.match(raw.lower()):
            return raw.lower()
    return ""


def _size_of(resp: httpx.Response) -> int | None:
    raw = resp.headers.get("x-linked-size")
    if raw and raw.isdigit():
        return int(raw)
    span = (resp.headers.get("content-range") or "").rsplit("/", 1)
    if len(span) == 2 and span[1].strip().isdigit():
        return int(span[1].strip())
    raw = resp.headers.get("content-length")
    return int(raw) if raw and raw.isdigit() and resp.status_code == 200 else None


async def _hop(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> httpx.Response:
    """One request that reads headers only, whatever the server allows."""
    resp = await client.request("HEAD", url, headers=headers, follow_redirects=False)
    if resp.status_code in (403, 405, 501):
        ranged = dict(headers, Range="bytes=0-0")
        resp = await client.request("GET", url, headers=ranged, follow_redirects=False)
        await resp.aclose()
    return resp


async def preflight(client: httpx.AsyncClient, url: str, token: str = "") -> Preflight:
    """Walk the redirects by hand and report what is on the other end."""
    origin = urlparse(url).hostname or ""
    if not origin:
        raise DownloadError(f"not a URL: {url!r}")
    if urlparse(url).scheme not in ("http", "https"):
        raise DownloadError(f"only http(s) URLs can be fetched, got {url!r}")

    current, hosts = url, []
    size: int | None = None
    sha = ""
    for _ in range(MAX_HOPS):
        hosts.append(urlparse(current).hostname or "")
        resp = await _hop(client, current, _headers(current, origin, token))
        size = size or _size_of(resp)
        sha = sha or _sha256_of(resp)
        if resp.status_code in REDIRECTS and resp.headers.get("location"):
            current = str(httpx.URL(current).join(resp.headers["location"]))
            continue
        if resp.status_code >= 400:
            raise DownloadError(_explain(resp.status_code, current))
        return Preflight(
            url=url,
            final_url=current,
            hosts=hosts,
            size=size,
            sha256=sha,
            filename=filename_from_url(url),
            resumable=(resp.headers.get("accept-ranges") or "").lower() == "bytes"
            or resp.status_code == 206,
        )
    raise DownloadError(f"more than {MAX_HOPS} redirects starting at {url}")


def _explain(status: int, url: str) -> str:
    if status in (401, 403):
        return (
            f"HTTP {status} for {url} - the file is private or gated. Accept its licence on the "
            "site and put a token in COMFYUI_DOWNLOAD_TOKEN in .env (a shell variable does not "
            "reach this server)."
        )
    if status == 404:
        return f"HTTP 404 for {url} - no such file. Check the link in the workflow's note."
    return f"HTTP {status} for {url}"


@dataclass
class Transfer:
    """The outcome of a completed fetch."""

    path: Path
    size: int
    resumed_from: int
    attempts: int
    final_url: str
    sha256: str = ""


ProgressFn = Callable[[int, int | None], None]


def _pause(attempt: int) -> float:
    """Backoff between attempts. A named function so tests need not wait it out."""
    return min(2.0**attempt, BACKOFF_CAP)


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    destination: Path,
    *,
    token: str = "",
    size: int | None = None,
    retries: int = 5,
    on_progress: ProgressFn | None = None,
    chunk: int = CHUNK,
) -> Transfer:
    """Download `url` to `destination`, resuming a previous attempt if one is there.

    The bytes go to `<destination>.part` and are moved into place only once the
    whole file is present, so ComfyUI never sees a half-written model: `.part` is
    not a model extension, and the rename is atomic within the directory.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    started_at = part.stat().st_size if part.is_file() else 0
    last: Exception | None = None

    for attempt in range(1, max(1, retries) + 1):
        have = part.stat().st_size if part.is_file() else 0
        if size is not None and have >= size:
            have = 0  
        try:
            resolved, written, sha = await _attempt(
                client, url, part, have, size, token, chunk, on_progress
            )
            if size is not None and written != size:
                raise DownloadError(f"expected {size} bytes, got {written}")
            os.replace(part, destination)
            return Transfer(
                path=destination,
                size=written,
                resumed_from=started_at,
                attempts=attempt,
                final_url=resolved,
                sha256=sha,
            )
        except DownloadError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            last = exc
            if attempt >= max(1, retries):
                break
            await asyncio.sleep(_pause(attempt))

    raise DownloadError(f"gave up after {max(1, retries)} attempts: {last}")


async def _attempt(
    client: httpx.AsyncClient,
    url: str,
    part: Path,
    have: int,
    size: int | None,
    token: str,
    chunk: int,
    on_progress: ProgressFn | None,
) -> tuple[str, int, str]:
    plan = await preflight(client, url, token)
    origin = urlparse(url).hostname or ""
    headers = _headers(plan.final_url, origin, token)
    if have:
        headers["Range"] = f"bytes={have}-"

    async with client.stream(
        "GET", plan.final_url, headers=headers, follow_redirects=False
    ) as resp:
        if resp.status_code >= 400:
            if resp.status_code in RETRY_STATUS:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
            raise DownloadError(_explain(resp.status_code, plan.final_url))
        if have and resp.status_code != 206:
            have = 0  
        total = size or plan.size or _total_from(resp, have)

        done = have
        with open(part, "r+b" if have else "wb") as fh:
            if have:
                fh.seek(have)
            async for block in resp.aiter_bytes(chunk):
                fh.write(block)
                done += len(block)
                if on_progress is not None:
                    on_progress(done, total)
    return plan.final_url, done, plan.sha256


def _total_from(resp: httpx.Response, have: int) -> int | None:
    """Full size from a partial response: content-length is only what is left."""
    span = resp.headers.get("content-range") or ""
    if "/" in span:
        tail = span.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    length = resp.headers.get("content-length")
    return have + int(length) if length and length.isdigit() else None
