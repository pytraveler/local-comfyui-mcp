"""Running uv against ComfyUI's own interpreter, and the checkpoints that undo it.

`env.py` decides things; this runs them. The split is the one `graph.py` / `server.py`
and `logs.py` / `client.py` already draw, and it is what lets every rule in the other
module be tested without a ComfyUI, a network or a 286-package environment.

**A portable ComfyUI has no virtual environment to activate.** `python_embeded` is an
unpacked CPython with a `._pth` file beside it, so nothing can be "entered" - every tool
has to be handed the interpreter explicitly. uv takes `--python <path>` and is happy
with that, which is why the whole layer goes through uv rather than through the embedded
pip. It is also already here: this project vendors `uv.exe` at its root and runs itself
under it.

Two refusals in this module are the point of it existing.

**The interpreter is confirmed against the running ComfyUI, never merely found.** A
portable root can hold more than one python - the build's own, a leftover venv, a
`update/` folder with another - and packages written into the wrong one are a silent
no-op: the install succeeds, the import still fails, and the caller concludes the
package was the wrong one. `/system_stats` reports `sys.version` of the process that is
actually serving, and `env.python_matches` compares the two whole.

**Nothing here downloads uv.** `install.bat` fetches it as step one and
`configure_comfy` can say where it is; a tool call that quietly pulls an executable off
the internet to satisfy itself is the shape of thing this whole concept exists to stop.
Absent, it refuses and says which two commands produce one.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import env as E
from .config import PROJECT_ROOT, Config

CHECKPOINT_FORMAT = 1
STAMP = "%Y-%m-%d_%H-%M-%S"

MANIFEST = "manifest.json"
PACKAGES_FILE = "packages.txt"
PACKS_FILE = "custom_nodes.json"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

NOT_BACKED_UP = (
    "models, inputs, outputs and the contents of custom_nodes are not copied - they are "
    "the user's own and run to hundreds of gigabytes. A checkpoint records what was "
    "installed, not the files themselves."
)


class PackageError(RuntimeError):
    """Something about the environment stops the operation. The message says what."""


def find_uv(cfg: Config) -> Path:
    """The uv this server drives ComfyUI's environment with.

    The vendored copy first: it is the one `install.bat` fetched, the one this server
    is itself running under, and the one whose version the project has been tested
    against. `shutil.which` afterwards for a checkout that was installed some other way.
    """
    if cfg.uv_exe.strip():
        path = Path(cfg.uv_exe.strip()).expanduser()
        if not path.is_file():
            raise PackageError(f"COMFYUI_UV points at a missing file: {path}")
        return path

    name = "uv.exe" if sys.platform == "win32" else "uv"
    vendored = PROJECT_ROOT / name
    if vendored.is_file():
        return vendored

    found = shutil.which("uv")
    if found:
        return Path(found)

    raise PackageError(
        f"uv was not found. It is normally at {vendored}, put there by install.bat / "
        "install.sh. Run the installer, or set COMFYUI_UV to a uv executable. Nothing "
        "here downloads one on its own."
    )


def python_candidates(cfg: Config) -> list[Path]:
    """Every interpreter that could plausibly be the one ComfyUI runs on, best first.

    Order is by how a ComfyUI is actually installed rather than by preference: a
    portable Windows build is `python_embeded` beside the ComfyUI folder and nothing
    else, a manual install is a venv either beside `main.py` or one level up. The list
    is a list rather than a first-hit because the wrong one still runs and still
    installs, so the caller is told what else was in the running when the confirmation
    fails.
    """
    win = sys.platform == "win32"
    leaf = ("Scripts", "python.exe") if win else ("bin", "python")
    out: list[Path] = []

    embedded = cfg.comfy_root / "python_embeded" / ("python.exe" if win else "bin/python")
    out.append(embedded)

    for base in (cfg.comfy_root, cfg.comfy_dir):
        for venv in ("venv", ".venv"):
            out.append(base.joinpath(venv, *leaf))

    if not win:
        out.append(cfg.comfy_root / "python_embeded" / "python")

    seen: list[Path] = []
    for path in out:
        if path not in seen:
            seen.append(path)
    return [p for p in seen if p.is_file()]


def find_python(cfg: Config) -> tuple[Path, str]:
    """(interpreter, how it was chosen). Raises when there is nothing to choose from.

    `COMFYUI_PYTHON` wins and is not checked for being inside `COMFYUI_ROOT`, unlike
    `COMFYUI_LAUNCH_SCRIPT`. The two look alike and are not: a launch script outside the
    install is a program nobody named, while an interpreter outside it is an ordinary
    layout - a venv in `~/.virtualenvs`, a conda environment, a system python running a
    git checkout. The guard that matters here is the version confirmation below, which
    is stronger than a path prefix because it asks the running process rather than the
    filesystem.
    """
    if cfg.python_exe.strip():
        path = Path(cfg.python_exe.strip()).expanduser()
        if not path.is_file():
            raise PackageError(f"COMFYUI_PYTHON points at a missing file: {path}")
        return path, "COMFYUI_PYTHON"

    found = python_candidates(cfg)
    if not found:
        raise PackageError(
            f"no Python interpreter found under {cfg.comfy_root}. Looked for "
            "python_embeded/, venv/ and .venv/. Set COMFYUI_PYTHON to the interpreter "
            "ComfyUI runs on - `comfy_status` reports the version it should match."
        )
    return found[0], "found under COMFYUI_ROOT"


def child_env() -> dict[str, str]:
    """The environment a uv child gets.

    `PYTHONIOENCODING`/`PYTHONUTF8` because the answer is parsed and a Russian Windows
    console is cp866, which cannot encode half of what uv prints. `UV_NO_PROGRESS`
    because a progress bar written for a terminal is noise in a captured stream, and
    the download progress a caller needs is reported separately.
    """
    out = dict(os.environ)
    out["PYTHONIOENCODING"] = "utf-8"
    out["PYTHONUTF8"] = "1"
    out["UV_NO_PROGRESS"] = "1"
    out.setdefault("UV_HTTP_TIMEOUT", "300")
    return out


@dataclass
class Run:
    """One finished child process."""

    code: int
    out: str
    command: tuple[str, ...] = ()
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.code == 0


async def run(argv: list[str | Path], timeout: float) -> Run:
    """Run a command, capture everything it said, return it. Never raises on exit code.

    stderr is merged into stdout on purpose: uv writes its plan to stderr and its
    listings to stdout, and a caller reading a plan does not want to know which stream
    a line arrived on. Both are parsed together by `env.parse_plan`.
    """
    cmd = [str(part) for part in argv]
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=child_env(),
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        raise PackageError(f"could not run {cmd[0]}: {exc}") from exc

    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise PackageError(
            f"{cmd[0]} did not finish within {timeout:.0f}s: {' '.join(cmd[:4])}. "
            "Raise COMFYUI_PACKAGE_TIMEOUT if this is an install that genuinely takes "
            "that long; a resolve that hangs is usually an unreachable index."
        ) from None

    return Run(
        code=proc.returncode or 0,
        out=(raw or b"").decode("utf-8", errors="replace"),
        command=tuple(cmd),
        seconds=time.monotonic() - started,
    )


_UNREACHABLE = (
    "failed to fetch",
    "request failed after",
    "operation timed out",
    "error sending request",
    "connection refused",
    "dns error",
    "temporary failure in name resolution",
    "certificate",
)


def _resolve_failure(output: str) -> str:
    """Turn uv's failure into the sentence naming what to do about it."""
    text = output.strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _UNREACHABLE):
        return (
            "uv could not reach a package index, so nothing is known about this plan - "
            "this is a network answer, not a verdict on the requirements. Check the "
            "connection, a proxy, or whether download.pytorch.org is blocked; the plan "
            f"needs it whenever this install carries a +cuXXX build.\n{text[:2000]}"
        )
    return f"uv could not resolve that:\n{text[:2000]}"


