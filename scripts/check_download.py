r"""Check where a model would be downloaded to, without downloading anything.

Two halves fail in different places and only one of them is about the network.
Where a file lands is decided by the running ComfyUI - `extra_model_paths.yaml`
maps folders in from anywhere and `is_default: true` puts them first - and a
registered directory need not exist. ComfyUI's own shipped example contains

    text_encoders: |
         models/text_encoders/
         models/clip/  # legacy location still supported

where the trailing text is part of the path, because `#` inside a `|` block
scalar is not a comment. So this reports every registered directory and marks
the missing ones, which is the only way to see that before a 5 GB transfer
finds it out.

    .\uv.exe run python scripts\check_download.py
    .\uv.exe run python scripts\check_download.py --url <link> --folder vae

With a URL it also does the preflight: redirects are walked, size and checksum
read from the headers, free space compared. Still nothing is fetched.

Needs a running ComfyUI. No browser and no bridge node.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from comfyui_mcp import download as D  # noqa: E402
from comfyui_mcp.client import ComfyClient  # noqa: E402
from comfyui_mcp.config import load_config  # noqa: E402

CFG = load_config()

INTERESTING = ("checkpoints", "diffusion_models", "vae", "text_encoders", "loras", "clip_vision")


def say(ok: bool, text: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {text}")


async def report_folders(client: ComfyClient) -> int:
    known = await client.model_directories()
    print(f"\nComfyUI registers {len(known)} model folders.\n")
    missing = 0
    for name in INTERESTING:
        entry = known.get(name)
        if entry is None:
            say(False, f"{name}: not registered")
            continue
        chosen = ""
        try:
            chosen = str(D.choose_destination(name, entry["folders"], [], "probe.safetensors").directory)
        except D.DownloadError as exc:
            say(False, f"{name}: {exc}")
        print(f"  {name}")
        for path in entry["folders"]:
            exists = Path(path).is_dir()
            missing += not exists
            mark = "->" if path == chosen else ("  " if exists else "!!")
            print(f"    {mark} {path}{'' if exists else '   (does not exist)'}")
        if entry["extensions"]:
            print(f"       extensions: {', '.join(entry['extensions'])}")
    if missing:
        print(
            f"\n  {missing} registered directories do not exist. That is normal - a download "
            "\n  picks the first one that does, and says so when it had to pass one over."
        )
    return 0


async def report_url(client: ComfyClient, url: str, folder: str) -> int:
    known = await client.model_directories()
    entry = known.get(folder)
    if entry is None:
        say(False, f"ComfyUI has no model folder {folder!r}")
        return 1
    name = D.filename_from_url(url)
    if not name:
        say(False, f"no file name in {url}")
        return 1

    try:
        dest = D.choose_destination(folder, entry["folders"], entry["extensions"], name)
    except D.DownloadError as exc:
        say(False, str(exc))
        return 1
    say(True, f"destination: {dest.path}")
    if dest.note:
        say(True, f"note: {dest.note}")

    timeout = httpx.Timeout(connect=30.0, read=CFG.download_timeout, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
        try:
            plan = await D.preflight(http, url, CFG.download_token)
        except D.DownloadError as exc:
            say(False, str(exc))
            return 1
    say(True, f"hops: {' -> '.join(plan.hosts)}")
    say(True, f"size: {D.human_size(plan.size) if plan.size else 'not declared'}")
    say(plan.resumable, f"resumable: {plan.resumable}")
    say(True, f"sha256: {plan.sha256 or 'not declared'}")

    free = D.free_space(dest.directory)
    try:
        D.check_space(dest.directory, plan.size)
        say(True, f"free space: {D.human_size(free)}")
    except D.DownloadError as exc:
        say(False, str(exc))
        return 1
    if dest.path.is_file():
        say(True, f"already present ({D.human_size(dest.path.stat().st_size)}) - would be skipped")
    part = dest.path.with_name(dest.path.name + ".part")
    if part.is_file():
        say(True, f"a part file is there; would resume from {D.human_size(part.stat().st_size)}")
    print("\n  Nothing was fetched.")
    return 0


async def main_async(args: argparse.Namespace) -> int:
    client = ComfyClient(CFG)
    try:
        if not await client.is_alive():
            say(False, f"ComfyUI is not answering on {CFG.base_url}")
            return 1
        if args.url:
            return await report_url(client, args.url, args.folder)
        return await report_folders(client)
    finally:
        await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="", help="a direct link to preflight")
    parser.add_argument("--folder", default="vae", help="model folder the link belongs in")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