class Uv:
    """uv, pointed at one interpreter. Everything that touches packages goes through here."""

    def __init__(
        self,
        uv: Path,
        python: Path,
        timeout: float,
        index_url: str = "",
        default_index: str = "",
    ) -> None:
        self.uv = uv
        self.python = python
        self.timeout = timeout
        self.index_url = index_url
        self.default_index = default_index.strip()

    def _base(self, *verb: str) -> list[str | Path]:
        """`--no-config` so a `uv.toml` anywhere above the install cannot redirect an
        install this server is reporting on, and `--default-index` only when somebody
        has set one."""
        argv: list[str | Path] = [self.uv, "pip", *verb, "--no-config", "--python", self.python]
        if self.default_index:
            argv += ["--default-index", self.default_index]
        return argv

    def _index(self) -> list[str]:
        """The extra index a `+cuXXX` build needs, and the strategy that reaches it.

        Without `unsafe-best-match` the resolver finds `torch` on PyPI, never looks at
        download.pytorch.org, and reports that the recorded `+cu130` version does not
        exist. The name is alarming and the behaviour is the ordinary one for a second
        index that carries builds of the same package.
        """
        if not self.index_url:
            return []
        return ["--extra-index-url", self.index_url, "--index-strategy", "unsafe-best-match"]

    async def version(self) -> str:
        result = await run([self.python, "-s", "-c", "import sys;print(sys.version)"], 60)
        return result.out.strip() if result.ok else ""

    async def freeze(self) -> list[E.Pin]:
        """What is installed, in the one form a restore can use.

        `pip list --format=freeze`, never `pip freeze`. The latter reports a wheel
        install as `name @ file:///D:/a/ComfyUI/...` - the path on the runner that built
        the portable archive, which exists on no user's machine - and a checkpoint
        written that way fails on its first line. Measured: 80 of 286 distributions here
        record exactly that provenance.
        """
        result = await run(self._base("list", "--format=freeze"), self.timeout)
        if not result.ok:
            raise PackageError(
                "could not read the installed packages from "
                f"{self.python}:\n{result.out.strip()[:2000]}"
            )
        pins = E.parse_freeze(result.out)
        if not pins:
            raise PackageError(
                f"{self.python} reports no installed packages, which cannot be right for a "
                "ComfyUI environment. Check COMFYUI_PYTHON."
            )
        return pins

    async def dry_run(self, requirements: list[str], upgrade: bool = False) -> E.Plan:
        """What installing these would do. Nothing is written.

        The one call that answers "is this safe" before the network, and the reason the
        whole concept has a front door: a caller that can see `torch 2.11.0+cu130 ->
        2.10.0` in a plan does not need to be told not to run pip.
        """
        argv = self._base("install", "--dry-run", *requirements) + self._index()
        if upgrade:
            argv.append("--upgrade")
        result = await run(argv, self.timeout)
        plan = E.parse_plan(result.out)
        if not result.ok and not plan.moves and not plan.no_changes:
            raise PackageError(_resolve_failure(result.out))
        return plan

    async def install(self, requirements: list[str], upgrade: bool = False) -> Run:
        argv = self._base("install", *requirements) + self._index()
        if upgrade:
            argv.append("--upgrade")
        return await run(argv, self.timeout)

    async def install_file(self, path: Path, exact: bool = False) -> Run:
        """`install -r` or `sync -r`. The difference is what happens to extras.

        `install` puts the recorded versions back and leaves everything else alone,
        which undoes a bad upgrade. `sync` also uninstalls whatever is not on the list,
        which is a true rollback and removes the packages a node pack pulled in since -
        so it is offered rather than assumed, and the caller is told by name what it
        would take away.

        **The two verbs take the file differently and uv is unforgiving about it**:
        `install` wants `-r <file>`, `sync` takes the file as a bare positional and
        rejects `-r` outright (`unexpected argument '-r' found`). Found by running a
        restore end to end rather than by reading, which is the argument for doing that.

        **`--no-deps` is what makes a restore possible at all, and the reason is a fact
        about ComfyUI environments rather than a preference.** A real one is routinely
        not resolvable as a whole: packs pip-install their requirements one at a time,
        and pip lets a later install break an earlier pack's constraint in silence.
        Measured on this install - `albucore==0.0.24` requires
        `opencv-python-headless>=4.9.0.80`, while was-node-suite has pinned that to
        `<=4.7.0.72`, so uv refuses the set as unsatisfiable and the restore fails
        having done nothing. There is nothing to resolve anyway: a checkpoint records
        the complete closed set, every dependency included, so dependency edges add no
        information and only supply a contradiction to trip over.
        """
        if exact:
            return await self.sync(path)
        return await run(
            self._base("install", "--no-deps", "-r", path) + self._index(), self.timeout
        )

    async def sync(self, path: Path, dry_run: bool = False) -> Run:
        """`uv pip sync`: make the environment be exactly this list, removals included.

        Separate from `install_file` because only this one reports removals, and the
        removals are the whole question a full rollback has to answer before it starts.
        """
        argv = self._base("sync", path) + self._index()
        if dry_run:
            argv.append("--dry-run")
        return await run(argv, self.timeout)


async def site_packages(uv: Path, python: Path) -> Path:
    """Where this interpreter keeps its installed distributions.

    Asked of the interpreter rather than assembled from its path, which is this
    project's rule everywhere else and is not cosmetic here: an embedded CPython has a
    `._pth` file that decides its own `sys.path`, and a venv, a conda environment and a
    portable build put `site-packages` in three different places relative to the
    executable. `sysconfig` answers for all of them, and `site.getsitepackages()` is not
    the one to ask - it is absent under an isolated `._pth`.
    """
    result = await run(
        [python, "-s", "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
        60,
    )
    path = Path(result.out.strip().splitlines()[-1]) if result.ok and result.out.strip() else None
    if path is None or not path.is_dir():
        raise PackageError(
            f"could not work out where {python} keeps its packages"
            f"{': ' + result.out.strip()[:400] if result.out.strip() else ''}"
        )
    return path


async def audit(uv: Path, python: Path, timeout: float, tmp_dir: Path) -> dict:
    """Run pip-audit over the installed distributions and return its JSON.

    **Via `uv tool run`, so nothing is added to this project's dependencies.** Same
    principle as the vendored `uv.exe`: the project already ships the one tool that can
    fetch and cache the rest on demand, and a security scanner living in the server's own
    venv would be a package this server then has to keep patched itself.

    **`--path`, not `-r <freeze>`, and the reasoning that said otherwise was backwards.**
    Handing pip-audit a requirements file makes it *resolve* that file - it shells out to
    `pip install --dry-run --report` - and a ComfyUI environment routinely holds packages
    that are on no index at all. Measured here: `cstr==0.1.0` is installed from a git URL
    by was-node-suite, and `-r` failed the entire audit on it with `No matching
    distribution found`, having audited nothing. `--path` reads the `.dist-info`
    directories where they are and resolves nothing, so a locally built `sageattention`
    or a git-installed `cstr` is audited like anything else. It emits a warning about
    which interpreter pip would use, which is noise: verified against the report, 286
    packages including `torch 2.11.0+cu130` and `cstr 0.1.0` - the ComfyUI environment,
    not uv's.

    **OSV rather than PyPI's own advisory service, for two reasons and one of them is
    measured.** It is the upstream aggregator, so it sees more - 118 findings across 19
    packages here against 82 across 18 - and it lives on api.osv.dev, so the audit still
    answers when pypi.org is the thing that is unreachable, which is not hypothetical:
    that is exactly the state this machine was in when the choice was made.

    **A blocked index falls back to the cached tool.** `uv tool run` re-resolves the tool
    against the index on every call, so a warm cache is not by itself enough; `--offline`
    uses what is already there. It is the retry rather than the default because a first
    run has nothing cached and must be allowed to fetch.
    """
    where = await site_packages(uv, python)
    report = tmp_dir / "audit.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)

    def argv(offline: bool) -> list[str | Path]:
        head: list[str | Path] = [uv, "tool", "run"]
        if offline:
            head.append("--offline")
        return head + [
            "--from", "pip-audit", "pip-audit",
            "--path", where,
            "--vulnerability-service", "osv",
            "--format", "json",
            "--progress-spinner", "off",
            "--output", report,
        ]

    result = await run(argv(offline=False), timeout)
    if not report.is_file() and any(m in result.out.lower() for m in _UNREACHABLE):
        result = await run(argv(offline=True), timeout)

    if not report.is_file():
        raise PackageError(_audit_failure(result.out, where))
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PackageError(f"pip-audit wrote something that is not JSON: {exc}") from exc
    finally:
        report.unlink(missing_ok=True)
    return data


def _audit_failure(output: str, where: Path) -> str:
    text = output.strip()
    if any(marker in text.lower() for marker in _UNREACHABLE):
        return (
            "the audit could not run because a package index is unreachable, and no cached "
            "copy of pip-audit was available to fall back on. uv fetches the tool from "
            "PyPI, and the advisories themselves come from api.osv.dev - both have to be "
            f"reachable at least once. Nothing was read and nothing was changed.\n{text[:2000]}"
        )
    return f"pip-audit produced no report for {where}.\n{text[:2000]}"


def summarise_audit(data: dict, pins: list[E.Pin]) -> dict:
    """pip-audit's report, narrowed to what a caller can act on.

    The raw JSON measured 216 KB for 286 packages, most of it the description of
    vulnerabilities in packages that have none of the caller's attention. What is kept
    is one row per affected package with its ids and the version that fixes it, plus
    the flag that decides whether the fix is even available: a package on the pinned
    family cannot be moved by this server whatever the advisory says, and saying so
    beside the finding is what stops the next call from being a refused upgrade.
    """
    local = {p.name: p for p in pins}
    rows = []
    for entry in data.get("dependencies", data if isinstance(data, list) else []):
        vulns = entry.get("vulns") or []
        if not vulns:
            continue
        name = E.normalise(entry.get("name", ""))
        fixes: list[str] = []
        seen_ids: list[str] = []
        for vuln in vulns:
            fixes.extend(str(v) for v in (vuln.get("fix_versions") or []))
            vuln_id = str(vuln.get("id", ""))
            if vuln_id and vuln_id not in seen_ids:
                seen_ids.append(vuln_id)
        pin = local.get(name)
        row = {
            "name": name,
            "version": entry.get("version", ""),
            "ids": seen_ids[:8],
            "count": len(seen_ids),
        }
        if fixes:
            row["fixed_in"] = sorted(set(fixes))[:4]
        else:
            row["fixed_in"] = []
            row["note"] = "no fixed version published"
        if name in E.PINNED_FAMILY or (pin is not None and pin.local):
            row["pinned"] = True
            row["note"] = (
                "this package's version is decided by the CUDA build of this install; "
                "upgrading it here is refused"
            )
        rows.append(row)
    rows.sort(key=lambda r: (-r["count"], r["name"]))
    return {"vulnerable": rows, "packages_audited": len(pins), "packages_affected": len(rows)}


def git_state(path: Path) -> tuple[str, str]:
    """(commit, remote) read straight out of `.git`, without running git.

    Reading the files rather than shelling out, for the reason the safe-updater does the
    same: git need not be installed - a portable ComfyUI unpacked from a zip has no
    repository at all and no git either - and this has to keep working there rather than
    turning a routine checkpoint into an error about a missing executable.
    """
    git_dir = path / ".git"
    if git_dir.is_file():
        try:
            pointer = git_dir.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return "", ""
        if pointer.startswith("gitdir:"):
            git_dir = (path / pointer[7:].strip()).resolve()
    if not git_dir.is_dir():
        return "", ""

    commit = ""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            ref_file = git_dir / ref
            if ref_file.is_file():
                commit = ref_file.read_text(encoding="utf-8").strip()
            else:
                packed = git_dir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.endswith(" " + ref):
                            commit = line.split()[0]
                            break
        else:
            commit = head
    except OSError:
        pass

    remote = ""
    config = git_dir / "config"
    if config.is_file():
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        import re as _re

        match = _re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text, _re.S)
        if match:
            remote = match.group(1)
    return commit, remote


def read_packs(cfg: Config) -> list[E.Pack]:
    """Every node pack in custom_nodes, enabled and disabled alike.

    **The disk is the only source for this one**, which is the exception to this
    project's rule that ComfyUI is asked rather than guessed at - and the exception
    proves it. `/object_info` lists the node types that *registered*, so a pack that is
    disabled and a pack whose import died are both simply absent from it, and absent is
    indistinguishable from never installed. Those two are exactly the packs worth
    reporting on. The disk says what is installed, `/object_info` says what works, and
    `get_comfy_log` says why not - the three together are the answer, and no one of them
    is.

    Both of ComfyUI-Manager's disabled spellings are recognised: a `.disabled`
    subdirectory (3.x) and a `<name>.disabled` sibling (older). Agreeing with Manager
    here is not politeness - a pack this server calls installed while Manager's own UI
    calls it disabled is a disagreement the user experiences as a node that is present
    and does not work.
    """
    root = cfg.comfy_dir / "custom_nodes"
    if not root.is_dir():
        return []

    found: list[E.Pack] = []

    def read_one(path: Path, enabled: bool) -> E.Pack | None:
        name = path.name
        if name.endswith(E.DISABLED_SUFFIX):
            name = name[: -len(E.DISABLED_SUFFIX)]
        if name in E.NOT_A_PACK or name.startswith("."):
            return None
        if path.is_file():
            if path.suffix != ".py":
                return None
            return E.Pack(name=name, enabled=enabled, files=False)

        pack = E.Pack(name=name, enabled=enabled)
        meta_file = path / E.COMFY_METADATA
        if meta_file.is_file():
            try:
                meta = E.parse_pyproject(meta_file.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                meta = {}
            pack.registry_id = meta.get("registry_id", "")
            pack.version = meta.get("version", "")
            pack.repo = meta.get("repo", "")
            pack.dependencies = meta.get("dependencies", [])
        commit, remote = git_state(path)
        pack.commit = commit[:12]
        pack.repo = pack.repo or remote
        return pack

    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []

    for entry in entries:
        if entry.name == E.DISABLED_DIR:
            continue
        pack = read_one(entry, enabled=not entry.name.endswith(E.DISABLED_SUFFIX))
        if pack is not None:
            found.append(pack)

    disabled_root = root / E.DISABLED_DIR
    if disabled_root.is_dir():
        try:
            for entry in sorted(disabled_root.iterdir()):
                pack = read_one(entry, enabled=False)
                if pack is not None:
                    found.append(pack)
        except OSError:
            pass

    return sorted(found, key=lambda p: E.normalise(p.name))


RESTART_NEEDED = (
    "ComfyUI imports every custom node once at startup and never re-reads them, so this "
    "changes nothing until it restarts - restart_comfy does that."
)

DEPENDENCIES_STAY = (
    "Disabling moves the folder and nothing else: the pack's Python packages stay "
    "installed, and so does anything else that came in with them. That is deliberate - "
    "they are shared, and uninstalling one pack's requirements routinely takes another "
    "pack's with them. restore_checkpoint is the tool that moves packages."
)


def custom_nodes_dir(cfg: Config) -> Path:
    return cfg.comfy_dir / "custom_nodes"


def pack_location(cfg: Config, pack: E.Pack) -> Path | None:
    """Where this pack's files are right now, whichever spelling put them there.

    All three are checked rather than the one its `enabled` flag implies, because the
    flag came from a read that may be minutes old and a Manager running in the same
    ComfyUI can have moved the folder in between.
    """
    root = custom_nodes_dir(cfg)
    for candidate in (
        root / pack.name,
        root / E.DISABLED_DIR / pack.name,
        root / f"{pack.name}{E.DISABLED_SUFFIX}",
    ):
        if candidate.exists():
            return candidate
    return None


def toggle_pack(cfg: Config, pack: E.Pack, enable: bool) -> tuple[Path, Path]:
    """Move a pack between enabled and disabled, and report where it went.

    One `rename` within one directory, which is what makes this the safest tool in the
    concept: it is atomic on both platforms, it copies nothing, and it is undone by
    calling it again with the other argument. Nothing here writes to the pack itself.

    **`rename` is used rather than `shutil.move` precisely because it refuses to
    overwrite.** A pack present in both places at once is a state Manager can produce
    (install, disable, install again), and silently replacing one copy with the other
    would destroy a checkout that may hold local edits - the folders in custom_nodes
    are the user's, which is the same reason a checkpoint never copies them.

    The `.disabled` directory is created on demand: a portable install that has never
    disabled anything does not have one, and that is the common case rather than a
    fault.

    **A running ComfyUI is usually not in the way, and the obvious reason it would be
    is wrong.** Measured on Windows 11: renaming a directory holding a *loaded* `.pyd`
    succeeds - the module loader opens it with `FILE_SHARE_DELETE`, so the mapping does
    not pin the path. What does refuse is an ordinary open handle, which is what
    `open()` produces by default: a pack's log, cache or database, held for as long as
    it runs. So this is attempted rather than pre-refused, and a failure is reported
    with the fix (stop ComfyUI) rather than being predicted.
    """
    root = custom_nodes_dir(cfg)
    if not root.is_dir():
        raise PackageError(f"{root} is not a directory, so there is nothing to enable or disable.")

    source, target = E.toggle_target(root, pack, enable)
    if not source.exists():
        state = "disabled" if enable else "enabled"
        raise PackageError(
            f"{pack.name} is not {state}: nothing at {source}. Read describe_environment "
            "for the packs that are actually installed."
        )
    if target.exists():
        raise PackageError(
            f"refusing to move {source} onto {target}, which already exists. Both copies of "
            f"{pack.name} are on disk - one enabled and one disabled - and this server will "
            "not choose which of them to destroy. Remove or rename the one you do not want."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(target)
    except OSError as exc:
        raise PackageError(
            f"could not move {source} to {target}: {exc}. On Windows that is what a file "
            "still open inside the folder looks like - a log, a cache, a database a pack "
            "keeps open while it runs. Stopping ComfyUI closes them: comfy_stop, then call "
            "again. Compiled extensions are not the problem, whatever it looks like."
        ) from exc
    return source, target


@dataclass
class Checkpoint:
    """A folder recording what was installed, and enough to put it back."""

    path: Path
    manifest: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def created(self) -> str:
        return str(self.manifest.get("created", ""))

    @property
    def note(self) -> str:
        return str(self.manifest.get("note", ""))

    @property
    def packages_file(self) -> Path:
        return self.path / PACKAGES_FILE

    def pins(self) -> list[E.Pin]:
        if not self.packages_file.is_file():
            return []
        return E.parse_freeze(self.packages_file.read_text(encoding="utf-8"))

    def packs(self) -> list[dict]:
        path = self.path / PACKS_FILE
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def as_dict(self) -> dict:
        manifest = self.manifest
        return {
            "name": self.name,
            "created": self.created,
            "note": self.note,
            "packages": manifest.get("packages_count", 0),
            "custom_nodes": manifest.get("packs_count", 0),
            "comfyui_version": manifest.get("comfyui_version", ""),
            "python": manifest.get("python_version", ""),
            "torch_index": manifest.get("torch_index", ""),
            "unrestorable": manifest.get("unrestorable", []),
        }


def checkpoints_dir(cfg: Config) -> Path:
    """Where checkpoints live.

    Beside the install rather than inside this checkout, for three reasons that all
    point the same way: it describes that install and should travel with it, the free-
    space question is about that volume, and a `git clone` of this repository somewhere
    else must not look like a machine with no checkpoints.
    """
    return cfg.checkpoint_dir or (cfg.comfy_root / "mcp_checkpoints")


def load_all(cfg: Config) -> list[Checkpoint]:
    """Every readable checkpoint, newest first. A damaged one is skipped, not raised on."""
    base = checkpoints_dir(cfg)
    if not base.is_dir():
        return []
    out: list[Checkpoint] = []
    for entry in sorted(base.iterdir(), reverse=True):
        manifest = entry / MANIFEST
        if not entry.is_dir() or not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(Checkpoint(path=entry, manifest=data))
    return out


def find(cfg: Config, name: str) -> Checkpoint:
    """One checkpoint by name, or by the prefix of one. Ambiguity is refused, not guessed."""
    wanted = name.strip()
    every = load_all(cfg)
    if not every:
        raise PackageError(
            f"no checkpoints in {checkpoints_dir(cfg)}. create_checkpoint writes one."
        )
    exact = [c for c in every if c.name == wanted]
    if exact:
        return exact[0]
    partial = [c for c in every if c.name.startswith(wanted)]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise PackageError(
            f"no checkpoint named {wanted!r}. Have: {', '.join(c.name for c in every[:8])}"
        )
    raise PackageError(
        f"{wanted!r} matches {len(partial)} checkpoints: {', '.join(c.name for c in partial[:6])}. "
        "Name one of them."
    )


def new_name(comfyui_version: str) -> str:
    """`<stamp>__v<version>`, with the version reduced to what a folder name can hold.

    The version comes from `/system_stats`, which reports whatever the install says it
    is - a branch name on a checkout built from git, and on Windows a colon or a
    backslash in one is unwritable rather than merely ugly. Everything outside a safe
    set becomes `-`
    rather than being dropped, so two versions that differ only in punctuation still
    produce two names.
    """
    stamp = datetime.now().strftime(STAMP)
    tail = re.sub(r"[^A-Za-z0-9._-]", "-", (comfyui_version or "unknown").strip())[:24]
    return f"{stamp}__v{tail or 'unknown'}"


def write(
    cfg: Config,
    pins: list[E.Pin],
    packs: list[E.Pack],
    *,
    note: str = "",
    comfyui_version: str = "",
    python_version: str = "",
    python_path: str = "",
) -> Checkpoint:
    """Write a checkpoint. Text files only, so it costs kilobytes and no disk check.

    That is the difference from a ComfyUI updater's backup and it is deliberate: this
    server does not replace ComfyUI's sources, so it has nothing to restore them from
    and no business copying them. What it does change is the package environment, and
    that is exactly what is recorded.
    """
    base = checkpoints_dir(cfg)
    path = base / new_name(comfyui_version)
    path.mkdir(parents=True, exist_ok=False)

    try:
        path.joinpath(PACKAGES_FILE).write_text(E.format_freeze(pins), encoding="utf-8")
        path.joinpath(PACKS_FILE).write_text(
            json.dumps([p.as_dict() for p in packs], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest = {
            "format": CHECKPOINT_FORMAT,
            "created": datetime.now().isoformat(timespec="seconds"),
            "note": note,
            "comfyui_version": comfyui_version,
            "python_version": python_version,
            "python": python_path,
            "platform": platform.platform(),
            "comfy_root": str(cfg.comfy_root),
            "packages_count": len(pins),
            "packs_count": len(packs),
            "torch_index": E.torch_index_url(pins),
            "unrestorable": [p.line for p in E.unrestorable(pins)],
            "not_backed_up": NOT_BACKED_UP,
        }
        path.joinpath(MANIFEST).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise

    return Checkpoint(path=path, manifest=manifest)


def prune(cfg: Config, keep: int) -> list[str]:
    """Drop the oldest checkpoints past `keep`. Returns what went."""
    if keep <= 0:
        return []
    gone = []
    for old in load_all(cfg)[keep:]:
        shutil.rmtree(old.path, ignore_errors=True)
        gone.append(old.name)
    return gone


def delete(checkpoint: Checkpoint) -> None:
    shutil.rmtree(checkpoint.path, ignore_errors=True)
